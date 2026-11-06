# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Provisioning tests model ARM responses and never contact or change Azure."""

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_project_install import load_script


SUB = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
SCOPE = "/subscriptions/" + SUB + "/resourceGroups/rg-example"
PARENT = SCOPE + "/providers/Microsoft.Network/virtualNetworks/parent"
STORAGE = SCOPE + "/providers/Microsoft.Storage/storageAccounts/example"


class FakeAzure:
    def __init__(self, module):
        self.module = module
        self.calls = []
        self.roles = list(module.HSM_ROLES.values())
        self.resources = {
            SCOPE: {"id": SCOPE},
            PARENT: {"id": PARENT, "location": "regiona", "properties": {
                "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
                "subnets": [{"name": "scheduler", "properties": {"addressPrefix": "10.0.0.0/24"}}],
                "dhcpOptions": {"dnsServers": []},
            }},
            STORAGE: {"id": STORAGE, "properties": {
                "allowSharedKeyAccess": True, "publicNetworkAccess": "Disabled",
                "networkAcls": {"defaultAction": "Deny"},
            }},
        }
        for container in ("data", "logs"):
            container_id = STORAGE + "/blobServices/default/containers/" + container
            self.resources[container_id] = {"id": container_id}
        self.skus = [{"name": "AMLFS-Durable-Premium-500",
                      "locations": ["regiona", "regionb", "regionc", "regiond", "regione"],
                      "locationInfo": [{"location": "regionb", "zones": ["2", "1"]}]}]

    def rest(self, resource_id, api, method="GET", body=None, missing=False, query=""):
        self.calls.append((method, resource_id, body))
        if resource_id.endswith("/getRequiredAmlFSSubnetsSize"):
            assert method == "POST"
            assert body["storageCapacityTiB"] == 4
            return {"filesystemSubnetSize": 23}
        if method == "PUT" and "/roleAssignments/" in resource_id:
            self.roles.append(body["properties"]["roleDefinitionId"].split("/")[-1])
            return {}
        assert method == "GET", (method, resource_id)
        if resource_id in self.resources:
            return copy.deepcopy(self.resources[resource_id])
        if missing:
            return None
        raise ValueError("Resource missing: " + resource_id)

    def listing(self, resource_id, api, query=""):
        if resource_id.endswith("/roleAssignments"):
            return [{"properties": {"roleDefinitionId": "/roles/" + role, "principalId": PRINCIPAL}}
                    for role in self.roles]
        if resource_id.endswith("/skus"):
            return copy.deepcopy(self.skus)
        if resource_id.endswith("/virtualNetworks"):
            return [copy.deepcopy(value) for key, value in self.resources.items()
                    if key.startswith(resource_id + "/") and key.count("/") == resource_id.count("/") + 1]
        if resource_id.endswith("/virtualNetworkLinks"):
            return [copy.deepcopy(value) for key, value in self.resources.items()
                    if key.startswith(resource_id + "/")]
        if resource_id.endswith("/privateDnsZoneGroups"):
            return [copy.deepcopy(value) for key, value in self.resources.items()
                    if key.startswith(resource_id + "/")]
        raise AssertionError(resource_id)

    def run(self, *args, **kwargs):
        self.calls.append(("CLI", args))
        if args[:3] == ("ad", "sp", "show"):
            return {"id": PRINCIPAL}
        if args[:2] == ("account", "show"):
            return {"id": SUB}
        if args[:3] == ("deployment", "group", "what-if"):
            assert "--no-pretty-print" in args
            return {"status": "Succeeded", "changes": []}
        assert args[:3] == ("deployment", "group", "create")
        document = json.loads(Path(args[args.index("--template-file") + 1]).read_text())
        for item in document["resources"]:
            resource_id = self.module.resource_id({"subscription": SUB, "resource_group": "rg-example"}, item)
            existing = dict(copy.deepcopy(item), id=resource_id)
            if item["type"].endswith("/amlFilesystems"):
                existing["properties"].update(provisioningState="Succeeded",
                                              clientInfo={"mgsAddress": "10.0.1.4"})
            if item["type"].endswith("/privateDnsZoneGroups"):
                existing["properties"]["privateDnsZoneConfigs"][0]["properties"]["recordSets"] = []
            self.resources[resource_id] = existing
            if item["type"] == "Microsoft.Network/virtualNetworks/subnets":
                parent = self.resources[PARENT]["properties"]["subnets"]
                parent[:] = [subnet for subnet in parent if subnet["name"] != item["name"].split("/")[-1]]
                parent.append({"name": item["name"].split("/")[-1], "properties": copy.deepcopy(item["properties"])})
        return {"properties": {"provisioningState": "Succeeded"}}


