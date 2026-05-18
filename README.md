# Slurm Burst Buffer for Cross-Region AMLFS

Slurm `burst_buffer/lua` plugin that transparently moves data between two
Azure Managed Lustre (AMLFS) filesystems in different regions, using a
shared Azure Blob container as the staging tier — driven entirely by the
[`azure-mgmt-storagecache`](https://learn.microsoft.com/python/api/azure-mgmt-storagecache/)
Python SDK from the Slurm scheduler.

No `azcopy`, no data movers, no SSH between regions.

## Scenario

```
                      ┌───────────────────────────────┐
                      │        Slurm scheduler        │
                      │        (primary region)       │
                      └───────────────┬───────────────┘
                                      │ ARM API (Managed Identity)
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
   ┌────────────────┐         ┌────────────────┐         ┌────────────────┐
   │    PRIMARY     │         │      Blob      │         │   SECONDARY    │
   │     AMLFS      │   HSM   │   container    │   HSM   │     AMLFS      │
   │   (Region A)   │◀───────▶│    (shared)    │◀───────▶│   (Region B)   │
   └───────┬────────┘         └────────────────┘         └───────┬────────┘
           │                                                     │
   /lustre on `hpc`                                      /lustre on `htc`
   primary partition                                     secondary partition
   [no staging needed]                                   [stage in / stage out]
```

Users submit a normal Slurm job to the **secondary partition** with
`#BB_LUA stage_in` / `#BB_LUA stage_out` directives; the plugin:

1. **stage_in** → `archive` primary AMLFS subtree to Blob, then `ImportJob` Blob → secondary AMLFS
2. Job runs on secondary-region compute
3. **stage_out** → `archive` secondary AMLFS subtree to Blob, then `ImportJob` Blob → primary AMLFS

Jobs submitted to the **primary partition** skip staging entirely — they
already have direct access to the primary AMLFS on `/lustre`.

## Repository layout

```
slurm-amlfs-flow/
├── slurm/
│   ├── burst_buffer.lua          Lua hook (installed at /etc/slurm/)
│   └── burst_buffer.conf         BB pool / timeouts
├── amlfs_flow/
│   ├── __init__.py
│   ├── __main__.py               Python helper (stage-in/out/teardown/selftest)
│   └── config.example.py         Per-cluster resource IDs (rename to config.py)
├── scripts/
│   ├── install.sh                Installs venv, hook, lock dir, perms
│   ├── selftest.sh               Wraps `amlfs_flow selftest`
│   └── uninstall.sh
├── examples/
│   └── job-example.sh            Reference sbatch script
├── docs/
│   ├── architecture.md
│   ├── installation.md
│   ├── usage.md
│   └── troubleshooting.md
├── requirements.txt
├── LICENSE
└── README.md
```

## Quick start

### 1. Prerequisites

- Azure CycleCloud / Slurm 23.x+ cluster (tested on Slurm 25.05)
- One AMLFS per region, HSM-linked to the **same** Blob container
- Both regions mount AMLFS at `/lustre` on compute nodes
- Scheduler VM has Managed Identity with:
  - `Azure Managed Lustre Contributor` on **both** AMLFS resources
  - `Storage Blob Data Contributor` on the Blob container

### 2. Install

```bash
git clone <this repo>
cd slurm-amlfs-flow
sudo ./scripts/install.sh \
  --subscription <SUB_ID> \
  --amlfs-primary-rg <RG_A> --amlfs-primary-name <FS_A> \
  --amlfs-secondary-rg <RG_B> --amlfs-secondary-name <FS_B> \
  --primary-partition hpc \
  --secondary-partition htc
```

This installs to:
- `/opt/slurm-amlfs-flow/venv/`        — Python venv
- `/opt/slurm-amlfs-flow/amlfs_flow/`    — Python helper
- `/etc/slurm/burst_buffer.lua`, `burst_buffer.conf`
- `/sched/slurm-amlfs-flow/`       — locks & per-job state

Add to `/etc/slurm/slurm.conf`:

```conf
BurstBufferType=burst_buffer/lua
DebugFlags=BurstBuffer
```

Then:

```bash
sudo scontrol reconfigure
sudo ./scripts/selftest.sh
```

### 3. Submit a job

```bash
#!/bin/bash
#SBATCH -p htc
#SBATCH -N 1
#BB_LUA stage_in  source=/lustre/data/run42 type=directory
#BB_LUA stage_out source=/lustre/data/run42 type=directory

./solver /lustre/data/run42
```

`sbatch` then `squeue` — the `(BurstBufferStageIn)` reason indicates the
plugin is actively archiving/importing.

## How serialization works

AMLFS only allows **one active archive per filesystem**. The Lua hook
wraps each Python invocation with `flock(1)` on:
`/sched/slurm-amlfs-flow/archive-stagein.lock` and `archive-stageout.lock`,
so concurrent jobs queue at the scheduler instead of getting ARM errors.

## Validation

End-to-end tested with:

| Step                          | Status |
|-------------------------------|--------|
| Selftest (RBAC)               | ✅     |
| Stage-in archive (A→Blob)     | ✅     |
| Stage-in import (Blob→B)      | ✅     |
| Job run on htc node           | ✅     |
| Stage-out archive (B→Blob)    | ✅     |
| Stage-out import (Blob→A)     | ✅     |
| ImportJob cleanup             | ✅     |

## License

MIT — see [LICENSE](LICENSE).
