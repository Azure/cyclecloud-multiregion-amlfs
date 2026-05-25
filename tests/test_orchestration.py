"""Tests for do_stage_in / do_stage_out / do_teardown / do_selftest.

These exercise the top-level orchestration logic by mocking `_archive`
and `_import_data` and verifying the right resource groups, filesystem
names, prefixes and conflict modes flow through.
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, call

from azure.core.exceptions import HttpResponseError


@pytest.fixture
def patched(monkeypatch, mod):
    """Replace `_archive` and `_import_data` with spies."""
    archive = MagicMock(name="_archive")
    import_data = MagicMock(name="_import_data")
    monkeypatch.setattr(mod, "_archive", archive)
    monkeypatch.setattr(mod, "_import_data", import_data)
    return archive, import_data


# ─── do_stage_in ────────────────────────────────────────────────────────


def test_stage_in_archives_primary_then_imports_secondary(mod, mock_client, patched, cfg):
    archive, import_data = patched
    fs_secondary = MagicMock(); fs_secondary.location = "westus3"
    mock_client.aml_filesystems.get.return_value = fs_secondary

    mod.do_stage_in("11", ["/lustre/data/run42"])

    archive.assert_called_once_with(
        cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME,
        "/data/run42", "11", "stagein-archive",
    )
    import_data.assert_called_once_with(
        cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME,
        ["/data/run42"], "11", "stagein", "westus3",
        cfg.STAGEIN_CONFLICT_MODE,
    )


def test_stage_in_handles_multiple_paths(mod, mock_client, patched, cfg):
    archive, import_data = patched
    fs = MagicMock(); fs.location = "westus3"
    mock_client.aml_filesystems.get.return_value = fs

    mod.do_stage_in("12", ["/lustre/a", "/lustre/b"])
    assert archive.call_count == 2
    assert import_data.call_count == 2


# ─── do_stage_out ───────────────────────────────────────────────────────


def test_stage_out_archives_secondary_then_imports_primary(mod, mock_client, patched, cfg):
    archive, import_data = patched
    fs_primary = MagicMock(); fs_primary.location = "scus"
    mock_client.aml_filesystems.get.return_value = fs_primary

    mod.do_stage_out("21", ["/lustre/data/run42"])

    archive.assert_called_once_with(
        cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME,
        "/data/run42", "21", "stageout-archive",
    )
    import_data.assert_called_once_with(
        cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME,
        ["/data/run42"], "21", "stageout", "scus",
        cfg.STAGEOUT_CONFLICT_MODE,
    )


# ─── do_teardown ────────────────────────────────────────────────────────


def test_teardown_removes_job_state_files(mod, mock_client, state_dir):
    mock_client.aml_filesystems.get.return_value = MagicMock(archive_status=None)
    mock_client.import_jobs.list_by_aml_filesystem.return_value = []

    mod._save_token("99", "stagein-import", "TOK")
    mod._save_token("99", "stageout-import", "TOK")
    mod._save_token("100", "stagein-import", "OTHER")

    mod.do_teardown("99")

    # job-99-* gone, job-100-* preserved
    leftover = sorted(os.listdir(state_dir))
    assert leftover == ["job-100-stagein-import.token"]


def test_teardown_cancels_in_progress_archive(mod, mock_client, state_dir):
    fs_active = MagicMock()
    fs_active.archive_status.state = "InProgress"
    mock_client.aml_filesystems.get.return_value = fs_active
    mock_client.import_jobs.list_by_aml_filesystem.return_value = []

    mod.do_teardown("77")

    # cancel_archive should be called for both AMLFS instances
    assert mock_client.aml_filesystems.cancel_archive.call_count == 2


def test_teardown_deletes_matching_import_jobs(mod, mock_client, state_dir):
    fs_idle = MagicMock(); fs_idle.archive_status = None
    mock_client.aml_filesystems.get.return_value = fs_idle

    matching_ij = MagicMock(); matching_ij.name = "bb-55-stagein-abcd1234"
    matching_ij.provisioning_state = "InProgress"
    other_ij = MagicMock(); other_ij.name = "bb-99-stagein-abcd1234"  # different job
    mock_client.import_jobs.list_by_aml_filesystem.return_value = [matching_ij, other_ij]

    mod.do_teardown("55")

    delete_calls = mock_client.import_jobs.begin_delete.call_args_list
    deleted_names = [c.args[2] for c in delete_calls]
    assert "bb-55-stagein-abcd1234" in deleted_names
    assert "bb-99-stagein-abcd1234" not in deleted_names


def test_teardown_skips_already_finished_import_jobs(mod, mock_client, state_dir):
    fs_idle = MagicMock(); fs_idle.archive_status = None
    mock_client.aml_filesystems.get.return_value = fs_idle

    finished = MagicMock(); finished.name = "bb-66-stagein-xxxx"
    finished.provisioning_state = "Succeeded"
    mock_client.import_jobs.list_by_aml_filesystem.return_value = [finished]

    mod.do_teardown("66")
    mock_client.import_jobs.begin_delete.assert_not_called()


# ─── do_selftest ────────────────────────────────────────────────────────


def test_selftest_returns_zero_when_both_amlfs_reachable(mod, mock_client, capsys):
    mock_client.aml_filesystems.get.return_value = MagicMock(
        name="amlfs", location="eastus", provisioning_state="Succeeded"
    )
    assert mod.do_selftest() == 0
    assert "SELFTEST PASSED" in capsys.readouterr().out


def test_selftest_returns_nonzero_when_get_fails(mod, mock_client, capsys):
    err = HttpResponseError(message="forbidden", response=MagicMock(status_code=403))
    err.status_code = 403
    mock_client.aml_filesystems.get.side_effect = err
    assert mod.do_selftest() == 1
    assert "SELFTEST FAILED" in capsys.readouterr().err
