# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Export completion requires a successful iteration and verified shutdown."""

import json
from datetime import datetime, timezone
from itertools import chain, repeat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("owned_paths")
from azure.core.exceptions import HttpResponseError
from azure.mgmt.storagecache.models import AutoExportJob


START = "2026-11-04T09:30:35.1991859+00:00"
COMPLETE = "2026-11-04T09:30:44+00:00"


def snapshot(state="InProgress", iterations=0, completed=None, suffix="", **fields):
    status = {
        "state": state, "exportIterationCount": iterations,
        "totalFilesFailed": 0, "currentIterationFilesFailed": 0,
        "lastStartedTime" + suffix: START,
    }
    if completed is not None:
        status["lastSuccessfulIterationCompletionTime" + suffix] = completed
    status.update(fields)
    return AutoExportJob({"properties": {"provisioningState": "Succeeded", "status": status}})


@pytest.fixture(autouse=True)
def bounded_clock(mod, cfg, monkeypatch):
    now = [0]
    monkeypatch.setattr(cfg, "LRO_TIMEOUT", 3)
    monkeypatch.setattr(cfg, "ARCHIVE_POLL_INTERVAL", 1)
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.fixture
def export_api(mock_client, cfg):
    def setup(snapshots):
        operations = mock_client.auto_export_jobs
        responses = chain(snapshots[1:], repeat(snapshots[-1]))
        stopping = [False]
        create = MagicMock()
        create.result.return_value = snapshots[0]
        create.continuation_token.return_value = "opaque-export-token"
        disable = MagicMock()
        disable.result.return_value = snapshot("Disabled")
        operations.begin_delete.return_value.done.return_value = True

        def begin(rg, filesystem, name, job):
            stopping[0] = False
            records = list(Path(cfg.STATE_DIR).glob("*.export.json"))
            assert any(json.loads(path.read_text())["name"] == name for path in records)
            assert job.properties.admin_status == "Enable"
            assert job.properties.auto_export_prefixes == ["/data/run42"]
            return create

        def get(*args):
            value = snapshot("Disabled") if stopping[0] else next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        def update(rg, filesystem, name, job):
            assert job.properties.admin_status == "Disable"
            stopping[0] = True
            return disable

        operations.begin_create_or_update.side_effect = begin
        operations.get.side_effect = get
        operations.begin_update.side_effect = update
        return operations

    return setup


@pytest.mark.parametrize("suffix", ["", "UTC"])
def test_export_waits_for_success_then_disables_and_deletes(mod, export_api, state_dir, suffix):
    operations = export_api([snapshot(), snapshot(iterations=1, completed=COMPLETE, suffix=suffix)])
    mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")
    name = operations.begin_create_or_update.call_args.args[2]
    assert name.startswith("bb-1-stagein-archive-")
    assert [call[0] for call in operations.method_calls] == [
        "begin_create_or_update", "get", "get", "begin_update", "begin_delete",
    ]
    operations.begin_delete.assert_called_once_with("rg", "amlfs", name)
    assert not list(Path(state_dir).glob("*.export.json"))
    assert not list(Path(state_dir).glob("*.token"))


def test_fast_export_can_finish_before_first_poll(mod, export_api):
    operations = export_api([snapshot(iterations=1, completed=COMPLETE)])
    mod._archive("rg", "amlfs", "/data/run42", "2", "stageout-archive")
    assert operations.get.call_count == 1
    operations.begin_update.assert_called_once()


@pytest.mark.parametrize("fraction,microseconds", [("1", 100000), ("1991859", 199185), ("123456789", 123456)])
def test_service_timestamp_precision_is_portable_to_python310(mod, fraction, microseconds):
    status = snapshot(lastStartedTime="2026-11-04T09:30:35." + fraction + "Z").properties.status
    assert mod._export_time(status, "lastStartedTime") == datetime(
        2026, 11, 4, 9, 30, 35, microseconds, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [
    snapshot(),
    snapshot(iterations=1),
    snapshot(iterations=0, completed=COMPLETE),
    snapshot(iterations=1, completed="2026-11-04T09:00:00Z"),
    snapshot(iterations=1, completed=COMPLETE, totalFilesFailed=None),
    snapshot(iterations=1, completed=COMPLETE, currentIterationFilesFailed=None),
    AutoExportJob({"properties": {"provisioningState": "Succeeded"}}),
])
def test_unproven_export_never_imports_and_still_stops(
        mod, export_api, monkeypatch, bounded_clock, value):
    operations = export_api([value])
    hydrate = MagicMock()
    monkeypatch.setattr(mod, "_import_data", hydrate)
    with pytest.raises(RuntimeError, match="timed out after 3s"):
        mod.do_stage_in("3", ["/lustre/data/run42"], "multi-regionB", 123)
    hydrate.assert_not_called()
    operations.begin_update.assert_called_once()
    operations.begin_delete.assert_called_once()
    assert bounded_clock[0] == 3


@pytest.mark.parametrize("value", [
    snapshot("Failed"),
    snapshot("Disabled"),
    snapshot("DisableFailed"),
    snapshot(iterations=1, completed=COMPLETE, totalFilesFailed=1),
    snapshot(iterations=1, completed=COMPLETE, currentIterationFilesFailed=1),
    AutoExportJob({"properties": {"provisioningState": "Failed"}}),
])
def test_export_failure_is_explicit_and_cleanup_runs(mod, export_api, value):
    operations = export_api([value])
    with pytest.raises(RuntimeError, match="ExportJob"):
        mod._archive("rg", "amlfs", "/data/run42", "4", "stagein-archive")
    operations.begin_delete.assert_called_once()


