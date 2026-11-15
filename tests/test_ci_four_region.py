# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

import copy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
from types import SimpleNamespace

import pytest

from tests.test_project_install import ROOT, load_script
from tests.test_provision import FakeAzure, arguments
from tests.test_configure import TEMPLATE


ASSIGNMENTS = '''
if [[ -z "${!var:-}" ]]; then missing+=("$var"); fi
eval "$var_name=\\"$sku_value\\""
eval "$var_name=\\"$sku_value\\""
eval "$var_name=\\"$input\\""
'''


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = load_script("four_region", directory="ci")
    monkeypatch.setattr(module, "HELPER_SHA256", hashlib.sha256(ASSIGNMENTS.encode()).hexdigest())
    source = tmp_path / "source"
    for name in (
        "project.ini", "roles/cyclecloud-multiregion-amlfs-runtime.json", "ci/four_region.py",
        "scripts/configure.py", "scripts/provision.py", "ci/bootstrap.sh",
        "ci/slurm_jobs.py", "ci/scheduler_jobs.py",
        "specs/default/cluster-init/scripts/20-install-cyclecloud-multiregion-amlfs.sh",
    ):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (source / "blobs").mkdir()
    (source / "blobs/cyclecloud-multiregion-amlfs.tar.gz").write_bytes(b"synthetic-payload")
    (source / "blobs/cyclecloud-multiregion-amlfs.tar.gz.sha256").write_text("synthetic-checksum")
    monkeypatch.setattr(module, "ROOT", source)
    return module


class ScenarioAzure:
    def __init__(self):
        self.calls = []
        self.group = None
        self.deployment = None
        self.regional_deployments = []
        self.subscription = "00000000-0000-0000-0000-000000000001"
        self.resources = {}
        self.vms = [{"id": "/subscriptions/" + self.subscription
                     + "/resourceGroups/cyclecloud-multi-region-ci-123-1/providers/Microsoft.Compute/virtualMachines/example-manager"}]
        self.workspace_outputs = {}

    def rest(self, resource_id, api, method="GET", body=None, missing=False):
        self.calls.append((method, resource_id))
        if method == "PUT":
            self.resources[resource_id] = body
            return body
        if method == "DELETE":
            self.resources.pop(resource_id)
            return {}
        return self.resources.get(resource_id)

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("group", "show"):
            return self.group
        if args[:2] == ("group", "create"):
            tags = dict(item.split("=", 1) for item in args[args.index("--tags") + 1:])
            self.group = {"tags": tags}
            return self.group
        if args[:3] == ("deployment", "sub", "show"):
            return self.deployment
        if args[:3] == ("deployment", "group", "list"):
            return self.regional_deployments
        if args[:3] == ("deployment", "group", "show"):
            return {"properties": {"outputs": self.workspace_outputs}}
        if args[:2] == ("group", "delete"):
            self.group = None
            return {}
        if args[:2] == ("group", "exists"):
            return self.group is not None
        if args[:2] == ("identity", "show"):
            return {"principalId": "00000000-0000-0000-0000-000000000002"}
        if args[:2] == ("vm", "list"):
            return self.vms
        if args[:3] == ("vm", "run-command", "invoke"):
            return {"value": [{"message": "CCWS_CI_READY"}]}
        return {"properties": {"provisioningState": "Succeeded"}}


@pytest.fixture
def polling_azure(scenario, monkeypatch):
    clock, calls, requests, logins = [0], [], [], []
    account = {
        "id": "00000000-0000-0000-0000-000000000001",
        "tenantId": "00000000-0000-0000-0000-000000000002",
        "user": {"type": "servicePrincipal", "name": "00000000-0000-0000-0000-000000000003"},
    }
    response = SimpleNamespace(token=oidc_assertion(300), returncode=0, stderr="", exception=None)
    monkeypatch.setattr(scenario.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scenario.time, "time", lambda: clock[0])
    monkeypatch.setattr(scenario.provision.Azure, "__init__", lambda self: None)

    def run(self, *args, **kwargs):
        calls.append(args)
        return account if args == ("account", "show") else {}

    def opener(handler):
        assert isinstance(handler, scenario.NoOidcRedirect)

        def open_request(request, timeout):
            requests.append(request)
            assert timeout == 30
            return io.BytesIO(json.dumps({"value": response.token}).encode())

        return SimpleNamespace(open=open_request)

    def login(command, **kwargs):
        logins.append(command)
        assert kwargs["capture_output"] and kwargs["timeout"] == 120
        if response.exception:
            raise response.exception
        return response

    monkeypatch.setattr(scenario.provision.Azure, "run", run)
    monkeypatch.setattr(scenario.request, "build_opener", opener)
    monkeypatch.setattr(scenario.subprocess, "run", login)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL",
                       "https://pipelines.actions.githubusercontent.com/token?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-secret")
    azure = scenario.PollingAzure()
    azure.subscription = account["id"]
    return SimpleNamespace(azure=azure, clock=clock, calls=calls, requests=requests,
                           logins=logins, account=account, response=response)


def oidc_assertion(expires):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def test_long_polling_renews_same_identity_and_subscription(scenario, polling_azure, capsys):
    state = polling_azure
    state.azure.refresh_login()
    assert state.azure.refresh_after == 180
    state.azure.run("group", "show")
    assert len(state.requests) == 1
    state.clock[0] = 180
    state.response.token = oidc_assertion(480)
    state.azure.run("vm", "list")
    assert state.requests[0].get_header("Authorization") == "Bearer request-secret"
    assert scenario.parse.parse_qs(scenario.parse.urlsplit(state.requests[0].full_url).query) == {
        "api-version": ["2.0"], "audience": ["api://AzureADTokenExchange"],
    }
    login = state.logins[-1]
    assert login[login.index("--username") + 1] == state.account["user"]["name"]
    assert login[login.index("--tenant") + 1] == state.account["tenantId"]
    assert login[login.index("--federated-token") + 1] == state.response.token
    assert state.calls[-2:] == [("account", "set", "--subscription", state.azure.subscription),
                               ("vm", "list")]
    assert "::add-mask::" + state.response.token in capsys.readouterr().out
    state.azure.run("vm", "list")
    assert len(state.logins) == 2


