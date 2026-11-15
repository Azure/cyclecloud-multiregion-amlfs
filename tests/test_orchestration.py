# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for do_stage_in / do_stage_out / do_teardown / do_selftest.

These exercise the top-level orchestration logic by mocking `_archive`
and `_import_data` and verifying the right resource groups, filesystem
names, prefixes and conflict modes flow through.
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock

from azure.core.exceptions import HttpResponseError
from azure.mgmt.storagecache.models import AmlFilesystem

pytestmark = pytest.mark.usefixtures("owned_paths")


@pytest.fixture
def patched(monkeypatch, mod):
    """Replace `_archive` and `_import_data` with spies."""
    archive = MagicMock(name="_archive")
    import_data = MagicMock(name="_import_data")
    monkeypatch.setattr(mod, "_archive", archive)
    monkeypatch.setattr(mod, "_import_data", import_data)
    return archive, import_data


# ─── do_stage_in ────────────────────────────────────────────────────────


def test_stage_in_archives_home_then_imports_target(mod, mock_client, patched, cfg):
    archive, import_data = patched
    home, target = cfg.REGIONS["regionA"], cfg.REGIONS["regionB"]

    mod.do_stage_in("11", ["/lustre/data/run42"], "multi-regionB", 123)

    archive.assert_called_once_with(
        home["resource_group"], home["filesystem"],
        "/data/run42", "11", "stagein-archive",
        subscription_id=cfg.SUBSCRIPTION_ID,
    )
    import_data.assert_called_once_with(
        target["resource_group"], target["filesystem"],
        ["/data/run42/"], "11", "stagein", target["location"],
        cfg.STAGEIN_CONFLICT_MODE,
        subscription_id=cfg.SUBSCRIPTION_ID,
    )
    mock_client.aml_filesystems.get.assert_not_called()


def test_stage_in_handles_multiple_paths(mod, mock_client, patched, cfg):
    archive, import_data = patched
    mod.do_stage_in("12", ["/lustre/a", "/lustre/b"], "multi-regionB", 123)
    assert archive.call_count == 2
    assert import_data.call_count == 2


# ─── do_stage_out ───────────────────────────────────────────────────────


def test_stage_out_archives_target_then_imports_home(mod, mock_client, patched, cfg):
    archive, import_data = patched
    home, target = cfg.REGIONS["regionA"], cfg.REGIONS["regionB"]

    mod.do_stage_out("21", ["/lustre/data/run42"], "multi-regionB", 123)

    archive.assert_called_once_with(
        target["resource_group"], target["filesystem"],
        "/data/run42", "21", "stageout-archive",
        subscription_id=cfg.SUBSCRIPTION_ID,
    )
    import_data.assert_called_once_with(
        home["resource_group"], home["filesystem"],
        ["/data/run42/"], "21", "stageout", home["location"],
        cfg.STAGEOUT_CONFLICT_MODE,
        subscription_id=cfg.SUBSCRIPTION_ID,
    )
    mock_client.aml_filesystems.get.assert_not_called()


@pytest.mark.parametrize("method", ["do_stage_in", "do_stage_out"])
@pytest.mark.parametrize("suffix", ["", "/", "//", "/."])
def test_directory_import_never_selects_sibling_blob_prefixes(
    mod, patched, method, suffix,
):
    archive, import_data = patched
    subtree = "/data/run42/output"
    blobs = {
        subtree: "source directory metadata",
        subtree + "/result": "new result",
        subtree + "/nested/result": "nested result",
        subtree + "-backup/result": "unrelated blob backup",
        subtree + "2/result": "unrelated blob output2",
    }
    destination = {
        subtree: "destination directory metadata",
    }
    if method == "do_stage_out":
        destination.update({
            subtree + "-backup/result": "keep home backup",
            subtree + "2/result": "keep home output2",
        })

    def flat_blob_import(rg, fs, prefixes, job, phase, location, conflict, **kwargs):
        destination.update({
            name: value for name, value in blobs.items()
            if any(name.startswith(prefix) for prefix in prefixes)
            and (conflict != "Skip" or name not in destination)
        })

    import_data.side_effect = flat_blob_import
    getattr(mod, method)("22", ["/lustre" + subtree + suffix], "multi-regionB", 123)

    assert destination[subtree + "/result"] == "new result"
    assert destination[subtree + "/nested/result"] == "nested result"
    if method == "do_stage_out":
        assert destination[subtree + "-backup/result"] == "keep home backup"
        assert destination[subtree + "2/result"] == "keep home output2"
    else:
        assert subtree + "-backup/result" not in destination
        assert subtree + "2/result" not in destination
    # The exact root metadata blob cannot be selected without broadening the prefix.
    assert destination[subtree] == "destination directory metadata"
    archive.assert_called_once()
    assert archive.call_args.args[2] == subtree


