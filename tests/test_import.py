"""Tests for `_import_data` (ImportJob creation + cleanup)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def _make_poller(status="Succeeded", token="cont-token"):
    poller = MagicMock(name="LROPoller")
    poller.continuation_token.return_value = token
    result = MagicMock()
    result.status = status
    poller.result.return_value = result
    return poller


def test_import_data_happy_path_calls_begin_create(mod, mock_client):
    poller = _make_poller()
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    delete_poller = MagicMock()
    mock_client.import_jobs.begin_delete.return_value = delete_poller

    mod._import_data(
        rg="rg-secondary",
        amlfs_name="amlfs-secondary",
        prefixes=["/data/run42"],
        job_id="7",
        phase="stagein",
        location="eastus",
        conflict_mode="Skip",
    )

    mock_client.import_jobs.begin_create_or_update.assert_called_once()
    kwargs = mock_client.import_jobs.begin_create_or_update.call_args.kwargs
    assert kwargs["resource_group_name"] == "rg-secondary"
    assert kwargs["aml_filesystem_name"] == "amlfs-secondary"
    assert kwargs["import_job"]["import_prefixes"] == ["/data/run42"]
    assert kwargs["import_job"]["conflict_resolution_mode"] == "Skip"
    assert kwargs["import_job"]["location"] == "eastus"
    # Cleanup of the ARM resource is attempted
    mock_client.import_jobs.begin_delete.assert_called_once()
    delete_poller.result.assert_called_once()


def test_import_data_persists_then_clears_token(mod, mock_client, state_dir):
    poller = _make_poller(token="TOKEN-XYZ")
    mock_client.import_jobs.begin_create_or_update.return_value = poller

    mod._import_data(
        rg="rg-secondary", amlfs_name="amlfs-secondary",
        prefixes=["/data/x"], job_id="42", phase="stagein",
        location="eastus", conflict_mode="Skip",
    )
    # Token is cleared after the LRO finishes successfully
    assert mod._load_token("42", "stagein-import") is None


def test_import_data_raises_on_failed_status(mod, mock_client):
    poller = _make_poller(status="Failed")
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    with pytest.raises(RuntimeError, match="Failed"):
        mod._import_data(
            rg="rg", amlfs_name="amlfs", prefixes=["/x"],
            job_id="1", phase="stagein", location="eastus",
            conflict_mode="Skip",
        )


def test_import_data_continues_on_cleanup_error(mod, mock_client):
    poller = _make_poller()
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    mock_client.import_jobs.begin_delete.side_effect = Exception("delete blew up")
    # Must NOT propagate the cleanup failure — the data movement succeeded.
    mod._import_data(
        rg="rg", amlfs_name="amlfs", prefixes=["/x"],
        job_id="1", phase="stagein", location="eastus",
        conflict_mode="Skip",
    )


def test_import_data_looks_up_location_when_missing(mod, mock_client):
    poller = _make_poller()
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    fs = MagicMock(); fs.location = "westus3"
    mock_client.aml_filesystems.get.return_value = fs

    mod._import_data(
        rg="rg", amlfs_name="amlfs", prefixes=["/x"],
        job_id="1", phase="stagein", location="",  # falsy → must be auto-discovered
        conflict_mode="OverwriteAlways",
    )

    mock_client.aml_filesystems.get.assert_called_once_with("rg", "amlfs")
    kwargs = mock_client.import_jobs.begin_create_or_update.call_args.kwargs
    assert kwargs["import_job"]["location"] == "westus3"
