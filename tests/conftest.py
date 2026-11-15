# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Test fixtures for amlfs_flow.

Synthesises an `amlfs_flow.config` module in `sys.modules` *before*
`amlfs_flow.__main__` is imported, so the helper can be tested without
a real cluster config or live Azure credentials.
"""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path

import pytest


def _install_fake_config(state_dir: str) -> types.ModuleType:
    """Build and register a synthetic `amlfs_flow.config` module."""
    m = types.ModuleType("amlfs_flow.config")
    m.SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"

    m.REGIONS = {
        "regionA": {
            "resource_group": "rg-home", "filesystem": "amlfs-home",
            "mountpoint": "/lustre", "location": "eastus",
        },
        "regionB": {
            "resource_group": "rg-multi-region", "filesystem": "amlfs-multi-region",
            "mountpoint": "/lustre", "location": "westus3",
        },
    }
    m.HOME_REGION = "regionA"
    m.PARTITION_REGIONS = {"hpc": "regionA", "multi-regionB": "regionB"}

    m.STATE_DIR = state_dir

    m.STAGEIN_CONFLICT_MODE = "Skip"
    m.STAGEOUT_CONFLICT_MODE = "OverwriteAlways"

    m.ARCHIVE_POLL_INTERVAL = 0
    m.IMPORT_POLL_INTERVAL = 0
    m.LRO_TIMEOUT = 5

    sys.modules["amlfs_flow.config"] = m
    return m


# Install the fake config + log file env override at *collection time*,
# before any test imports `amlfs_flow.__main__`.
_ARTIFACTS = Path(__file__).parent / ".pytest_cache" / "runtime"
_ARTIFACTS.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AMLFS_FLOW_LOG_FILE", str(_ARTIFACTS / "runtime.log"))
_install_fake_config(str(_ARTIFACTS / "state"))


def pytest_configure(config):
    if config.option.basetemp is None:
        config.option.basetemp = str(_ARTIFACTS / "pytest")


@pytest.fixture
def cfg():
    """Live reference to the fake `amlfs_flow.config` module."""
    return sys.modules["amlfs_flow.config"]


@pytest.fixture(autouse=True)
def state_dir(tmp_path, cfg):
    """Per-test STATE_DIR pointed at tmp_path."""
    original = cfg.STATE_DIR
    cfg.STATE_DIR = str(tmp_path)
    yield str(tmp_path)
    cfg.STATE_DIR = original


@pytest.fixture
def mod():
    """The amlfs_flow.__main__ module (imported lazily so conftest is set up)."""
    import amlfs_flow.__main__ as m
    yield m


@pytest.fixture
def owned_paths(monkeypatch, mod):
    """Routing/transfer tests use synthetic paths; access checks have dedicated tests."""
    monkeypatch.setattr(mod, "_validate_path_access", lambda path, uid, write=False:
                        types.SimpleNamespace(st_mode=stat.S_IFDIR))


@pytest.fixture(autouse=True)
def _reset_clients():
    """Drop lazily-built SDK clients between tests."""
    yield
    try:
        import amlfs_flow.__main__ as m
        m._clients.clear()
    except Exception:
        pass


@pytest.fixture
def mock_client(monkeypatch, mod):
    """A `MagicMock` standing in for `StorageCacheManagementClient`."""
    from unittest.mock import MagicMock
    client = MagicMock(name="StorageCacheManagementClient")
    monkeypatch.setattr(mod, "_get_client", lambda subscription_id=None: client)
    return client
