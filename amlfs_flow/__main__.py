#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Azure CycleCloud Multi-region plugin for AMLFS: Slurm AMLFS data movement.

Usage (called by burst_buffer.lua):
    python -m amlfs_flow stage-in  --job <id> --uid <submitter-uid> --partition <name> --paths /lustre/proj/input [...]
    python -m amlfs_flow stage-out --job <id> --uid <submitter-uid> --partition <name> --paths /lustre/proj/output [...]
    python -m amlfs_flow teardown  --job <id>
    python -m amlfs_flow selftest

Exit 0 → success; non-zero → failure (stderr shown in Slurm BB error).
"""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from enum import Enum
import errno
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import pwd
import re
import stat
import struct
import sys
import time
import uuid

from azure.identity import DefaultAzureCredential
from azure.mgmt.storagecache import StorageCacheManagementClient
from azure.mgmt.storagecache.models import (
    AutoExportJob,
    AutoExportJobProperties,
    AutoExportJobUpdate,
    AutoExportJobUpdateProperties,
    ImportJob,
    ImportJobProperties,
)
from azure.core.exceptions import HttpResponseError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)


def _load_config():
    path = os.environ.get("AMLFS_FLOW_CONFIG")
    if not path:
        from . import config
        return config
    if not os.path.isabs(path):
        raise ValueError("AMLFS_FLOW_CONFIG must be an absolute Python config path")
    spec = importlib.util.spec_from_file_location("amlfs_flow.config", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load Python config from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cfg = _load_config()

# ── Logging ─────────────────────────────────────────────────────────────
LOG_FILE = os.environ.get("AMLFS_FLOW_LOG_FILE", "/var/log/slurm/amlfs_flow.log")

_handlers = [logging.StreamHandler(sys.stderr)]
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    _handlers.insert(0, logging.FileHandler(LOG_FILE))
except (OSError, PermissionError):
    pass  # CI / dev environments where /var/log/slurm is unwritable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("amlfs_flow")


# ── Retry predicate ────────────────────────────────────────────────────
def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpResponseError):
        return exc.status_code in (429, 500, 502, 503, 504)
    return False


_retry_sdk = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=2, min=4, max=120),
    stop=stop_after_attempt(8),
    reraise=True,
)


# ── SDK clients (lazy, per subscription) ───────────────────────────────
_clients = {}


def _get_client(subscription_id=None) -> StorageCacheManagementClient:
    subscription_id = subscription_id or getattr(cfg, "SUBSCRIPTION_ID", None)
    if not subscription_id:
        raise ValueError("A subscription_id is required for each AMLFS region")
    if subscription_id not in _clients:
        _clients[subscription_id] = StorageCacheManagementClient(
            DefaultAzureCredential(), subscription_id)
    return _clients[subscription_id]


def _regions():
    """Validate and normalize the explicit named-region configuration."""
    raw = getattr(cfg, "REGIONS", None)
    home = getattr(cfg, "HOME_REGION", None)
    partitions = getattr(cfg, "PARTITION_REGIONS", None)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("REGIONS must be a nonempty mapping")
    regions = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ValueError("REGIONS requires named region dictionaries")
        region = dict(value)
        region["subscription_id"] = region.get("subscription_id") or getattr(cfg, "SUBSCRIPTION_ID", None)
        for field in ("subscription_id", "resource_group", "filesystem", "mountpoint", "location"):
            if not isinstance(region.get(field), str) or not region[field].strip():
                raise ValueError(f"Region {key!r} requires {field}")
        mount = region["mountpoint"]
        if not os.path.isabs(mount) or ".." in mount.split("/") or "\0" in mount:
            raise ValueError(f"Region {key!r} has an invalid mountpoint")
        region["mountpoint"] = os.path.normpath(mount)
        regions[key] = region
    if home not in regions:
        raise ValueError("HOME_REGION must name a configured region")
    if not isinstance(partitions, dict) or any(
        not isinstance(p, str) or not p or r not in regions for p, r in partitions.items()
    ):
        raise ValueError("PARTITION_REGIONS must map partition names to configured regions")
    return regions, home, partitions


def _resolve_route(partition):
    regions, home, partitions = _regions()
    if partition is None:
        raise ValueError("--partition is required; the job route is ambiguous")
    if partition not in partitions:
        raise ValueError(f"Unknown partition {partition!r}; configure PARTITION_REGIONS")
    target = partitions[partition]
    return {"partition": partition, "home_region": home, "target_region": target,
            "home": regions[home], "target": regions[target]}


def _resource_identity(region):
    return (f"/subscriptions/{region['subscription_id']}/resourceGroups/"
            f"{region['resource_group']}/providers/Microsoft.StorageCache/"
            f"amlFilesystems/{region['filesystem']}").lower()


def _validate_job(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_]+", job_id):
        raise ValueError("Invalid Slurm job ID")


def _state_path(job_id, suffix):
    _validate_job(job_id)
    return os.path.join(cfg.STATE_DIR, f"job-{job_id}-{suffix}")


def _write_json(path, value):
    os.makedirs(cfg.STATE_DIR, mode=0o700, exist_ok=True)
    pending = path + ".pending"
    try:
        with open(pending, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.remove(pending)


@contextmanager
def _lock(identity):
    lock_dir = os.path.join(cfg.STATE_DIR, "locks")
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    name = hashlib.sha256(identity.encode()).hexdigest() + ".lock"
    # Lock files are permanent: unlinking them would permit two inode locks.
    with open(os.path.join(lock_dir, name), "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _persist_route(job_id, route):
    path = _state_path(job_id, "route.json")
    if os.path.exists(path):
        with open(path) as stream:
            previous = json.load(stream)
        if previous != route:
            raise ValueError(f"Job {job_id} route changed; teardown old state before rerouting")
        return previous
    _write_json(path, route)
    return route


# ── Continuation-token persistence ─────────────────────────────────────
def _token_path(job_id: str, phase: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", phase):
        raise ValueError("Invalid phase")
    return _state_path(job_id, f"{phase}.token")


def _save_token(job_id: str, phase: str, token: str):
    path = _token_path(job_id, phase)
    _write_json(path, {"token": token})


def _load_token(job_id: str, phase: str) -> str | None:
    path = _token_path(job_id, phase)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("token")
    return None


def _clear_token(job_id: str, phase: str):
    path = _token_path(job_id, phase)
    if os.path.exists(path):
        os.remove(path)


# ── Archive (AMLFS → Blob) ─────────────────────────────────────────────

def _status_value(value):
    return value.value if isinstance(value, Enum) else value


def _export_time(status, field):
    # The service also returns these timestamps without the SDK's UTC suffix.
    value = status.get(field + "UTC") or status.get(field)
    if isinstance(value, str):
        value = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d+)",
                       lambda match: match[1] + "." + match[2][:6].ljust(6, "0"), value)
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return None


def _stop_export(client, rg, amlfs_name, name, timeout=300):
    """Disable and remove only the explicitly recorded export resource."""
    deadline = time.monotonic() + timeout
    try:
        result = _retry_sdk(client.auto_export_jobs.get)(rg, amlfs_name, name)
    except HttpResponseError as error:
        if error.status_code != 404:
            raise
        return
    properties = result.properties
    status = properties.status if properties else None
    if not status or _status_value(status.state) != "Disabled":
        result = _retry_sdk(client.auto_export_jobs.begin_update)(
            rg, amlfs_name, name,
            AutoExportJobUpdate(properties=AutoExportJobUpdateProperties(admin_status="Disable")),
        ).result(timeout=timeout)
    while True:
        properties = result.properties
        status = properties.status if properties else None
        state = _status_value(status.state) if status else "Unknown"
        if state == "Disabled":
            break
        if state in ("Failed", "DisableFailed"):
            raise RuntimeError(f"ExportJob {name} could not be disabled: {state}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"ExportJob {name} disable timed out")
        time.sleep(min(cfg.ARCHIVE_POLL_INTERVAL, max(0, deadline - time.monotonic())))
        result = _retry_sdk(client.auto_export_jobs.get)(rg, amlfs_name, name)
    poller = _retry_sdk(client.auto_export_jobs.begin_delete)(rg, amlfs_name, name)
    poller.result(timeout=max(0, deadline - time.monotonic()))
    if not poller.done():
        raise RuntimeError(f"ExportJob {name} deletion timed out")


def _archive(rg: str, amlfs_name: str, fs_path: str, job_id: str, phase: str,
             subscription_id=None):
    """Export a stable subtree, proving one successful iteration before stopping."""
    client = _get_client(subscription_id)
    name = f"bb-{job_id}-{phase}-{uuid.uuid4().hex[:8]}"
    ownership_path = _state_path(job_id, f"{name}.export.json")
    _write_json(ownership_path, {
        "subscription_id": subscription_id or cfg.SUBSCRIPTION_ID,
        "resource_group": rg, "filesystem": amlfs_name, "name": name,
    })
    deadline = time.monotonic() + cfg.LRO_TIMEOUT
    log.info("Creating ExportJob %s on %s:%s (job %s)", name, amlfs_name, fs_path, job_id)
    try:
        poller = _retry_sdk(client.auto_export_jobs.begin_create_or_update)(
            rg, amlfs_name, name,
            AutoExportJob(properties=AutoExportJobProperties(
                admin_status="Enable", auto_export_prefixes=[fs_path])),
        )
        token = poller.continuation_token()
        if token:
            _save_token(job_id, f"{phase}-export", token)
        result = poller.result(timeout=cfg.LRO_TIMEOUT)
        while True:
            properties = result.properties
            status = properties.status if properties else None
            provisioning = _status_value(properties.provisioning_state) if properties else None
            state = _status_value(status.state) if status else "Unknown"
            if provisioning in ("Failed", "Canceled", "Cancelled") or state in (
                "Failed", "DisableFailed", "Disabled", "Disabling",
            ):
                raise RuntimeError(f"ExportJob {name} ended before completion: {state} (ARM: {provisioning})")
            if status:
                if (status.total_files_failed or 0) > 0 or (status.current_iteration_files_failed or 0) > 0:
                    raise RuntimeError(f"ExportJob {name} reported failed files")
                started = _export_time(status, "lastStartedTime")
                completed = _export_time(status, "lastSuccessfulIterationCompletionTime")
                iterations = status.export_iteration_count
                log.info("ExportJob %s state=%s completed_iterations=%s (job %s)",
                         name, state, iterations, job_id)
                if (state == "InProgress" and type(iterations) is int and iterations > 0
                        and status.total_files_failed == 0 and status.current_iteration_files_failed == 0
                        and started is not None and completed is not None and completed >= started):
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"ExportJob {name} timed out after {cfg.LRO_TIMEOUT}s")
            time.sleep(min(cfg.ARCHIVE_POLL_INTERVAL, max(0, deadline - time.monotonic())))
            result = _retry_sdk(client.auto_export_jobs.get)(rg, amlfs_name, name)
    finally:
        try:
            _stop_export(client, rg, amlfs_name, name)
            os.remove(ownership_path)
            _clear_token(job_id, f"{phase}-export")
        except Exception:
            log.exception("ExportJob %s cleanup incomplete; ownership retained for teardown", name)
            raise
    log.info("Archive completed for %s:%s (job %s)", amlfs_name, fs_path, job_id)


# ── Import (Blob → AMLFS) ──────────────────────────────────────────────
def _import_data(rg: str, amlfs_name: str, prefixes: list[str],
                 job_id: str, phase: str, location: str,
                 conflict_mode: str, subscription_id=None):
    """Create an ImportJob to hydrate data from Blob into AMLFS.

    The caller retains both filesystem locks until import completes.

    conflict_mode: how the ImportJob reconciles a blob with an existing
        file on AMLFS. One of: "Skip", "Fail", "OverwriteAlways",
        "OverwriteIfDirty".
    """
    if not isinstance(location, str) or not location.strip():
        raise ValueError("An explicit Azure location is required for import")
    client = _get_client(subscription_id)
    job_name = f"bb-{job_id}-{phase}-{uuid.uuid4().hex[:8]}"
    ownership_path = _state_path(job_id, f"{job_name}.import.json")

    import_job = ImportJob(
        location=location,
        properties=ImportJobProperties(
            import_prefixes=prefixes,
            conflict_resolution_mode=conflict_mode,
        ),
    )

    log.info("Creating ImportJob %s on %s (prefixes=%s, conflict=%s, job %s)",
             job_name, amlfs_name, prefixes, conflict_mode, job_id)

    # Record the exact ARM identity before creating it, including failed LROs.
    _write_json(ownership_path, {
        "subscription_id": subscription_id or cfg.SUBSCRIPTION_ID,
        "resource_group": rg, "filesystem": amlfs_name, "name": job_name,
    })
    @_retry_sdk
    def create_import():
        return client.import_jobs.begin_create_or_update(
            resource_group_name=rg,
            aml_filesystem_name=amlfs_name,
            import_job_name=job_name,
            import_job=import_job,
        )

    poller = create_import()

    token = poller.continuation_token()
    if token:
        _save_token(job_id, f"{phase}-import", token)

    deadline = time.monotonic() + cfg.LRO_TIMEOUT
    result = poller.result(timeout=cfg.LRO_TIMEOUT)
    while True:
        properties = result.properties
        provisioning = _status_value(properties.provisioning_state) if properties else None
        status = properties.status if properties else None
        state = _status_value(status.state) if status else "Unknown"
        if provisioning in ("Failed", "Canceled", "Cancelled") or state in (
            "Failed", "Canceled", "Cancelled", "CompletedPartial"
        ):
            raise RuntimeError(f"ImportJob {job_name} ended with status {state} (ARM: {provisioning})")
        if state == "Completed":
            break
        # ARM provisioning success alone does not mean that data import finished.
        if time.monotonic() >= deadline:
            raise RuntimeError(f"ImportJob {job_name} timed out after {cfg.LRO_TIMEOUT}s")
        time.sleep(cfg.IMPORT_POLL_INTERVAL)
        result = _retry_sdk(client.import_jobs.get)(rg, amlfs_name, job_name)
    _clear_token(job_id, f"{phase}-import")
    log.info("ImportJob %s completed (job %s)", job_name, job_id)

    # Clean up the ImportJob ARM resource
    try:
        client.import_jobs.begin_delete(rg, amlfs_name, job_name).result(timeout=300)
        os.remove(ownership_path)
        log.info("Cleaned up ImportJob %s", job_name)
    except Exception as e:
        log.warning("Failed to clean up ImportJob %s: %s", job_name, e)


# ── Path helpers ────────────────────────────────────────────────────────
def _relative_path(abs_path: str, mount: str) -> str:
    """Validate a source-mount boundary, then return the filesystem path."""
    if (not isinstance(abs_path, str) or not os.path.isabs(abs_path)
            or "\0" in abs_path or ".." in abs_path.split("/")):
        raise ValueError(f"Invalid absolute filesystem path {abs_path!r}")
    mount = os.path.realpath(mount)
    path = os.path.realpath(abs_path)
    if path == mount or os.path.commonpath([mount, path]) != mount:
        raise ValueError(f"Path {abs_path} is not under mount {mount}")
    return "/" + os.path.relpath(path, mount)


def _path_allows(path, info, uid, groups, required):
    """Evaluate Linux POSIX access ACLs without changing the helper's identity."""
    if uid == 0:
        return True
    if uid == info.st_uid:
        return (info.st_mode >> 6) & required == required
    try:
        acl = os.getxattr(path, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise
        permissions = (info.st_mode >> 3) if info.st_gid in groups else info.st_mode
        return permissions & required == required

    # Linux's POSIX ACL xattr: version 2, followed by (tag, permissions, ID).
    if len(acl) < 4 or struct.unpack_from("<I", acl)[0] != 2 or (len(acl) - 4) % 8:
        raise ValueError(f"Invalid POSIX access ACL on {path!r}")
    entries = list(struct.iter_unpack("<HHI", acl[4:]))
    if (any(tag not in (1, 2, 4, 8, 16, 32) or perms > 7 for tag, perms, _ in entries)
            or any(sum(tag == base for tag, _, _ in entries) != 1 for base in (1, 4, 32))
            or sum(tag == 16 for tag, _, _ in entries) > 1
            or (any(tag in (2, 8) for tag, _, _ in entries)
                and not any(tag == 16 for tag, _, _ in entries))):
        raise ValueError(f"Invalid POSIX access ACL on {path!r}")
    mask = next((perms for tag, perms, _ in entries if tag == 16), 7)
    for tag, perms, identity in entries:
        if tag == 2 and identity == uid:
            return perms & mask & required == required
    matches = [
        perms for tag, perms, identity in entries
        if (tag == 4 and info.st_gid in groups) or (tag == 8 and identity in groups)
    ]
    if matches:
        return any(perms & mask & required == required for perms in matches)
    permissions = next(perms for tag, perms, _ in entries if tag == 32)
    return permissions & required == required


def _validate_path_access(path, uid, write=False):
    try:
        account = pwd.getpwuid(uid)
        groups = {account.pw_gid, *os.getgrouplist(account.pw_name, account.pw_gid)}
        info = os.stat(path)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise PermissionError(f"Staging path {path!r} must be a directory or regular file")
        # Check both spellings so a symlink cannot bypass traversal restrictions.
        ancestors = set()
        for spelling in (os.path.abspath(path), os.path.realpath(path)):
            parent = os.path.dirname(spelling)
            while True:
                ancestors.add(parent)
                if parent == "/":
                    break
                parent = os.path.dirname(parent)
        for parent in sorted(ancestors):
            if not _path_allows(parent, os.stat(parent), uid, groups, os.X_OK):
                raise PermissionError(
                    f"Staging denied: submitter UID {uid} lacks traverse permission on {parent!r} "
                    f"for path {path!r}"
                )
        required = os.R_OK | (os.W_OK if write else 0)
        if stat.S_ISDIR(info.st_mode):
            required |= os.X_OK
        if not _path_allows(path, info, uid, groups, required):
            access = "read/write" if write else "read"
            raise PermissionError(
                f"Staging denied: submitter UID {uid} lacks {access}"
                f"{'/traverse' if stat.S_ISDIR(info.st_mode) else ''} permission on {path!r}"
            )
    except KeyError as exc:
        raise PermissionError(f"Cannot resolve submitter UID {uid} for staging path {path!r}") from exc
    except OSError as exc:
        if isinstance(exc, PermissionError) and exc.errno is None:
            raise
        raise PermissionError(
            f"Cannot verify staging path {path!r} for submitter UID {uid}: {exc.strerror}"
        ) from exc


def _stage(job_id, paths, partition, phase, uid):
    _validate_job(job_id)
    if type(uid) is not int or uid < 0:
        raise ValueError("A nonnegative submitter UID is required")
    route = _resolve_route(partition)
    source, destination = (route["home"], route["target"]) if phase == "stagein" else (
        route["target"], route["home"])
    # Validate the entire request before recording state or making Azure calls.
    if not paths:
        raise ValueError("At least one path is required")
    relative = [_relative_path(path, source["mountpoint"]) for path in paths]
    for path in paths:
        _validate_path_access(path, uid, write=phase == "stageout")
    with _lock(f"job:{job_id}"):
        _persist_route(job_id, route)
        if _resource_identity(source) == _resource_identity(destination):
            log.info("Job %s: same AMLFS resource; %s skipped", job_id, phase)
            return
        with ExitStack() as locks:
            for identity in sorted({_resource_identity(source), _resource_identity(destination)}):
                locks.enter_context(_lock(identity))
            location = destination["location"]
            conflict = cfg.STAGEIN_CONFLICT_MODE if phase == "stagein" else cfg.STAGEOUT_CONFLICT_MODE
            for rel in relative:
                _archive(source["resource_group"], source["filesystem"], rel,
                         job_id, f"{phase}-archive",
                         subscription_id=source["subscription_id"])
                _import_data(destination["resource_group"], destination["filesystem"],
                             [rel], job_id, phase, location, conflict,
                             subscription_id=destination["subscription_id"])


def do_stage_in(job_id: str, paths: list[str], partition: str, uid: int):
    """Archive from the home region and import into the partition's region."""
    _stage(job_id, paths, partition, "stagein", uid)


def do_stage_out(job_id: str, paths: list[str], partition: str, uid: int):
    """Reverse the recorded route: partition region to home region."""
    _stage(job_id, paths, partition, "stageout", uid)


# ── Teardown: remove only operations with recorded job ownership ────────
def do_teardown(job_id: str):
    """Clean only recorded job-owned operations; never cancel global archives."""
    _validate_job(job_id)
    log.info("=== TEARDOWN job %s ===", job_id)
    with _lock(f"job:{job_id}"):
        owned_files = [
            os.path.join(cfg.STATE_DIR, name) for name in os.listdir(cfg.STATE_DIR)
            if name.startswith(f"job-{job_id}-")
        ]
        failed = False
        for path in owned_files:
            if not path.endswith((".import.json", ".export.json")):
                continue
            try:
                with open(path) as stream:
                    operation = json.load(stream)
                exporting = path.endswith(".export.json")
                phase = r"stage(?:in|out)-archive" if exporting else r"stage(?:in|out)"
                if not re.fullmatch(rf"bb-{re.escape(job_id)}-{phase}-[0-9a-f]{{8}}",
                                    operation["name"]):
                    raise ValueError("Recorded operation does not belong to this job")
                client = _get_client(operation["subscription_id"])
                if exporting:
                    _stop_export(client, operation["resource_group"],
                                 operation["filesystem"], operation["name"])
                else:
                    try:
                        client.import_jobs.begin_delete(
                            operation["resource_group"], operation["filesystem"],
                            operation["name"]).result(timeout=300)
                    except HttpResponseError as exc:
                        if exc.status_code != 404:
                            raise
                os.remove(path)
            except Exception as exc:
                failed = True
                log.warning("Teardown: retaining %s for retry: %s", path, exc)
        if failed:
            raise RuntimeError(f"Job {job_id}: operation cleanup incomplete; state retained for retry")
        for path in owned_files:
            if os.path.isfile(path):
                os.remove(path)
    log.info("=== TEARDOWN complete for job %s ===", job_id)


# ── Self-test ───────────────────────────────────────────────────────────
def do_selftest():
    """Verify read connectivity and RBAC to every configured AMLFS resource."""
    log.info("=== SELFTEST ===")
    errors = []
    regions, _, _ = _regions()
    for label, region in regions.items():
        try:
            client = _get_client(region["subscription_id"])
            fs = client.aml_filesystems.get(region["resource_group"], region["filesystem"])
            log.info("%s: OK (%s, location=%s, state=%s)",
                     label, fs.name, fs.location,
                     _status_value(fs.properties.provisioning_state) if fs.properties else None)
        except HttpResponseError as e:
            msg = f"{label}: FAILED — {e.status_code} {e.message}"
            log.error(msg)
            errors.append(msg)
        except Exception as e:
            msg = f"{label}: FAILED — {e}"
            log.error(msg)
            errors.append(msg)

    if errors:
        print("SELFTEST FAILED:", "; ".join(errors), file=sys.stderr)
        return 1

    log.info("=== SELFTEST PASSED ===")
    print("SELFTEST PASSED")
    return 0


# ── CLI entry point ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Azure CycleCloud Multi-region plugin for AMLFS staging helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_in = sub.add_parser("stage-in", help="Stage data from home AMLFS to the partition's AMLFS")
    p_in.add_argument("--job", required=True, help="Slurm job ID")
    p_in.add_argument("--paths", nargs="+", required=True, help="Paths to stage")
    p_in.add_argument("--partition", required=True, help="Slurm partition")
    p_in.add_argument("--uid", required=True, type=int, help="Job submitter UID supplied by Slurm")

    p_out = sub.add_parser("stage-out", help="Stage data from the partition's AMLFS back home")
    p_out.add_argument("--job", required=True, help="Slurm job ID")
    p_out.add_argument("--paths", nargs="+", required=True, help="Paths to stage")
    p_out.add_argument("--partition", required=True, help="Slurm partition")
    p_out.add_argument("--uid", required=True, type=int, help="Job submitter UID supplied by Slurm")

    p_td = sub.add_parser("teardown", help="Delete recorded job-owned imports and clean state")
    p_td.add_argument("--job", required=True, help="Slurm job ID")

    sub.add_parser("selftest", help="Verify AMLFS connectivity and RBAC")

    args = parser.parse_args()

    try:
        if args.command == "stage-in":
            do_stage_in(args.job, args.paths, args.partition, args.uid)
        elif args.command == "stage-out":
            do_stage_out(args.job, args.paths, args.partition, args.uid)
        elif args.command == "teardown":
            do_teardown(args.job)
        elif args.command == "selftest":
            sys.exit(do_selftest())
    except Exception as e:
        log.exception("FATAL: %s failed for job %s", args.command,
                      getattr(args, "job", "N/A"))
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
