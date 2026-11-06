# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Installer tests stage files locally and never change host services or mounts."""

import argparse
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("corrupt", [False, True])
def test_cluster_init_uses_native_blobs_and_checks_integrity(tmp_path, corrupt):
    source = tmp_path / "source"
    source.mkdir()
    data = b"print('INSTALLER_EXECUTED')\n"
    payload = source / "cyclecloud-multi-region.tar.gz"
    with tarfile.open(payload, "w:gz") as archive:
        member = tarfile.TarInfo("scripts/install.py")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    digest = "0" * 64 if corrupt else hashlib.sha256(payload.read_bytes()).hexdigest()
    (source / "cyclecloud-multi-region.tar.gz.sha256").write_text(digest + "  " + payload.name + "\n")
    spec = tmp_path / "spec"
    (spec / "files").mkdir(parents=True)
    (spec / "files/cyclecloud-multi-region.tar.gz").write_bytes(b"obsolete payload must not be used")
    commands = tmp_path / "bin"
    commands.mkdir()
    jetpack = commands / "jetpack"
    jetpack.write_text(
        '#!/bin/bash\nset -eu\n'
        'test "$1" = download && test "$2" = --project && test "$3" = cyclecloud-multi-region\n'
        'printf "%s\\n" "$4" >> "$DOWNLOAD_LOG"\n'
        'cp "$BLOB_SOURCE/$4" "$5/$4"\n')
    jetpack.chmod(0o755)
    script = (ROOT / "specs/default/cluster-init/scripts/20-install-cyclecloud-multi-region.sh").read_text()
    script = script.replace("/opt/cyclecloud-multi-region", str(tmp_path / "installed"))
    environment = dict(os.environ, CYCLECLOUD_SPEC_PATH=str(spec), BLOB_SOURCE=str(source),
                       DOWNLOAD_LOG=str(tmp_path / "downloads.log"),
                       PATH=str(commands) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)
    if corrupt:
        assert result.returncode != 0 and "INSTALLER_EXECUTED" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert "INSTALLER_EXECUTED" in result.stdout
    assert (tmp_path / "downloads.log").read_text().splitlines() == [
        "cyclecloud-multi-region.tar.gz", "cyclecloud-multi-region.tar.gz.sha256"]


def load_script(name, directory="scripts"):
    spec = importlib.util.spec_from_file_location(name, ROOT / directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    path = ROOT / ".test-work" / str(uuid.uuid4())
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path)
    try:
        path.parent.rmdir()
    except OSError:
        pass


