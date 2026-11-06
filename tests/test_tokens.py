# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for continuation-token persistence helpers."""

from __future__ import annotations

import os


def test_save_and_load_token_roundtrip(mod, state_dir):
    mod._save_token("42", "stagein-import", "TOKEN-ABC")
    assert mod._load_token("42", "stagein-import") == "TOKEN-ABC"


def test_load_returns_none_when_file_missing(mod, state_dir):
    assert mod._load_token("nope", "stagein-import") is None


def test_clear_token_removes_file(mod, state_dir):
    mod._save_token("99", "stageout-import", "T")
    path = mod._token_path("99", "stageout-import")
    assert os.path.exists(path)
    mod._clear_token("99", "stageout-import")
    assert not os.path.exists(path)


def test_clear_token_is_idempotent(mod, state_dir):
    # must not raise even if the file never existed
    mod._clear_token("ghost", "stagein-import")


def test_token_path_is_per_job_and_phase(mod, state_dir):
    p1 = mod._token_path("1", "a")
    p2 = mod._token_path("1", "b")
    p3 = mod._token_path("2", "a")
    assert p1 != p2 != p3
    assert all(state_dir in p for p in (p1, p2, p3))