@pytest.fixture
def infra():
    module = load_script("provision")
    return module, FakeAzure(module)


def arguments(**overrides):
    values = dict(subscription=SUB, resource_group="rg-example", parent_vnet="parent",
                  regions=["regionb", "regionc", "regiond"], sku="AMLFS-Durable-Premium-500",
                  capacity_tib=4, storage_account="example", data_container="data", log_container="logs",
                  prefix="multi-region", address_pool="10.64.0.0/12", vnet_prefix=20, amlfs_subnet_prefix=None,
                  exclude_cidr=[], blob_network="private-endpoint", hsm_principal_id=PRINCIPAL,
                  assign_hsm_roles=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parent_always_included_and_no_azure_writes_in_plan(infra):
    module, azure = infra
    before = copy.deepcopy(azure.resources)
    plan = module.build_plan(arguments(regions=["regionc", "regiona", "regionc", "regionb", "regione"]), azure)
    assert [region["location"] for region in plan["regions"]] == ["regiona", "regionc", "regionb", "regione"]
    resources = plan["template"]["resources"]
    assert len([item for item in resources if item["type"].endswith("/amlFilesystems")]) == 4
    assert len([item for item in resources if item["type"] == "Microsoft.Network/virtualNetworks"]) == 3
    assert not any(item["name"] == "parent" for item in resources)
    assert len([item for item in resources if item["type"].endswith("/virtualNetworkPeerings")]) == 6
    assert azure.resources == before
    assert not any(call[0] in ("PUT", "DELETE", "PATCH") for call in azure.calls)
    assert plan["regions"][0]["amlfs_subnet_cidr"] == "10.0.1.0/24"
    assert plan["regions"][0]["compute_subnet"] is None
    assert plan["regions"][1]["compute_subnet"] == "rg-example/multi-region-regionc-vnet/compute"


def test_sku_discovery_uses_each_regions_record_and_normalizes_location_case(infra):
    module, azure = infra
    azure.skus = [
        {"name": "AMLFS-Durable-Premium-500", "locations": [location],
         "locationInfo": [{"location": location, "zones": [str(index)]}]}
        for index, location in enumerate(["Unrelated", "RegionA", "regionb", "REGIONC", "RegionD"])
    ]
    plan = module.build_plan(arguments(), azure)
    filesystems = [item for item in plan["template"]["resources"]
                   if item["type"] == "Microsoft.StorageCache/amlFilesystems"]
    assert {item["location"]: item["zones"] for item in filesystems} == {
        "regiona": ["1"], "regionb": ["2"], "regionc": ["3"], "regiond": ["4"],
    }


@pytest.mark.parametrize("restricted_region", ["REGIONB", "UNRELATED"])
def test_sku_restrictions_use_case_insensitive_region_matching(infra, restricted_region):
    module, azure = infra
    azure.skus[0]["restrictions"] = [{"restrictionInfo": {"locations": [restricted_region]}}]
    if restricted_region == "REGIONB":
        with pytest.raises(ValueError, match="SKU restricted in regionb"):
            module.build_plan(arguments(), azure)
    else:
        module.build_plan(arguments(), azure)
    assert not any(call[0] in ("PUT", "DELETE", "PATCH") for call in azure.calls)


def test_network_dependencies_and_shared_hsm(infra):
    module, azure = infra
    plan = module.build_plan(arguments(), azure)
    resources = plan["template"]["resources"]
    ids = {module.resource_id(plan, item): item for item in resources}
    peerings = {key for key, item in ids.items() if item["type"].endswith("/virtualNetworkPeerings")}
    dns_links = {key for key, item in ids.items() if item["type"].endswith("/virtualNetworkLinks")}
    assert len(dns_links) == 4
    for key, item in ids.items():
        assert set(item.get("dependsOn", [])).issubset(ids), key
        if item["type"].endswith("/amlFilesystems"):
            assert peerings.issubset(item["dependsOn"])
            assert dns_links.issubset(item["dependsOn"])
            settings = item["properties"]["hsm"]["settings"]
            assert settings["container"] == STORAGE + "/blobServices/default/containers/data"
            assert settings["loggingContainer"] == STORAGE + "/blobServices/default/containers/logs"
            assert settings["importPrefix"] == "/"
        if item["type"] == "Microsoft.Network/virtualNetworks":
            subnets = item["properties"]["subnets"]
            assert {subnet["name"] for subnet in subnets} == {"compute", "amlfs"}
            a, b = [module.network(subnet["properties"]["addressPrefix"]) for subnet in subnets]
            assert not a.overlaps(b)
            assert all(subnet["properties"]["natGateway"]["id"] in ids for subnet in subnets)
    visited = set()

    def visit(key, ancestors):
        assert key not in ancestors, "Dependency cycle"
        if key in visited:
            return
        for dependency in ids[key].get("dependsOn", []):
            visit(dependency, ancestors | {key})
        visited.add(key)

    for key in ids:
        visit(key, set())


def test_allocation_avoids_existing_peer_and_ipam_ranges(infra):
    module, azure = infra
    peer = SCOPE + "/providers/Microsoft.Network/virtualNetworks/peer"
    azure.resources[peer] = {"id": peer, "properties": {"addressSpace": {"addressPrefixes": ["10.64.0.0/20"]}}}
    azure.resources[PARENT]["properties"]["virtualNetworkPeerings"] = [
        {"properties": {"remoteVirtualNetwork": {"id": peer}}}]
    plan = module.build_plan(arguments(regions=["regionb"], exclude_cidr=["10.64.16.0/20"]), azure)
    assert plan["regions"][1]["vnet_cidr"] == "10.64.32.0/20"
    with pytest.raises(ValueError, match="No free"):
        module.build_plan(arguments(address_pool="10.64.0.0/20"), azure)


@pytest.mark.parametrize("override,message", [
    ({"log_container": "data"}, "must be different"),
    ({"regions": ["unavailable"]}, "not offered"),
    ({"amlfs_subnet_prefix": 29}, "too small"),
    ({"address_pool": "192.0.2.0/24"}, "RFC1918"),
    ({"address_pool": "2001:db8::/32"}, "IPv4"),
    ({"blob_network": "public"}, "already publicly reachable"),
])
def test_invalid_plans_fail_before_writes(infra, override, message):
    module, azure = infra
    with pytest.raises(ValueError, match=message):
        module.build_plan(arguments(**override), azure)
    assert not any(call[0] in ("PUT", "PATCH", "DELETE") for call in azure.calls)


def test_existing_public_access_is_not_widened(infra):
    module, azure = infra
    azure.resources[STORAGE]["properties"].update(publicNetworkAccess="Enabled",
                                                networkAcls={"defaultAction": "Allow"})
    before = copy.deepcopy(azure.resources[STORAGE])
    plan = module.build_plan(arguments(blob_network="public"), azure)
    assert not any("private" in item["type"].lower() for item in plan["template"]["resources"])
    assert azure.resources[STORAGE] == before


def test_hsm_roles_require_explicit_permission_and_wait_before_creation(infra, tmp_path, monkeypatch):
    module, azure = infra
    azure.roles.clear()
    with pytest.raises(ValueError, match="assign-hsm-roles"):
        module.build_plan(arguments(), azure)
    plan = module.build_plan(arguments(assign_hsm_roles=True), azure)
    assert not any(call[0] == "PUT" for call in azure.calls)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: azure.calls.append(("WAIT", seconds)))
    output = module.apply_plan(plan, azure, tmp_path / "plan.json", yes=True)
    operations = [call[0] if call[0] != "CLI" else call[1][2] for call in azure.calls]
    assert operations.index("what-if") < operations.index("PUT") < operations.index("WAIT") < operations.index("create")
    assert len(output["regions"]) == 4
    assert output["regions"][0]["mount_source"] == "10.0.1.4@tcp:/lustrefs"
    assert (tmp_path / "plan.outputs.json").stat().st_mode & 0o777 == 0o600


