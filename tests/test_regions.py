# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Named-region routing, stage serialization, and isolated job cleanup."""

import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("owned_paths")


@pytest.fixture
def regions(cfg, monkeypatch):
    values = {
        "research": {
            "resource_group": "rg-research", "filesystem": "research-fs",
            "location": "eastus", "mountpoint": "/data/home",
        },
        "accelerators": {
            "resource_group": "rg-gpu", "filesystem": "gpu-fs",
            "location": "westus3", "mountpoint": "/data/gpu",
        },
        "overflow": {
            "subscription_id": "other-subscription",
            "resource_group": "rg-batch", "filesystem": "batch-fs",
            "location": "northeurope", "mountpoint": "/data/batch",
        },
    }
    monkeypatch.setattr(cfg, "REGIONS", values, raising=False)
    monkeypatch.setattr(cfg, "HOME_REGION", "research", raising=False)
    monkeypatch.setattr(cfg, "PARTITION_REGIONS", {
        "hpc": "research", "gpu": "accelerators", "batch": "overflow",
    }, raising=False)
    return values


@pytest.fixture
def operations(mod, monkeypatch):
    archive, hydrate = MagicMock(), MagicMock()
    monkeypatch.setattr(mod, "_archive", archive)
    monkeypatch.setattr(mod, "_import_data", hydrate)
    return archive, hydrate


@pytest.mark.parametrize("partition,target", [
    ("gpu", "accelerators"), ("batch", "overflow"),
])
def test_stage_routes_and_reverses_across_three_regions(
    mod, cfg, regions, operations, mock_client, partition, target
):
    archive, hydrate = operations
    mod.do_stage_in("123", ["/data/home/project/input"], partition, 123)
    destination = regions[target]
    subscription = destination.get("subscription_id", cfg.SUBSCRIPTION_ID)
    archive.assert_called_once_with(
        "rg-research", "research-fs", "/project/input", "123", "stagein-archive",
        subscription_id=cfg.SUBSCRIPTION_ID)
    hydrate.assert_called_once_with(
        destination["resource_group"], destination["filesystem"], ["/project/input"],
        "123", "stagein", destination["location"], cfg.STAGEIN_CONFLICT_MODE,
        subscription_id=subscription)

    with open(mod._state_path("123", "route.json")) as stream:
        route = json.load(stream)
    assert route["target_region"] == target
    assert route["target"]["subscription_id"] == subscription

    archive.reset_mock()
    hydrate.reset_mock()
    mod.do_stage_out("123", [destination["mountpoint"] + "/project/output"], partition, 123)
    archive.assert_called_once_with(
        destination["resource_group"], destination["filesystem"], "/project/output",
        "123", "stageout-archive", subscription_id=subscription)
    hydrate.assert_called_once_with(
        "rg-research", "research-fs", ["/project/output"], "123", "stageout",
        "eastus", cfg.STAGEOUT_CONFLICT_MODE, subscription_id=cfg.SUBSCRIPTION_ID)
    mock_client.aml_filesystems.get.assert_not_called()


@pytest.mark.parametrize("partition", ["unknown", None, "gpu,batch", ""])
def test_bad_partition_fails_before_clients_or_state(
    mod, regions, operations, state_dir, monkeypatch, partition
):
    client = MagicMock()
    monkeypatch.setattr(mod, "_get_client", client)
    with pytest.raises(ValueError, match="partition"):
        mod.do_stage_in("124", ["/data/home/input"], partition, 123)
    client.assert_not_called()
    for operation in operations:
        operation.assert_not_called()
    assert os.listdir(state_dir) == []


@pytest.mark.parametrize("alias", [False, True])
def test_same_resource_is_noop_even_with_different_region_keys(
    mod, cfg, regions, operations, monkeypatch, alias
):
    if alias:
        regions["accelerators"] = dict(regions["research"])
        regions["accelerators"]["resource_group"] = "RG-RESEARCH"
        regions["accelerators"]["filesystem"] = "RESEARCH-FS"
    get_client = MagicMock()
    monkeypatch.setattr(mod, "_get_client", get_client)
    partition = "gpu" if alias else "hpc"
    mod.do_stage_in("125", ["/data/home/input"], partition, 123)
    mod.do_stage_out("125", ["/data/home/output"], partition, 123)
    get_client.assert_not_called()
    for operation in operations:
        operation.assert_not_called()


