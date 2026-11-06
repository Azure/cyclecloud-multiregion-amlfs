# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for `_import_data` (ImportJob creation + cleanup)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from azure.mgmt.storagecache.models import ImportJob


def _make_poller(status="Completed", token="cont-token"):
    poller = MagicMock(name="LROPoller")
    poller.continuation_token.return_value = token
    result = ImportJob({
        "properties": {"status": {"state": status}, "provisioningState": "Succeeded"},
    })
    poller.result.return_value = result
    return poller


def test_import_data_happy_path_calls_begin_create(mod, mock_client):
    poller = _make_poller()
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    delete_poller = MagicMock()
    mock_client.import_jobs.begin_delete.return_value = delete_poller

    mod._import_data(
        rg="rg-multi-region",
        amlfs_name="amlfs-multi-region",
        prefixes=["/data/run42"],
        job_id="7",
        phase="stagein",
        location="eastus",
        conflict_mode="Skip",
    )

    mock_client.import_jobs.begin_create_or_update.assert_called_once()
    kwargs = mock_client.import_jobs.begin_create_or_update.call_args.kwargs
    assert kwargs["resource_group_name"] == "rg-multi-region"
    assert kwargs["aml_filesystem_name"] == "amlfs-multi-region"
    body = kwargs["import_job"].as_dict()
    assert body["properties"]["importPrefixes"] == ["/data/run42"]
    assert body["properties"]["conflictResolutionMode"] == "Skip"
    assert body["location"] == "eastus"
    # Cleanup of the ARM resource is attempted
    mock_client.import_jobs.begin_delete.assert_called_once()
    delete_poller.result.assert_called_once()


def test_import_data_persists_then_clears_token(mod, mock_client, state_dir):
    poller = _make_poller(token="TOKEN-XYZ")
    mock_client.import_jobs.begin_create_or_update.return_value = poller

    mod._import_data(
        rg="rg-multi-region", amlfs_name="amlfs-multi-region",
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


def test_import_data_requires_location_before_any_azure_call(mod, mock_client, state_dir):
    with pytest.raises(ValueError, match="explicit Azure location"):
        mod._import_data(
            rg="rg", amlfs_name="amlfs", prefixes=["/x"],
            job_id="1", phase="stagein", location="",
            conflict_mode="OverwriteAlways",
        )
    mock_client.aml_filesystems.get.assert_not_called()
    mock_client.import_jobs.begin_create_or_update.assert_not_called()
    assert mod._load_token("1", "stagein-import") is None


@pytest.mark.parametrize("properties", [
    None, {"provisioningState": "Succeeded"},
    {"provisioningState": "Succeeded", "status": {"state": "InProgress"}},
])
def test_import_waits_for_data_completion_not_just_arm_provisioning(mod, mock_client, properties):
    poller = _make_poller(status="InProgress")
    poller.result.return_value = ImportJob({"properties": properties})
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    mock_client.import_jobs.get.return_value = _make_poller().result.return_value
    mod._import_data("rg", "fs", ["/x"], "43", "stagein", "eastus", "Skip")
    mock_client.import_jobs.get.assert_called_once()
    mock_client.import_jobs.begin_delete.assert_called_once()


@pytest.mark.parametrize("state", ["Failed", "Canceled", "CompletedPartial"])
def test_import_rejects_incomplete_data_and_preserves_ownership(
    mod, mock_client, state_dir, state
):
    poller = _make_poller()
    poller.result.return_value = ImportJob({
        "properties": {"provisioningState": "Succeeded", "status": {"state": state}},
    })
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    with pytest.raises(RuntimeError, match=state):
        mod._import_data("rg", "fs", ["/x"], "44", "stagein", "eastus", "Skip")
    mock_client.import_jobs.begin_delete.assert_not_called()
    assert mod._load_token("44", "stagein-import") == "cont-token"


def test_import_times_out_without_releasing_ownership(mod, mock_client, cfg, monkeypatch):
    poller = _make_poller(status="InProgress")
    mock_client.import_jobs.begin_create_or_update.return_value = poller
    mock_client.import_jobs.get.return_value = poller.result.return_value
    monkeypatch.setattr(cfg, "LRO_TIMEOUT", 0.001)
    with pytest.raises(RuntimeError, match="timed out"):
        mod._import_data("rg", "fs", ["/x"], "45", "stageout", "eastus", "Skip")
    mock_client.import_jobs.begin_delete.assert_not_called()
    assert mod._load_token("45", "stageout-import") == "cont-token"


def test_create_retry_keeps_owned_identity_and_request_model(mod, mock_client, monkeypatch):
    from azure.core.exceptions import HttpResponseError
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

    monkeypatch.setattr(mod, "_retry_sdk", retry(
        retry=retry_if_exception(mod._is_retryable),
        wait=wait_none(), stop=stop_after_attempt(2), reraise=True))
    calls = []

    def create(**kwargs):
        calls.append((kwargs["import_job_name"], kwargs["import_job"].as_dict()))
        if len(calls) == 1:
            error = HttpResponseError("throttled")
            error.status_code = 429
            raise error
        return _make_poller()

    mock_client.import_jobs.begin_create_or_update.side_effect = create
    mod._import_data("rg", "fs", ["/x"], "46", "stagein", "eastus", "Skip")
    assert len(calls) == 2
    assert calls[0] == calls[1]