@pytest.mark.parametrize("url", [
    "https://unrelated.example/token",
    "http://pipelines.actions.githubusercontent.com/token",
    "https://pipelines.actions.githubusercontent.com.unrelated.example/token",
    "https://user@pipelines.actions.githubusercontent.com/token",
    "https://pipelines.actions.githubusercontent.com:8443/token",
])
def test_federation_never_sends_credentials_to_untrusted_endpoint(polling_azure, monkeypatch, url):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", url)
    with pytest.raises(ValueError, match="endpoint"):
        polling_azure.azure.refresh_login()
    assert not polling_azure.requests and not polling_azure.logins


def test_federation_refuses_redirects(scenario):
    with pytest.raises(ValueError, match="redirect"):
        scenario.NoOidcRedirect().redirect_request(None, None, 302, "", {}, "https://unrelated.example")


@pytest.mark.parametrize("token", [None, "invalid", "header.payload.signature\nuntrusted"])
def test_federation_rejects_malformed_assertion(polling_azure, token):
    polling_azure.response.token = token
    with pytest.raises(ValueError, match="assertion"):
        polling_azure.azure.refresh_login()
    assert not polling_azure.logins


@pytest.mark.parametrize("expires", [None, True, "300", 0, 120])
def test_federation_rejects_missing_or_insufficient_assertion_lifetime(polling_azure, expires):
    polling_azure.response.token = oidc_assertion(expires)
    with pytest.raises(ValueError, match="expires"):
        polling_azure.azure.refresh_login()
    assert not polling_azure.logins


@pytest.mark.parametrize("fault", ["subscription", "identity", "login", "timeout"])
def test_federation_failure_blocks_pending_command_without_leaking_assertion(polling_azure, fault):
    state = polling_azure
    state.clock[0] = 1
    if fault == "subscription":
        state.account["id"] = "another-subscription"
    elif fault == "identity":
        state.account["user"]["type"] = "user"
    elif fault == "login":
        state.response.returncode = 1
        state.response.stderr = "Rejected " + state.response.token
    else:
        state.response.exception = subprocess.TimeoutExpired(["az", state.response.token], 120)
    with pytest.raises(ValueError) as error:
        state.azure.run("vm", "list")
    assert state.response.token not in str(error.value)
    assert ("vm", "list") not in state.calls


@pytest.mark.parametrize("github", ["true", "false"])
def test_federated_polling_is_validated_upfront_only_on_runner(scenario, tmp_path, monkeypatch, github):
    events = []

    class RunnerAzure(ScenarioAzure):
        def refresh_login(self):
            events.append("refresh")

    monkeypatch.setattr(scenario, "PollingAzure", RunnerAzure)
    monkeypatch.setattr(scenario.provision, "Azure", ScenarioAzure)
    monkeypatch.setattr(scenario, "wait_for_bootstrap", lambda *args: events.append("bootstrap"))
    for key, value in {
        "GITHUB_ACTIONS": github, "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "FOUR_REGION_CONFIG": json.dumps({"regions": ["regiona", "regionb", "regionc", "regiond"]}),
    }.items():
        monkeypatch.setenv(key, value)
    assert scenario.main(["bootstrap", "--work-dir", str(tmp_path)]) == 0
    assert events == (["refresh", "bootstrap"] if github == "true" else ["bootstrap"])


@pytest.mark.parametrize("value", [
    {}, [], {"regions": ["regiona"]}, {"regions": ["regiona"] * 4},
    {"regions": ["regiona", "regionb", {}, "regiond"]},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "vm_size": "$(unsafe)"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "parent_cidr": "10.0.0.0/24"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "address_pool": "10.0.0.0/16"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "admin_username": "../user"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "admin_username": "root"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "cluster_name": "$(unsafe)"},
    {"regions": ["regiona", "regionb", "regionc", "regiond"], "workspace_ref": "2026.10.24"},
])
def test_rejects_non_four_region_and_unsafe_upstream_inputs(scenario, value):
    with pytest.raises(ValueError):
        scenario.validate_config(value)


def test_validation_rejects_admin_not_used_by_required_jobs(scenario, monkeypatch, capsys):
    for key, value in {
        "FOUR_REGION_CONFIG": json.dumps({
            "regions": ["regiona", "regionb", "regionc", "regiond"],
            "admin_username": "example-user",
        }),
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "HSM_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000002",
    }.items():
        monkeypatch.setenv(key, value)
    assert scenario.main(["validate"]) == 1
    assert "admin_username must be hpcadmin" in capsys.readouterr().err


def test_helper_assignments_are_hardened_and_changes_fail_closed(scenario, monkeypatch):
    hardened = scenario.prepare_helper(ASSIGNMENTS)
    assert "eval " not in hardened
    assert "${!" not in hardened
    assert hardened.count("printf -v") == 3
    assert "declare -n required_value" in hardened
    with pytest.raises(ValueError, match="changed"):
        scenario.prepare_helper(ASSIGNMENTS.replace('eval "$var_name=\\"$input\\""', "changed"))
    residual = ASSIGNMENTS + "\neval another\n"
    monkeypatch.setattr(scenario, "HELPER_SHA256", hashlib.sha256(residual.encode()).hexdigest())
    with pytest.raises(ValueError, match="dynamic evaluation"):
        scenario.prepare_helper(residual)


