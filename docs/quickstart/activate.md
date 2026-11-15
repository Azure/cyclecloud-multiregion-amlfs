---
title: Start and verify staging
description: Start the new cluster, inspect cluster-init installation, activate the Slurm hooks, and verify an end-to-end staged workload.
---

# 4. Start and verify staging

Start the reviewed cluster definition from [step 3](configure.md), then
verify installation and data movement on the Slurm scheduler.

## Start the cluster

In the administration-host shell:

```bash
cyclecloud start_cluster "$CLUSTER_NAME"
```

Starting allocates billable compute. Wait for successful cluster-init and
regional mounts. Azure CycleCloud applies the plugin configuration as nodes
initialize; the native Slurm autoscaler manages regional compute capacity.

## Inspect and activate the scheduler hooks

On the **Slurm scheduler**, inspect:

- `/etc/cyclecloud-multi-region/config.py` for the expected regions and partitions.
- `/etc/slurm/burst_buffer.lua` and `/etc/slurm/burst_buffer.conf` for installed hooks.
- The home Lustre mount and Slurm-owned shared state directory.

Once the hook files are present, activate the configuration and check read access:

```bash
sudo scontrol reconfigure
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow selftest
```

Use the configured Slurm service account if its name differs from `slurm`.
Preserve the existing `slurm.conf` symlink and shared scheduler state ownership.
Selftest verifies AMLFS GET access. A staged workload in the next step
exercises archive/import permissions and the data path.

## Run a staged workload

Follow [Submit an example workload](../usage.md#submit-an-example-workload)
as a workload user with access to home AMLFS. Prepare the example input,
submit the job to a configured remote partition, and inspect its returned
output after stage-out.

Confirm these outcomes:

| Check | Expected outcome |
|---|---|
| Stage-in | Input is exported to the shared Blob namespace and imported into the selected regional AMLFS |
| Execution | The job runs in its selected partition and reads/writes the regional Lustre mount |
| Stage-out | Results are exported from the remote AMLFS and imported back into home AMLFS |
| Output | Returned content, checksum, and execution details match the submitted workload |

Repeat sequentially for each remote partition using the example's shared
paths. For concurrent workloads, allocate distinct per-run staging subtrees.
See [Blob namespace and path mapping](../data-paths.md) for the path contract.

## Operate the cluster

Use [Run and monitor jobs](../usage.md) for log and job-state commands, and
[Troubleshooting](../troubleshooting.md) for staging or deployment failures.
Apply future definition and node changes through the
[existing-cluster update procedure](../cyclecloud-import.md#update-an-existing-cluster).
