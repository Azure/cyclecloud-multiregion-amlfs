# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for `_import_data` (ImportJob creation + cleanup)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from azure.core.exceptions import HttpResponseError
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


@pytest.mark.parametrize("done", [False, True])
def test_import_cleanup_requires_completed_deletion(
    mod, mock_client, state_dir, done,
):
    mock_client.import_jobs.begin_create_or_update.return_value = _make_poller()
    deletion = mock_client.import_jobs.begin_delete.return_value
    deletion.done.return_value = done

    mod._import_data("rg", "fs", ["/x/"], "47", "stagein", "eastus", "Skip")

    owned = list(Path(state_dir).glob("job-47-*.import.json"))
    assert bool(owned) is not done
    deletion.result.assert_called_once_with(timeout=300)
    if not done:
        identity = json.loads(owned[0].read_text())
        assert identity["resource_group"] == "rg"
        assert identity["filesystem"] == "fs"
        assert mod._load_token("47", "stagein-import") == "cont-token"
        deletion.done.return_value = True
        mod.do_teardown("47")
        assert not owned[0].exists()
    assert mod._load_token("47", "stagein-import") is None


@pytest.mark.parametrize("where", ["begin_delete", "poll"])
@pytest.mark.parametrize("get_status", [None, 404, 403, "network-error"])
def test_delete_import_404_requires_resource_get_confirmation(
    mod, mock_client, where, get_status,
):
    error = HttpResponseError("delete endpoint or operation-status not found")
    error.status_code = 404
    if where == "begin_delete":
        mock_client.import_jobs.begin_delete.side_effect = error
    else:
        mock_client.import_jobs.begin_delete.return_value.result.side_effect = error
    if get_status == "network-error":
        mock_client.import_jobs.get.side_effect = OSError("connection lost")
    elif get_status:
        missing = HttpResponseError("resource lookup")
        missing.status_code = get_status
        mock_client.import_jobs.get.side_effect = missing

    if get_status == 404:
        mod._delete_import(mock_client, "rg", "fs", "owned-import")
    else:
        with pytest.raises(HttpResponseError) as caught:
            mod._delete_import(mock_client, "rg", "fs", "owned-import")
        assert caught.value is error
    mock_client.import_jobs.get.assert_called_once_with("rg", "fs", "owned-import")


def test_delete_import_does_not_hide_terminal_polling_errors(mod, mock_client):
    error = HttpResponseError("deletion forbidden")
    error.status_code = 403
    mock_client.import_jobs.begin_delete.return_value.result.side_effect = error
    with pytest.raises(HttpResponseError) as caught:
        mod._delete_import(mock_client, "rg", "fs", "owned-import")
    assert caught.value is error
    mock_client.import_jobs.get.assert_not_called()


@pytest.mark.parametrize("absent", [False, True])
def test_import_cleanup_polling_404_keeps_ownership_unless_actual_resource_is_absent(
    mod, mock_client, state_dir, absent,
):
    mock_client.import_jobs.begin_create_or_update.return_value = _make_poller()
    error = HttpResponseError("operation-status endpoint missing")
    error.status_code = 404
    mock_client.import_jobs.begin_delete.return_value.result.side_effect = error
    if absent:
        missing = HttpResponseError("ImportJob does not exist")
        missing.status_code = 404
        mock_client.import_jobs.get.side_effect = missing

    mod._import_data("rg", "fs", ["/x/"], "48", "stageout", "eastus", "OverwriteAlways")

    assert bool(list(Path(state_dir).glob("job-48-*.import.json"))) is not absent
    assert mod._load_token("48", "stageout-import") == (None if absent else "cont-token")
    name = mock_client.import_jobs.begin_create_or_update.call_args.kwargs["import_job_name"]
    mock_client.import_jobs.get.assert_called_once_with("rg", "fs", name)