def helper_parameters(config, key="ssh-rsa AAAA", group="cyclecloud-multi-region-ci-123-1"):
    return {"parameters": {
        "adminUsername": {"value": config["admin_username"]},
        "clusterName": {"value": config["cluster_name"]},
        "resourceGroup": {"value": group},
        "key": {"value": {"type": "entered", "value": key}},
        "schedFilesystem": {"value": {"type": "nfs-new", "nfsCapacityInGb": 30}},
        "sharedFilesystem": {"value": {"type": "nfs-new", "nfsCapacityInGb": 128}},
        "slurmSettings": {"value": {"startCluster": False, "version": "26.05.4"}},
        "tags": {"value": {}},
    }}


def test_workspace_is_stopped_and_only_safe_artifacts_are_written(scenario, tmp_path, monkeypatch):
    azure = ScenarioAzure()
    upstream = tmp_path / "upstream"
    source = upstream / scenario.HELPER_PATH
    source.parent.mkdir(parents=True)
    source.write_text(ASSIGNMENTS)
    original = source.read_bytes()
    work = tmp_path / "work"
    env = {"AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
           "CCWS_ADMIN_PASSWORD": "synthetic-ci-value", "SSH_PUBLIC_KEY": 'ssh-rsa AAAA fixture "comment"',
           "HSM_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000003"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(scenario.subprocess, "check_output", lambda command, **kwargs:
                        (scenario.UPSTREAM_COMMIT if command[2] == str(upstream) else scenario.WORKSPACE_COMMIT) + "\n")
    commands = []

    def run(*command):
        commands.append(list(map(str, command)))
        output = Path(command[command.index("--output-file") + 1])
        key = Path(command[command.index("--ssh-public-key-file") + 1]).read_text().strip()
        assert key == "ssh-rsa AAAA"
        output.write_text(json.dumps(helper_parameters(config, key)))
        workspace = Path(command[command.index("--workspace-dir") + 1])
        fake_workspace(workspace)

    monkeypatch.setattr(scenario, "run_local", run)
    config = scenario.validate_config({"regions": ["regiona", "regionb", "regionc", "regiond"],
                                       "admin_username": "hpcadmin", "cluster_name": "example-cluster"})
    scenario.deploy_workspace(azure, config, upstream, work, "owner/repo", "123", "1")
    assert source.read_bytes() == original
    assert "--slurm-no-start" in commands[0] and "--silent" in commands[0]
    assert "--deploy" not in commands[0] and "--data-filesystem" not in commands[0]
    assert commands[0][commands[0].index("--workspace-ref") + 1] == "2026.10.23"
    assert commands[0][commands[0].index("--shared-filesystem") + 1] == "nfs"
    assert commands[0][commands[0].index("--shared-nfs-size") + 1] == "128"
    assert commands[0][commands[0].index("--admin-username") + 1] == "hpcadmin"
    assert commands[0][commands[0].index("--cluster-name") + 1] == "example-cluster"
    assert commands[0][commands[0].index("--workspace-commit") + 1] == scenario.WORKSPACE_COMMIT
    params = json.loads((work / "private/workspace.parameters.json").read_text())
    assert params["parameters"]["tags"]["value"]["Resource group"]["repository"] == "owner/repo"
    assert params["parameters"]["key"] == {"value": {"type": "entered", "value": "ssh-rsa AAAA"}}
    assert params["parameters"]["schedFilesystem"]["value"] == {"type": "nfs-new", "nfsCapacityInGb": 30}
    assert params["parameters"]["sharedFilesystem"]["value"] == {"type": "nfs-new", "nfsCapacityInGb": 128}
    assert "adminSshPublicKey" not in params["parameters"]
    assert not (work / "private/secrets.parameters.json").exists()
    for artifact in (work / "artifacts").iterdir():
        assert env["CCWS_ADMIN_PASSWORD"] not in artifact.read_text()
        assert env["SSH_PUBLIC_KEY"] not in artifact.read_text()
    assert not any(env["CCWS_ADMIN_PASSWORD"] in str(command) for command in azure.calls)
    patched = (work / "private/workspace/bicep/install.sh").read_text()
    assert "vm-install" in patched
    assert not any(call[:3] == ("vm", "run-command", "invoke") for call in azure.calls)


def test_current_helper_parameters_are_accepted_without_rewriting(scenario):
    config = scenario.validate_config({"regions": ["regiona", "regionb", "regionc", "regiond"]})
    parameters = helper_parameters(config)
    original = copy.deepcopy(parameters)
    scenario.validate_parameters(parameters, config, "ssh-rsa AAAA", "cyclecloud-multi-region-ci-123-1")
    assert parameters == original


@pytest.mark.parametrize("change", [
    {"key": {"value": {"type": "entered", "value": "unexpected"}}},
    {"sharedFilesystem": {"value": {"type": "nfs-new"}}},
    {"sharedFilesystem": {"value": {"type": "anf-new", "anfCapacityInTiB": 2}}},
    {"schedFilesystem": {"value": {"type": "nfs-new", "nfsCapacityInGb": 128}}},
    {"adminSshPublicKey": {"value": "ssh-rsa AAAA"}},
    {"clusterName": {"value": "another-cluster"}},
    {"slurmSettings": {"value": {"startCluster": True}}},
])
def test_parameter_validation_rejects_contract_and_safety_drift(scenario, change):
    config = scenario.validate_config({"regions": ["regiona", "regionb", "regionc", "regiond"]})
    parameters = helper_parameters(config)
    parameters["parameters"].update(change)
    original = copy.deepcopy(parameters)
    with pytest.raises(ValueError, match="contract changed"):
        scenario.validate_parameters(parameters, config, "ssh-rsa AAAA", "cyclecloud-multi-region-ci-123-1")
    assert parameters == original


def test_upstream_commit_mismatch_stops_before_creating_resources(scenario, tmp_path, monkeypatch):
    azure = ScenarioAzure()
    upstream = tmp_path / "upstream"
    source = upstream / scenario.HELPER_PATH
    source.parent.mkdir(parents=True)
    source.write_text(ASSIGNMENTS)
    for key, value in {"AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
                       "CCWS_ADMIN_PASSWORD": "synthetic", "SSH_PUBLIC_KEY": "ssh-rsa AAAA fixture"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(scenario.subprocess, "check_output", lambda *args, **kwargs: "different-commit\n")
    with pytest.raises(ValueError, match="reviewed upstream commit"):
        scenario.deploy_workspace(azure, scenario.validate_config({"regions": ["a1", "b1", "c1", "d1"]}),
                                  upstream, tmp_path / "work", "owner/repo", "123", "1")
    assert not any(command[:2] == ("group", "create") for command in azure.calls)


def test_workspace_drift_stops_before_template_deployment(scenario, tmp_path, monkeypatch):
    azure = ScenarioAzure()
    upstream = tmp_path / "upstream"
    source = upstream / scenario.HELPER_PATH
    source.parent.mkdir(parents=True)
    source.write_text(ASSIGNMENTS)
    for key, value in {"AZURE_SUBSCRIPTION_ID": azure.subscription,
                       "CCWS_ADMIN_PASSWORD": "synthetic", "SSH_PUBLIC_KEY": "ssh-rsa AAAA",
                       "HSM_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000003"}.items():
        monkeypatch.setenv(key, value)
    revisions = iter([scenario.UPSTREAM_COMMIT, "changed-workspace"])
    monkeypatch.setattr(scenario.subprocess, "check_output", lambda *args, **kwargs: next(revisions))
    monkeypatch.setattr(scenario, "run_local", lambda *args: None)
    with pytest.raises(ValueError, match="Workspace commit changed"):
        scenario.deploy_workspace(azure, scenario.validate_config({"regions": ["a1", "b1", "c1", "d1"]}),
                                  upstream, tmp_path / "work", "owner/repo", "123", "1")
    assert not any(command[:3] == ("deployment", "sub", "create") for command in azure.calls)


def test_cleanup_refuses_unrelated_or_untagged_groups(scenario):
    azure = ScenarioAzure()
    with pytest.raises(ValueError, match="naming convention"):
        scenario.cleanup(azure, "production", "owner/repo")
    azure.group = {"tags": {}}
    with pytest.raises(ValueError, match="ownership"):
        scenario.cleanup(azure, "cyclecloud-multi-region-ci-123-1", "owner/repo")
    assert not any(command[:2] == ("group", "delete") for command in azure.calls)


def test_cleanup_handles_partial_workspace_and_active_regional_deployments(scenario):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    azure.regional_deployments = [{"name": "multi-region-deployment", "properties": {"provisioningState": "Running"}}]
    scenario.cleanup(azure, "cyclecloud-multi-region-ci-123-1", "owner/repo")
    cancel = next(i for i, call in enumerate(azure.calls) if call[:3] == ("deployment", "group", "cancel"))
    delete = next(i for i, call in enumerate(azure.calls) if call[:2] == ("group", "delete"))
    stop = next(i for i, call in enumerate(azure.calls) if call[:2] == ("vm", "deallocate"))
    assert stop < cancel < delete
    assert azure.calls[stop][-1] == "example-manager"
    assert azure.group is None


def test_cleanup_checks_subscription_deployment_target(scenario):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    azure.deployment = {"properties": {"parameters": {"resourceGroup": {"value": "another-group"}},
                                       "provisioningState": "Running"}}
    with pytest.raises(ValueError, match="does not target"):
        scenario.cleanup(azure, "cyclecloud-multi-region-ci-123-1", "owner/repo")
    assert not any(call[:2] == ("group", "delete") for call in azure.calls)


def test_vm_discovery_rejects_foreign_resources_and_ambiguous_bootstrap(scenario, tmp_path):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    azure.vms.append(dict(azure.vms[0], id=azure.vms[0]["id"] + "-second"))
    with pytest.raises(ValueError, match="exactly one"):
        scenario.wait_for_bootstrap(azure, tmp_path, "owner/repo", "123", "1")
    azure.vms[1]["id"] = azure.vms[1]["id"].replace("cyclecloud-multi-region-ci-123-1", "unrelated")
    with pytest.raises(ValueError, match="outside the owned"):
        scenario.cleanup(azure, "cyclecloud-multi-region-ci-123-1", "owner/repo")
    assert not any(call[:2] in (("vm", "deallocate"), ("group", "delete")) for call in azure.calls)


@pytest.mark.parametrize("fault", [
    None, "foreign-vnet", "storage-wait", "hsm-readiness", "hsm-timeout", "deployment-failure",
])
def test_regional_deployment_uses_scoped_vnet_output(scenario, tmp_path, monkeypatch, fault):
    azure = ScenarioAzure()
    group = "cyclecloud-multi-region-ci-123-1"
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    config = scenario.validate_config({"regions": ["regiona", "regionb", "regionc", "regiond"]})
    azure.workspace_outputs = {
        "resourceGroup": {"value": group}, "subscriptionId": {"value": azure.subscription},
        "clusterName": {"value": config["cluster_name"]},
        "vnet": {"value": {"id": "/subscriptions/" + azure.subscription + "/resourceGroups/"
                          + ("unrelated" if fault == "foreign-vnet" else group)
                          + "/providers/Microsoft.Network/virtualNetworks/example-network"}},
    }
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", azure.subscription)
    monkeypatch.setenv("HSM_PRINCIPAL_ID", "00000000-0000-0000-0000-000000000003")
    plan = {"id": "synthetic-plan"}
    built, applied = [], []
    clock = [0]
    monkeypatch.setattr(scenario.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scenario.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def build(args, client):
        built.append(args)
        assert args.parent_vnet == "example-network"
        assert args.resource_group == group
        assert args.regions == config["regions"][1:]
        return plan

    def apply(value, client, path, yes):
        assert value is plan
        assert path == tmp_path / "artifacts/infrastructure.plan.json"
        assert yes is True
        applied.append(value)
        if fault == "deployment-failure":
            raise ValueError("QuotaExceeded")
        if fault == "hsm-timeout" or fault == "hsm-readiness" and len(applied) == 1:
            raise ValueError("Storage Account " + scenario.storage_name("owner/repo", group)
                             + " is not found or is inaccessible.")

    monkeypatch.setattr(scenario.provision, "build_plan", build)
    monkeypatch.setattr(scenario.provision, "apply_plan", apply)
    monkeypatch.setattr(scenario, "verify_infrastructure", lambda *args: {})
    (tmp_path / "artifacts").mkdir()
    storage_id = ("/subscriptions/" + azure.subscription + "/resourceGroups/" + group
                  + "/providers/Microsoft.Storage/storageAccounts/" + scenario.storage_name("owner/repo", group))
    if fault == "foreign-vnet":
        with pytest.raises(ValueError, match="outside the owned"):
            scenario.deploy_regions(azure, config, tmp_path, "owner/repo", "123", "1")
        assert not azure.resources
    elif fault == "storage-wait":
        run = azure.run

        def fail_wait(*args, **kwargs):
            if args[:2] == ("resource", "wait"):
                raise ValueError("Storage creation failed")
            return run(*args, **kwargs)

        monkeypatch.setattr(azure, "run", fail_wait)
        with pytest.raises(ValueError, match="Storage creation failed"):
            scenario.deploy_regions(azure, config, tmp_path, "owner/repo", "123", "1")
        assert set(azure.resources) == {storage_id}
    elif fault in ("hsm-timeout", "deployment-failure"):
        with pytest.raises(ValueError, match="inaccessible" if fault == "hsm-timeout" else "QuotaExceeded"):
            scenario.deploy_regions(azure, config, tmp_path, "owner/repo", "123", "1")
        assert len(built) == 1
        assert len(applied) == (31 if fault == "hsm-timeout" else 1)
        assert clock[0] == (1800 if fault == "hsm-timeout" else 0)
    else:
        assert scenario.deploy_regions(azure, config, tmp_path, "owner/repo", "123", "1")["id"] == "synthetic-plan"
        assert len(built) == 1
        assert len(applied) == (2 if fault == "hsm-readiness" else 1)
        assert clock[0] == (60 if fault == "hsm-readiness" else 0)
        assert not any(call[0] == "storage" for call in azure.calls)
        assert azure.resources[storage_id]["properties"] == {
            "allowBlobPublicAccess": False, "allowSharedKeyAccess": True,
            "publicNetworkAccess": "Disabled", "minimumTlsVersion": "TLS1_2",
            "supportsHttpsTrafficOnly": True,
        }
        wait = ("resource", "wait", "--ids", storage_id, "--api-version", scenario.provision.STORAGE_API,
                "--created", "--timeout", "600")
        for container in ("data", "logs"):
            container_id = storage_id + "/blobServices/default/containers/" + container
            assert azure.resources[container_id] == {"properties": {"publicAccess": "None"}}
            assert azure.calls.index(wait) < azure.calls.index(("PUT", container_id))
    assert ("deployment", "group", "show", "--resource-group", group,
            "--name", group + "-workspace-resources") in azure.calls


def test_infrastructure_checks_require_four_ready_filesystems_and_connected_peerings(scenario, tmp_path):
    azure = FakeAzure(scenario.provision)
    plan = scenario.provision.build_plan(arguments(), azure)
    template = tmp_path / "template.json"
    scenario.provision.write_json(template, plan["template"])
    azure.run("deployment", "group", "create", "--template-file", str(template))
    for value in azure.resources.values():
        if value.get("type", "").endswith("/virtualNetworkPeerings"):
            value["properties"]["peeringState"] = "Connected"
    report = scenario.verify_infrastructure(plan, azure)
    assert len(report["regions"]) == 4 and report["data_transfer_tested"] is False
    incomplete = copy.deepcopy(plan)
    incomplete["regions"].pop()
    with pytest.raises(ValueError, match="exactly four"):
        scenario.verify_infrastructure(incomplete, azure)
    filesystem = azure.resources[plan["regions"][0]["filesystem_id"]]
    filesystem["properties"]["hsm"]["settings"]["loggingContainer"] = "wrong-container"
    with pytest.raises(ValueError, match="containers"):
        scenario.verify_infrastructure(plan, azure)


def fake_workspace(path, deployment="upstream-workspace"):
    bicep = path / "bicep"
    bicep.mkdir(parents=True)
    original = (
        b"#!/bin/bash\nset -e\n"
        + ("deployment_name='" + deployment + "'\n").encode()
        + b'printf "upstream finished\\n"\nexit 0\n'
    )
    (bicep / "install.sh").write_bytes(original)
    (bicep / "ccw.bicep").write_text("deployScript: loadTextContent('./install.sh')\n")
    (bicep / "mainTemplate.bicep").write_text(
        "module makeCCWresources 'ccw.bicep' = {\n"
        f"  name: '{deployment}'\n"
        "  scope: ccwResourceGroup\n"
        "}\n")
    return original


@pytest.mark.parametrize("final_newline", [True, False])
@pytest.mark.parametrize("upstream_name", ["upstream-workspace", "another.workspace-deployment"])
def test_tail_injection_preserves_upstream_and_embeds_only_local_payload(
        scenario, tmp_path, final_newline, upstream_name):
    original = fake_workspace(tmp_path, deployment=upstream_name)
    if not final_newline:
        original = original.rstrip(b"\n")
        (tmp_path / "bicep/install.sh").write_bytes(original)
    config = scenario.validate_config({"regions": ["regiona", "regionb", "regionc", "regiond"]})
    context = {"subscription": "00000000-0000-0000-0000-000000000001",
               "hsm_principal_id": "00000000-0000-0000-0000-000000000003",
               "repository": "owner/repo", "run_id": "123", "attempt": "1"}
    digest = scenario.inject_bootstrap(tmp_path, config, context)
    patched = (tmp_path / "bicep/install.sh").read_bytes()
    deployment = "cyclecloud-multi-region-ci-123-1-workspace-resources"
    expected_prefix = original[:original.rfind(b"exit 0")].replace(
        ("deployment_name='" + upstream_name + "'").encode(),
        ("deployment_name='" + deployment + "'").encode(),
    )
    assert patched.startswith(expected_prefix)
    assert patched.endswith(b"exit 0\n")
    assert patched.index(b"upstream finished") < patched.index(b"vm-install")
    assert ('--name "' + deployment + '"').encode() in patched
    assert "name: '" + deployment + "'" in (tmp_path / "bicep/mainTemplate.bicep").read_text()
    assert b"@WORKSPACE_DEPLOYMENT_NAME@" not in patched
    assert ("deployment_name='" + deployment + "'").encode() in patched
    assert upstream_name.encode() not in patched
    assert len(patched) <= 65535
    subprocess.run(["bash", "-n", str(tmp_path / "bicep/install.sh")], check=True)
    data = base64.b64decode((tmp_path / "bicep/multi-region-bundle.base64").read_text())
    assert hashlib.sha256(data).hexdigest() == digest
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        settings = json.load(archive.extractfile("ci-settings.json"))
        assert settings == dict(context, config=config)
        assert "CCWS_ADMIN_PASSWORD" not in settings
        assert set(archive.getnames()) == {
            "ci-settings.json", "project.ini", "ci/four_region.py",
            "ci/slurm_jobs.py", "ci/scheduler_jobs.py",
            "scripts/provision.py", "scripts/configure.py",
            "specs/default/cluster-init/scripts/20-install-cyclecloud-multiregion-amlfs.sh",
            "blobs/cyclecloud-multiregion-amlfs.tar.gz",
            "blobs/cyclecloud-multiregion-amlfs.tar.gz.sha256",
        }
        bundle = tmp_path / "unpacked"
        for member in archive.getmembers():
            target = bundle / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    environment = dict(
        os.environ, FOUR_REGION_CONFIG=json.dumps(config),
        AZURE_SUBSCRIPTION_ID=context["subscription"],
        HSM_PRINCIPAL_ID=context["hsm_principal_id"],
    )
    result = subprocess.run(
        [sys.executable, "-I", str(bundle / "ci/four_region.py"), "validate"],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Validated four distinct regions" in result.stdout
    with pytest.raises(ValueError, match="changed"):
        scenario.inject_bootstrap(tmp_path, config, context)


@pytest.mark.parametrize("assignment", [
    b"renamed_deployment='upstream-workspace'",
    b"deployment_name='another-deployment'",
    b"deployment_name='upstream-workspace'\ndeployment_name='upstream-workspace'",
])
def test_changed_bootstrap_deployment_name_is_rejected_before_patching(scenario, tmp_path, assignment):
    fake_workspace(tmp_path)
    install = tmp_path / "bicep/install.sh"
    original = install.read_bytes().replace(b"deployment_name='upstream-workspace'", assignment)
    install.write_bytes(original)
    with pytest.raises(ValueError, match="deployment name changed"):
        scenario.inject_bootstrap(tmp_path, {}, {
            "repository": "owner/repo", "run_id": "456", "attempt": "2",
        })
    assert install.read_bytes() == original
    assert not (tmp_path / "bicep/multi-region-bundle.base64").exists()


@pytest.mark.parametrize("ending", [
    b"exit 1\n",
    b'echo "exit 0"\n',
    b'exit 0\necho "after exit"\n',
])
def test_tail_injection_still_requires_final_standalone_exit(scenario, tmp_path, ending):
    original = fake_workspace(tmp_path).removesuffix(b"exit 0\n") + ending
    install = tmp_path / "bicep/install.sh"
    install.write_bytes(original)
    with pytest.raises(ValueError, match="must end with a standalone 'exit 0'"):
        scenario.inject_bootstrap(tmp_path, {}, {})
    assert install.read_bytes() == original
    assert not (tmp_path / "bicep/multi-region-bundle.base64").exists()


@pytest.mark.parametrize("module", [
    "module renamed 'ccw.bicep' = {\n  name: 'upstream-workspace'\n}\n",
    "module makeCCWresources 'ccw.bicep' = {\n  name: deploymentName\n}\n",
    ("module makeCCWresources 'ccw.bicep' = {\n  name: 'upstream-workspace'\n}\n") * 2,
])
def test_changed_deployment_module_is_rejected_before_patching(scenario, tmp_path, module):
    original = fake_workspace(tmp_path)
    template = tmp_path / "bicep/mainTemplate.bicep"
    template.write_text(module)
    with pytest.raises(ValueError, match="deployment module changed"):
        scenario.inject_bootstrap(tmp_path, {}, {
            "repository": "owner/repo", "run_id": "456", "attempt": "2",
        })
    assert template.read_text() == module
    assert (tmp_path / "bicep/install.sh").read_bytes() == original
    assert not (tmp_path / "bicep/multi-region-bundle.base64").exists()


def test_vm_installer_provisions_before_configuring_and_exports_allowlisted_results(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(scenario, "VM_WORK", str(tmp_path))
    monkeypatch.setattr(scenario.provision, "Azure", ScenarioAzure)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scenario.os, "chdir", lambda path: None)
    stages = []

    def provision(azure, config, work, *context):
        stages.append("provision")
        assert azure.calls[0] == ("login", "--identity")
        for name in scenario.REMOTE_ARTIFACTS[:3]:
            (work / "artifacts" / name).write_text("{}")
        return {"id": "plan"}

    def configure(config, plan, work, version):
        stages.append("configure")
        assert plan["id"] == "plan" and version == "4.0.11"
        (work / "private").mkdir()
        (work / "private/secret.json").write_text("not-an-artifact")
        return {"cluster_imported": True}

    monkeypatch.setattr(scenario, "deploy_regions", provision)
    monkeypatch.setattr(scenario, "configure_workspace", configure)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "config": {"regions": ["regiona", "regionb", "regionc", "regiond"]},
        "subscription": "00000000-0000-0000-0000-000000000001",
        "hsm_principal_id": "00000000-0000-0000-0000-000000000003",
        "repository": "owner/repo", "run_id": "123", "attempt": "1",
    }))
    scenario.install_on_vm(settings, "4.0.11")
    assert stages == ["provision", "configure"]
    with tarfile.open(tmp_path / "results.tar.gz") as archive:
        assert set(archive.getnames()) == set(scenario.REMOTE_ARTIFACTS)


@pytest.mark.parametrize("status", ["succeeded", "failed", "incomplete"])
def test_required_job_phase_waits_on_vm_and_rejects_bad_results(scenario, tmp_path, monkeypatch, status):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    (tmp_path / "artifacts").mkdir()
    report = {
        "status": "failed" if status == "failed" else "succeeded", "user": "hpcadmin",
        "data_transfer_tested": True,
        "jobs": [{"region": "region" + letter, "job_id": str(index * 2 + attempt + 100),
                  "partition": "htc" if letter == "A" else "multi-region" + letter,
                  "state": "COMPLETED", "input_verified": True, "output_verified": True}
                 for index, letter in enumerate("ABCD") for attempt in range(2)],
    }
    if status == "incomplete":
        report["jobs"][0]["output_verified"] = False
    responses = iter(["started", json.dumps({"status": "running"}), json.dumps(report)])
    scripts = []

    def message(client, group, vm, script, prefix):
        assert group == "cyclecloud-multi-region-ci-123-1" and vm == "example-manager"
        scripts.append(script)
        return next(responses)

    monkeypatch.setattr(scenario, "vm_message", message)
    monkeypatch.setattr(scenario.time, "sleep", lambda seconds: None)
    if status == "succeeded":
        scenario.validate_jobs(azure, tmp_path, "owner/repo", "123", "1")
    else:
        with pytest.raises(ValueError, match="validation"):
            scenario.validate_jobs(azure, tmp_path, "owner/repo", "123", "1")
    assert json.loads((tmp_path / "artifacts/jobs.json").read_text()) == report
    assert "systemd-run --unit=cyclecloud-multi-region-ci-jobs" in scripts[0]
    assert "RuntimeMaxSec=6600" in scripts[0]
    assert "/source/ci/slurm_jobs.py" in scripts[0]
    assert "cyclecloud.pem" not in "".join(scripts)


def test_job_phase_refuses_unowned_group_and_times_out(scenario, tmp_path, monkeypatch):
    azure = ScenarioAzure()
    calls = []
    monkeypatch.setattr(scenario, "vm_message", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="missing"):
        scenario.validate_jobs(azure, tmp_path, "owner/repo", "123", "1")
    assert not calls
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    with pytest.raises(ValueError, match="Timed out"):
        scenario.validate_jobs(azure, tmp_path, "owner/repo", "123", "1", timeout=0)


@pytest.mark.parametrize("mismatch", [None, "clusterName", "adminUsername", "subscriptionId"])
def test_vm_configuration_preserves_workspace_and_imports_enabled_four_region_cluster(scenario, tmp_path, monkeypatch, mismatch):
    azure = FakeAzure(scenario.provision)
    plan = scenario.provision.build_plan(arguments(), azure)
    config = scenario.validate_config({"regions": [r["location"] for r in plan["regions"]],
                                       "cluster_name": "example-cluster", "admin_username": "hpcadmin"})
    home = tmp_path / "home"
    home.mkdir()
    (home / "slurm_template.txt").write_text(TEMPLATE)
    identity = "/subscriptions/" + plan["subscription"] + "/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/ccwLockerManagedIdentity"
    deployment = {
        "clusterName": {"value": config["cluster_name"]}, "resourceGroup": {"value": plan["resource_group"]},
        "adminUsername": {"value": config["admin_username"]}, "subscriptionId": {"value": plan["subscription"]},
        "slurmSettings": {"value": {"startCluster": False}}, "managedIdentityId": {"value": identity},
    }
    if mismatch:
        deployment[mismatch]["value"] = "unrelated"
    (home / "deployment.json").write_text(json.dumps(deployment))
    (tmp_path / "artifacts").mkdir()
    scenario.provision.write_json(tmp_path / "artifacts/infrastructure.plan.outputs.json", {
        "plan_id": plan["id"], "regions": [dict(region, mount_source="10.0.0.4@tcp:/lustrefs")
                                        for region in plan["regions"]],
    })
    def show_nodes(command, **kwargs):
        assert command[command.index("-c") + 1] == "example-cluster"
        assert command[command.index("-a") + 1] == "Name,Status"
        return json.dumps([{"Name": "scheduler", "Status": "Off", "Instance": {}}])

    monkeypatch.setattr(scenario.subprocess, "check_output", show_nodes)
    commands = []
    original = {"Credentials": "azure", "ManagedIdentity": None, "SubnetId": "rg/ccw-vnet/compute",
                "HTCClusterInitSpecs": {"workspace:default:2026.10.23": {"Order": 10000}},
                "additional_slurm_config": "SchedulerParameters=sched_interval=20\n",
                "configuration_slurm_accounting_password": "private-fixture"}

    def run(*command):
        commands.append(tuple(map(str, command)))
        if command[1] == "export_parameters":
            output = Path(command[-1])
            value = original if output.name == "parameters.before.json" else json.loads(
                (tmp_path / "private/cluster/parameters.json").read_text())
            output.write_text(json.dumps(value))

    monkeypatch.setattr(scenario, "run_local", run)
    if mismatch:
        with pytest.raises(ValueError, match="owned stopped scenario"):
            scenario.configure_workspace(config, plan, tmp_path, "4.0.11", home)
        assert not any(c[1] == "import_cluster" or c[1:3] == ("project", "upload") for c in commands)
        return
    report = scenario.configure_workspace(config, plan, tmp_path, "4.0.11", home)
    assert report["cluster_imported"] and report["extension_enabled"]
    assert not report["node_installation_tested"] and not report["slurm_started"]
    generated = tmp_path / "private/cluster"
    params = json.loads((generated / "parameters.json").read_text())
    assert params["configuration_slurm_accounting_password"] == "private-fixture"
    assert params["ManagedIdentity"] == identity and params["MultiRegionEnabled"] is True
    assert len(json.loads(params["MultiRegionConfigJson"])["REGIONS"]) == 4
    template = scenario.configure.parse_template((generated / "slurm-multi-region.txt").read_text())
    cluster = template.child("cluster Slurm")
    assert params["additional_slurm_config"] == "SchedulerParameters=sched_interval=20\nMinJobAge=7200"
    for letter in ("B", "C", "D"):
        assert cluster.child("nodearray multi-region" + letter).properties()["AdditionalClusterInitSpecs"] == "$HTCClusterInitSpecs"
    upload = next(i for i, c in enumerate(commands) if c[1:3] == ("project", "upload"))
    imported = next(i for i, c in enumerate(commands) if c[1] == "import_cluster")
    assert upload < imported
    assert commands[imported][2] == "example-cluster"
    assert all(c[2] == "example-cluster" for c in commands if c[1] == "export_parameters")
    assert scenario.workspace_home(config) == Path("/home/hpcadmin/example-cluster")
    assert not any("start_cluster" in c for c in commands)
    assert all("private-fixture" not in path.read_text() for path in (tmp_path / "artifacts").iterdir())


@pytest.mark.parametrize("value,expected", [
    (None, "MinJobAge=7200"),
    ("MinJobAge=300 # keep jobs\r\n", "MinJobAge=7200 # keep jobs\r\n"),
    ("MinJobAge=14400\nSchedulerParameters=x", "MinJobAge=14400\nSchedulerParameters=x"),
])
def test_ci_retention_preserves_existing_slurm_configuration(scenario, value, expected):
    assert scenario.ci_slurm_configuration(value) == expected


@pytest.mark.parametrize("value", [{}, "MinJobAge=invalid", "MinJobAge=300\nMinJobAge=600"])
def test_ci_retention_rejects_ambiguous_or_non_text_configuration(scenario, value):
    with pytest.raises(ValueError):
        scenario.ci_slurm_configuration(value)


@pytest.mark.parametrize("status", ["Ready", "Acquiring", "Preparing", "Deallocated", None])
def test_vm_configuration_refuses_running_or_unknown_cluster(scenario, tmp_path, monkeypatch, status):
    monkeypatch.setattr(scenario.subprocess, "check_output", lambda *a, **k: json.dumps([
        {"Name": "scheduler", "Status": status}]))
    with pytest.raises(ValueError, match="not stopped"):
        scenario.configure_workspace({"cluster_name": "example-cluster", "admin_username": "example-user"},
                                     {}, tmp_path, "4.0.11")


@pytest.mark.parametrize("state", ["failed", "waiting"])
def test_runner_rejects_failed_or_timed_out_bootstrap(scenario, tmp_path, monkeypatch, state):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    monkeypatch.setattr(scenario, "vm_message", lambda *args: state)
    with pytest.raises(ValueError, match="bootstrap"):
        scenario.wait_for_bootstrap(azure, tmp_path, "owner/repo", "123", "1", timeout=0)


def test_runner_transfers_bounded_checked_results(scenario, tmp_path, monkeypatch):
    azure = ScenarioAzure()
    azure.group = {"tags": scenario.owner_tags("owner/repo", "123", "1")}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in scenario.REMOTE_ARTIFACTS:
            data = json.dumps({"fixture": name}).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    data = buffer.getvalue()
    responses = iter(["succeeded", json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}),
                      base64.b64encode(data).decode()])
    monkeypatch.setattr(scenario, "vm_message", lambda *args: next(responses))
    (tmp_path / "artifacts").mkdir()
    scenario.wait_for_bootstrap(azure, tmp_path, "owner/repo", "123", "1")
    assert set(p.name for p in (tmp_path / "artifacts").iterdir()) == set(scenario.REMOTE_ARTIFACTS)


def test_runtime_roles_separate_filesystem_writes_from_subscription_polling_and_are_removed(scenario):
    azure = ScenarioAzure()
    group = "cyclecloud-multi-region-ci-123-1"
    scenario.grant_runtime(azure, azure.subscription, group)
    roles = [value["properties"] for key, value in azure.resources.items() if "/roleDefinitions/" in key]
    scoped = next(role for role in roles if role["roleName"].endswith("-runtime"))
    poll = next(role for role in roles if role["roleName"].endswith("-poll"))
    assert scoped["assignableScopes"][0].endswith("/resourceGroups/" + group)
    assert poll["permissions"][0]["actions"] == ["Microsoft.StorageCache/locations/ascOperations/read"]
    assert all("ascOperations" not in action for action in scoped["permissions"][0]["actions"])
    scenario.remove_runtime(azure, azure.subscription, group)
    assert not azure.resources
