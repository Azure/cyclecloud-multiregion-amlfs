# Usage

## Which partition do I submit to?

| Submit to             | What happens                                       |
|-----------------------|----------------------------------------------------|
| `PRIMARY_PARTITION`   | Job runs directly against PRIMARY AMLFS. `#BB_LUA` directives are **ignored** — no staging. |
| `SECONDARY_PARTITION` | Plugin stages data PRIMARY → SECONDARY before the job and SECONDARY → PRIMARY after the job. |

The default partition names are `hpc` (primary) and `htc` (secondary);
override them at install time with `--primary-partition` / `--secondary-partition`.

## Job script syntax

Add `#BB_LUA` directives anywhere in the comment block (alongside
`#SBATCH`) of your batch script. Only meaningful when submitting to the
secondary partition:

```bash
#!/bin/bash
#SBATCH -p htc                  # secondary partition → staging applies
#SBATCH -N 1
#SBATCH -t 01:00:00

#BB_LUA stage_in  source=/lustre/data/run42/input  type=directory
#BB_LUA stage_out source=/lustre/data/run42/output type=directory

./solver --in /lustre/data/run42/input --out /lustre/data/run42/output
```

### Directive fields

| Field          | Required | Notes                                    |
|----------------|----------|------------------------------------------|
| `source=PATH`  | yes      | Absolute path under `/lustre/`           |
| `type=`        | no       | `directory` (default) or `file`          |

> `destination=` is **not** required — both regions mount AMLFS at the
> same path (`/lustre`), so source = destination by convention.
> The plugin selects which AMLFS to archive from based on the stage
> direction (in/out).

## Multiple directives

You can declare multiple `stage_in` and `stage_out` lines; they're
processed sequentially per filesystem:

```bash
#BB_LUA stage_in source=/lustre/data/run42/inputs  type=directory
#BB_LUA stage_in source=/lustre/data/shared/refs   type=directory

#BB_LUA stage_out source=/lustre/data/run42/results type=directory
#BB_LUA stage_out source=/lustre/data/run42/logs    type=directory
```

## Lifecycle

```
   sbatch job.sh
        │
        ▼
   ┌──────────────────────┐
   │      submitted       │  ──▶  slurm_bb_job_process()   validates paths
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │       pending        │  ──▶  slurm_bb_data_in()       archive PRIMARY→Blob, import Blob→SECONDARY
   │ (BurstBufferStageIn) │
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │       pending        │  ──▶  Slurm allocates compute on secondary partition
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │   configuring (CF)   │  ──▶  node boots, mounts secondary AMLFS at /lustre
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │       running        │  ──▶  your script runs against /lustre
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │     completing       │  ──▶  slurm_bb_data_out()      archive SECONDARY→Blob, import Blob→PRIMARY
   │     (Stage-out)      │
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │      completed       │
   └──────────────────────┘
```

## Monitoring

```bash
# Queue with BB reason
squeue -a

# Detailed job state
scontrol show job <id>

# Live Python helper log
sudo tail -f /var/log/slurm/amlfs_flow.log

# Slurm controller log
sudo journalctl -u slurmctld -f
```

## Manual operations

Run any phase by hand for debugging:

```bash
# Stage-in just like Slurm would
sudo /opt/slurm-amlfs-flow/venv/bin/python -m amlfs_flow stage-in \
     --job test001 --paths /lustre/data/run42/input

# Stage-out
sudo /opt/slurm-amlfs-flow/venv/bin/python -m amlfs_flow stage-out \
     --job test001 --paths /lustre/data/run42/output

# Teardown (cancel in-flight, clean state)
sudo /opt/slurm-amlfs-flow/venv/bin/python -m amlfs_flow teardown --job test001
```

## Expected latency

Steady-state for cold data (typical values; tune to your AMLFS tier):

| Operation                | Latency          |
|--------------------------|------------------|
| `archive` start          | ~3–5s            |
| `archive` 1 GB / dir     | ~30–60s          |
| `archive` 100 GB / dir   | ~5–15 min        |
| `ImportJob` create + run | ~30–90s          |
| `ImportJob` delete       | ~30s             |

End-to-end stage-in latency for a 1 GB dataset in our validation test
was **~95 s**.

## Best practices

- **Stage directories, not individual files.** One archive call per
  parent path keeps ARM rate-limiting and ImportJob count low.
- **Group data into per-project subtrees** under `/lustre/data/`.
  Avoid mixing project A's stage-in and project B's stage-out in the
  same directory.
- **Idempotent jobs welcome.** Archive is incremental — re-running a
  job stages only the delta.
- **Use `EnableStageIn` / `EnableStageOut` flags** in
  `burst_buffer.conf` if you want to enforce that compute jobs *must*
  declare staging.