@pytest.fixture
def installer(workspace, monkeypatch):
    module = load_script("install")
    monkeypatch.setattr(module, "PREFIX", workspace / "opt")
    monkeypatch.setattr(module, "CONFIG", workspace / "etc/config.py")
    monkeypatch.setattr(module, "LOG", workspace / "logs/runtime.log")
    monkeypatch.setattr(module, "FSTAB", workspace / "fstab")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    monkeypatch.setattr(module.pwd, "getpwnam",
                        lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    return module


def config(workspace):
    return {
        "SUBSCRIPTION_ID": "sub",
        "HOME_REGION": "home",
        "REGIONS": {
            "home": {"resource_group": "rg", "filesystem": "fs-home",
                     "location": "regiona", "mountpoint": "/lustre"},
            "remote": {"resource_group": "rg", "filesystem": "fs-remote",
                       "location": "regionb", "mountpoint": "/lustre"},
        },
        "PARTITION_REGIONS": {"hpc": "home", "multi-regionB": "remote"},
        "STATE_DIR": str(workspace / "state"),
    }


def arguments(workspace, **overrides):
    values = dict(jetpack=False, enabled=False, role="scheduler", config=None,
                  config_json=None, mounts_json="[]", reconfigure=False,
                  wheelhouse=None, slurm_conf=str(workspace / "etc/slurm/slurm.conf"))
    values.update(overrides)
    return argparse.Namespace(**values)


def test_disabled_install_is_repeatable_without_network_or_slurm(installer, workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled install invoked a command")
    monkeypatch.setattr(installer, "run", forbidden)
    args = arguments(workspace)
    installer.install(args)
    first = installer.CONFIG.read_bytes()
    installer.install(args)
    assert installer.CONFIG.read_bytes() == first
    assert (installer.PREFIX / "amlfs_flow/config.py").resolve() == installer.CONFIG
    assert not Path(args.slurm_conf).exists()
    assert not (installer.PREFIX / "venv").exists()
    assert json.loads((installer.PREFIX / "installation.json").read_text())["enabled"] is False


def test_enabled_requires_config_before_writing(installer, workspace):
    with pytest.raises(ValueError, match="REGIONS"):
        installer.install(arguments(workspace, enabled=True))
    assert not installer.PREFIX.exists()


def test_enabled_scheduler_preserves_symlink_and_is_idempotent(installer, workspace, monkeypatch):
    ownership = []
    monkeypatch.setattr(installer.os, "chown",
                        lambda path, uid, gid: ownership.append((str(path), uid, gid)))
    target = workspace / "sched/ccw/slurm.conf"
    target.parent.mkdir(parents=True)
    original = "ClusterName=current\nSlurmUser=slurm\n"
    target.write_text(original)
    target.chmod(0o640)
    link = workspace / "etc/slurm/slurm.conf"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    site = workspace / "site-packages"
    commands = []

    def fake_run(*args, **kwargs):
        commands.append(list(map(str, args)))
        return SimpleNamespace(stdout=str(site) + "\n")

    monkeypatch.setattr(installer, "run", fake_run)
    args = arguments(workspace, enabled=True, config_json=json.dumps(config(workspace)),
                     wheelhouse=str(workspace / "wheels"))
    installer.install(args)
    expected = target.read_text()
    installer.install(args)
    assert link.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o640
    assert target.read_text() == expected
    assert expected.count("BurstBufferType=burst_buffer/lua") == 1
    assert original in expected
    assert (link.parent / "burst_buffer.lua").read_text().count("@PYTHON@") == 0
    assert any("--no-index" in command for command in commands)
    assert all("reconfigure" not in command for command in commands)
    assert installer.PREFIX.stat().st_mode & 0o777 == 0o750
    assert (installer.PREFIX / "amlfs_flow").stat().st_mode & 0o777 == 0o750
    assert (installer.PREFIX / "amlfs_flow/__main__.py").stat().st_mode & 0o777 == 0o640
    assert installer.CONFIG.stat().st_mode & 0o777 == 0o640
    for path in (installer.PREFIX, installer.PREFIX / "amlfs_flow",
                 installer.PREFIX / "amlfs_flow/__main__.py", installer.CONFIG):
        assert (str(path), 0, 1000) in ownership
    assert (workspace / "state").stat().st_mode & 0o777 == 0o700
    assert installer.LOG.stat().st_mode & 0o777 == 0o600
    installer.install(arguments(workspace))
    assert link.is_symlink()
    assert target.read_text() == original


@pytest.mark.parametrize("user", ["root", "scheduler-service"])
def test_scheduler_accepts_configured_service_account(installer, workspace, monkeypatch, user):
    args = arguments(workspace, enabled=True, config_json=json.dumps(config(workspace)))
    path = Path(args.slurm_conf)
    path.parent.mkdir(parents=True)
    path.write_text(f"SlurmUser={user}\n")
    accounts = []
    def account(name):
        accounts.append(name)
        return SimpleNamespace(pw_uid=0 if user == "root" else 123, pw_gid=123)
    monkeypatch.setattr(installer.pwd, "getpwnam", account)
    monkeypatch.setattr(installer, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=str(workspace / "site")))
    installer.install(args)
    assert accounts == [user]
    assert installer.PREFIX.exists()


def test_compute_only_mounts_without_slurm_or_sdk(installer, workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "mount_filesystems", lambda mounts: calls.extend(mounts))
    def forbidden(*args, **kwargs):
        pytest.fail("Compute install tried installing scheduler dependencies")
    monkeypatch.setattr(installer, "run", forbidden)
    mounts = [{"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre"}]
    args = arguments(workspace, enabled=True, role="compute", mounts_json=json.dumps(mounts))
    installer.install(args)
    assert calls == mounts
    assert not Path(args.slurm_conf).exists()


def test_install_from_checkout_validates_installed_package(installer, workspace, monkeypatch):
    source = workspace / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "amlfs_flow").mkdir()
    (source / "amlfs_flow/__init__.py").write_text("")
    (source / "amlfs_flow/__main__.py").write_text(
        "from . import config\n"
        "def _regions():\n"
        "    assert config.HOME_REGION == 'home'\n"
    )
    shutil.copytree(ROOT / "slurm", source / "slurm")
    (source / "requirements.txt").write_text("")
    monkeypatch.setattr(installer, "__file__", str(source / "scripts/install.py"))
    monkeypatch.chdir(ROOT)
    args = arguments(workspace, enabled=True, config_json=json.dumps(config(workspace)))
    path = Path(args.slurm_conf)
    path.parent.mkdir(parents=True)
    path.write_text("ClusterName=current\nSlurmUser=slurm\n")
    installer.install(args)
    assert json.loads((installer.PREFIX / "installation.json").read_text())["enabled"] is True


