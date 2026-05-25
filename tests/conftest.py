"""Test fixtures for amlfs_flow.

Synthesises an `amlfs_flow.config` module in `sys.modules` *before*
`amlfs_flow.__main__` is imported, so the helper can be tested without
a real cluster config or live Azure credentials.
"""

from __future__ import annotations

import os
import sys
import types
import tempfile

import pytest


def _install_fake_config(state_dir: str) -> types.ModuleType:
    """Build and register a synthetic `amlfs_flow.config` module."""
    m = types.ModuleType("amlfs_flow.config")
    m.SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"

    m.AMLFS_PRIMARY_RESOURCE_GROUP = "rg-primary"
    m.AMLFS_PRIMARY_NAME = "amlfs-primary"
    m.AMLFS_PRIMARY_MOUNT = "/lustre"

    m.AMLFS_SECONDARY_RESOURCE_GROUP = "rg-secondary"
    m.AMLFS_SECONDARY_NAME = "amlfs-secondary"
    m.AMLFS_SECONDARY_MOUNT = "/lustre"

    m.BLOB_STAGE_PREFIX = ""

    m.PRIMARY_PARTITION = "hpc"
    m.SECONDARY_PARTITION = "htc"

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
_TMP_LOG = tempfile.NamedTemporaryFile(prefix="amlfs_flow_test_", suffix=".log",
                                       delete=False)
_TMP_LOG.close()
os.environ.setdefault("AMLFS_FLOW_LOG_FILE", _TMP_LOG.name)

_TMP_STATE = tempfile.mkdtemp(prefix="amlfs_flow_state_")
_install_fake_config(_TMP_STATE)


@pytest.fixture
def cfg():
    """Live reference to the fake `amlfs_flow.config` module."""
    return sys.modules["amlfs_flow.config"]


@pytest.fixture
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


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Drop the lazily-built SDK client between tests."""
    yield
    try:
        import amlfs_flow.__main__ as m
        m._client = None
    except Exception:
        pass


@pytest.fixture
def mock_client(monkeypatch, mod):
    """A `MagicMock` standing in for `StorageCacheManagementClient`."""
    from unittest.mock import MagicMock
    client = MagicMock(name="StorageCacheManagementClient")
    monkeypatch.setattr(mod, "_get_client", lambda: client)
    return client