def test_apply_is_resumable_and_preserves_parent(infra, tmp_path):
    module, azure = infra
    original = copy.deepcopy(azure.resources[PARENT]["properties"]["subnets"][0])
    plan = module.build_plan(arguments(), azure)
    path = tmp_path / "plan.json"
    first = module.apply_plan(plan, azure, path, yes=True)
    second = module.apply_plan(plan, azure, path, yes=True)
    assert first == second
    assert azure.resources[PARENT]["properties"]["subnets"][0] == original
    assert len(azure.resources[PARENT]["properties"]["subnets"]) == 3


def test_apply_cancellation_and_drift_do_not_deploy(infra, tmp_path, monkeypatch):
    module, azure = infra
    plan = module.build_plan(arguments(), azure)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert module.apply_plan(plan, azure, tmp_path / "plan.json") is None
    assert not any(call[0] == "CLI" and call[1][:3] == ("deployment", "group", "create") for call in azure.calls)
    azure.resources[PARENT]["properties"]["subnets"].append(
        {"name": "new-application", "properties": {"addressPrefix": "10.0.1.0/24"}})
    with pytest.raises(ValueError, match="overlaps"):
        module.apply_plan(plan, azure, tmp_path / "plan.json", yes=True)


def test_refuses_foreign_resource_and_custom_parent_routes(infra, tmp_path):
    module, azure = infra
    plan = module.build_plan(arguments(), azure)
    item = next(resource for resource in plan["template"]["resources"]
                if resource["type"].endswith("/amlFilesystems"))
    resource_id = module.resource_id(plan, item)
    azure.resources[resource_id] = {"id": resource_id, "properties": {}}
    with pytest.raises(ValueError, match="not owned"):
        module.preflight(plan, azure)
    del azure.resources[resource_id]
    module.apply_plan(plan, azure, tmp_path / "plan.json", yes=True)
    azure.resources[PARENT]["properties"]["subnets"][1]["properties"]["routeTable"] = {"id": "custom-route"}
    with pytest.raises(ValueError, match="customized parent subnet"):
        module.preflight(plan, azure)


