#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Install the CycleCloud extension; disabled by default, without service restarts."""

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import runpy
import shutil
import subprocess
import sys

PREFIX = Path("/opt/cyclecloud-multi-region")
CONFIG = Path("/etc/cyclecloud-multi-region/config.py")
LOG = Path("/var/log/slurm/amlfs_flow.log")
FSTAB = Path("/etc/fstab")
BEGIN = "# BEGIN cyclecloud-multi-region"
END = "# END cyclecloud-multi-region"
DEFAULTS = {
    "STATE_DIR": "/sched/cyclecloud-multi-region",
    "STAGEIN_CONFLICT_MODE": "Skip",
    "STAGEOUT_CONFLICT_MODE": "OverwriteAlways",
    "ARCHIVE_POLL_INTERVAL": 15,
    "IMPORT_POLL_INTERVAL": 15,
    "LRO_TIMEOUT": 10800,
}


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def jetpack(key, default=""):
    executable = shutil.which("jetpack") or "/opt/cycle/jetpack/bin/jetpack"
    result = run(executable, "config", key, str(default),
                 stdout=subprocess.PIPE, universal_newlines=True)
    return result.stdout.strip()


def boolean(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() not in ("true", "false"):
        raise ValueError("Expected true or false, got {!r}".format(value))
    return str(value).lower() == "true"


def validate_config(config):
    regions = config.get("REGIONS")
    if not isinstance(regions, dict) or not regions:
        raise ValueError("Enabled scheduler requires a nonempty REGIONS dictionary")
    for name, region in regions.items():
        if not isinstance(name, str) or not name or not isinstance(region, dict):
            raise ValueError("REGIONS entries must be named dictionaries")
        for field in ("resource_group", "filesystem", "location", "mountpoint"):
            if not isinstance(region.get(field), str) or not region[field].strip():
                raise ValueError("REGIONS[{}] requires {}".format(name, field))
        if not Path(region["mountpoint"]).is_absolute() or region["mountpoint"] == "/":
            raise ValueError("Each region mountpoint must be absolute and not /")
        if not region.get("subscription_id", config.get("SUBSCRIPTION_ID")):
            raise ValueError("Region {} requires subscription_id or SUBSCRIPTION_ID".format(name))
    if config.get("HOME_REGION") not in regions:
        raise ValueError("HOME_REGION must name a configured region")
    partitions = config.get("PARTITION_REGIONS")
    if not isinstance(partitions, dict) or not partitions:
        raise ValueError("PARTITION_REGIONS must explicitly map partitions to regions")
    if any(not isinstance(p, str) or not p or "," in p or r not in regions
           for p, r in partitions.items()):
        raise ValueError("PARTITION_REGIONS contains an invalid partition or region")
    if not Path(config["STATE_DIR"]).is_absolute() or config["STATE_DIR"] == "/":
        raise ValueError("STATE_DIR must be absolute and not /")
    for key in ("STAGEIN_CONFLICT_MODE", "STAGEOUT_CONFLICT_MODE"):
        if config[key] not in ("Skip", "Fail", "OverwriteAlways", "OverwriteIfDirty"):
            raise ValueError("Invalid " + key)


def config_text(config):
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in config):
        raise ValueError("Configuration keys must be uppercase Python identifiers")
    return '"""Managed CycleCloud Multi-Region configuration."""\n' + "".join(
        "{} = {!r}\n".format(key, value) for key, value in sorted(config.items())
    )


