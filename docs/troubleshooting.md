---
title: Troubleshoot deployment and staging
description: Diagnose Azure CycleCloud Multi-region plugin for AMLFS provisioning, permissions, routing, mounts, and interrupted staging operations.
---

# Troubleshoot deployment and staging

This article describes common Azure CycleCloud Multi-region plugin for AMLFS symptoms,
possible causes, and resolution steps. Diagnose resource provisioning separately
from job staging: a deployed resource doesn't prove that its data path is ready.

## Prerequisites

- Read access to Azure deployment records and the affected resources.
- For job diagnostics, access to the Slurm scheduler, job state, and runtime logs.
- The affected job ID and the effective regional configuration.

## Collect diagnostic information

For staging failures, start on the scheduler with the helper log and actual job state:

```bash
scontrol show job "<job-id>"
sudo tail -n 100 /var/log/slurm/amlfs_flow.log
sudo journalctl -u slurmctld -n 100 --no-pager
```

> **Important**
>
> Review diagnostic output before sharing it. Don't include credentials, tokens,
> or exported cluster parameters in public reports.

## Provisioning fails before any job runs

### Possible causes

HSM permissions, private storage connectivity, regional capacity, or subnet sizing
can prevent AMLFS deployment from completing.

### Resolution

1. Check actual ARM provisioning state and the failed operation, not just resource
   existence. HSM readiness checks may pass before Blob access fails later.
2. Confirm the **HPC Cache Resource Provider** has **both Storage Account
   Contributor and Storage Blob Data Contributor on the storage account**.
   Contributor operators cannot grant these roles. Ask a role administrator;
   adding roles to the scheduler doesn't fix the provider's permissions.
3. Check private Blob DNS and connectivity from every AMLFS subnet, and required
   service egress. Review [network and RBAC prerequisites](installation.md).
4. Verify regional SKU/capacity availability and dedicated AMLFS subnet sizing.

## Project installed but staging is inactive

### Possible causes

The node configuration might not enable the plugin, the runtime files might
be missing, or Slurm might not have loaded the updated configuration.

### Resolution

Check that the full node definition includes
`cyclecloud-multiregion-amlfs:default:0.2.0`, `cyclecloud.multi_region.enabled=true`, and
the correct role. A project upload does not update existing running nodes.
The enabled scheduler needs runtime configuration and the Slurm burst-buffer
plugin enabled; explicitly reconfigure after reviewing the generated settings.
Do not replace an existing cluster with a partial template to fix this.

## Unknown or ambiguous partition

### Possible causes

The job doesn't resolve to one supported partition, or the effective region map
doesn't contain that partition.

### Resolution

The Lua hook requires one partition from `scontrol show job <id> -o`; the Python
runtime requires it in `PARTITION_REGIONS`. Check spelling, the effective
configuration, and native Slurm partition definitions. Do not work around an
error by defaulting to home or another remote region. Use one partition rather
than a comma-separated list.

## Job waits in `BurstBufferStageIn` or stage-out

### Possible causes

The operation may be queued on a **Python source or destination resource lock**, polling an
export iteration or shutdown, or waiting for import. Consult both logs and the
job-owned auto-export and import resources in Azure.

### Resolution

Confirm that all cooperating callers share the same `STATE_DIR`.

If the `slurm` account cannot write state, correct directory ownership and
permissions in the installation. The Python runtime owns file system resource locks;
don't add separate Lua locks around it.

An archive collision can also come from a manual operation outside the runtime.
Coordinate the resource's use; do not cancel an archive of unknown ownership.
Disjoint file system pairs do not guarantee isolation of shared Blob paths.

> **Warning**
>
> Retained lock files are normal. Never delete a lock file to unblock a job or
> cancel an archive whose ownership is unknown.

## Selftest passes but stage-in fails

### Possible causes

Selftest checks AMLFS GET access only. It doesn't establish write permissions,
HSM provider roles, Blob data access, network access, or a valid mount.

### Resolution

1. Verify scheduler permissions for archive and ImportJob creation, read, and deletion.
2. Verify HSM provider permissions on the storage account.
3. Confirm that the source path is under the correct configured mountpoint.
4. Inspect AMLFS and Blob operation details, private DNS resolution, and firewall access.

To run the installed GET check from any working directory:

```bash
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow selftest
```

Credential failures can mean no assigned VM identity, an unreachable IMDS
endpoint, or unintended `AZURE_*` credentials taking precedence.
Resource-not-found errors usually mean the configured subscription, resource
group, or file system is wrong. Avoid sharing credential-bearing diagnostic logs.

## Wrong data or missing files on compute

### Possible causes

The node might mount the wrong regional file system, the data might not be
hydrated yet, or stage-in might have retained an existing destination file.

### Resolution

Verify the mount **source**, not only that `/lustre` exists:

```bash
findmnt -T /lustre -o SOURCE,FSTYPE,TARGET
ls -l /lustre/data/multi-region-demo/input/message.txt
sha256sum /lustre/data/multi-region-demo/input/message.txt
```

Each nodearray must mount its regional AMLFS. `REGIONS` does not itself mount
anything. An import can expose metadata before all data is hydrated; read the
file and compare checksums, not just directory listings.

Default stage-in `Skip` keeps existing destination files. Use a fresh isolated
workload prefix or deliberately select a replacement policy; do not infer that a
successful import refreshed stale data.

## Interrupted operations and cleanup

### Possible causes

Continuation tokens may be recorded, but automatic token-based resume is not
implemented. An interrupted operation can still be active in Azure.

### Resolution

Check the Azure operation state before retrying. A job's route is recorded:
changing its partition or file system endpoints mid-lifecycle isn't a safe retry strategy.

For an inactive job, retry its scoped cleanup:

```bash
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow teardown \
  --job "<job-id>"
```

Cleanup disables and deletes recorded job-owned exports and deletes recorded
ImportJobs, leaving failed cleanup state for retry. It does not globally cancel archives. Never bulk-delete all
`bb-*` resources or erase state files to hide failures. For an orphan not in
state, establish its exact ARM identity and ownership before administrator
intervention.

## SDK or image mismatch

### Possible causes

The command might use a different Python environment, or the node image might
lack a kernel-compatible Lustre client.

### Resolution

Use the installed project interpreter and the dependency versions declared in
`requirements.txt`. The runtime requires StorageCache SDK 4.x, version 4.1.0 or later.
Install a compatible project version rather than independently upgrading scheduler
packages or editing generated payloads. A missing Lustre client is an image
prerequisite failure; the plugin does not manage kernel/client packages.

## Related content

- [Configure managed identities and permissions](managed-identity.md).
- [Review regional networking prerequisites](manual-infrastructure.md).
- [Review staging and cleanup behavior](staging.md).
