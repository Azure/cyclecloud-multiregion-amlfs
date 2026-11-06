# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Tests for small pure helpers."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock
import errno
import os
import stat
import struct
import sys
from types import SimpleNamespace

from azure.core.exceptions import HttpResponseError


@pytest.fixture
def staging_paths(mod, cfg, monkeypatch, tmp_path):
    # Isolate permission metadata mocks from pytest's own filesystem operations.
    monkeypatch.setattr(mod, "os", SimpleNamespace(**{
        name: getattr(os, name) for name in dir(os) if not name.startswith("__")
    }))
    mount = tmp_path / "mount"
    mount.mkdir()
    folder = mount / "project"
    folder.mkdir()
    monkeypatch.setattr(mod.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_name="submitter", pw_gid=folder.stat().st_gid))
    monkeypatch.setattr(mod.os, "getgrouplist", lambda name, gid: [gid])
    for region in cfg.REGIONS.values():
        monkeypatch.setitem(region, "mountpoint", str(mount))
    archive, hydrate = MagicMock(), MagicMock()
    monkeypatch.setattr(mod, "_archive", archive)
    monkeypatch.setattr(mod, "_import_data", hydrate)
    return mount, folder, archive, hydrate


@pytest.mark.parametrize("command", ["stage-in", "stage-out"])
def test_staging_checks_submitter_not_process_uid(mod, staging_paths, monkeypatch, command):
    _, folder, archive, hydrate = staging_paths
    uid = folder.stat().st_uid
    monkeypatch.setattr(mod.os, "getuid", lambda: uid + 1)
    monkeypatch.setattr(mod.os, "geteuid", lambda: uid + 1)
    monkeypatch.setattr(sys, "argv", [
        "amlfs_flow", command, "--job", "42", "--partition", "multi-regionB",
        "--uid", str(uid), "--paths", str(folder),
    ])
    mod.main()
    archive.assert_called_once()
    hydrate.assert_called_once()


@pytest.mark.parametrize("command", ["stage-in", "stage-out"])
def test_permission_denial_prints_error_without_transfer_or_state(
    mod, staging_paths, monkeypatch, capsys, command,
):
    mount, folder, archive, hydrate = staging_paths
    folder.chmod(0o700)
    uid = folder.stat().st_uid + 1
    monkeypatch.setattr(sys, "argv", [
        "amlfs_flow", command, "--job", "42", "--partition", "multi-regionB",
        "--uid", str(uid), "--paths", str(folder),
    ])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert f"ERROR: Staging denied: submitter UID {uid}" in error
    assert str(folder) in error and "permission" in error
    archive.assert_not_called()
    hydrate.assert_not_called()
    assert list(mount.parent.iterdir()) == [mount]


@pytest.mark.parametrize("method", ["do_stage_in", "do_stage_out"])
def test_all_paths_checked_before_any_transfer(mod, staging_paths, method):
    mount, folder, archive, hydrate = staging_paths
    with pytest.raises(PermissionError, match="Cannot verify staging path"):
        getattr(mod, method)("42", [str(folder), str(mount / "missing")],
                             "multi-regionB", folder.stat().st_uid)
    archive.assert_not_called()
    hydrate.assert_not_called()
    assert list(mount.parent.iterdir()) == [mount]


def test_symlink_does_not_bypass_target_permissions(mod, staging_paths):
    mount, folder, archive, _ = staging_paths
    link = mount / "link"
    link.symlink_to(folder, target_is_directory=True)
    folder.chmod(0o700)
    with pytest.raises(PermissionError, match="permission"):
        mod.do_stage_in("42", [str(link)], "multi-regionB", folder.stat().st_uid + 1)
    archive.assert_not_called()


def no_acl(*args):
    raise OSError(errno.ENODATA, "No access ACL")


@pytest.mark.parametrize("owner,group,mode,groups,required,allowed", [
    (123, 90, 0o400, {88}, os.R_OK, True),
    (123, 90, 0o077, {88}, os.R_OK, False),
    (999, 88, 0o050, {77, 88}, os.R_OK | os.X_OK, True),
    (999, 77, 0o070, {77}, os.R_OK | os.W_OK | os.X_OK, True),
    (999, 88, 0o007, {88}, os.R_OK, False),
    (999, 90, 0o005, {88}, os.R_OK | os.X_OK, True),
    (999, 90, 0o005, {88}, os.W_OK, False),
])
def test_posix_owner_group_and_other_permissions(
    mod, monkeypatch, owner, group, mode, groups, required, allowed,
):
    monkeypatch.setattr(mod.os, "getxattr", no_acl)
    info = SimpleNamespace(st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)
    assert mod._path_allows("/shared", info, 123, groups, required) is allowed