def test_conflicting_slurm_plugin_not_replaced(installer, workspace):
    path = workspace / "slurm.conf"
    path.write_text("BurstBufferType=burst_buffer/datawarp\n")
    with pytest.raises(ValueError, match="Refusing"):
        installer.slurm_configuration(path, True)
    assert path.read_text() == "BurstBufferType=burst_buffer/datawarp\n"


def test_lua_interpreter_is_escaped(installer):
    assert installer.render_lua('local PYTHON = "@PYTHON@"', '/opt/a"b\\c/python') == (
        'local PYTHON = "/opt/a\\"b\\\\c/python"'
    )


@pytest.mark.parametrize("mount", [
    {"source": "server:/not-lustre", "mountpoint": "/lustre"},
    {"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/"},
    {"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre/../etc"},
    {"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre", "options": "a\nb"},
])
def test_invalid_mount_rejected_before_commands(installer, mount):
    with pytest.raises(ValueError):
        installer.validate_mounts([mount])


def test_matching_existing_lustre_mount_is_not_remounted(installer, monkeypatch):
    installer.FSTAB.write_text("# Existing host config\n")
    calls = []
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/sbin/" + name)
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="10.0.0.4@tcp:/lustrefs lustre\n"))
    mounts = [{"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre"}]
    installer.mount_filesystems(mounts)
    content = installer.FSTAB.read_text()
    installer.mount_filesystems(mounts)
    assert installer.FSTAB.read_text() == content
    assert content.count("10.0.0.4@tcp:/lustrefs") == 1
    assert all(command[0] == "modprobe" for command in calls)


def test_conflicting_existing_mount_is_not_replaced(installer, monkeypatch):
    installer.FSTAB.write_text("")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/sbin/" + name)
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="/dev/sdb1 ext4\n"))
    with pytest.raises(ValueError, match="Refusing"):
        installer.mount_filesystems([{"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre"}])
    assert installer.FSTAB.read_text() == ""


def test_jetpack_disabled_unknown_role_is_safe(installer, workspace, monkeypatch):
    monkeypatch.setattr(installer, "jetpack", lambda key, default="": default)
    installer.install(arguments(workspace, jetpack=True))
    record = json.loads((installer.PREFIX / "installation.json").read_text())
    assert record["role"] == "compute"
    assert not record["enabled"]


@pytest.mark.parametrize("role", ["master", "compute"])
def test_auto_role_rejects_obsolete_native_aliases(installer, workspace, monkeypatch, role):
    values = {"cyclecloud.multi_region.enabled": "true", "slurm.role": role}
    monkeypatch.setattr(installer, "jetpack", lambda key, default="": values.get(key, default))
    with pytest.raises(ValueError, match="role to scheduler or compute"):
        installer.install(arguments(workspace, jetpack=True))
    assert not installer.PREFIX.exists()


def test_missing_lustre_client_fails_before_mounting(installer, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="kernel-matched Lustre client"):
        installer.mount_filesystems([{"source": "10.0.0.4@tcp:/lustrefs", "mountpoint": "/lustre"}])


@pytest.mark.parametrize("change", [
    {"HOME_REGION": "missing"},
    {"PARTITION_REGIONS": {"multi-regionB": "missing"}},
    {"SUBSCRIPTION_ID": ""},
])
def test_invalid_region_configuration_rejected(installer, workspace, change):
    values = dict(installer.DEFAULTS, **config(workspace))
    values.update(change)
    with pytest.raises(ValueError):
        installer.validate_config(values)
