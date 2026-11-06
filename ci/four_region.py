#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Four-region Azure CycleCloud deployment and VM bootstrap for GitHub Actions."""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
from urllib import parse, request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import configure
import provision


UPSTREAM_BRANCH = "main"
UPSTREAM_COMMIT = "97f57e3ccc72b49ca70e889be9799efb9cc6fff0"
HELPER_PATH = "infrastructure_references/azure_cyclecloud_workspace_for_slurm/scripts/deploy-ccws.sh"
HELPER_SHA256 = "29455c583278f7fe3e3100bacd9741b9b8570aa085e22834419502d55f335657"
WORKSPACE_REF = "2026.10.23"
WORKSPACE_COMMIT = "9228f1f14f56609e7886aea317295c9eb5088dd8"
VM_WORK = "/var/lib/cyclecloud-multi-region-ci"
REMOTE_ARTIFACTS = ("infrastructure.plan.json", "infrastructure.plan.outputs.json",
                    "checks.json", "installation.json")


class NoOidcRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing to redirect the GitHub OIDC token request")


class PollingAzure(provision.Azure):
    """Keep the short-lived runner assertion valid throughout Azure polling."""

    def __init__(self):
        super().__init__()
        self.refresh_after = time.monotonic()

    def run(self, *args, **kwargs):
        if time.monotonic() >= self.refresh_after:
            self.refresh_login()
        return super().run(*args, **kwargs)

    def refresh_login(self):
        url = parse.urlsplit(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
        if (url.scheme != "https" or not url.hostname
                or not url.hostname.endswith(".actions.githubusercontent.com")
                or url.username or url.password or url.port not in (None, 443) or url.fragment):
            raise ValueError("Invalid GitHub OIDC token endpoint")
        account = super().run("account", "show")
        if (account.get("id", "").lower() != self.subscription.lower()
                or account.get("user", {}).get("type") != "servicePrincipal"):
            raise ValueError("Runner login does not describe the expected federated subscription")
        client = str(uuid.UUID(account["user"]["name"]))
        tenant = str(uuid.UUID(account["tenantId"]))
        query = [(key, value) for key, value in parse.parse_qsl(url.query) if key != "audience"]
        query.append(("audience", "api://AzureADTokenExchange"))
        endpoint = parse.urlunsplit(url._replace(query=parse.urlencode(query)))
        token_request = request.Request(endpoint, headers={
            "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"],
        })
        with request.build_opener(NoOidcRedirect()).open(token_request, timeout=30) as response:
            payload = json.load(response)
        token = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ValueError("GitHub did not return a valid OIDC assertion")
        try:
            encoded = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, UnicodeError):
            raise ValueError("GitHub OIDC assertion has invalid expiry metadata") from None
        expires = claims.get("exp") if isinstance(claims, dict) else None
        if type(expires) is not int or expires <= time.time() + 120:
            raise ValueError("GitHub OIDC assertion is expired or expires too soon")
        print("::add-mask::" + token, flush=True)
        try:
            result = subprocess.run([
                "az", "login", "--service-principal", "--username", client, "--tenant", tenant,
                "--federated-token", token, "--only-show-errors", "--output", "none",
            ], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise ValueError("Azure federated login refresh timed out") from None
        if result.returncode:
            raise ValueError("Azure federated login refresh failed: "
                             + result.stderr.replace(token, "[redacted]").strip())
        super().run("account", "set", "--subscription", self.subscription)
        self.refresh_after = time.monotonic() + max(0, expires - time.time() - 120)
        print("Refreshed the existing Azure runner login for continued polling.", flush=True)


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("FOUR_REGION_CONFIG must be a JSON object")
    regions = value.get("regions")
    if (not isinstance(regions, list) or len(regions) != 4
            or any(not isinstance(region, str) or not re.fullmatch(r"[a-z][a-z0-9]+", region) for region in regions)
            or len(set(regions)) != 4):
        raise ValueError("FOUR_REGION_CONFIG.regions must contain four distinct Azure locations, home first")
    result = {
        "regions": regions, "parent_cidr": "10.0.0.0/16", "address_pool": "10.64.0.0/12",
        "vm_size": "Standard_D4as_v5", "image": "cycle.image.ubuntu24",
        "amlfs_sku": "AMLFS-Durable-Premium-500", "capacity_tib": 4,
        "workspace_ref": WORKSPACE_REF, "admin_username": "hpcadmin", "cluster_name": "multi-region-ci",
    }
    if set(value) - set(result):
        raise ValueError("Unknown FOUR_REGION_CONFIG keys: " + ", ".join(sorted(set(value) - set(result))))
    result.update(value)
    for key, pattern in {
        "vm_size": r"Standard_[A-Za-z0-9_]+",
        "image": r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        "amlfs_sku": r"AMLFS-Durable-Premium-(40|125|250|500)",
        "workspace_ref": r"\d{4}\.\d{2}\.\d{2}",
        "admin_username": r"[a-z][a-z0-9_-]{0,31}",
        "cluster_name": r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}",
    }.items():
        if not isinstance(result[key], str) or not re.fullmatch(pattern, result[key]):
            raise ValueError("Invalid literal CI setting: " + key)
    if result["workspace_ref"] != WORKSPACE_REF:
        raise ValueError("Workspace release changed; review its contract before deployment")
    if result["admin_username"] in {"root", "admin", "administrator", "user", "test", "guest"}:
        raise ValueError("Use a non-reserved admin_username")
    if type(result["capacity_tib"]) is not int or result["capacity_tib"] < 4:
        raise ValueError("capacity_tib must be an integer of at least 4")
    parent, pool = [provision.network(result[key]) for key in ("parent_cidr", "address_pool")]
    if parent.prefixlen > 20 or parent.overlaps(pool):
        raise ValueError("Use a parent /20 or larger, separate from the remote VNet address pool")
    private = [provision.network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if not all(any(cidr.subnet_of(block) for block in private) for cidr in (parent, pool)):
        raise ValueError("CI address spaces must be RFC1918")
    return result


def owner_tags(repository, run_id, attempt):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("Run ID and attempt must be numeric")
    return {"purpose": "cyclecloud-four-region-ci", "repository": repository,
            "run-id": run_id, "run-attempt": attempt}


def group_name(tags):
    return "cyclecloud-multi-region-ci-" + tags["run-id"] + "-" + tags["run-attempt"]


def workspace_deployment_name(group):
    return group + "-workspace-resources"


def owned_group(azure, group, repository):
    match = re.fullmatch(r"cyclecloud-multi-region-ci-(\d+)-(\d+)", group)
    if not match:
        raise ValueError("Refusing to manage a resource group outside the CI naming convention")
    value = azure.run("group", "show", "--name", group, missing=True)
    if value is not None:
        expected = owner_tags(repository, *match.groups())
        if any(value.get("tags", {}).get(key) != content for key, content in expected.items()):
            raise ValueError("Resource group ownership tags do not match this repository/run")
    return value


def prepare_helper(source):
    """Replace only the reviewed branch's dynamic assignments; fail on drift."""
    if hashlib.sha256(source.encode()).hexdigest() != HELPER_SHA256:
        raise ValueError("Upstream helper changed; review its content before deployment")
    replacements = [
        ('if [[ -z "${!var:-}" ]]; then missing+=("$var"); fi',
         'declare -n required_value="$var"\n'
         '\tif [[ -z "${required_value:-}" ]]; then missing+=("$var"); fi\n'
         '\tunset -n required_value', 1),
        ('eval "$var_name=\\"$sku_value\\""', 'printf -v "$var_name" \'%s\' "$sku_value"', 2),
        ('eval "$var_name=\\"$input\\""', 'printf -v "$var_name" \'%s\' "$input"', 1),
    ]
    for old, new, count in replacements:
        if source.count(old) != count:
            raise ValueError("Upstream helper changed; review its contract and assignment hardening")
        source = source.replace(old, new)
    if re.search(r"\beval\b|\$\{!|\$\{[^}]*@P", source):
        raise ValueError("Upstream helper still contains dynamic evaluation")
    return source


def run_local(*command):
    # Never echo command arguments: the upstream helper requires a password argument.
    result = subprocess.run(list(map(str, command)), check=False)
    if result.returncode:
        raise ValueError("Local CI command failed (arguments withheld)")


def storage_name(repository, group):
    return "cimr" + hashlib.sha256((repository + group).encode()).hexdigest()[:16]


def validate_parameters(parameters, config, public_key, group):
    """Require the reviewed helper's stopped-cluster and NFS contract."""
    values = parameters["parameters"]
    expected = {
        "key": {"type": "entered", "value": public_key},
        "adminUsername": config["admin_username"],
        "clusterName": config["cluster_name"], "resourceGroup": group,
        "schedFilesystem": {"type": "nfs-new", "nfsCapacityInGb": 30},
        "sharedFilesystem": {"type": "nfs-new", "nfsCapacityInGb": 128},
    }
    if (any(values.get(key) != {"value": value} for key, value in expected.items())
            or "adminSshPublicKey" in values
            or values.get("slurmSettings", {}).get("value", {}).get("startCluster") is not False):
        raise ValueError("Upstream parameter contract changed; refusing to deploy it")


def scoped_resource_name(resource_id, subscription, group, resource_type):
    prefix = "/subscriptions/" + subscription + "/resourceGroups/" + group + "/providers/" + resource_type + "/"
    if not isinstance(resource_id, str) or not resource_id.lower().startswith(prefix.lower()):
        raise ValueError("Discovered resource is outside the owned scenario")
    name = resource_id[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise ValueError("Invalid discovered resource name")
    return name


def owned_vms(azure, group):
    return [
        scoped_resource_name(vm["id"], azure.subscription, group, "Microsoft.Compute/virtualMachines")
        for vm in azure.run("vm", "list", "--resource-group", group)
    ]


def workspace_home(config):
    return Path("/home") / config["admin_username"] / config["cluster_name"]


def inject_bootstrap(workspace, config, context):
    """Patch the disposable Workspace's deployment lookup and append our bootstrap."""
    install = workspace / "bicep/install.sh"
    original = install.read_bytes()
    if not original.rstrip(b"\n").endswith(b"\nexit 0"):
        raise ValueError("Workspace install.sh must end with a standalone 'exit 0' before bootstrap injection")
    ccw = workspace / "bicep/ccw.bicep"
    bicep = ccw.read_text()
    if bicep.count("deployScript: loadTextContent('./install.sh')") != 1 or "output multiRegionBundle " in bicep:
        raise ValueError("Workspace bootstrap wiring changed or is already extended")
    deployment = workspace_deployment_name(group_name(owner_tags(
        context["repository"], context["run_id"], context["attempt"])))
    template = workspace / "bicep/mainTemplate.bicep"
    template_text = template.read_text()
    module_pattern = r"(?m)^(module makeCCWresources 'ccw\.bicep' = \{\n[ \t]+name: )'([^'\r\n]+)'"
    modules = list(re.finditer(module_pattern, template_text))
    if len(modules) != 1:
        raise ValueError("Workspace deployment module changed; review its naming contract")
    upstream_name = modules[0][2].encode()
    original, replacements = re.subn(
        rb"(?m)^deployment_name='" + re.escape(upstream_name) + rb"'$",
        ("deployment_name='" + deployment + "'").encode(),
        original,
    )
    if replacements != 1:
        raise ValueError("Workspace install.sh deployment name changed; it must match the deployment module")
    template_text = re.sub(
        module_pattern, lambda match: match[1] + "'" + deployment + "'", template_text)
    settings = dict(context, config=config)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in (
            "project.ini", "ci/four_region.py", "scripts/configure.py", "scripts/provision.py",
            "ci/slurm_jobs.py", "ci/scheduler_jobs.py",
            "specs/default/cluster-init/scripts/20-install-cyclecloud-multi-region.sh",
            "blobs/cyclecloud-multi-region.tar.gz",
            "blobs/cyclecloud-multi-region.tar.gz.sha256",
        ):
            archive.add(ROOT / name, arcname=name, recursive=False)
        data = json.dumps(settings).encode()
        entry = tarfile.TarInfo("ci-settings.json")
        entry.size, entry.mode = len(data), 0o600
        archive.addfile(entry, io.BytesIO(data))
    payload = buffer.getvalue()
    encoded = base64.b64encode(payload).decode()
    # Bicep loadTextContent has a 131072-character limit.
    if len(encoded) > 131072:
        raise ValueError("Bootstrap bundle exceeds the reviewed Bicep payload limit")
    (workspace / "bicep/multi-region-bundle.base64").write_text(encoded)
    hook = (ROOT / "ci/bootstrap.sh").read_text().replace(
        "@BUNDLE_SHA256@", hashlib.sha256(payload).hexdigest()).replace(
        "@WORKSPACE_DEPLOYMENT_NAME@", deployment)
    patched = original[:original.rfind(b"exit 0")] + hook.encode() + b"\nexit 0\n"
    if len(patched) > 65535:
        raise ValueError("Extended bootstrap exceeds the Azure VM customData limit")
    install.write_bytes(patched)
    ccw.write_text(bicep + "\noutput multiRegionBundle string = loadTextContent('./multi-region-bundle.base64')\n")
    template.write_text(template_text)
    return hashlib.sha256(payload).hexdigest()


def runtime_ids(subscription, group):
    scope = "/subscriptions/" + subscription
    seed = scope.lower() + "/" + group.lower()
    return {
        key: str(uuid.uuid5(uuid.NAMESPACE_URL, seed + "/multi-region-ci/" + key))
        for key in ("runtime-role", "poll-role", "runtime-assignment", "poll-assignment")
    }


def grant_runtime(azure, subscription, group):
    """Keep RBAC administration on the federated runner, never on the VM."""
    identity = azure.run("identity", "show", "--resource-group", group,
                         "--name", "ccwLockerManagedIdentity")
    principal = str(uuid.UUID(identity["principalId"]))
    scope = "/subscriptions/" + subscription
    group_scope = scope + "/resourceGroups/" + group
    ids = runtime_ids(subscription, group)
    actions = json.loads((ROOT / "roles/cyclecloud-multi-region-runtime.json").read_text())["Actions"]
    poll = "Microsoft.StorageCache/locations/ascOperations/read"
    for kind, role_scope, permissions in (
        ("runtime", group_scope, [action for action in actions if action != poll]),
        ("poll", scope, [poll]),
    ):
        role_id = scope + "/providers/Microsoft.Authorization/roleDefinitions/" + ids[kind + "-role"]
        azure.rest(role_id, provision.AUTH_API, "PUT", {"properties": {
            "roleName": group + "-multi-region-" + kind, "type": "CustomRole",
            "description": "Disposable four-region Azure CycleCloud CI permission",
            "assignableScopes": [role_scope],
            "permissions": [{"actions": permissions, "notActions": [], "dataActions": [], "notDataActions": []}],
        }})
        azure.rest(role_scope + "/providers/Microsoft.Authorization/roleAssignments/"
                   + ids[kind + "-assignment"], provision.AUTH_API, "PUT", {"properties": {
                       "principalId": principal, "principalType": "ServicePrincipal",
                       "roleDefinitionId": role_id,
                   }})


def remove_runtime(azure, subscription, group):
    scope = "/subscriptions/" + subscription
    ids = runtime_ids(subscription, group)
    for kind, role_scope in (("runtime", scope + "/resourceGroups/" + group), ("poll", scope)):
        role_id = scope + "/providers/Microsoft.Authorization/roleDefinitions/" + ids[kind + "-role"]
        role = azure.rest(role_id, provision.AUTH_API, missing=True)
        if role is None:
            continue
        if (role["properties"]["roleName"] != group + "-multi-region-" + kind
                or role["properties"]["assignableScopes"] != [role_scope]):
            raise ValueError("CI runtime role ownership mismatch")
        assignment = role_scope + "/providers/Microsoft.Authorization/roleAssignments/" + ids[kind + "-assignment"]
        existing = azure.rest(assignment, provision.AUTH_API, missing=True)
        if existing is not None:
            if existing["properties"]["roleDefinitionId"].lower() != role_id.lower():
                raise ValueError("CI runtime assignment ownership mismatch")
            azure.rest(assignment, provision.AUTH_API, "DELETE")
        azure.rest(role_id, provision.AUTH_API, "DELETE")


def verify_infrastructure(plan, azure):
    if len(plan["regions"]) != 4 or len({region["location"] for region in plan["regions"]}) != 4:
        raise ValueError("Integration scenario did not produce exactly four distinct regions")
    filesystems = []
    for region in plan["regions"]:
        filesystem = azure.rest(region["filesystem_id"], provision.AMLFS_API)
        properties = filesystem["properties"]
        if (properties.get("provisioningState") != "Succeeded"
                or not properties.get("clientInfo", {}).get("mgsAddress")):
            raise ValueError("Filesystem not ready: " + region["location"])
        settings = properties["hsm"]["settings"]
        expected = plan["storage_id"] + "/blobServices/default/containers/"
        if settings["container"] != expected + "data" or settings["loggingContainer"] != expected + "logs":
            raise ValueError("HSM containers do not match the shared CI storage account")
        filesystems.append({"region": region["location"], "filesystem_id": region["filesystem_id"]})
    for resource in plan["template"]["resources"]:
        if resource["type"].endswith("/virtualNetworkPeerings"):
            actual = azure.rest(provision.resource_id(plan, resource), provision.NETWORK_API)
            if actual["properties"].get("peeringState") != "Connected":
                raise ValueError("Peering is not Connected: " + resource["name"])
    return {"regions": filesystems, "checks": ["four-filesystems-succeeded", "shared-hsm", "peerings-connected"],
            "slurm_started": False, "data_transfer_tested": False}


def deploy_workspace(azure, config, upstream, work, repository, run_id, attempt):
    tags = owner_tags(repository, run_id, attempt)
    group = group_name(tags)
    if owned_group(azure, group, repository) is not None:
        raise ValueError("CI resource group already exists; use a new run attempt or inspect/clean it up")
    subscription = str(uuid.UUID(os.environ["AZURE_SUBSCRIPTION_ID"]))
    password = os.environ["CCWS_ADMIN_PASSWORD"]
    public_key = os.environ["SSH_PUBLIC_KEY"].strip()
    if not password or not re.fullmatch(r"ssh-(rsa|ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]+)?", public_key):
        raise ValueError("Provide CCWS_ADMIN_PASSWORD and a single OpenSSH SSH_PUBLIC_KEY")
    public_key = " ".join(public_key.split()[:2])
    helper = prepare_helper((upstream / HELPER_PATH).read_text())
    artifacts = work / "artifacts"
    artifacts.mkdir(parents=True, mode=0o700, exist_ok=True)
    private = work / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    helper_file = private / "deploy-ccws.sh"
    helper_file.write_text(helper)
    key_file = private / "key.pub"
    key_file.write_text(public_key + "\n")
    parameters_file = private / "workspace.parameters.json"
    workspace = private / "workspace"
    deployment = group + "-workspace"
    source_sha = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if source_sha != UPSTREAM_COMMIT:
        raise ValueError("Checkout does not match the reviewed upstream commit; refusing deployment")
    provision.write_json(artifacts / "scenario.json", {
        "resource_group": group, "subscription": subscription, "upstream_branch": UPSTREAM_BRANCH,
        "upstream_commit": source_sha, "workspace_ref": config["workspace_ref"],
        "workspace_commit": WORKSPACE_COMMIT,
        "admin_username": config["admin_username"], "cluster_name": config["cluster_name"],
        "regions": config["regions"], "deployment": deployment,
        "workspace_deployment": workspace_deployment_name(group),
    })
    azure.run("group", "create", "--name", group, "--location", config["regions"][0],
              "--tags", *[key + "=" + value for key, value in tags.items()])
    # Inherited by the VM-created HSM account; the VM needs no role-assignment rights.
    principal = str(uuid.UUID(os.environ["HSM_PRINCIPAL_ID"]))
    for role in provision.HSM_ROLES.values():
        azure.run("role", "assignment", "create", "--assignee-object-id", principal,
                  "--assignee-principal-type", "ServicePrincipal", "--role", role,
                  "--scope", "/subscriptions/" + subscription + "/resourceGroups/" + group)
    command = [
        "bash", helper_file, "--subscription-id", subscription, "--resource-group", group,
        "--location", config["regions"][0], "--ssh-public-key-file", key_file,
        "--admin-password", password, "--admin-username", config["admin_username"],
        "--cluster-name", config["cluster_name"], "--workspace-ref", config["workspace_ref"],
        "--workspace-commit", WORKSPACE_COMMIT, "--workspace-dir", workspace,
        "--output-file", parameters_file, "--network-address-space", config["parent_cidr"],
        "--shared-filesystem", "nfs", "--shared-nfs-size", "128",
        "--slurm-no-start", "--ood-no-start", "--no-az", "--accept-marketplace", "--silent",
    ]
    for node in ("scheduler", "login", "htc", "hpc", "gpu"):
        command.extend(["--" + node + "-sku", config["vm_size"], "--" + node + "-image", config["image"]])
    for node in ("htc", "hpc", "gpu"):
        command.extend(["--" + node + "-max-nodes", "1"])
    run_local(*command)
    workspace_sha = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
    if workspace_sha != WORKSPACE_COMMIT:
        raise ValueError("Workspace commit changed; review its contract before deployment")
    inject_bootstrap(workspace, config, {
        "subscription": subscription, "hsm_principal_id": principal,
        "repository": repository, "run_id": run_id, "attempt": attempt,
    })
    parameters = json.loads(parameters_file.read_text())
    validate_parameters(parameters, config, public_key, group)
    parameters["parameters"]["tags"]["value"] = {"Resource group": tags}
    parameters_file.write_text(json.dumps(parameters))
    # A separate protected file keeps secrets out of Azure CLI arguments/artifacts.
    secret_file = private / "secrets.parameters.json"
    provision.write_json(secret_file, {"parameters": {"adminPassword": {"value": password},
                                                    "databaseAdminPassword": {"value": ""}}})
    try:
        azure.run("deployment", "sub", "create", "--name", deployment, "--location", config["regions"][0],
                  "--template-file", workspace / "bicep/mainTemplate.bicep",
                  "--parameters", "@" + str(parameters_file), "@" + str(secret_file))
    finally:
        secret_file.unlink()
    # Verify subscription deployment preserved the ownership boundary used by cleanup.
    owned_group(azure, group, repository)
    grant_runtime(azure, subscription, group)


def deploy_regions(azure, config, work, repository, run_id, attempt):
    group = group_name(owner_tags(repository, run_id, attempt))
    if owned_group(azure, group, repository) is None:
        raise ValueError("Deploy the owned Workspace before regional infrastructure")
    subscription = str(uuid.UUID(os.environ["AZURE_SUBSCRIPTION_ID"]))
    principal = str(uuid.UUID(os.environ["HSM_PRINCIPAL_ID"]))
    deployment = azure.run("deployment", "group", "show", "--resource-group", group,
                           "--name", workspace_deployment_name(group))
    outputs = deployment["properties"]["outputs"]
    if (outputs["resourceGroup"]["value"] != group
            or outputs["subscriptionId"]["value"].lower() != subscription.lower()
            or outputs["clusterName"]["value"] != config["cluster_name"]):
        raise ValueError("Workspace outputs do not describe the owned scenario")
    parent_vnet = scoped_resource_name(outputs["vnet"]["value"]["id"], subscription, group,
                                      "Microsoft.Network/virtualNetworks")
    artifacts = work / "artifacts"
    account = storage_name(repository, group)
    storage_id = ("/subscriptions/" + subscription + "/resourceGroups/" + group
                  + "/providers/Microsoft.Storage/storageAccounts/" + account)
    azure.rest(storage_id, provision.STORAGE_API, "PUT", {
        "location": config["regions"][0], "sku": {"name": "Standard_LRS"}, "kind": "StorageV2",
        "properties": {
            "allowBlobPublicAccess": False, "allowSharedKeyAccess": True,
            "publicNetworkAccess": "Disabled", "minimumTlsVersion": "TLS1_2",
            "supportsHttpsTrafficOnly": True,
        },
    })
    azure.run("resource", "wait", "--ids", storage_id, "--api-version", provision.STORAGE_API,
              "--created", "--timeout", "600")
    for container in ("data", "logs"):
        azure.rest(storage_id + "/blobServices/default/containers/" + container,
                   provision.STORAGE_API, "PUT", {"properties": {"publicAccess": "None"}})
    args = argparse.Namespace(
        subscription=subscription, resource_group=group, parent_vnet=parent_vnet,
        regions=config["regions"][1:], sku=config["amlfs_sku"], capacity_tib=config["capacity_tib"],
        storage_account=account, data_container="data", log_container="logs", prefix="multi-region",
        address_pool=config["address_pool"], exclude_cidr=[], vnet_prefix=20, amlfs_subnet_prefix=None,
        blob_network="private-endpoint", hsm_principal_id=principal, assign_hsm_roles=False,
    )
    plan = provision.build_plan(args, azure)
    plan_path = artifacts / "infrastructure.plan.json"
    provision.write_json(plan_path, plan)
    readiness_deadline = None
    while True:
        try:
            provision.apply_plan(plan, azure, plan_path, yes=True)
            break
        except ValueError as error:
            message = "Storage Account " + account + " is not found or is inaccessible"
            if message not in str(error):
                raise
            if readiness_deadline is None:
                readiness_deadline = time.monotonic() + 1800
            remaining = readiness_deadline - time.monotonic()
            if remaining <= 0:
                raise
            print("AMLFS HSM storage access is not ready; retrying the same saved plan.",
                  file=sys.stderr, flush=True)
            time.sleep(min(60, remaining))
    provision.write_json(artifacts / "checks.json", verify_infrastructure(plan, azure))
    return plan


def ci_slurm_configuration(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("Workspace additional_slurm_config must be text")
    directives = re.findall(r"(?im)^[ \t]*MinJobAge[ \t]*=", value)
    if directives:
        matches = list(re.finditer(
            r"(?im)^[ \t]*MinJobAge[ \t]*=[ \t]*(\d+)[ \t]*(?:#[^\r\n]*)?\r?$", value))
        if len(directives) != 1 or len(matches) != 1:
            raise ValueError("Ambiguous or invalid MinJobAge in additional Slurm configuration")
        match = matches[0]
        if int(match[1]) >= 7200:
            return value
        return value[:match.start(1)] + "7200" + value[match.end(1):]
    return value + ("\n" if value and not value.endswith("\n") else "") + "MinJobAge=7200"


def configure_workspace(config, plan, work, version, home=None):
    """Preserve the exact installed template and exported Workspace parameters."""
    private = work / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    name = config["cluster_name"]
    home = workspace_home(config) if home is None else home
    nodes = json.loads(subprocess.check_output([
        "/usr/local/bin/cyclecloud", "show_nodes", "-c", name, "--format=json",
        "-a", "Name,Status",
    ], text=True))
    if not isinstance(nodes, list) or not nodes or any(
        node.get("Status") not in ("Off", "Terminated") for node in nodes
    ):
        raise ValueError("Refusing to replace a cluster that is not stopped")
    before = private / "parameters.before.json"
    run_local("/usr/local/bin/cyclecloud", "export_parameters", name, "--format=json", "-o", before)
    parameters = json.loads(before.read_text())
    deployment = json.loads((home / "deployment.json").read_text())
    if (deployment["clusterName"]["value"] != name
            or deployment["adminUsername"]["value"] != config["admin_username"]
            or deployment["subscriptionId"]["value"].lower() != plan["subscription"].lower()
            or deployment["resourceGroup"]["value"] != plan["resource_group"]
            or deployment["slurmSettings"]["value"]["startCluster"] is not False):
        raise ValueError("Workspace outputs do not describe the owned stopped scenario")
    identity = deployment["managedIdentityId"]["value"]
    if parameters.get("ManagedIdentity") not in (None, "", identity, [identity]):
        raise ValueError("Workspace scheduler identity differs from the runtime role recipient")
    parameters["ManagedIdentity"] = identity
    parameters["additional_slurm_config"] = ci_slurm_configuration(parameters.get("additional_slurm_config"))
    original = (home / "slurm_template.txt").read_text()
    tree = configure.parse_template(original)
    cluster, _ = configure.inspect_template(tree)
    version = configure.release(version)
    outputs = json.loads((work / "artifacts/infrastructure.plan.outputs.json").read_text())
    if outputs["plan_id"] != plan["id"]:
        raise ValueError("Filesystem outputs do not match the applied plan")
    regions = []
    for index, location in enumerate(config["regions"]):
        region = next(region for region in outputs["regions"] if region["location"] == location)
        letter = configure.region_letter(index)
        regions.append({
            "key": "region" + letter, "letter": letter, "location": location,
            "subscription_id": plan["subscription"], "resource_group": plan["resource_group"],
            "filesystem": region["filesystem_id"].split("/")[-1], "source": region["mount_source"],
            "mountpoint": "/lustre",
            "subnet": region["compute_subnet"] if index else parameters["SubnetId"],
            "partition": "multi-region" + letter, "machine_type": config["vm_size"],
            "image": config["image"], "max_count": 1, "hpc": False, "use_pcpu": False,
        })
    partitions = []
    for node in configure.home_arrays(cluster):
        native = node.child("configuration")
        props = native.properties() if native else {}
        if props.get("slurm.role") != "login":
            partition = props.get("slurm.partition", node.name.split(" ", 1)[1])
            partitions.append(configure.identifier(partition))
    settings = {
        "subscription": plan["subscription"], "locker": "azure-storage",
        "state": "/sched/cyclecloud-multi-region", "home_partitions": partitions,
        "enabled": True, "parameters": parameters, "regions": regions,
    }
    _, files = configure.configure_template(tree, settings, version)
    # New arrays must retain Workspace's execute-node project customizations.
    for region in regions[1:]:
        cluster.child("nodearray " + region["partition"]).set("AdditionalClusterInitSpecs", "$HTCClusterInitSpecs")
    rendered = tree.render()
    files = {name: json.dumps(value, indent=2) + "\n" for name, value in files.items()}
    files.update({"slurm-upstream.txt": original, "slurm-multi-region.txt": rendered})
    bundle = private / "cluster"
    configure.write_bundle(bundle, files)
    run_local("/usr/local/bin/cyclecloud", "project", "build")
    run_local("/usr/local/bin/cyclecloud", "project", "upload", "azure-storage")
    run_local("/usr/local/bin/cyclecloud", "import_cluster", name, "-f",
              bundle / "slurm-multi-region.txt", "-c", cluster.name.split(" ", 1)[1],
              "-p", bundle / "parameters.json", "--force")
    after = private / "parameters.after.json"
    run_local("/usr/local/bin/cyclecloud", "export_parameters", name, "--format=json", "-o", after)
    effective = json.loads(after.read_text())
    expected = json.loads(files["parameters.json"])
    if any(effective.get(key) != value for key, value in expected.items()
           if key.startswith("MultiRegion") or key in ("ManagedIdentity", "additional_slurm_config")):
        raise ValueError("Imported cluster parameters do not match the generated configuration")
    return {
        "execution_host": "Azure CycleCloud VM", "project_uploaded": True,
        "cluster_imported": True, "extension_enabled": True, "slurm_started": False,
        "node_installation_tested": False, "data_transfer_tested": False,
        "slurm_project_version": version, "regions": config["regions"],
        "template_sha256": hashlib.sha256(original.encode()).hexdigest(),
    }


def install_on_vm(settings_path, version):
    settings = json.loads(settings_path.read_text())
    config = validate_config(settings["config"])
    work = Path(VM_WORK)
    artifacts = work / "artifacts"
    artifacts.mkdir(mode=0o700, exist_ok=True)
    for key, field in (("AZURE_SUBSCRIPTION_ID", "subscription"), ("HSM_PRINCIPAL_ID", "hsm_principal_id")):
        os.environ[key] = str(uuid.UUID(settings[field]))
    azure = provision.Azure()
    # Bootstrap uses its system identity; no runner credentials or tokens are copied.
    azure.run("login", "--identity")
    azure.subscription = settings["subscription"]
    os.chdir(ROOT)
    plan = deploy_regions(azure, config, work, settings["repository"], settings["run_id"], settings["attempt"])
    report = configure_workspace(config, plan, work, version)
    provision.write_json(artifacts / "installation.json", report)
    with tarfile.open(work / "results.tar.gz", "w:gz") as archive:
        for name in REMOTE_ARTIFACTS:
            archive.add(artifacts / name, arcname=name, recursive=False)


def vm_message(azure, group, vm_name, script, prefix):
    response = azure.run("vm", "run-command", "invoke", "--resource-group", group,
                         "--name", vm_name, "--command-id", "RunShellScript",
                         "--scripts", script)
    for value in response.get("value", []):
        for line in value.get("message", "").splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
    raise ValueError("VM Run Command did not return the expected " + prefix + " response")


def wait_for_bootstrap(azure, work, repository, run_id, attempt, timeout=11700):
    group = group_name(owner_tags(repository, run_id, attempt))
    if owned_group(azure, group, repository) is None:
        raise ValueError("Owned Workspace is missing")
    vms = owned_vms(azure, group)
    if len(vms) != 1:
        raise ValueError("Expected exactly one Workspace VM in the owned scenario")
    vm_name = vms[0]
    deadline = time.monotonic() + timeout
    while True:
        state = vm_message(azure, group, vm_name,
                           "if test -f " + VM_WORK + "/status; then\n"
                           " printf 'MULTI_REGION_STATUS:'; cat " + VM_WORK + "/status\n"
                           "elif test -f /var/lib/cloud/instance/boot-finished; then\n"
                           " echo MULTI_REGION_STATUS:failed\nelse echo MULTI_REGION_STATUS:waiting\nfi",
                           "MULTI_REGION_STATUS:")
        if state == "succeeded":
            break
        if state == "failed":
            raise ValueError("VM bootstrap failed; inspect protected VM bootstrap logs (retain=true)")
        if state not in ("waiting", "running") or time.monotonic() >= deadline:
            raise ValueError("VM bootstrap did not finish within its deadline")
        time.sleep(30)
    metadata = json.loads(vm_message(azure, group, vm_name,
        "python3 - <<'PY'\nimport hashlib,json,pathlib\n"
        "data=pathlib.Path('" + VM_WORK + "/results.tar.gz').read_bytes()\n"
        "print('MULTI_REGION_RESULT:'+json.dumps({'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}))\nPY",
        "MULTI_REGION_RESULT:"))
    size = metadata["size"]
    if type(size) is not int or not 0 < size <= 1024 * 1024:
        raise ValueError("Invalid bootstrap results size")
    data = bytearray()
    # Action Run Command only retains the last 4 KiB of output.
    for offset in range(0, size, 1800):
        chunk = vm_message(azure, group, vm_name,
            "python3 - <<'PY'\nimport base64\n"
            "with open('" + VM_WORK + "/results.tar.gz','rb') as f:\n"
            " f.seek(" + str(offset) + ")\n print('MULTI_REGION_CHUNK:'+base64.b64encode(f.read(1800)).decode())\nPY",
            "MULTI_REGION_CHUNK:")
        data.extend(base64.b64decode(chunk, validate=True))
    if len(data) != size or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise ValueError("VM results checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if (len(members) != len(REMOTE_ARTIFACTS)
                or {member.name for member in members} != set(REMOTE_ARTIFACTS)
                or any(not member.isfile() or member.size > 2 * 1024 * 1024 for member in members)):
            raise ValueError("Unexpected files in VM results")
        for member in members:
            provision.write_json(work / "artifacts" / member.name,
                                 json.load(archive.extractfile(member)))


def cleanup(azure, group, repository):
    if owned_group(azure, group, repository) is None:
        print("Owned CI resource group is already absent.")
        return
    deployment = group + "-workspace"
    existing = azure.run("deployment", "sub", "show", "--name", deployment, missing=True)
    if existing is not None:
        parameters = existing.get("properties", {}).get("parameters", {})
        if parameters.get("resourceGroup", {}).get("value") != group:
            raise ValueError("Subscription deployment does not target the owned CI resource group")
        if existing["properties"].get("provisioningState") not in ("Succeeded", "Failed", "Canceled"):
            azure.run("deployment", "sub", "cancel", "--name", deployment)
            azure.run("deployment", "sub", "wait", "--name", deployment, "--custom",
                      "properties.provisioningState=='Canceled' || properties.provisioningState=='Failed' || properties.provisioningState=='Succeeded'",
                      "--timeout", "1800")
    # Stop cloud-init before canceling its ARM deployment, or it could create more resources.
    for name in owned_vms(azure, group):
        azure.run("vm", "deallocate", "--resource-group", group, "--name", name)
    for deployment in azure.run("deployment", "group", "list", "--resource-group", group):
        if deployment["properties"].get("provisioningState") not in ("Succeeded", "Failed", "Canceled"):
            azure.run("deployment", "group", "cancel", "--resource-group", group, "--name", deployment["name"])
            azure.run("deployment", "group", "wait", "--resource-group", group, "--name", deployment["name"],
                      "--custom", "properties.provisioningState=='Canceled' || properties.provisioningState=='Failed' || properties.provisioningState=='Succeeded'",
                      "--timeout", "1800")
    remove_runtime(azure, azure.subscription, group)
    # Blocking deletion: do not report cleanup complete while billable resources remain.
    azure.run("group", "delete", "--name", group, "--yes")
    if azure.run("group", "exists", "--name", group):
        raise ValueError("Resource group deletion is not complete")
    print("Deleted owned four-region CI resource group: " + group)


def validate_jobs(azure, work, repository, run_id, attempt, timeout=6900):
    group = group_name(owner_tags(repository, run_id, attempt))
    if owned_group(azure, group, repository) is None:
        raise ValueError("Owned CI resource group is missing")
    vms = owned_vms(azure, group)
    if len(vms) != 1:
        raise ValueError("Expected exactly one Workspace VM for job validation")
    vm_name = vms[0]
    vm_message(azure, group, vm_name,
        "set -eu\n"
        "test \"$(cat " + VM_WORK + "/status)\" = succeeded\n"
        "test ! -e " + VM_WORK + "/artifacts/jobs.json\n"
        "systemd-run --unit=cyclecloud-multi-region-ci-jobs --property=RuntimeMaxSec=6600 "
        "--property=UMask=0077 /usr/bin/python3 "
        + VM_WORK + "/source/ci/slurm_jobs.py " + VM_WORK + "/source/ci-settings.json\n"
        "echo MULTI_REGION_JOBS_START:started", "MULTI_REGION_JOBS_START:")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = vm_message(azure, group, vm_name,
            "python3 - <<'PY'\nimport json,pathlib,subprocess\n"
            "p=pathlib.Path('" + VM_WORK + "/artifacts/jobs.json')\n"
            "if p.exists():\n"
            " print('MULTI_REGION_JOBS_RESULT:'+json.dumps(json.loads(p.read_text()),separators=(',',':')))\n"
            "else:\n"
            " state=subprocess.run(['systemctl','is-active','cyclecloud-multi-region-ci-jobs'],capture_output=True,text=True).stdout.strip()\n"
            " print('MULTI_REGION_JOBS_RESULT:'+json.dumps({'status':'running' if state in ('active','activating') else 'failed','error':'VM job runner stopped without a report'}))\n"
            "PY", "MULTI_REGION_JOBS_RESULT:")
        report = json.loads(result)
        if report.get("status") == "running":
            time.sleep(30)
            continue
        provision.write_json(work / "artifacts/jobs.json", report)
        if (report.get("status") != "succeeded" or report.get("user") != "hpcadmin"
                or report.get("data_transfer_tested") is not True):
            raise ValueError("Regional job execution/data movement validation failed; inspect jobs.json")
        jobs = report.get("jobs", [])
        if (len(jobs) != 8 or len({job["region"] for job in jobs}) != 4
                or any(job.get("state") != "COMPLETED" or job.get("input_verified") is not True
                       or job.get("output_verified") is not True for job in jobs)):
            raise ValueError("Incomplete cross-region workload validation report")
        return
    raise ValueError("Timed out waiting for VM-side regional Slurm job validation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "workspace", "bootstrap", "jobs", "vm-install", "cleanup"])
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--resource-group")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--slurm-version", type=configure.release)
    args = parser.parse_args(argv)
    try:
        if args.operation == "vm-install":
            if not args.settings or not args.slurm_version:
                raise ValueError("--settings and --slurm-version are required for VM installation")
            install_on_vm(args.settings, args.slurm_version)
            return 0
        if args.operation == "validate":
            config = validate_config(json.loads(os.environ["FOUR_REGION_CONFIG"]))
            uuid.UUID(os.environ["AZURE_SUBSCRIPTION_ID"])
            uuid.UUID(os.environ["HSM_PRINCIPAL_ID"])
            print("Validated four distinct regions: " + ", ".join(config["regions"]))
            return 0
        azure = (PollingAzure() if args.operation in ("bootstrap", "jobs")
                 and os.environ.get("GITHUB_ACTIONS") == "true" else provision.Azure())
        azure.subscription = str(uuid.UUID(os.environ["AZURE_SUBSCRIPTION_ID"]))
        if isinstance(azure, PollingAzure):
            azure.refresh_login()
        if args.operation == "cleanup":
            if not args.resource_group:
                raise ValueError("--resource-group is required for cleanup")
            cleanup(azure, args.resource_group, os.environ["GITHUB_REPOSITORY"])
        else:
            if not args.work_dir or (args.operation == "workspace" and not args.upstream):
                raise ValueError("--work-dir is required; workspace also requires --upstream")
            config = validate_config(json.loads(os.environ["FOUR_REGION_CONFIG"]))
            context = (args.work_dir, os.environ["GITHUB_REPOSITORY"],
                       os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
            if args.operation == "workspace":
                deploy_workspace(azure, config, args.upstream, *context)
            elif args.operation == "jobs":
                validate_jobs(azure, *context)
            else:
                wait_for_bootstrap(azure, *context)
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print("CI scenario failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