def access_acl(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


@pytest.mark.parametrize("entries,groups,required,allowed", [
    ([(2, 5, 123)], {77}, 5, True),
    ([(2, 0, 123)], {77}, 4, False),
    ([(8, 7, 88)], {77, 88}, 7, True),
    ([(8, 0, 88)], {77, 88}, 4, False),
    ([(8, 4, 88), (8, 2, 99)], {88, 99}, 6, False),
    ([(2, 4, 123), (8, 7, 88)], {88}, 6, False),
])
def test_posix_acl_named_user_and_supplementary_groups(
    mod, monkeypatch, entries, groups, required, allowed,
):
    acl = access_acl((1, 7, 0xffffffff), *entries, (4, 0, 0xffffffff),
                     (16, 7, 0xffffffff), (32, 7, 0xffffffff))
    monkeypatch.setattr(mod.os, "getxattr", lambda *args: acl)
    info = SimpleNamespace(st_uid=999, st_gid=90, st_mode=stat.S_IFDIR | 0o777)
    assert mod._path_allows("/shared", info, 123, groups, required) is allowed


def test_acl_mask_limits_group_grant(mod, monkeypatch):
    acl = access_acl((1, 7, 0xffffffff), (4, 0, 0xffffffff), (8, 7, 88),
                     (16, 5, 0xffffffff), (32, 0, 0xffffffff))
    monkeypatch.setattr(mod.os, "getxattr", lambda *args: acl)
    info = SimpleNamespace(st_uid=999, st_gid=90, st_mode=stat.S_IFDIR | 0o750)
    assert mod._path_allows("/shared", info, 123, {88}, 5)
    assert not mod._path_allows("/shared", info, 123, {88}, 7)


@pytest.mark.parametrize("acl", [b"", struct.pack("<I", 99), access_acl((8, 7, 88))])
def test_unrecognized_acl_fails_closed(mod, monkeypatch, acl):
    monkeypatch.setattr(mod.os, "getxattr", lambda *args: acl)
    info = SimpleNamespace(st_uid=999, st_gid=90, st_mode=stat.S_IFDIR | 0o777)
    with pytest.raises(ValueError, match="ACL"):
        mod._path_allows("/shared", info, 123, {88}, 4)


@pytest.mark.parametrize("write,allowed", [(False, True), (True, False)])
def test_group_access_and_ancestor_traversal(mod, staging_paths, monkeypatch, write, allowed):
    mount, folder, _, _ = staging_paths
    uid = 123
    monkeypatch.setattr(mod.pwd, "getpwuid",
                        lambda value: SimpleNamespace(pw_name="submitter", pw_gid=77))
    groups = MagicMock(return_value=[77, 88])
    monkeypatch.setattr(mod.os, "getgrouplist", groups)
    monkeypatch.setattr(mod.os, "getxattr", no_acl)
    def metadata(path):
        return SimpleNamespace(st_uid=999, st_gid=88, st_mode=stat.S_IFDIR | (
            0o050 if str(path) == str(folder) else 0o010))
    monkeypatch.setattr(mod.os, "stat", metadata)
    if allowed:
        mod._validate_path_access(str(folder), uid, write=write)
    else:
        with pytest.raises(PermissionError, match="read/write/traverse"):
            mod._validate_path_access(str(folder), uid, write=write)
    groups.assert_called_once_with("submitter", 77)
    monkeypatch.setattr(mod.os, "stat", lambda path: SimpleNamespace(
        st_uid=999, st_gid=88, st_mode=stat.S_IFDIR | (0 if str(path) == str(mount) else 0o070)))
    with pytest.raises(PermissionError, match="traverse"):
        mod._validate_path_access(str(folder), uid)


def test_acl_read_errors_do_not_fall_back_to_mode_bits(mod, monkeypatch):
    def denied(*args):
        raise PermissionError(errno.EACCES, "ACL unreadable")
    monkeypatch.setattr(mod.os, "getxattr", denied)
    info = SimpleNamespace(st_uid=999, st_gid=90, st_mode=stat.S_IFDIR | 0o777)
    with pytest.raises(PermissionError):
        mod._path_allows("/shared", info, 123, {88}, 4)


def test_unknown_submitter_fails_before_transfer(mod, staging_paths, monkeypatch):
    _, folder, archive, hydrate = staging_paths
    def unknown(uid):
        raise KeyError(uid)
    monkeypatch.setattr(mod.pwd, "getpwuid", unknown)
    with pytest.raises(PermissionError, match="Cannot resolve submitter UID"):
        mod.do_stage_in("42", [str(folder)], "multi-regionB", 123)
    archive.assert_not_called()
    hydrate.assert_not_called()


@pytest.mark.parametrize("uid", [None, -1, "123", True])
def test_invalid_uid_fails_closed(mod, staging_paths, uid):
    _, folder, archive, _ = staging_paths
    with pytest.raises(ValueError, match="submitter UID"):
        mod.do_stage_in("42", [str(folder)], "multi-regionB", uid)
    archive.assert_not_called()


@pytest.mark.parametrize("command", ["stage-in", "stage-out"])
def test_cli_requires_submitter_uid(mod, staging_paths, monkeypatch, capsys, command):
    _, folder, archive, _ = staging_paths
    monkeypatch.setattr(sys, "argv", [
        "amlfs_flow", command, "--job", "42", "--partition", "multi-regionB",
        "--paths", str(folder),
    ])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
    assert "--uid" in capsys.readouterr().err
    archive.assert_not_called()


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


# ─── _relative_path ─────────────────────────────────────────────────────


def test_relative_path_strips_mount(mod):
    assert mod._relative_path("/lustre/data/run42", "/lustre") == "/data/run42"


def test_relative_path_keeps_leading_slash(mod):
    out = mod._relative_path("/lustre/x", "/lustre")
    assert out.startswith("/")


def test_relative_path_rejects_paths_outside_mount(mod):
    with pytest.raises(ValueError):
        mod._relative_path("/elsewhere/data", "/lustre")


def test_relative_path_trailing_slash_on_mount(mod):
    assert mod._relative_path("/lustre/data", "/lustre/") == "/data"
