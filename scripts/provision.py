#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Plan and provision regional networks and AMLFS for Azure CycleCloud."""

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


NETWORK_API = "2024-05-01"
STORAGE_API = "2023-05-01"
AMLFS_API = "2024-07-01"
DNS_API = "2020-06-01"
AUTH_API = "2022-04-01"
HSM_APPLICATION = "4392ab71-2ce2-4b0d-8770-b352745c73f5"
HSM_ROLES = {
    "Storage Account Contributor": "17d1049b-9a84-46fb-8f53-869881c3d3ab",
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
}
DNS_ZONE = "privatelink.blob.core.windows.net"


class Azure:
    def __init__(self):
        if not shutil.which("az"):
            raise ValueError("Install Azure CLI and authenticate first (az login or az login --identity)")
        self.subscription = None

    def run(self, *args, missing=False):
        command = ["az", *map(str, args), "--only-show-errors", "--output", "json"]
        if self.subscription and "--subscription" not in command:
            command.extend(["--subscription", self.subscription])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            if missing:
                # az rest formats ARM errors differently from typed CLI commands.
                codes = re.findall(r'"code"\s*:\s*"([^"]+)"', result.stderr)
                codes += re.findall(r"\(([A-Za-z]+)\)", result.stderr)
                if any(code in (
                        "ResourceNotFound", "ResourceGroupNotFound", "ParentResourceNotFound",
                        "DeploymentNotFound", "NotFound")
                       for code in codes):
                    return None
            raise ValueError("Azure CLI failed: " + result.stderr.strip())
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def rest(self, resource_id, api, method="GET", body=None, missing=False, query=""):
        url = "https://management.azure.com" + resource_id + "?api-version=" + api + query
        args = ["rest", "--method", method, "--url", url]
        if body is not None:
            args += ["--body", json.dumps(body)]
        return self.run(*args, missing=missing)

    def listing(self, resource_id, api, query=""):
        page = self.rest(resource_id, api, query=query)
        values = list(page.get("value", []))
        while page.get("nextLink"):
            url = page["nextLink"]
            if not url.startswith("https://management.azure.com/"):
                raise ValueError("Unexpected ARM pagination URL")
            page = self.run("rest", "--method", "GET", "--url", url)
            values.extend(page.get("value", []))
        return values


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9_().-]+", value):
        raise argparse.ArgumentTypeError("Use an Azure resource name, not a resource ID")
    return value


def network(value):
    result = ipaddress.ip_network(value, strict=True)
    if result.version != 4:
        raise ValueError("AMLFS requires IPv4 networks")
    return result


def prefixes(resource):
    properties = resource.get("properties", {})
    values = list(properties.get("addressSpace", {}).get("addressPrefixes", []))
    values += properties.get("addressPrefixes", [])
    if properties.get("addressPrefix"):
        values += [properties["addressPrefix"]]
    return [ipaddress.ip_network(value) for value in values if ":" not in value]


def allocate(pools, prefix, occupied):
    for pool in pools:
        if pool.prefixlen > prefix:
            continue
        for candidate in pool.subnets(new_prefix=prefix):
            if not any(candidate.overlaps(item) for item in occupied):
                occupied.append(candidate)
                return str(candidate)
    raise ValueError("No free /{} network in {}; choose a different pool or prepare "
                     "more parent VNet address space".format(prefix, ", ".join(map(str, pools))))


def template(resources, outputs=None):
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "resources": resources,
        "outputs": outputs or {},
    }


def missing_roles(azure, storage_id, principal):
    assignments = azure.listing(storage_id + "/providers/Microsoft.Authorization/roleAssignments",
                                AUTH_API, query="&$filter=atScope()")
    present = {
        item["properties"]["roleDefinitionId"].split("/")[-1].lower()
        for item in assignments
        if item["properties"].get("principalId", "").lower() == principal.lower()
        and not item["properties"].get("condition")
    }
    return [role for role in HSM_ROLES.values() if role not in present]