def write_file(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Follow CycleCloud's /etc/slurm/slurm.conf -> /sched/ccw/slurm.conf link.
    target = path.resolve() if path.is_symlink() else path
    if target.exists() and target.read_text() == text:
        return False
    existed = target.exists()
    target.write_text(text)
    if not existed:
        target.chmod(mode)
    return True


def render_lua(template, python):
    escaped = str(python).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
    if "\0" in escaped:
        raise ValueError("Interpreter path cannot contain NUL")
    if template.count("@PYTHON@") != 1:
        raise ValueError("Lua template must contain one @PYTHON@ interpreter placeholder")
    return template.replace("@PYTHON@", escaped)


def managed_block(text, content):
    pattern = r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?"
    cleaned = re.sub(pattern, "", text, flags=re.DOTALL)
    if not content:
        return cleaned
    return cleaned.rstrip("\n") + "\n" + BEGIN + "\n" + content + "\n" + END + "\n"


def slurm_configuration(path, enabled):
    if not path.exists():
        raise ValueError("Scheduler requires existing Slurm configuration: " + str(path))
    text = path.read_text()
    external = managed_block(text, "")
    types = re.findall(r"(?im)^\s*BurstBufferType\s*=\s*([^\s#]+)", external)
    if enabled and any(value != "burst_buffer/lua" for value in types):
        raise ValueError("Refusing to replace existing non-Lua BurstBufferType")
    return managed_block(text, "BurstBufferType=burst_buffer/lua" if enabled and not types else "")


def validate_mounts(mounts):
    if not isinstance(mounts, list):
        raise ValueError("mounts_json must be a JSON list")
    seen = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            raise ValueError("Each mount must be an object")
        source, target = mount.get("source", ""), mount.get("mountpoint", "")
        options = mount.get("options", "defaults,_netdev")
        if not re.fullmatch(r"[A-Za-z0-9.:\[\]_-]+@tcp[0-9]*:/[A-Za-z0-9_/-]+", source):
            raise ValueError("Mount source must be an explicit Lustre MGS@tcp:/filesystem")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", target) or ".." in Path(target).parts:
            raise ValueError("Mountpoint must be a safe absolute non-root path")
        if target in seen:
            raise ValueError("Duplicate mountpoint: " + target)
        if not re.fullmatch(r"[A-Za-z0-9_=,.-]+", options):
            raise ValueError("Invalid Lustre mount options")
        seen.add(target)


def mount_filesystems(mounts):
    if not mounts:
        return
    # Never guess a client package: the image must provide a client for this kernel.
    if not shutil.which("mount.lustre"):
        raise ValueError("Install a kernel-matched Lustre client in the image before enabling mounts")
    run("modprobe", "-n", "lustre")
    entries = []
    fstab = FSTAB
    original = fstab.read_text()
    external = managed_block(original, "")
    for mount in mounts:
        source, target = mount["source"], mount["mountpoint"]
        options = mount.get("options", "defaults,_netdev")
        for line in external.splitlines():
            fields = line.split()
            if fields and not fields[0].startswith("#") and len(fields) > 2 and fields[1] == target:
                if fields[0] != source or fields[2] != "lustre":
                    raise ValueError("Conflicting existing fstab entry for " + target)
        status = subprocess.run(
            ["findmnt", "-rn", "--mountpoint", target, "-o", "SOURCE,FSTYPE"],
            stdout=subprocess.PIPE, universal_newlines=True, check=False,
        )
        if status.returncode == 0:
            if status.stdout.split() != [source, "lustre"]:
                raise ValueError("Refusing to replace existing mount at " + target)
        elif status.returncode == 1:
            Path(target).mkdir(parents=True, exist_ok=True)
            run("mount", "-t", "lustre", "-o", options, source, target)
        else:
            raise ValueError("findmnt failed for " + target)
        if not any(len(line.split()) > 1 and not line.lstrip().startswith("#")
                   and line.split()[1] == target for line in external.splitlines()):
            entries.append("{} {} lustre {} 0 0".format(source, target, options))
    write_file(fstab, managed_block(original, "\n".join(entries)))


def install(args):
    source = Path(__file__).resolve().parents[1]
    prefix = PREFIX
    if os.geteuid() != 0:
        raise ValueError("Installation must run as root")
    if args.jetpack:
        ns = "cyclecloud.multi_region."
        args.enabled = boolean(jetpack(ns + "enabled", "false"))
        args.role = jetpack(ns + "role", "auto")
        args.config = jetpack(ns + "config_path") or None
        args.config_json = jetpack(ns + "config_json") or None
        args.mounts_json = jetpack(ns + "mounts_json", "[]")
        args.reconfigure = boolean(jetpack(ns + "reconfigure", "false"))
        if args.role == "auto":
            role = jetpack("slurm.role")
            args.role = "scheduler" if role == "scheduler" else "compute"
            if args.enabled and role not in ("scheduler", "execute"):
                raise ValueError("Set cyclecloud.multi_region.role to scheduler or compute")
    if args.role not in ("scheduler", "compute"):
        raise ValueError("--role must be scheduler or compute")
    config = dict(DEFAULTS)
    if args.config and args.config_json:
        raise ValueError("Supply config_path/--config OR config_json, not both")
    if args.config_json:
        values = json.loads(args.config_json)
        if not isinstance(values, dict):
            raise ValueError("config_json must be an object of runtime settings")
        config.update(values)
    elif args.config or CONFIG.exists():
        values = runpy.run_path(str(args.config or CONFIG))
        config.update({key: value for key, value in values.items() if key.isupper()})
    mounts = json.loads(args.mounts_json)
    validate_mounts(mounts)
    slurm_conf = Path(args.slurm_conf)
    new_slurm = None
    if args.role == "scheduler":
        if args.enabled:
            validate_config(config)
        if args.enabled or slurm_conf.exists():
            new_slurm = slurm_configuration(slurm_conf, args.enabled)
    account = None
    if args.enabled and args.role == "scheduler":
        users = re.findall(r"(?im)^\s*SlurmUser\s*=\s*([^\s#]+)", slurm_conf.read_text())
        account = pwd.getpwnam(users[-1] if users else "slurm")
    config_content = config_text(config)
    prefix.mkdir(parents=True, exist_ok=True)
    os.chown(str(prefix), 0, account.pw_gid if account else 0)
    prefix.chmod(0o750 if account else 0o700)
    package = prefix / "amlfs_flow"
    package.mkdir(exist_ok=True)
    os.chown(str(package), 0, account.pw_gid if account else 0)
    package.chmod(0o750 if account else 0o700)
    for path in (source / "amlfs_flow").glob("*.py"):
        if path.name != "config.py":
            target = package / path.name
            shutil.copy2(str(path), str(target))
            os.chown(str(target), 0, account.pw_gid if account else 0)
            target.chmod(0o640 if account else 0o600)
    write_file(CONFIG, config_content)
    os.chown(str(CONFIG.parent), 0, account.pw_gid if account else 0)
    CONFIG.parent.chmod(0o750 if account else 0o700)
    os.chown(str(CONFIG), 0, account.pw_gid if account else 0)
    CONFIG.chmod(0o640 if account else 0o600)
    config_link = package / "config.py"
    if config_link.is_symlink() or config_link.exists():
        config_link.unlink()
    config_link.symlink_to(CONFIG)
    if args.enabled:
        mount_filesystems(mounts)
    if args.enabled and args.role == "scheduler":
        venv = prefix / "venv"
        if not (venv / "bin/python").exists():
            run(sys.executable, "-m", "venv", venv)
        pip = [venv / "bin/python", "-m", "pip", "install", "--disable-pip-version-check",
               "-r", source / "requirements.txt"]
        wheelhouse = Path(args.wheelhouse) if args.wheelhouse else source / "wheelhouse"
        if args.wheelhouse or wheelhouse.exists():
            pip += ["--no-index", "--find-links", wheelhouse]
        run(*pip)
        site = run(venv / "bin/python", "-c",
                   "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                   stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        write_file(Path(site) / "amlfs_flow.pth", str(prefix) + "\n")
        run(venv / "bin/python", "-I", "-c",
            "from amlfs_flow import __main__ as runtime; runtime._regions()")
        state = Path(config["STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        os.chown(str(state), account.pw_uid, account.pw_gid)
        state.chmod(0o700)
        log = LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch(exist_ok=True)
        os.chown(str(log), account.pw_uid, account.pw_gid)
        log.chmod(0o600)
        write_file(slurm_conf.parent / "burst_buffer.lua",
                   render_lua((source / "slurm/burst_buffer.lua").read_text(),
                              venv / "bin/python"))
        write_file(slurm_conf.parent / "burst_buffer.conf",
                   (source / "slurm/burst_buffer.conf").read_text())
    changed = False
    if new_slurm is not None:
        changed = write_file(slurm_conf, new_slurm)
    write_file(prefix / "installation.json", json.dumps({
        "project": "cyclecloud-multi-region", "version": "0.2.0",
        "enabled": args.enabled, "role": args.role,
    }, sort_keys=True) + "\n")
    if args.reconfigure and args.role == "scheduler":
        run("scontrol", "reconfigure")
    print("cyclecloud-multi-region 0.2.0 installed: role={}, enabled={}".format(args.role, args.enabled))
    if changed and not args.reconfigure:
        print("Slurm configuration changed; run sudo scontrol reconfigure after validation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jetpack", action="store_true", help="Read CycleCloud node configuration")
    parser.add_argument("--enabled", action="store_true", help="Enable mounts and scheduler hooks")
    parser.add_argument("--role", choices=("scheduler", "compute"), default="scheduler")
    parser.add_argument("--config", help="Existing Python configuration file")
    parser.add_argument("--config-json", help="JSON object of runtime settings")
    parser.add_argument("--mounts-json", default="[]", help="JSON list of explicit Lustre mounts")
    parser.add_argument("--wheelhouse", help="Offline dependency directory (no package index)")
    parser.add_argument("--slurm-conf", default="/etc/slurm/slurm.conf")
    parser.add_argument("--reconfigure", action="store_true", help="Run scontrol reconfigure")
    try:
        install(parser.parse_args())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, "cyclecloud-multi-region install failed: {}\n".format(exc))


if __name__ == "__main__":
    main()