def test_import_requires_export_shutdown_and_deletion(mod, export_api, monkeypatch):
    operations = export_api([snapshot(), snapshot(iterations=1, completed=COMPLETE)])

    def hydrate(*args, **kwargs):
        operations.begin_update.assert_called_once()
        operations.begin_delete.assert_called_once()
        assert operations.begin_delete.return_value.done.called

    hydrate = MagicMock(side_effect=hydrate)
    monkeypatch.setattr(mod, "_import_data", hydrate)
    mod.do_stage_in("5", ["/lustre/data/run42"], "multi-regionB", 123)
    hydrate.assert_called_once()


def test_each_export_uses_fresh_owned_resource(mod, export_api):
    operations = export_api([snapshot(iterations=1, completed=COMPLETE)])
    for _ in range(2):
        mod._archive("rg", "amlfs", "/data/run42", "6", "stagein-archive")
    names = [call.args[2] for call in operations.begin_create_or_update.call_args_list]
    assert len(set(names)) == 2
    assert [call.args[2] for call in operations.begin_delete.call_args_list] == names


def test_transient_polling_error_does_not_create_another_export(mod, export_api, monkeypatch):
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

    monkeypatch.setattr(mod, "_retry_sdk", retry(
        retry=retry_if_exception(mod._is_retryable),
        wait=wait_none(), stop=stop_after_attempt(2), reraise=True))
    error = HttpResponseError("transient")
    error.status_code = 500
    operations = export_api([snapshot(), error, snapshot(iterations=1, completed=COMPLETE)])
    mod._archive("rg", "amlfs", "/data/run42", "7", "stagein-archive")
    operations.begin_create_or_update.assert_called_once()


def test_cleanup_failure_retains_owned_resource_for_teardown(mod, export_api, state_dir):
    operations = export_api([snapshot(iterations=1, completed=COMPLETE)])
    operations.begin_delete.return_value.done.return_value = False
    with pytest.raises(RuntimeError, match="deletion timed out"):
        mod._archive("rg", "amlfs", "/data/run42", "8", "stagein-archive")
    records = list(Path(state_dir).glob("*.export.json"))
    assert len(records) == 1
    operations.begin_delete.return_value.done.return_value = True
    mod.do_teardown("8")
    assert not records[0].exists()


def test_disable_waits_for_operational_state_and_is_bounded(mod, mock_client, cfg, monkeypatch, bounded_clock):
    monkeypatch.setattr(cfg, "ARCHIVE_POLL_INTERVAL", 15)
    operations = mock_client.auto_export_jobs
    operations.get.return_value = snapshot("Disabling")
    operations.begin_update.return_value.result.return_value = snapshot("Disabling")
    with pytest.raises(RuntimeError, match="disable timed out"):
        mod._stop_export(mock_client, "rg", "amlfs", "owned", timeout=3)
    operations.begin_delete.assert_not_called()
    assert bounded_clock[0] == 3


def test_missing_export_is_already_cleaned(mod, mock_client):
    error = HttpResponseError("missing")
    error.status_code = 404
    mock_client.auto_export_jobs.get.side_effect = error
    mod._stop_export(mock_client, "rg", "amlfs", "owned")
    mock_client.auto_export_jobs.begin_update.assert_not_called()
    mock_client.auto_export_jobs.begin_delete.assert_not_called()


def test_missing_lro_status_is_not_proof_export_was_deleted(mod, export_api, state_dir):
    operations = export_api([snapshot(iterations=1, completed=COMPLETE)])
    error = HttpResponseError("operation-status endpoint missing")
    error.status_code = 404
    operations.begin_delete.return_value.result.side_effect = error
    with pytest.raises(HttpResponseError):
        mod._archive("rg", "amlfs", "/data/run42", "9", "stageout-archive")
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        mod.do_teardown("9")
    assert len(list(Path(state_dir).glob("*.export.json"))) == 1


def test_teardown_only_stops_recorded_exports_for_this_job(mod, mock_client, cfg, state_dir):
    for job in ("55", "99"):
        name = f"bb-{job}-stageout-archive-abcd1234"
        mod._write_json(mod._state_path(job, name + ".export.json"), {
            "name": name, "subscription_id": cfg.SUBSCRIPTION_ID,
            "resource_group": "rg", "filesystem": "amlfs",
        })
    mock_client.auto_export_jobs.get.return_value = snapshot("Disabled")
    mock_client.auto_export_jobs.begin_delete.return_value.done.return_value = True
    mod.do_teardown("55")
    mock_client.auto_export_jobs.begin_delete.assert_called_once_with(
        "rg", "amlfs", "bb-55-stageout-archive-abcd1234")
    assert len(list(Path(state_dir).glob("job-99-*.export.json"))) == 1
    mock_client.auto_export_jobs.list_by_aml_filesystem.assert_not_called()


def test_teardown_rejects_another_jobs_export_identity(mod, mock_client, cfg):
    path = mod._state_path("55", "invalid.export.json")
    mod._write_json(path, {
        "name": "bb-99-stagein-archive-abcd1234", "subscription_id": cfg.SUBSCRIPTION_ID,
        "resource_group": "rg", "filesystem": "amlfs",
    })
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        mod.do_teardown("55")
    mock_client.auto_export_jobs.get.assert_not_called()
    assert Path(path).exists()