def test_directory_contents_import_preserves_child_directory_metadata_and_write_access(
    mod, patched,
):
    _, import_data = patched
    root = "/data/run42"

    def directory(mode):
        return SimpleNamespace(st_uid=123, st_gid=90, st_mode=stat.S_IFDIR | mode)

    existing_root = directory(0o750)
    blobs = {
        root: directory(0o700),
        root + "/input": directory(0o750),
        root + "/input/data": "input contents",
        root + "/output": directory(0o770),
    }
    destination = {root: existing_root}
    selected = []

    def flat_blob_import(rg, fs, prefixes, job, phase, location, conflict, **kwargs):
        selected.extend(
            name for name in blobs if any(name.startswith(prefix) for prefix in prefixes)
        )
        destination.update({
            name: blobs[name] for name in selected
            if conflict != "Skip" or name not in destination
        })

    import_data.side_effect = flat_blob_import
    mod.do_stage_in("23", ["/lustre" + root], "multi-regionB", 123)

    assert root not in selected
    assert destination[root] is existing_root
    assert destination[root + "/input/data"] == "input contents"
    # Empty child directories have metadata blobs within the selected boundary.
    assert destination[root + "/output"] is blobs[root + "/output"]
    for path in (root, root + "/output"):
        assert mod._path_allows(path, destination[path], 123, {90}, os.W_OK | os.X_OK)


def test_empty_selected_directory_does_not_import_its_exact_metadata_blob(mod, patched):
    _, import_data = patched
    mod.do_stage_in("24", ["/lustre/empty"], "multi-regionB", 123)
    prefixes = import_data.call_args.args[2]
    blobs = {"/empty": "directory metadata", "/empty-backup/result": "unrelated"}
    assert not [name for name in blobs if any(name.startswith(prefix) for prefix in prefixes)]


def test_similar_and_nested_directories_use_separate_boundary_imports(mod, patched):
    _, import_data = patched
    mod.do_stage_in("25", ["/lustre/data", "/lustre/data2", "/lustre/data/child"],
                    "multi-regionB", 123)
    assert [call.args[2] for call in import_data.call_args_list] == [
        ["/data/"], ["/data2/"], ["/data/child/"],
    ]


# ─── do_teardown ────────────────────────────────────────────────────────


def test_teardown_removes_job_state_files(mod, mock_client, state_dir):
    mod._save_token("99", "stagein-import", "TOK")
    mod._save_token("99", "stageout-import", "TOK")
    mod._save_token("100", "stagein-import", "OTHER")

    mod.do_teardown("99")

    # job-99-* gone, job-100-* preserved
    leftover = sorted(os.listdir(state_dir))
    assert leftover == ["job-100-stagein-import.token", "locks"]


def test_teardown_never_cancels_global_archives(mod, mock_client, state_dir):
    mod.do_teardown("77")

    mock_client.aml_filesystems.cancel_archive.assert_not_called()
    mock_client.aml_filesystems.get.assert_not_called()


def test_teardown_deletes_only_recorded_import_jobs(mod, mock_client, state_dir, cfg):
    for job_id in ("55", "99"):
        name = f"bb-{job_id}-stagein-abcd1234"
        mod._write_json(mod._state_path(job_id, f"{name}.import.json"), {
            "name": name, "subscription_id": cfg.SUBSCRIPTION_ID,
            "resource_group": "actual-rg", "filesystem": "actual-fs",
        })
    mod.do_teardown("55")
    mock_client.import_jobs.begin_delete.assert_called_once_with(
        "actual-rg", "actual-fs", "bb-55-stagein-abcd1234")
    mock_client.import_jobs.list_by_aml_filesystem.assert_not_called()
    assert os.path.exists(mod._state_path("99", "bb-99-stagein-abcd1234.import.json"))