def sku_zones(sku, location):
    if location not in [value.lower() for value in sku.get("locations", [])]:
        raise ValueError("SKU {} is not offered in {}".format(sku["name"], location))
    for restriction in sku.get("restrictions", []):
        affected = restriction.get("restrictionInfo", {}).get("locations",
                                                              restriction.get("values", []))
        if not affected or location in [value.lower() for value in affected]:
            raise ValueError("SKU restricted in {}: {}".format(location, restriction))
    return next((entry.get("zones", []) for entry in sku.get("locationInfo", [])
                 if entry["location"].lower() == location), [])


def parent_blob_endpoint(azure, storage, parent_id, zone_id):
    for connection in storage["properties"].get("privateEndpointConnections", []):
        properties = connection["properties"]
        if properties.get("privateLinkServiceConnectionState", {}).get("status") != "Approved":
            continue
        endpoint = azure.rest(properties["privateEndpoint"]["id"], NETWORK_API)
        if not endpoint["properties"]["subnet"]["id"].lower().startswith(parent_id.lower() + "/subnets/"):
            continue
        services = endpoint["properties"].get("privateLinkServiceConnections", [])
        if not any("blob" in service["properties"].get("groupIds", [])
                   and service["properties"]["privateLinkServiceId"].lower() == storage["id"].lower()
                   for service in services):
            continue
        for group in azure.listing(endpoint["id"] + "/privateDnsZoneGroups", NETWORK_API):
            if any(config["properties"]["privateDnsZoneId"].lower() == zone_id.lower()
                   for config in group["properties"].get("privateDnsZoneConfigs", [])):
                return endpoint["id"], group["id"]
    return None


