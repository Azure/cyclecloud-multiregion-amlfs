---
title: Install the plugin on an existing scheduler
description: Perform a reviewed in-place installation or repair of Azure CycleCloud Multi-region plugin for AMLFS on an existing Slurm scheduler.
---

# Install the plugin on an existing scheduler

Use this procedure for a reviewed in-place repair or update of Azure CycleCloud Multi-region plugin for AMLFS on an existing Slurm scheduler. For normal installation
through cluster-init, use the [deployment guide](installation.md).

## Prerequisites

- Administrative access to the Slurm scheduler, not just the Azure CycleCloud management VM.
- Reviewed project source and generated regional configuration and mount JSON files.
- A kernel-matched Lustre client on the scheduler.
- The scheduler identity's [runtime permissions](managed-identity.md).
- A maintenance window and backups of the runtime configuration, Slurm configuration,
  and complete Azure CycleCloud definition.

## Prepare the scheduler

1. Stop new staged submissions and let active staging operations finish.
2. Preserve the current runtime configuration, Slurm configuration, and complete
   Azure CycleCloud definition for rollback.
3. Transfer the reviewed project source and generated deployment JSON files to the scheduler.

## Run the installer

From the project directory on the scheduler:

```bash
DEPLOY_DIR="$HOME/cyclecloud-deploy/cyclecloud-multiregion-amlfs"
sudo scripts/install.sh --role scheduler --enabled \
  --config-json "$(cat "$DEPLOY_DIR/regions.json")" \
  --mounts-json "$(cat "$DEPLOY_DIR/mounts-regionA.json")"
```

Use real resources and mount sources. If mounts are already managed elsewhere,
omit `--mounts-json` to avoid competing mount definitions. Alternatively use
`--config /absolute/path/config.py` for a trusted root-owned Python config.
The JSON options take JSON strings, not filenames.

> **Important**
>
> The installer writes the hook files before enabling the Lua plugin and preserves
> the existing `slurm.conf` symlink. It doesn't restart Slurm. Omit `--reconfigure`
> so you can inspect the resulting configuration first.

### Existing burst-buffer integrations

The installer validates ownership before changing hooks, Slurm configuration, or
runtime permissions. It refuses to enable over another integration's
`burst_buffer.lua` or `burst_buffer.conf`, and refuses to enable or disable an
unowned active Lua integration. A `BurstBufferType=burst_buffer/lua` setting or
the extension's named configuration block alone does not establish ownership.

Ownership is recorded as hook paths and content hashes in the existing
`/opt/cyclecloud-multi-region/installation.json`. Unmodified 0.2.0 installations
without those hashes can upgrade using their matching project record and known
hook contents, including the configured interpreter path. Keep that record with
the hook files when backing up the installation. Modified or unrelated hooks
must be reviewed: restore the extension's matching record and files, or back up
and relocate the conflicting integration and its directive before retrying.
There is no automatic takeover.

Disabling a verified installation removes its Lua directive, including an
external directive accepted by the older installer. Hooks, data, mounts, and
configuration remain in place, and the helper retains Slurm-user access so
in-flight work can finish. Reconfigure Slurm to apply the disabled setting.

## Activate the configuration

After reviewing the files, run:

```bash
sudo scontrol reconfigure
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow selftest
```

Selftest checks file system read access, not an archive/import round trip.
Keep the complete Azure CycleCloud definition aligned using the
[existing-cluster update procedure](cyclecloud-import.md#update-an-existing-cluster).
Manual installation does not configure future nodes or refresh Jetpack's
startup-configuration snapshot. Do not clear completion markers or rerun
unrelated cluster-init projects to work around stale configuration.

## Roll back an installation

If rollback is necessary, restore the reviewed configuration and matching
cluster definition before reconfiguring Slurm. Do not delete AMLFS, Blob data,
or shared state as an installation rollback.

## Next steps

- [Run and monitor multi-region Slurm jobs](usage.md).
- [Troubleshoot deployment and staging](troubleshooting.md).
