# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for do_stage_in / do_stage_out / do_teardown / do_selftest.

These exercise the top-level orchestration logic by mocking `_archive`
and `_import_data` and verifying the right resource groups, filesystem
names, prefixes and conflict modes flow through.
"""

from __future__ import annotations

import os
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
        ["/data/run42"], "11", "stagein", target["location"],
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
        ["/data/run42"], "21", "stageout", home["location"],
        cfg.STAGEOUT_CONFLICT_MODE,
        subscription_id=cfg.SUBSCRIPTION_ID,
    )
    mock_client.aml_filesystems.get.assert_not_called()


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