@pytest.mark.parametrize("path", [
    "/data/home-other/input", "/data/home/../escape", "/data/home/project/../../escape",
    "/data/home", "data/home/input", "/data/home/input\0hidden",
])
def test_unsafe_paths_rejected_before_any_archive(mod, regions, operations, path):
    with pytest.raises(ValueError):
        mod.do_stage_in("126", ["/data/home/valid", path], "gpu", 123)
    for operation in operations:
        operation.assert_not_called()


def test_symlink_escape_rejected(mod, tmp_path):
    mount = tmp_path / "mount"
    outside = tmp_path / "outside"
    mount.mkdir()
    outside.mkdir()
    (mount / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="not under mount"):
        mod._relative_path(str(mount / "escape" / "secret"), str(mount))
    assert mod._relative_path(str(mount / "safe" / "file"), str(mount)) == "/safe/file"


def test_stageout_uses_remote_mount_not_home(mod, regions, operations):
    with pytest.raises(ValueError, match="not under mount"):
        mod.do_stage_out("127", ["/data/home/output"], "batch", 123)
    operations[0].assert_not_called()


def test_route_cannot_change_mid_job(mod, regions, operations):
    mod.do_stage_in("128", ["/data/home/input"], "gpu", 123)
    for operation in operations:
        operation.reset_mock()
    with pytest.raises(ValueError, match="route changed"):
        mod.do_stage_out("128", ["/data/batch/output"], "batch", 123)
    for operation in operations:
        operation.assert_not_called()


def test_resource_configuration_change_cannot_silently_redirect_stageout(
    mod, regions, operations
):
    mod.do_stage_in("133", ["/data/home/input"], "gpu", 123)
    for operation in operations:
        operation.reset_mock()
    regions["accelerators"]["filesystem"] = "replacement-fs"
    with pytest.raises(ValueError, match="route changed"):
        mod.do_stage_out("133", ["/data/gpu/output"], "gpu", 123)
    for operation in operations:
        operation.assert_not_called()


@pytest.mark.parametrize("setting", ["REGIONS", "HOME_REGION", "PARTITION_REGIONS"])
def test_missing_region_configuration_fails_before_azure_or_state(
    mod, cfg, operations, mock_client, monkeypatch, state_dir, setting
):
    monkeypatch.delattr(cfg, setting)
    with pytest.raises(ValueError, match=setting):
        mod.do_stage_in("134", ["/lustre/input"], "multi-regionB", 123)
    for operation in operations:
        operation.assert_not_called()
    mock_client.aml_filesystems.get.assert_not_called()
    assert os.listdir(state_dir) == []


def test_missing_region_location_is_not_discovered_from_azure(
    mod, regions, operations, mock_client, state_dir
):
    del regions["accelerators"]["location"]
    with pytest.raises(ValueError, match="requires location"):
        mod.do_stage_in("136", ["/data/home/input"], "gpu", 123)
    for operation in operations:
        operation.assert_not_called()
    mock_client.aml_filesystems.get.assert_not_called()
    assert os.listdir(state_dir) == []


def test_each_region_can_supply_subscription_without_global_default(
    mod, cfg, regions, operations, monkeypatch
):
    for region in regions.values():
        region.setdefault("subscription_id", "explicit-sub")
    monkeypatch.delattr(cfg, "SUBSCRIPTION_ID")
    mod.do_stage_in("135", ["/data/home/input"], "gpu", 123)
    assert operations[0].call_args.kwargs["subscription_id"] == "explicit-sub"
    assert operations[1].call_args.kwargs["subscription_id"] == "explicit-sub"


