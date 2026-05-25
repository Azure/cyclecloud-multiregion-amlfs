"""Tests for the `_archive` polling state machine."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def _make_fs_with_states(states):
    """Build a sequence of fake AMLFS objects, one per poll cycle.

    Each ``states[i]`` is a string (the archive state) or ``None`` to
    indicate the status entry has been removed from `hsm.archive_status`.
    """
    snapshots = []
    for s in states:
        fs = MagicMock(name=f"fs[{s}]")
        if s is None:
            fs.hsm.archive_status = []
        else:
            entry = {
                "filesystem_path": "/data/run42",
                "status": {"state": s, "percent_complete": 50 if s == "InProgress" else 100},
            }
            fs.hsm.archive_status = [entry]
        snapshots.append(fs)
    return snapshots


def test_archive_returns_when_state_completed(mod, mock_client):
    mock_client.aml_filesystems.get.side_effect = _make_fs_with_states(
        ["InProgress", "Completed"]
    )
    mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")
    mock_client.aml_filesystems.archive.assert_called_once()


def test_archive_returns_when_status_entry_disappears(mod, mock_client):
    mock_client.aml_filesystems.get.side_effect = _make_fs_with_states([None])
    mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")


def test_archive_raises_on_failed_state(mod, mock_client):
    mock_client.aml_filesystems.get.side_effect = _make_fs_with_states(["Failed"])
    with pytest.raises(RuntimeError, match="Failed"):
        mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")


def test_archive_raises_on_canceled_state(mod, mock_client):
    mock_client.aml_filesystems.get.side_effect = _make_fs_with_states(["Canceled"])
    with pytest.raises(RuntimeError, match="Canceled"):
        mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")


def test_archive_times_out_if_never_completes(mod, mock_client, cfg, monkeypatch):
    # Always report InProgress, and shrink the timeout to force a fast failure.
    cfg.LRO_TIMEOUT = 0.01
    cfg.ARCHIVE_POLL_INTERVAL = 0
    mock_client.aml_filesystems.get.return_value = _make_fs_with_states(["InProgress"])[0]
    with pytest.raises(RuntimeError, match="timed out"):
        mod._archive("rg", "amlfs", "/data/run42", "1", "stagein-archive")