def build_plan(args, azure):
    subscription = str(uuid.UUID(args.subscription or azure.run("account", "show")["id"]))
    azure.subscription = subscription
    scope = "/subscriptions/" + subscription + "/resourceGroups/" + args.resource_group
    azure.rest(scope, "2022-09-01")
    parent_id = scope + "/providers/Microsoft.Network/virtualNetworks/" + args.parent_vnet
    storage_id = scope + "/providers/Microsoft.Storage/storageAccounts/" + args.storage_account
    parent = azure.rest(parent_id, NETWORK_API)
    storage = azure.rest(storage_id, STORAGE_API)
    zone_id = scope + "/providers/Microsoft.Network/privateDnsZones/" + DNS_ZONE
    reused_endpoint = (parent_blob_endpoint(azure, storage, parent_id, zone_id)
                       if args.blob_network == "private-endpoint" else None)
    if storage["properties"].get("allowSharedKeyAccess") is False:
        raise ValueError("AMLFS HSM requires storage shared-key access; the script will not enable it")
    if args.data_container == args.log_container:
        raise ValueError("Data and log containers must be different")
    references = [{"id": storage_id, "api": STORAGE_API}]
    for container in (args.data_container, args.log_container):
        container_id = storage_id + "/blobServices/default/containers/" + container
        azure.rest(container_id, STORAGE_API)
        references.append({"id": container_id, "api": STORAGE_API})
    if args.blob_network == "public":
        properties = storage["properties"]
        if (properties.get("publicNetworkAccess", "Enabled") != "Enabled"
                or properties.get("networkAcls", {}).get("defaultAction", "Allow") != "Allow"):
            raise ValueError("Public mode requires an already publicly reachable storage account; "
                             "use private-endpoint mode instead of widening its firewall")
    locations = list(dict.fromkeys([parent["location"].lower(), *args.regions]))
    if not all(re.fullmatch(r"[a-z0-9]+", location) for location in locations):
        raise ValueError("Use Azure location identifiers, not display names")
    skus = azure.listing("/subscriptions/" + subscription + "/providers/Microsoft.StorageCache/skus",
                        AMLFS_API)
    matching_skus = [item for item in skus if item["name"] == args.sku]
    if not matching_skus:
        raise ValueError("Unknown or unavailable AMLFS SKU: " + args.sku)
    zones = {}
    for location in locations:
        sku = next((item for item in matching_skus
                    if location in [value.lower() for value in item.get("locations", [])]), None)
        if sku is None:
            raise ValueError("SKU {} is not offered in {}".format(args.sku, location))
        zones[location] = sku_zones(sku, location)
    size = azure.rest("/subscriptions/" + subscription
                      + "/providers/Microsoft.StorageCache/getRequiredAmlFSSubnetsSize",
                      AMLFS_API, method="POST",
                      body={"sku": {"name": args.sku}, "storageCapacityTiB": args.capacity_tib})
    required = int(size["filesystemSubnetSize"]) + 5
    subnet_prefix = args.amlfs_subnet_prefix
    if subnet_prefix is None:
        subnet_prefix = min(24, 32 - math.ceil(math.log2(required)))
    if not 0 <= subnet_prefix <= 29 or 2 ** (32 - subnet_prefix) < required:
        raise ValueError("AMLFS subnet is too small: service requires {} usable IPs".format(required - 5))
    pool = network(args.address_pool)
    if not any(pool.subnet_of(network(cidr)) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("--address-pool must be within RFC1918 private address space")
    if not pool.prefixlen <= args.vnet_prefix <= 24:
        raise ValueError("--vnet-prefix must fit the pool and leave space for compute and AMLFS")

    known_vnets = azure.listing(scope + "/providers/Microsoft.Network/virtualNetworks", NETWORK_API)
    occupied = [cidr for vnet in known_vnets for cidr in prefixes(vnet)]
    occupied.extend(prefixes(parent))
    for peering in parent["properties"].get("virtualNetworkPeerings", []):
        peer = azure.rest(peering["properties"]["remoteVirtualNetwork"]["id"], NETWORK_API)
        occupied.extend(prefixes(peer))
    occupied.extend(network(cidr) for cidr in args.exclude_cidr)
    parent_used = [cidr for subnet in parent["properties"].get("subnets", []) for cidr in prefixes(subnet)]
    # Reserve exclusions inside the parent as well as in the remote allocation pool.
    parent_used.extend(network(cidr) for cidr in args.exclude_cidr)
    parent_amlfs = allocate(prefixes(parent), subnet_prefix, parent_used)
    parent_pe = (allocate(prefixes(parent), 27, parent_used)
                 if args.blob_network == "private-endpoint" and not reused_endpoint else None)
    principal = args.hsm_principal_id
    if principal is None:
        principal = azure.run("ad", "sp", "show", "--id", HSM_APPLICATION)["id"]
    principal = str(uuid.UUID(principal))
    roles = missing_roles(azure, storage_id, principal)
    if roles and not args.assign_hsm_roles:
        raise ValueError("HPC Cache Resource Provider lacks required storage roles. Have an "
                         "administrator assign them, or explicitly use --assign-hsm-roles "
                         "with roleAssignments/write permission")

    plan_id = str(uuid.uuid4())
    tags = {"managed-by": "cyclecloud-multi-region", "multi-region-plan": plan_id}
    resources, regions, parent_subnets = [], [], []

    def resource(kind, resource_name, location, properties, dependencies=(), **extra):
        item = {"type": "Microsoft.Network/" + kind, "apiVersion": NETWORK_API,
                "name": resource_name, "properties": properties}
        if location:
            item.update(location=location, tags=tags.copy())
        if dependencies:
            item["dependsOn"] = list(dependencies)
        item.update(extra)
        resources.append(item)
        return scope + "/providers/" + item["type"].split("/")[0] + "/" + "/".join(
            part for pair in zip(item["type"].split("/")[1:], resource_name.split("/")) for part in pair)

    for index, location in enumerate(locations):
        stem = args.prefix + "-" + location
        vnet_name = args.parent_vnet if index == 0 else stem + "-vnet"
        vnet_id = scope + "/providers/Microsoft.Network/virtualNetworks/" + vnet_name
        if index:
            cidr = allocate([pool], args.vnet_prefix, occupied)
            used = []
            compute_cidr = allocate([network(cidr)], 24, used)
            amlfs_cidr = allocate([network(cidr)], subnet_prefix, used)
        else:
            cidr, compute_cidr, amlfs_cidr = None, None, parent_amlfs
        pip = resource("publicIPAddresses", stem + "-egress-ip", location,
                       {"publicIPAllocationMethod": "Static", "publicIPAddressVersion": "IPv4"},
                       sku={"name": "Standard"})
        nat = resource("natGateways", stem + "-nat", location,
                       {"publicIpAddresses": [{"id": pip}], "idleTimeoutInMinutes": 10},
                       [pip], sku={"name": "Standard"})
        nsg = resource("networkSecurityGroups", stem + "-nsg", location, {"securityRules": []})
        subnet_properties = {"addressPrefix": amlfs_cidr, "defaultOutboundAccess": False,
                             "natGateway": {"id": nat}, "networkSecurityGroup": {"id": nsg}}
        amlfs_subnet = args.prefix + "-amlfs" if index == 0 else "amlfs"
        subnet_id = vnet_id + "/subnets/" + amlfs_subnet
        if index:
            resource("virtualNetworks", vnet_name, location, {
                "addressSpace": {"addressPrefixes": [cidr]},
                "dhcpOptions": parent["properties"].get("dhcpOptions", {"dnsServers": []}),
                "subnets": [
                    {"name": "compute", "properties": {
                        "addressPrefix": compute_cidr, "defaultOutboundAccess": False,
                        "natGateway": {"id": nat}, "networkSecurityGroup": {"id": nsg}}},
                    {"name": amlfs_subnet, "properties": subnet_properties},
                ],
            }, [nat, nsg])
            # Hub/spoke links are deliberately bidirectional, without gateway transit.
            for local, remote, dependency in ((args.parent_vnet, vnet_id, vnet_id),
                                               (vnet_name, parent_id, vnet_id)):
                resource("virtualNetworks/virtualNetworkPeerings",
                         local + "/" + args.prefix + "-to-" + (vnet_name if local == args.parent_vnet else args.parent_vnet),
                         None, {"remoteVirtualNetwork": {"id": remote},
                                "allowVirtualNetworkAccess": True, "allowForwardedTraffic": False,
                                "allowGatewayTransit": False, "useRemoteGateways": False}, [dependency])
            dependency = vnet_id
        else:
            dependency = resource("virtualNetworks/subnets", vnet_name + "/" + amlfs_subnet,
                                  None, subnet_properties, [nat, nsg])
            parent_subnets.append({"name": amlfs_subnet, "properties": subnet_properties})
        fs_name = stem + "-amlfs"
        fs_id = scope + "/providers/Microsoft.StorageCache/amlFilesystems/" + fs_name
        filesystem = {
            "type": "Microsoft.StorageCache/amlFilesystems", "apiVersion": AMLFS_API,
            "name": fs_name, "location": location, "tags": tags.copy(),
            "sku": {"name": args.sku},
            "dependsOn": [dependency],
            "properties": {
                "filesystemSubnet": subnet_id, "storageCapacityTiB": args.capacity_tib,
                "maintenanceWindow": {"dayOfWeek": "Sunday", "timeOfDayUTC": "02:00"},
                "hsm": {"settings": {
                    "container": storage_id + "/blobServices/default/containers/" + args.data_container,
                    "loggingContainer": storage_id + "/blobServices/default/containers/" + args.log_container,
                    "importPrefix": "/",
                }},
            },
        }
        if zones[location]:
            filesystem["zones"] = [sorted(zones[location])[0]]
        resources.append(filesystem)
        regions.append({
            "location": location, "home": index == 0, "vnet_id": vnet_id,
            "vnet_cidr": cidr, "filesystem_id": fs_id, "filesystem": fs_name,
            "resource_group": args.resource_group, "subscription_id": subscription,
            "amlfs_subnet_id": subnet_id, "amlfs_subnet_cidr": amlfs_cidr,
            "compute_subnet": args.resource_group + "/" + vnet_name + "/compute" if index else None,
        })

    if args.blob_network == "private-endpoint":
        if parent_pe:
            pe_subnet_name = args.prefix + "-blob-pe"
            pe_properties = {"addressPrefix": parent_pe, "privateEndpointNetworkPolicies": "Disabled",
                             "defaultOutboundAccess": False}
            pe_subnet = resource("virtualNetworks/subnets", args.parent_vnet + "/" + pe_subnet_name,
                                 None, pe_properties, [parent_id + "/subnets/" + args.prefix + "-amlfs"])
            parent_subnets.append({"name": pe_subnet_name, "properties": pe_properties})
        existing_zone = azure.rest(zone_id, DNS_API, missing=True)
        if not existing_zone:
            resource("privateDnsZones", DNS_ZONE, "global", {}, apiVersion=DNS_API)
        else:
            references.append({"id": zone_id, "api": DNS_API})
            if not reused_endpoint and azure.rest(zone_id + "/A/" + args.storage_account, DNS_API, missing=True):
                raise ValueError("An existing Blob DNS record points to another endpoint. Refusing "
                                 "to replace it; prepare an approved parent-VNet Blob endpoint "
                                 "with this same-group DNS zone before planning")
        links = azure.listing(zone_id + "/virtualNetworkLinks", DNS_API) if existing_zone else []
        linked = {link["properties"]["virtualNetwork"]["id"].lower() for link in links}
        dns_dependencies = []
        for region in regions:
            if region["vnet_id"].lower() in linked:
                for link in links:
                    if link["properties"]["virtualNetwork"]["id"].lower() == region["vnet_id"].lower():
                        references.append({"id": link["id"], "api": DNS_API})
                continue
            deps = [] if region["home"] else [region["vnet_id"]]
            if not existing_zone:
                deps.append(zone_id)
            link_id = resource("privateDnsZones/virtualNetworkLinks",
                               DNS_ZONE + "/" + args.prefix + "-" + region["location"],
                               "global", {"virtualNetwork": {"id": region["vnet_id"]},
                                          "registrationEnabled": False}, deps, apiVersion=DNS_API)
            dns_dependencies.append(link_id)
        if reused_endpoint:
            references.extend({"id": item, "api": NETWORK_API} for item in reused_endpoint)
        else:
            endpoint = resource("privateEndpoints", args.prefix + "-blob-pe", locations[0], {
                "subnet": {"id": pe_subnet},
                "privateLinkServiceConnections": [{
                    "name": "blob", "properties": {"privateLinkServiceId": storage_id, "groupIds": ["blob"]}
                }],
            }, [pe_subnet])
            zone_group = resource("privateEndpoints/privateDnsZoneGroups", args.prefix + "-blob-pe/default",
                                  None, {"privateDnsZoneConfigs": [{"name": "blob",
                                          "properties": {"privateDnsZoneId": zone_id}}]},
                                  [endpoint, *([] if existing_zone else [zone_id])])
            dns_dependencies.append(zone_group)
        for item in resources:
            if item["type"] == "Microsoft.StorageCache/amlFilesystems":
                item["dependsOn"].extend(dns_dependencies)

    # Serialize writes to the existing hub VNet and wait for peering before HSM.
    hub_dependency = parent_id + "/subnets/" + parent_subnets[-1]["name"]
    peerings = []
    for item in resources:
        if item["type"] != "Microsoft.Network/virtualNetworks/virtualNetworkPeerings":
            continue
        peering_id = (scope + "/providers/Microsoft.Network/virtualNetworks/"
                      + item["name"].replace("/", "/virtualNetworkPeerings/", 1))
        if item["name"].startswith(args.parent_vnet + "/"):
            item["dependsOn"].append(hub_dependency)
            hub_dependency = peering_id
        else:
            item["dependsOn"].append(hub_dependency)
        peerings.append(peering_id)
    for item in resources:
        if item["type"] == "Microsoft.StorageCache/amlFilesystems":
            item["dependsOn"].extend(peerings)

    plan = {
        "version": 1, "id": plan_id, "subscription": subscription, "resource_group": args.resource_group,
        "deployment": args.prefix + "-" + plan_id[:8], "parent_vnet_id": parent_id,
        "parent_address_space": [str(cidr) for cidr in prefixes(parent)],
        "parent_subnets": parent_subnets, "storage_id": storage_id, "hsm_principal_id": principal,
        "assign_hsm_roles": args.assign_hsm_roles, "regions": regions, "template": template(resources),
        "blob_network": args.blob_network, "references": references,
        "reused_blob_endpoint": list(reused_endpoint) if reused_endpoint else None,
    }
    preflight(plan, azure)
    return plan


def resource_id(plan, item):
    kind = item["type"].split("/")
    names = item["name"].split("/")
    return ("/subscriptions/" + plan["subscription"] + "/resourceGroups/" + plan["resource_group"]
            + "/providers/" + kind[0] + "/" + "/".join(
                part for pair in zip(kind[1:], names) for part in pair))


def preflight(plan, azure):
    parent = azure.rest(plan["parent_vnet_id"], NETWORK_API)
    if {str(cidr) for cidr in prefixes(parent)} != set(plan["parent_address_space"]):
        raise ValueError("Parent VNet address space changed; review and regenerate the plan")
    planned = {item["name"]: item for item in plan["parent_subnets"]}
    scope = "/subscriptions/" + plan["subscription"] + "/resourceGroups/" + plan["resource_group"]
    vnets = azure.listing(scope + "/providers/Microsoft.Network/virtualNetworks", NETWORK_API)
    for peering in parent["properties"].get("virtualNetworkPeerings", []):
        vnets.append(azure.rest(peering["properties"]["remoteVirtualNetwork"]["id"], NETWORK_API))
    for region in plan["regions"]:
        if region["home"]:
            continue
        for vnet in [parent, *vnets]:
            if vnet["id"].lower() == region["vnet_id"].lower():
                if prefixes(vnet) != [network(region["vnet_cidr"])]:
                    raise ValueError("Regional VNet address space changed: " + vnet["id"])
                continue
            if any(network(region["vnet_cidr"]).overlaps(cidr) for cidr in prefixes(vnet)):
                raise ValueError("Regional VNet CIDR overlaps an existing VNet: " + vnet["id"])
    for reference in plan.get("references", []):
        azure.rest(reference["id"], reference["api"])
    storage = azure.rest(plan["storage_id"], STORAGE_API)
    if storage["properties"].get("allowSharedKeyAccess") is False:
        raise ValueError("Storage shared-key access is no longer enabled")
    if plan["blob_network"] == "public":
        if (storage["properties"].get("publicNetworkAccess", "Enabled") != "Enabled"
                or storage["properties"].get("networkAcls", {}).get("defaultAction", "Allow") != "Allow"):
            raise ValueError("Public storage access changed since planning")
    if plan.get("reused_blob_endpoint"):
        zone_id = scope + "/providers/Microsoft.Network/privateDnsZones/" + DNS_ZONE
        current = parent_blob_endpoint(azure, storage, plan["parent_vnet_id"], zone_id)
        if not current or list(current) != plan["reused_blob_endpoint"]:
            raise ValueError("Reused Blob private endpoint is no longer approved/configured as planned")
    elif plan["blob_network"] == "private-endpoint":
        zone_id = scope + "/providers/Microsoft.Network/privateDnsZones/" + DNS_ZONE
        record = azure.rest(zone_id + "/A/" + plan["storage_id"].split("/")[-1], DNS_API, missing=True)
        if record:
            endpoint = next(item for item in plan["template"]["resources"]
                            if item["type"] == "Microsoft.Network/privateEndpoints")
            owner = azure.rest(resource_id(plan, endpoint), NETWORK_API, missing=True)
            if not owner or owner.get("tags", {}).get("multi-region-plan") != plan["id"]:
                raise ValueError("A Blob DNS record now exists outside this plan; refusing to replace it")
    for existing in parent["properties"].get("subnets", []):
        if existing["name"] in planned:
            expected = planned[existing["name"]]["properties"]
            actual = existing["properties"]
            for key, value in expected.items():
                if actual.get(key) != value:
                    raise ValueError("Parent subnet configuration conflicts: " + existing["name"])
            for key in ("routeTable", "serviceEndpoints", "delegations", "serviceEndpointPolicies",
                        "natGateway", "networkSecurityGroup", "ipAllocations"):
                if actual.get(key) and key not in expected:
                    raise ValueError("Refusing to overwrite customized parent subnet: " + existing["name"])
        elif any(cidr.overlaps(network(item["properties"]["addressPrefix"]))
                 for cidr in prefixes(existing) for item in planned.values()):
            raise ValueError("A parent subnet now overlaps the plan: " + existing["name"])
    for item in plan["template"]["resources"]:
        existing = azure.rest(resource_id(plan, item), item["apiVersion"], missing=True)
        if existing is None:
            continue
        if item["type"] == "Microsoft.Network/virtualNetworks/subnets":
            # Parent subnet equality was checked above; do not replace its other settings.
            continue
        if existing.get("tags", {}).get("multi-region-plan") == plan["id"]:
            continue
        if item["type"].endswith("/virtualNetworkPeerings"):
            actual, expected = existing["properties"], item["properties"]
            if all(actual.get(key) == value for key, value in expected.items()):
                continue
        if item["type"].endswith("/privateDnsZoneGroups"):
            owner_id = resource_id(plan, item).rsplit("/privateDnsZoneGroups/", 1)[0]
            owner = azure.rest(owner_id, NETWORK_API)
            if owner.get("tags", {}).get("multi-region-plan") == plan["id"]:
                continue
        raise ValueError("Refusing to replace an existing resource not owned by this plan: " + existing["id"])


def write_json(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def apply_plan(plan, azure, plan_path, yes=False):
    azure.subscription = plan["subscription"]
    output = plan_path.with_suffix(".outputs.json")
    if output.is_symlink() or (output.exists()
                              and json.loads(output.read_text()).get("plan_id") != plan["id"]):
        raise ValueError("Refusing to replace unrelated output file: " + str(output))
    preflight(plan, azure)
    roles = missing_roles(azure, plan["storage_id"], plan["hsm_principal_id"])
    if roles and not plan["assign_hsm_roles"]:
        raise ValueError("HSM role assignments are now missing; have an administrator restore them")
    with tempfile.TemporaryDirectory(prefix="multi-region-arm-") as directory:
        path = Path(directory) / "template.json"
        write_json(path, plan["template"])
        common = ("--subscription", plan["subscription"], "--resource-group", plan["resource_group"],
                  "--name", plan["deployment"], "--template-file", str(path), "--mode", "Incremental")
        # What-if does not grant roles or provision resources.
        preview = azure.run("deployment", "group", "what-if", *common, "--no-pretty-print")
        print(json.dumps(preview, indent=2))
        if preview.get("status") != "Succeeded":
            raise ValueError("ARM what-if did not succeed; no apply changes made")
        if roles:
            print("Will grant HPC Cache Resource Provider storage roles: " + ", ".join(roles))
        if not yes and input("Type 'apply' to create billable Azure resources: ").strip() != "apply":
            print("Cancelled; no resources created.")
            return None
        for role in roles:
            scope = plan["storage_id"]
            assignment = str(uuid.uuid5(uuid.NAMESPACE_URL, scope.lower() + plan["hsm_principal_id"] + role))
            azure.rest(scope + "/providers/Microsoft.Authorization/roleAssignments/" + assignment,
                       AUTH_API, "PUT", {
                           "properties": {"principalId": plan["hsm_principal_id"],
                                          "principalType": "ServicePrincipal",
                                          "roleDefinitionId": "/subscriptions/" + plan["subscription"]
                                          + "/providers/Microsoft.Authorization/roleDefinitions/" + role}
                       })
        if roles:
            print("Waiting five minutes for newly assigned HSM roles to propagate.")
            time.sleep(300)
        deployment = azure.run("deployment", "group", "create", *common)
        if deployment.get("properties", {}).get("provisioningState") != "Succeeded":
            raise ValueError("ARM deployment did not reach Succeeded; inspect its deployment record")
    results = []
    for region in plan["regions"]:
        filesystem = azure.rest(region["filesystem_id"], AMLFS_API)
        properties = filesystem["properties"]
        if properties.get("provisioningState") != "Succeeded":
            raise ValueError("AMLFS did not reach Succeeded: " + region["filesystem_id"])
        client = properties.get("clientInfo", {})
        if not client.get("mgsAddress"):
            raise ValueError("AMLFS returned no MGS address: " + region["filesystem_id"])
        results.append(dict(region, client_info=client,
                            mount_source=client["mgsAddress"] + "@tcp:/lustrefs"))
    result = {"plan_id": plan["id"], "regions": results}
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(result, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.replace(temporary, output)
        finally:
            if temporary.exists():
                temporary.unlink()
    print("Provisioned regional infrastructure. Outputs: " + str(output))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Inspect Azure and write a plan; create no resources")
    plan.add_argument("--subscription", help="Subscription UUID; defaults to the current Azure CLI account")
    plan.add_argument("--resource-group", required=True, type=name)
    plan.add_argument("--parent-vnet", required=True, type=name)
    plan.add_argument("--regions", required=True, nargs="+", help="Azure location identifiers; parent is always included")
    plan.add_argument("--sku", required=True, help="For example AMLFS-Durable-Premium-500")
    plan.add_argument("--capacity-tib", type=int, default=4)
    plan.add_argument("--storage-account", required=True, type=name)
    plan.add_argument("--data-container", required=True, type=name)
    plan.add_argument("--log-container", required=True, type=name)
    plan.add_argument("--prefix", default="ccmr", help="Short resource name prefix")
    plan.add_argument("--address-pool", required=True, help="IPAM-approved RFC1918 CIDR for new VNets")
    plan.add_argument("--exclude-cidr", action="append", default=[], help="Additional reserved/on-premises CIDR; repeatable")
    plan.add_argument("--vnet-prefix", type=int, default=20)
    plan.add_argument("--amlfs-subnet-prefix", type=int, help="Default: at least /24, enlarged using service sizing")
    plan.add_argument("--blob-network", choices=["private-endpoint", "public"], default="private-endpoint")
    plan.add_argument("--hsm-principal-id", help="Tenant object ID of HPC Cache Resource Provider; skips Graph lookup")
    plan.add_argument("--assign-hsm-roles", action="store_true", help="Explicitly allow missing HSM storage role grants during apply")
    plan.add_argument("--output", required=True, type=Path, help="New plan JSON file outside the repository")
    apply = commands.add_parser("apply", help="Review what-if and deploy a saved plan")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--yes", action="store_true", help="Skip interactive confirmation after what-if")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            if args.output.exists() or args.output.is_symlink():
                raise ValueError("Plan file already exists; choose a new --output path")
            if not args.output.parent.is_dir():
                raise ValueError("Create the protected output directory first")
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", args.prefix):
                raise ValueError("--prefix must contain 2-20 lowercase letters, digits or hyphens")
            if args.capacity_tib <= 0:
                raise ValueError("--capacity-tib must be positive")
            result = build_plan(args, Azure())
            write_json(args.output, result)
            print("Plan written: " + str(args.output))
            for region in result["regions"]:
                print("{}{}: {} ({})".format(region["location"], " [parent]" if region["home"] else "",
                                            region["filesystem"], region["amlfs_subnet_cidr"]))
            print("Plan only. Includes NAT gateways/public egress IPs and "
                  + ("Blob private endpoint/DNS links." if args.blob_network == "private-endpoint"
                     else "existing public Blob access."))
            print("Review IPAM, custom DNS forwarding, existing NSGs/routes, policy, quota and cost before apply.")
        else:
            saved = json.loads(args.plan.read_text())
            if saved.get("version") != 1:
                raise ValueError("Unsupported plan version")
            apply_plan(saved, Azure(), args.plan, args.yes)
        return 0
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as error:
        print("Error: " + str(error), file=sys.stderr)
        print("No automatic rollback. If apply started, inspect the Azure deployment and "
              "rerun the same saved plan after resolving the error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