def test_teardown_without_recorded_ownership_does_not_discover_imports(mod, mock_client, state_dir):
    mod.do_teardown("66")
    mock_client.import_jobs.begin_delete.assert_not_called()
    mock_client.import_jobs.list_by_aml_filesystem.assert_not_called()


@pytest.mark.parametrize("failure", ["unfinished", "poll-404", "initial-404", "poll-403"])
def test_teardown_retains_import_and_route_until_deletion_is_confirmed(
    mod, mock_client, cfg, failure,
):
    name = "bb-67-stagein-abcd1234"
    ownership = mod._state_path("67", f"{name}.import.json")
    route = mod._state_path("67", "route.json")
    mod._write_json(ownership, {
        "name": name, "subscription_id": cfg.SUBSCRIPTION_ID,
        "resource_group": "actual-rg", "filesystem": "actual-fs",
    })
    mod._write_json(route, {"recorded": "route"})
    mod._save_token("67", "stagein-import", "TOKEN")
    deletion = mock_client.import_jobs.begin_delete.return_value
    if failure == "unfinished":
        deletion.done.return_value = False
    else:
        error = HttpResponseError("not found")
        error.status_code = 403 if failure == "poll-403" else 404
        if failure.startswith("poll"):
            deletion.result.side_effect = error
        else:
            mock_client.import_jobs.begin_delete.side_effect = error
        # A missing operation-status URL is not evidence the ImportJob is gone.
        mock_client.import_jobs.get.return_value = MagicMock(name="existing_import")

    with pytest.raises(RuntimeError, match="state retained for retry"):
        mod.do_teardown("67")
    assert os.path.exists(ownership)
    assert os.path.exists(route)
    assert mod._load_token("67", "stagein-import") == "TOKEN"

    mock_client.import_jobs.begin_delete.side_effect = None
    deletion.result.side_effect = None
    deletion.done.return_value = True
    mod.do_teardown("67")
    assert not os.path.exists(ownership)
    assert not os.path.exists(route)
    assert mod._load_token("67", "stagein-import") is None


@pytest.mark.parametrize("where", ["begin_delete", "poll"])
def test_teardown_removes_state_after_get_confirms_import_absence(
    mod, mock_client, cfg, where,
):
    name = "bb-68-stageout-abcd1234"
    ownership = mod._state_path("68", f"{name}.import.json")
    mod._write_json(ownership, {
        "name": name, "subscription_id": cfg.SUBSCRIPTION_ID,
        "resource_group": "rg", "filesystem": "fs",
    })
    mod._save_token("68", "stageout-import", "TOKEN")
    error = HttpResponseError("not found")
    error.status_code = 404
    if where == "begin_delete":
        mock_client.import_jobs.begin_delete.side_effect = error
    else:
        mock_client.import_jobs.begin_delete.return_value.result.side_effect = error
    mock_client.import_jobs.get.side_effect = error

    mod.do_teardown("68")

    mock_client.import_jobs.get.assert_called_once_with("rg", "fs", name)
    assert not os.path.exists(ownership)
    assert mod._load_token("68", "stageout-import") is None


# ─── do_selftest ────────────────────────────────────────────────────────


def test_selftest_returns_zero_when_all_amlfs_reachable(mod, mock_client, capsys):
    mock_client.aml_filesystems.get.return_value = AmlFilesystem({
        "name": "amlfs", "location": "eastus", "properties": {"provisioningState": "Succeeded"},
    })
    assert mod.do_selftest() == 0
    assert "SELFTEST PASSED" in capsys.readouterr().out


def test_selftest_returns_nonzero_when_get_fails(mod, mock_client, capsys):
    err = HttpResponseError(message="forbidden", response=MagicMock(status_code=403))
    err.status_code = 403
    mock_client.aml_filesystems.get.side_effect = err
    assert mod.do_selftest() == 1
    assert "SELFTEST FAILED" in capsys.readouterr().err
