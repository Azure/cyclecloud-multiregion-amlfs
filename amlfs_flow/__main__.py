#!/usr/bin/env python3
"""Slurm burst-buffer helper for cross-region AMLFS data movement.

Usage (called by burst_buffer.lua):
    python -m amlfs_flow stage-in  --job <id> --paths /lustre/proj/input [...]
    python -m amlfs_flow stage-out --job <id> --paths /lustre/proj/output [...]
    python -m amlfs_flow teardown  --job <id>
    python -m amlfs_flow selftest

Exit 0 → success; non-zero → failure (stderr shown in Slurm BB error).
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid

from azure.identity import DefaultAzureCredential
from azure.mgmt.storagecache import StorageCacheManagementClient
from azure.mgmt.storagecache.models import AmlFilesystemArchiveInfo
from azure.core.exceptions import HttpResponseError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

from . import config as cfg

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


# ── SDK client (lazy singleton) ────────────────────────────────────────
_client = None


def _get_client() -> StorageCacheManagementClient:
    global _client
    if _client is None:
        cred = DefaultAzureCredential()
        _client = StorageCacheManagementClient(cred, cfg.SUBSCRIPTION_ID)
    return _client


# ── Continuation-token persistence ─────────────────────────────────────
def _token_path(job_id: str, phase: str) -> str:
    return os.path.join(cfg.STATE_DIR, f"job-{job_id}-{phase}.token")


def _save_token(job_id: str, phase: str, token: str):
    path = _token_path(job_id, phase)
    with open(path, "w") as f:
        json.dump({"token": token}, f)


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


@_retry_sdk
def _archive(rg: str, amlfs_name: str, fs_path: str, job_id: str, phase: str):
    """Archive a directory subtree from AMLFS to Blob.

    Serialization (one archive at a time per FS) is handled by flock in the
    Lua hook; this function just drives the archive and polls for completion.
    """
    client = _get_client()

    archive_info = AmlFilesystemArchiveInfo(filesystem_path=fs_path)
    log.info("Starting archive for %s:%s (job %s)", amlfs_name, fs_path, job_id)
    client.aml_filesystems.archive(
        resource_group_name=rg,
        aml_filesystem_name=amlfs_name,
        archive_info=archive_info,
    )

    log.info("Archive started for %s:%s (job %s)", amlfs_name, fs_path, job_id)

    # Poll archive_status until completion
    deadline = time.time() + cfg.LRO_TIMEOUT
    while time.time() < deadline:
        fs = client.aml_filesystems.get(rg, amlfs_name)
        archive_statuses = getattr(fs, "hsm", None)
        if archive_statuses:
            archive_statuses = getattr(archive_statuses, "archive_status", None)
        if not archive_statuses:
            log.info("Archive completed (no status) for %s:%s", amlfs_name, fs_path)
            break

        # Find the status entry for our path
        active = None
        for entry in archive_statuses:
            entry_d = entry if isinstance(entry, dict) else entry.as_dict() if hasattr(entry, "as_dict") else {}
            if isinstance(entry_d, dict) and entry_d.get("filesystem_path") == fs_path:
                active = entry_d.get("status", {})
                break

        if not active:
            log.info("Archive completed (path gone from status) for %s:%s", amlfs_name, fs_path)
            break

        state = active.get("state", "Unknown")
        pct = active.get("percent_complete", 0)
        log.info("Archive %s:%s state=%s pct=%s (job %s)", amlfs_name, fs_path, state, pct, job_id)

        if state == "Completed":
            break
        elif state in ("Failed", "Canceled", "Cancelled"):
            raise RuntimeError(f"Archive {amlfs_name}:{fs_path} ended with state {state}")
        elif state == "FSScanInProgress":
            log.info("Archive scan in progress for %s:%s", amlfs_name, fs_path)
        elif state != "InProgress":
            log.warning("Unknown archive state: %s", state)

        time.sleep(cfg.ARCHIVE_POLL_INTERVAL)
    else:
        raise RuntimeError(f"Archive {amlfs_name}:{fs_path} timed out after {cfg.LRO_TIMEOUT}s")

    log.info("Archive completed for %s:%s (job %s)", amlfs_name, fs_path, job_id)


# ── Import (Blob → AMLFS) ──────────────────────────────────────────────
@_retry_sdk
def _import_data(rg: str, amlfs_name: str, prefixes: list[str],
                 job_id: str, phase: str, location: str,
                 conflict_mode: str):
    """Create an ImportJob to hydrate data from Blob into AMLFS.

    AMLFS allows multiple concurrent ImportJobs per filesystem, so these
    are NOT serialized — only archive operations are (one per FS at a time).

    conflict_mode: how the ImportJob reconciles a blob with an existing
        file on AMLFS. One of: "Skip", "Fail", "OverwriteAlways",
        "OverwriteIfDirty".
    """
    client = _get_client()
    job_name = f"bb-{job_id}-{phase}-{uuid.uuid4().hex[:8]}"

    # Get AMLFS location for the ImportJob
    if not location:
        fs_info = client.aml_filesystems.get(rg, amlfs_name)
        location = fs_info.location

    import_job_params = {
        "location": location,
        "import_prefixes": prefixes,
        "conflict_resolution_mode": conflict_mode,
    }

    log.info("Creating ImportJob %s on %s (prefixes=%s, conflict=%s, job %s)",
             job_name, amlfs_name, prefixes, conflict_mode, job_id)

    poller = client.import_jobs.begin_create_or_update(
        resource_group_name=rg,
        aml_filesystem_name=amlfs_name,
        import_job_name=job_name,
        import_job=import_job_params,
    )

    token = poller.continuation_token()
    if token:
        _save_token(job_id, f"{phase}-import", token)

    result = poller.result(timeout=cfg.LRO_TIMEOUT)
    _clear_token(job_id, f"{phase}-import")

    status = getattr(result, "status", None) or getattr(result, "provisioning_state", "Unknown")
    log.info("ImportJob %s completed with status: %s (job %s)", job_name, status, job_id)

    if status in ("Failed", "Canceled"):
        raise RuntimeError(f"ImportJob {job_name} ended with status {status}")

    # Clean up the ImportJob ARM resource
    try:
        client.import_jobs.begin_delete(rg, amlfs_name, job_name).result(timeout=300)
        log.info("Cleaned up ImportJob %s", job_name)
    except Exception as e:
        log.warning("Failed to clean up ImportJob %s: %s", job_name, e)


# ── Path helpers ────────────────────────────────────────────────────────
def _relative_path(abs_path: str) -> str:
    """Strip the /lustre/ mount prefix to get the relative FS path."""
    mount = cfg.AMLFS_PRIMARY_MOUNT.rstrip("/")
    if not abs_path.startswith(mount + "/"):
        raise ValueError(f"Path {abs_path} is not under mount {mount}")
    return abs_path[len(mount):]  # keeps leading /


def _blob_prefix(rel_path: str) -> str:
    """Convert a relative FS path to a blob prefix for import."""
    return rel_path  # blob path = filesystem-relative path (shared container)


# ── Stage-in: PRIMARY → Blob → SECONDARY ───────────────────────────────
def do_stage_in(job_id: str, paths: list[str]):
    """Archive paths from the primary AMLFS, then import into the secondary."""
    log.info("=== STAGE-IN job %s, paths=%s ===", job_id, paths)

    for p in paths:
        rel = _relative_path(p)

        # Step 1: Archive from PRIMARY to Blob
        _archive(cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME,
                 rel, job_id, "stagein-archive")

        # Step 2: Import from Blob to SECONDARY
        prefixes = [_blob_prefix(rel)]

        client = _get_client()
        fs_secondary = client.aml_filesystems.get(
            cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME)

        _import_data(cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME,
                     prefixes, job_id, "stagein", fs_secondary.location,
                     cfg.STAGEIN_CONFLICT_MODE)

    log.info("=== STAGE-IN complete for job %s ===", job_id)


# ── Stage-out: SECONDARY → Blob → PRIMARY ──────────────────────────────
def do_stage_out(job_id: str, paths: list[str]):
    """Archive paths from the secondary AMLFS, then import into the primary."""
    log.info("=== STAGE-OUT job %s, paths=%s ===", job_id, paths)

    for p in paths:
        rel = _relative_path(p)

        # Step 1: Archive from SECONDARY to Blob
        _archive(cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME,
                 rel, job_id, "stageout-archive")

        # Step 2: Import from Blob to PRIMARY
        prefixes = [_blob_prefix(rel)]

        client = _get_client()
        fs_primary = client.aml_filesystems.get(
            cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME)

        _import_data(cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME,
                     prefixes, job_id, "stageout", fs_primary.location,
                     cfg.STAGEOUT_CONFLICT_MODE)

    log.info("=== STAGE-OUT complete for job %s ===", job_id)


# ── Teardown: cancel in-flight operations, clean state ──────────────────
def do_teardown(job_id: str):
    """Cancel any in-flight archive/import operations for this job."""
    log.info("=== TEARDOWN job %s ===", job_id)
    client = _get_client()

    # Try cancelling active archives on both filesystems
    for rg, name in [
        (cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME),
        (cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME),
    ]:
        try:
            fs = client.aml_filesystems.get(rg, name)
            archive_status = getattr(fs, "archive_status", None)
            if archive_status and getattr(archive_status, "state", None) == "InProgress":
                log.info("Cancelling archive on %s for job %s", name, job_id)
                client.aml_filesystems.cancel_archive(rg, name)
        except Exception as e:
            log.warning("Teardown: error checking/cancelling archive on %s: %s", name, e)

    # Clean up any ImportJobs matching this job ID
    for rg, name in [
        (cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME),
        (cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME),
    ]:
        try:
            for ij in client.import_jobs.list_by_aml_filesystem(rg, name):
                if ij.name and f"bb-{job_id}-" in ij.name:
                    prov = getattr(ij, "provisioning_state", "")
                    if prov not in ("Succeeded", "Failed", "Canceled"):
                        log.info("Deleting ImportJob %s (job %s)", ij.name, job_id)
                        try:
                            client.import_jobs.begin_delete(rg, name, ij.name).result(timeout=300)
                        except Exception as e2:
                            log.warning("Failed to delete ImportJob %s: %s", ij.name, e2)
        except Exception as e:
            log.warning("Teardown: error listing ImportJobs on %s: %s", name, e)

    # Clean up state files
    for f in os.listdir(cfg.STATE_DIR):
        if f.startswith(f"job-{job_id}-"):
            os.remove(os.path.join(cfg.STATE_DIR, f))

    log.info("=== TEARDOWN complete for job %s ===", job_id)


# ── Self-test ───────────────────────────────────────────────────────────
def do_selftest():
    """Verify connectivity and RBAC to both AMLFS instances."""
    log.info("=== SELFTEST ===")
    client = _get_client()
    errors = []

    for label, rg, name in [
        ("primary AMLFS", cfg.AMLFS_PRIMARY_RESOURCE_GROUP, cfg.AMLFS_PRIMARY_NAME),
        ("secondary AMLFS", cfg.AMLFS_SECONDARY_RESOURCE_GROUP, cfg.AMLFS_SECONDARY_NAME),
    ]:
        try:
            fs = client.aml_filesystems.get(rg, name)
            log.info("%s: OK (%s, location=%s, state=%s)",
                     label, fs.name, fs.location, fs.provisioning_state)
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
    parser = argparse.ArgumentParser(description="AMLFS burst-buffer helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_in = sub.add_parser("stage-in", help="Stage data from primary AMLFS to secondary AMLFS")
    p_in.add_argument("--job", required=True, help="Slurm job ID")
    p_in.add_argument("--paths", nargs="+", required=True, help="Paths to stage")

    p_out = sub.add_parser("stage-out", help="Stage data from secondary AMLFS to primary AMLFS")
    p_out.add_argument("--job", required=True, help="Slurm job ID")
    p_out.add_argument("--paths", nargs="+", required=True, help="Paths to stage")

    p_td = sub.add_parser("teardown", help="Cancel in-flight ops and clean state")
    p_td.add_argument("--job", required=True, help="Slurm job ID")

    sub.add_parser("selftest", help="Verify AMLFS connectivity and RBAC")

    args = parser.parse_args()

    try:
        if args.command == "stage-in":
            do_stage_in(args.job, args.paths)
        elif args.command == "stage-out":
            do_stage_out(args.job, args.paths)
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
