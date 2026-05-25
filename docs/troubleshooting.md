# Troubleshooting

## Job stuck in `PD (BurstBufferStageIn)`

Normal during staging — check the helper log:

```bash
sudo tail -f /var/log/slurm/amlfs_flow.log
```

If it's progressing through `Archive ... state=InProgress`, just wait.

## Job in `PD (JobHeldAdmin: ... failed)`

The Python helper exited non-zero and Slurm parked the job. Inspect:

```bash
scontrol show job <id> | grep -A3 Reason
```

Common causes:

### `PermissionError: ... archive-*.lock`

The `slurm` user can't write the flock file. Fix:

```bash
sudo mkdir -p /sched/slurm-amlfs-flow
sudo chown slurm:slurm /sched/slurm-amlfs-flow
sudo chmod 0775 /sched/slurm-amlfs-flow
sudo touch /sched/slurm-amlfs-flow/archive-stagein.lock \
           /sched/slurm-amlfs-flow/archive-stageout.lock
sudo chown slurm:slurm /sched/slurm-amlfs-flow/archive-*.lock
```

### `InvalidParameter: importPrefix must ... start with a slash`

The `_blob_prefix()` in `__main__.py` must return a path starting with
`/`. Check `amlfs_flow/config.py` — `BLOB_STAGE_PREFIX` should normally be
`""` (empty) since blob path = filesystem path.

### `AuthorizationFailed` / `Forbidden`

Managed Identity is missing role assignments. Re-check:

```bash
az role assignment list \
    --assignee <scheduler-mi-object-id> \
    --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.StorageCache/amlFilesystems/<fs>
```

Should include `Azure Managed Lustre Contributor`.

### `AttributeError: 'AmlFilesystemsOperations' has no attribute 'begin_archive'`

You're on an old SDK version that hasn't been pinned. Reinstall:

```bash
sudo /opt/slurm-amlfs-flow/venv/bin/pip install --upgrade -r requirements.txt
```

The correct method is `aml_filesystems.archive(...)` (synchronous —
returns when ARM accepts the request, then poll `archive_status`).

## Selftest fails

```bash
sudo /opt/slurm-amlfs-flow/scripts/selftest.sh
```

| Error                                   | Cause                                  |
|-----------------------------------------|----------------------------------------|
| `ManagedIdentityCredential failed`      | Identity not assigned to VM            |
| `404 ResourceNotFound`                  | Wrong RG/name in `config.py`           |
| `403 AuthorizationFailed`               | Missing role assignment (see RBAC doc) |
| `Could not connect to IMDS`             | Not running on an Azure VM             |

## Job stuck in `SO (Stage-out in progress)` after completion

Stage-out can take longer than the job itself for large outputs. Check
the helper log; if it's still running, just wait. To force-cancel:

```bash
scancel -f <jobid>          # signals stage-out teardown
```

## Stage-in succeeded but data not visible on secondary AMLFS

AMLFS uses lazy hydration — ImportJob populates metadata immediately but
file contents may be fetched on first read. Verify:

```bash
# On the compute node:
ls -la /lustre/data/run42/input          # metadata present?
stat /lustre/data/run42/input/file.bin   # full size?
cat /lustre/data/run42/input/file.bin    # triggers hydrate
```

For latency-sensitive workloads, consider AMLFS `auto_import_jobs` or
explicit `lfs hsm_restore`.

## Concurrent archive collisions

If you see `ConflictError: Another archive is already in progress` in
the helper log, the `flock` in the Lua hook isn't being honored —
verify:

```bash
# Both lock files exist and are owned by slurm:
ls -la /sched/slurm-amlfs-flow/archive-*.lock

# Lua hook is referencing flock(1):
grep flock /etc/slurm/burst_buffer.lua
```

If absent, copy the latest `slurm/burst_buffer.lua` from this repo and
`scontrol reconfigure`.

## ImportJob ARM resources accumulating

The helper deletes ImportJobs on success. To clean up orphans from
prior failures:

```bash
az resource list --resource-type Microsoft.StorageCache/amlFilesystems/importJobs \
    --query "[?starts_with(name, 'bb-')].name" -o tsv \
    | xargs -I{} az resource delete --ids {}
```

## Useful logs

```bash
/var/log/slurm/amlfs_flow.log         # helper (structured)
/var/log/slurm/slurmctld.log        # controller (BB callback dispatch)
journalctl -u slurmctld              # systemd
```

Set `LOG_LEVEL=DEBUG` to verbose-log SDK requests — but beware, every
HTTP roundtrip is dumped.