def test_existing_private_dns_zone_and_parent_link_are_reused(infra):
    module, azure = infra
    zone = SCOPE + "/providers/Microsoft.Network/privateDnsZones/" + module.DNS_ZONE
    link = zone + "/virtualNetworkLinks/original"
    azure.resources[zone] = {"id": zone, "properties": {}}
    azure.resources[link] = {"id": link, "properties": {"virtualNetwork": {"id": PARENT}}}
    plan = module.build_plan(arguments(), azure)
    assert not any(item["type"] == "Microsoft.Network/privateDnsZones" for item in plan["template"]["resources"])
    links = [item for item in plan["template"]["resources"] if item["type"].endswith("/virtualNetworkLinks")]
    assert len(links) == 3
    assert {"id": link, "api": module.DNS_API} in plan["references"]


def test_existing_parent_blob_endpoint_is_reused_without_changing_dns_record(infra):
    module, azure = infra
    zone = SCOPE + "/providers/Microsoft.Network/privateDnsZones/" + module.DNS_ZONE
    endpoint = SCOPE + "/providers/Microsoft.Network/privateEndpoints/existing-blob"
    group = endpoint + "/privateDnsZoneGroups/default"
    azure.resources[STORAGE]["properties"]["privateEndpointConnections"] = [{
        "properties": {"privateEndpoint": {"id": endpoint},
                       "privateLinkServiceConnectionState": {"status": "Approved"}}}]
    azure.resources[endpoint] = {"id": endpoint, "properties": {
        "subnet": {"id": PARENT + "/subnets/existing-pe"},
        "privateLinkServiceConnections": [{"properties": {
            "groupIds": ["blob"], "privateLinkServiceId": STORAGE}}],
    }}
    azure.resources[group] = {"id": group, "properties": {
        "privateDnsZoneConfigs": [{"properties": {"privateDnsZoneId": zone}}]}}
    azure.resources[zone] = {"id": zone, "properties": {}}
    azure.resources[zone + "/A/example"] = {"id": zone + "/A/example", "properties": {}}
    plan = module.build_plan(arguments(), azure)
    assert plan["reused_blob_endpoint"] == [endpoint, group]
    assert len(plan["parent_subnets"]) == 1
    assert not any(item["type"] == "Microsoft.Network/privateEndpoints" for item in plan["template"]["resources"])