def test_both_resource_flocks_span_all_archives_and_imports(
    mod, regions, operations, state_dir
):
    route = mod._resolve_route("batch")
    lock_paths = [
        os.path.join(state_dir, "locks", hashlib.sha256(
            mod._resource_identity(route[endpoint]).encode()).hexdigest() + ".lock")
        for endpoint in ("home", "target")
    ]
    observed = []

    def observe(*args, **kwargs):
        for lock_path in lock_paths:
            with open(lock_path, "a") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(mod._state_path("129", "route.json")) as stream:
            assert json.load(stream)["target_region"] == "overflow"
        observed.append(args[4])

    operations[0].side_effect = observe
    operations[1].side_effect = observe
    mod.do_stage_in("129", ["/data/home/a", "/data/home/b"], "batch", 123)
    assert observed == ["stagein-archive", "stagein", "stagein-archive", "stagein"]
    for lock_path in lock_paths:
        with open(lock_path, "a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)
        assert os.path.exists(lock_path)


def test_reverse_transfers_acquire_resource_locks_in_the_same_order(
        mod, regions, operations, monkeypatch):
    observed = []
    lock = mod._lock

    @contextmanager
    def observe(identity):
        if not identity.startswith("job:"):
            observed.append(identity)
        with lock(identity):
            yield

    monkeypatch.setattr(mod, "_lock", observe)
    mod.do_stage_in("129", ["/data/home/input"], "batch", 123)
    mod.do_stage_out("129", ["/data/batch/output"], "batch", 123)
    route = mod._resolve_route("batch")
    expected = sorted(mod._resource_identity(route[endpoint]) for endpoint in ("home", "target"))
    assert observed == expected + expected


def test_resource_lock_identity_ignores_region_alias_but_not_subscription(mod, regions):
    source = mod._resolve_route("gpu")["target"]
    alias = dict(source, filesystem="GPU-FS", location="irrelevant", mountpoint="/other")
    assert mod._resource_identity(source) == mod._resource_identity(alias)
    alias["subscription_id"] = "different-subscription"
    assert mod._resource_identity(source) != mod._resource_identity(alias)


def test_clients_are_selected_and_cached_by_subscription(mod, cfg, monkeypatch):
    factory, credential = MagicMock(), MagicMock()
    monkeypatch.setattr(mod, "StorageCacheManagementClient", factory)
    monkeypatch.setattr(mod, "DefaultAzureCredential", credential)
    first = mod._get_client(cfg.SUBSCRIPTION_ID)
    assert mod._get_client() is first
    second = mod._get_client("other")
    assert mod._get_client("other") is second
    assert [call.args[1] for call in factory.call_args_list] == [cfg.SUBSCRIPTION_ID, "other"]


def test_selftest_checks_every_region_with_its_subscription(mod, cfg, regions, monkeypatch):
    clients = {cfg.SUBSCRIPTION_ID: MagicMock(), "other-subscription": MagicMock()}
    monkeypatch.setattr(mod, "_get_client", clients.__getitem__)
    assert mod.do_selftest() == 0
    clients[cfg.SUBSCRIPTION_ID].aml_filesystems.get.assert_any_call("rg-research", "research-fs")
    clients[cfg.SUBSCRIPTION_ID].aml_filesystems.get.assert_any_call("rg-gpu", "gpu-fs")
    clients["other-subscription"].aml_filesystems.get.assert_called_once_with("rg-batch", "batch-fs")


def test_teardown_uses_recorded_import_identity_after_config_changes(
    mod, cfg, regions, mock_client, monkeypatch
):
    monkeypatch.setattr(mod, "_archive", lambda *args, **kwargs: None)
    poller = mock_client.import_jobs.begin_create_or_update.return_value
    poller.continuation_token.return_value = "continuation"
    poller.result.side_effect = RuntimeError("interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        mod.do_stage_in("130", ["/data/home/input"], "batch", 123)
    name = mock_client.import_jobs.begin_create_or_update.call_args.kwargs["import_job_name"]
    cfg.REGIONS["overflow"] = copy.deepcopy(cfg.REGIONS["accelerators"])
    selected = MagicMock(return_value=mock_client)
    monkeypatch.setattr(mod, "_get_client", selected)
    mod.do_teardown("130")
    selected.assert_called_once_with("other-subscription")
    mock_client.import_jobs.begin_delete.assert_called_once_with("rg-batch", "batch-fs", name)
    mock_client.aml_filesystems.cancel_archive.assert_not_called()
    mock_client.import_jobs.list_by_aml_filesystem.assert_not_called()
    assert not os.path.exists(mod._state_path("130", "route.json"))


def test_teardown_failure_retains_route_tokens_and_operation_for_retry(
    mod, mock_client, cfg
):
    mod._write_json(mod._state_path("131", "route.json"), {"actual": "route"})
    mod._save_token("131", "stagein-import", "token")
    operation_path = mod._state_path("131", "bb-131-stagein-abcdef12.import.json")
    mod._write_json(operation_path, {
        "subscription_id": cfg.SUBSCRIPTION_ID, "resource_group": "rg",
        "filesystem": "fs", "name": "bb-131-stagein-abcdef12",
    })
    mock_client.import_jobs.begin_delete.side_effect = RuntimeError("not authorized")
    with pytest.raises(RuntimeError, match="state retained"):
        mod.do_teardown("131")
    assert os.path.exists(operation_path)
    assert os.path.exists(mod._state_path("131", "route.json"))
    assert mod._load_token("131", "stagein-import") == "token"


@pytest.mark.parametrize("job_id", ["../42", "42/../../x", "42-1", "", "42\0"])
def test_job_id_cannot_escape_or_overlap_other_job_state(mod, job_id, mock_client):
    with pytest.raises(ValueError, match="job ID"):
        mod.do_teardown(job_id)
    mock_client.import_jobs.begin_delete.assert_not_called()


@pytest.mark.parametrize("command,method", [
    ("stage-in", "do_stage_in"), ("stage-out", "do_stage_out"),
])
def test_cli_passes_partition_and_original_paths(mod, monkeypatch, command, method):
    operation = MagicMock()
    monkeypatch.setattr(mod, method, operation)
    monkeypatch.setattr("sys.argv", [
        "amlfs_flow", command, "--job", "132", "--partition", "gpu", "--uid", "123",
        "--paths", "/data/home/a b", "/data/home/other",
    ])
    mod.main()
    operation.assert_called_once_with("132", ["/data/home/a b", "/data/home/other"], "gpu", 123)


@pytest.mark.parametrize("command,method", [
    ("stage-in", "do_stage_in"), ("stage-out", "do_stage_out"),
])
def test_cli_requires_explicit_partition(mod, monkeypatch, command, method, capsys):
    operation = MagicMock()
    monkeypatch.setattr(mod, method, operation)
    monkeypatch.setattr("sys.argv", [
        "amlfs_flow", command, "--job", "137", "--uid", "123", "--paths", "/lustre/input",
    ])
    with pytest.raises(SystemExit) as error:
        mod.main()
    assert error.value.code == 2
    assert "--partition" in capsys.readouterr().err
    operation.assert_not_called()


def test_external_config_loader_uses_explicit_absolute_file(mod, monkeypatch, tmp_path):
    config = tmp_path / "cluster-config.py"
    config.write_text("HOME_REGION = 'external-home'\nREGIONS = {}\n")
    monkeypatch.setenv("AMLFS_FLOW_CONFIG", str(config))
    loaded = mod._load_config()
    assert loaded.HOME_REGION == "external-home"
    assert loaded.REGIONS == {}


def test_external_config_loader_rejects_relative_path(mod, monkeypatch):
    monkeypatch.setenv("AMLFS_FLOW_CONFIG", "relative-config.py")
    with pytest.raises(ValueError, match="absolute"):
        mod._load_config()


def test_config_loader_uses_installed_module_by_default(mod, cfg, monkeypatch):
    monkeypatch.delenv("AMLFS_FLOW_CONFIG", raising=False)
    assert mod._load_config() is cfg
