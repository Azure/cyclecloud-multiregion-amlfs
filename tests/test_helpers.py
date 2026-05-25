"""Tests for small pure helpers."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from azure.core.exceptions import HttpResponseError


# ─── _is_retryable ──────────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_is_retryable_returns_true_for_throttling_and_5xx(mod, status_code):
    exc = HttpResponseError(message="boom", response=MagicMock(status_code=status_code))
    exc.status_code = status_code
    assert mod._is_retryable(exc) is True


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409])
def test_is_retryable_returns_false_for_client_errors(mod, status_code):
    exc = HttpResponseError(message="nope", response=MagicMock(status_code=status_code))
    exc.status_code = status_code
    assert mod._is_retryable(exc) is False


def test_is_retryable_returns_false_for_non_http_exceptions(mod):
    assert mod._is_retryable(ValueError("not http")) is False
    assert mod._is_retryable(RuntimeError("nope")) is False


# ─── _blob_prefix ───────────────────────────────────────────────────────


def test_blob_prefix_is_identity(mod):
    assert mod._blob_prefix("/data/run42") == "/data/run42"
    assert mod._blob_prefix("") == ""


# ─── _relative_path ─────────────────────────────────────────────────────


def test_relative_path_strips_mount(mod):
    # default cfg.AMLFS_PRIMARY_MOUNT == "/lustre"
    assert mod._relative_path("/lustre/data/run42") == "/data/run42"


def test_relative_path_keeps_leading_slash(mod):
    out = mod._relative_path("/lustre/x")
    assert out.startswith("/")


def test_relative_path_rejects_paths_outside_mount(mod):
    with pytest.raises(ValueError):
        mod._relative_path("/elsewhere/data")


def test_relative_path_trailing_slash_on_mount(mod, cfg):
    cfg.AMLFS_PRIMARY_MOUNT = "/lustre/"
    try:
        assert mod._relative_path("/lustre/data") == "/data"
    finally:
        cfg.AMLFS_PRIMARY_MOUNT = "/lustre"