def test_refuses_dns_record_created_after_planning(infra):
    module, azure = infra
    zone = SCOPE + "/providers/Microsoft.Network/privateDnsZones/" + module.DNS_ZONE
    azure.resources[zone] = {"id": zone, "properties": {}}
    plan = module.build_plan(arguments(), azure)
    azure.resources[zone + "/A/example"] = {"id": zone + "/A/example", "properties": {}}
    with pytest.raises(ValueError, match="Blob DNS record"):
        module.preflight(plan, azure)
    with pytest.raises(ValueError, match="existing Blob DNS record"):
        module.build_plan(arguments(), azure)


def test_failed_what_if_never_grants_roles_or_deploys(infra, tmp_path, monkeypatch):
    module, azure = infra
    azure.roles.clear()
    plan = module.build_plan(arguments(assign_hsm_roles=True), azure)
    monkeypatch.setattr(azure, "run", lambda *args: {"status": "Failed"})
    with pytest.raises(ValueError, match="what-if did not succeed"):
        module.apply_plan(plan, azure, tmp_path / "plan.json", yes=True)
    assert not any(call[0] == "PUT" for call in azure.calls)


def test_arm_error_classification_does_not_hide_forbidden(infra, monkeypatch):
    module, _ = infra
    monkeypatch.setattr(module.shutil, "which", lambda name: "/test/az")
    azure = module.Azure()
    result = SimpleNamespace(returncode=1, stdout="",
                             stderr='ERROR: Not Found({"error":{"code":"ResourceNotFound"}})')
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: result)
    assert azure.rest("/resource", module.NETWORK_API, missing=True) is None
    result.stderr = 'ERROR: Forbidden({"error":{"code":"AuthorizationFailed"}})'
    with pytest.raises(ValueError, match="AuthorizationFailed"):
        azure.rest("/resource", module.NETWORK_API, missing=True)


def test_plan_cli_writes_protected_file_without_deploying(infra, tmp_path, monkeypatch):
    module, azure = infra
    monkeypatch.setattr(module, "Azure", lambda: azure)
    output = tmp_path / "plan.json"
    args = ["plan", "--resource-group", "rg-example", "--parent-vnet", "parent",
            "--regions", "regionb", "regione", "--sku", "AMLFS-Durable-Premium-500",
            "--storage-account", "example", "--data-container", "data", "--log-container", "logs",
            "--address-pool", "10.64.0.0/12", "--output", str(output)]
    assert module.main(args) == 0
    original = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert module.main(args) == 1
    assert output.read_bytes() == original
    assert not any(call[0] in ("PUT", "DELETE", "PATCH") for call in azure.calls)
