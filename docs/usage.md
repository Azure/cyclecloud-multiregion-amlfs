---
title: Run and monitor multi-region Slurm jobs
description: Submit Slurm jobs with data staging directives and monitor or troubleshoot archive and import operations.
---

# 5. Run and monitor multi-region Slurm jobs

Azure CycleCloud Multi-region plugin for AMLFS stages input to a job's regional Azure
Managed Lustre (AMLFS) file system before execution and returns output to the
home file system afterward. This article shows you how to submit a staged
workload and monitor data movement.

## Prerequisites

- A completed [deployment](installation.md), with the plugin enabled and Slurm reconfigured.
- Regional Lustre mounts, shared HSM storage, and the required identity permissions.
- Access to submit Slurm jobs and prepare input data on home AMLFS.
- For diagnostics and manual operations, access to the scheduler and the Slurm service account.

## Select a regional partition

The [four-region configuration](../examples/regions.json) uses these partitions:

| Partition | Regional file system | Staging |
|---|---|---|
| `hpc`, `htc`, `gpu`, `dynamic` | Region A (home) | None |
| `multi-regionB` | Region B | A → B before job; B → A after |
| `multi-regionC` | Region C | A → C before job; C → A after |
| `multi-regionD` | Region D | A → D before job; D → A after |

> **Important**
>
> Submit to one partition prepared during deployment. Use its name in
> `#SBATCH --partition` so the job and its data staging use the selected region.

## Add staging directives to a job

Include `#BB_LUA` directives in the Slurm job script to identify input and output paths:

```bash
#!/bin/bash
#SBATCH --partition=multi-regionB
#SBATCH --nodes=1
#SBATCH --time=00:30:00
#BB_LUA stage_in source=/lustre/data/run42/input type=directory
#BB_LUA stage_out source=/lustre/data/run42/output type=directory

set -euo pipefail
mkdir -p /lustre/data/run42/output
./solver --in /lustre/data/run42/input --out /lustre/data/run42/output
```

`source=` is required and must be an absolute path under the source region's
configured mountpoint. The source is home for stage-in and the selected target
for stage-out. The hook extracts paths; `type=directory` / `type=file` are
descriptive labels for the same archive/import mechanism. The destination
follows from the preserved filesystem-relative path and its regional mount
root. See [Blob namespace and path mapping](data-paths.md) for an example with
the same `/lustre` paths across regions.

Multiple directives are supported and processed sequentially within a stage.
Use paths without whitespace. Home-partition jobs skip staging and directly
access home AMLFS.

> **Warning**
>
> Stage-in defaults to `Skip`, which retains existing destination files rather than
> refreshing stale input. Stage-out defaults to `OverwriteAlways`, which can replace
> existing home data.

Select conflict policy with `STAGEIN_CONFLICT_MODE` and `STAGEOUT_CONFLICT_MODE`.
Use unique run directories when validating or when stale input could affect results.

## Submit an example workload

1. As the submitting user on a client mounted to home AMLFS, prepare both paths:

   ```bash
   mkdir -p /lustre/data/multi-region-demo/{input,output}
   printf 'Azure CycleCloud Multi-region plugin for AMLFS\n' > /lustre/data/multi-region-demo/input/message.txt
   ```

2. From the project directory on a Slurm submission host, submit the example job:

   ```bash
   sbatch --partition=multi-regionB examples/job-example.sh
   ```

3. After staging and execution complete, inspect the copied input, checksum, and
   execution details in `/lustre/data/multi-region-demo/output/$SLURM_JOB_ID/`.

Repeat sequentially with `--partition=multi-regionC` or
`--partition=multi-regionD`; the override
replaces the script's default partition.
Run this shared output-prefix example sequentially across regions.
For concurrent production jobs, generate job scripts with disjoint literal
stage-in/out paths: `#BB_LUA` comments are parsed as literal text.

## Monitor staging and execution

On the scheduler, inspect the job state and runtime logs:

```bash
squeue -a
scontrol show job "<job-id>"
sudo tail -f /var/log/slurm/amlfs_flow.log
sudo journalctl -u slurmctld -f
```

`BurstBufferStageIn` can indicate active archive/import **or waiting for a
resource lock**. Stage-out can outlast the compute workload. Transfer duration
depends on the data, regions, and SKU combination.

## Run manual staging operations

For an isolated diagnostic operation, prepare the source paths and run as the
Slurm service account using the installed interpreter. Use a unique job ID
reserved for the diagnostic operation. Supply the actual job submitter's UID
(not the Slurm service account's UID); this example uses the current user's UID.
Replace `slurm` if the cluster uses a different `SlurmUser`:

```bash
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow selftest
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow stage-in \
  --job manual001 --uid "$(id -u)" --partition multi-regionB --paths /lustre/data/run42/input
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow stage-out \
  --job manual001 --uid "$(id -u)" --partition multi-regionB --paths /lustre/data/run42/output
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow teardown \
  --job manual001
```

Teardown cleans the recorded job-owned exports, ImportJobs, and state.
The installed commands use Python's `-I` mode to isolate the installed package
and configuration from the checkout or job working directory.

> **Note**
>
> Selftest checks AMLFS GET access. A staged workload additionally verifies
> archive/import permissions and data movement.

## Related content

- [Troubleshoot deployment and staging](troubleshooting.md).
- [Understand routing, locking, and the data lifecycle](staging.md).
