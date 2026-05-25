# Architecture

## Components

```
                      ┌──────────────────────────────────────────────────┐
                      │                 Azure CycleCloud                 │
                      │    provisions Slurm + AMLFS in both regions,     │
                      │    deploys amlfs_flow plugin on scheduler VM     │
                      └──────────────────────────────────────────────────┘
                                               │ provision (ARM / Bicep)
                                               ▼
                              ┌──────────────────────────────────┐
                              │   Azure Resource Manager (ARM)   │
                              └──────────────────────────────────┘
                                               │ control plane for both AMLFS
                     ┌─────────────────────────┴─────────────────────────┐
                     ▼                                                   ▼
┌────────────────────────────────────────────┐    ┌────────────────────────────────────────────┐
│        PRIMARY REGION   (e.g. SCUS)        │    │       SECONDARY REGION   (e.g. WUS3)       │
│ ────────────────────────────────────────── │    │ ────────────────────────────────────────── │
│                                            │    │                                            │
│  ┌──────────────────────────────────────┐  │    │  ┌──────────────────────────────────────┐  │
│  │          Slurm scheduler VM          │  │    │  │    Compute nodes (secondary pool)    │  │
│  │ ──────────────────────────────────── │  │    │  │ ──────────────────────────────────── │  │
│  │  slurmctld                           │  │    │  │         sbatch jobs run here         │  │
│  │          │ partition routing         │  │    │  │        #SBATCH -p <secondary>        │  │
│  │          ▼                           │  │    │  │                                      │  │
│  │  burst_buffer.lua                    │  │    │  │    POSIX read / write on /lustre     │  │
│  │          │ flock(/sched/...)         │  │    │  │       (SECONDARY AMLFS mount)        │  │
│  │          ▼                           │  │    │  │                                      │  │
│  │  python -m amlfs_flow                │  │    │  │    CycleCloud autoscales the pool    │  │
│  │          │ azure-mgmt-storagecache   │  │    │  │           on PENDING jobs            │  │
│  │          │ Managed Identity (SAMI)   │  │    │  └──────────────────────────────────────┘  │
│  │          ▼ archive / import / poll   │  │    │                                            │
│  └──────────────────────────────────────┘  │    │                     │                      │
│                     │                      │    │                     ▼                      │
│                     ▼                      │    │  ┌──────────────────────────────────────┐  │
│  ┌──────────────────────────────────────┐  │    │  │           SECONDARY  AMLFS           │  │
│  │            PRIMARY  AMLFS            │  │    │  │ ──────────────────────────────────── │  │
│  │ ──────────────────────────────────── │  │    │  │    mount: /lustre   (HSM-linked)     │  │
│  │    mount: /lustre   (HSM-linked)     │  │    │  │     archive_status / import_jobs     │  │
│  │     archive_status / import_jobs     │  │    │  │                                      │  │
│  │   jobs on primary partition use it   │  │    │  │      workload reads input after      │  │
│  │    directly — no staging required    │  │    │  │    stage-in; writes are picked up    │  │
│  └──────────────────────────────────────┘  │    │  └──────────────────────────────────────┘  │
│                     │                      │    │                     │                      │
│                     │ archive ▶ / ◀ import │    │ archive ▶ / ◀ import│                      │
└─────────────────────┴──────────────────────┘    └─────────────────────┴──────────────────────┘
                      │                                                 │
                      └────────────────────────┬────────────────────────┘
                                               ▼
                         ┌────────────────────────────────────────────┐
                         │ Azure Blob container   (shared HSM target) │
                         │ ────────────────────────────────────────── │
                         │   both AMLFS link to the SAME container;   │
                         │    blob keys == AMLFS filesystem paths     │
                         └────────────────────────────────────────────┘


Cross-region data flow
──────────────────────
  stage-in   :  PRIMARY AMLFS  ──archive──▶  Blob  ──import──▶  SECONDARY AMLFS
  workload   :  jobs read/write on SECONDARY AMLFS (no scheduler hop)
  stage-out  :  SECONDARY AMLFS ──archive──▶  Blob  ──import──▶  PRIMARY AMLFS
  teardown   :  ImportJob ARM resources deleted

```

## Partition routing

| Slurm partition       | Lustre mount     | Staging behavior                    |
|-----------------------|------------------|-------------------------------------|
| `PRIMARY_PARTITION`   | PRIMARY AMLFS    | **Skipped** — direct access        |
| `SECONDARY_PARTITION` | SECONDARY AMLFS  | stage_in + stage_out via Blob HSM  |

The Lua hook reads the job's partition via `scontrol show job <id>` and
short-circuits with `slurm.SUCCESS` whenever the partition matches
`PRIMARY_PARTITION` — `#BB_LUA` directives are ignored, the Python
helper isn't even invoked.

## Data flow

### Stage-in (primary → secondary)

```
primary AMLFS.archive(filesystem_path=/data/run42)
   ─poll archive_status until Completed─▶ files in Blob (shared HSM container)

secondary AMLFS.importJobs.create(import_prefixes=[/data/run42])
   ─LROPoller.result()─▶ files visible on secondary AMLFS at /lustre/data/run42

importJobs.delete()   # remove ARM resource
```

### Stage-out (secondary → primary)

Mirror of stage-in — archive from secondary AMLFS, import into primary AMLFS.

## Why HSM-shared container?

Both AMLFS instances are HSM-linked to the **same** Blob container. The
archive writes blobs at the filesystem-relative path (e.g.
`/data/run42/file.bin`), and the importing AMLFS reads the same path —
so no path translation is needed.

The `BLOB_STAGE_PREFIX` in `config.py` is empty for this reason. If you
later use *separate* containers, set it to a per-side prefix and adjust
`_blob_prefix()` in `__main__.py`.

## Locking

All lock files live in `/sched/slurm-amlfs-flow/`, which is on the shared NFS
volume mounted on all scheduler nodes (and reachable from any future HA
backup scheduler).

| Lock file                                  | Held by | Protects                              |
|--------------------------------------------|---------|---------------------------------------|
| `/sched/slurm-amlfs-flow/archive-stagein.lock`     | Lua hook (`flock(1)`) | primary AMLFS archive — one at a time |
| `/sched/slurm-amlfs-flow/archive-stageout.lock`    | Lua hook (`flock(1)`) | secondary AMLFS archive — one at a time |

AMLFS only allows one active **archive** per filesystem (ARM API
constraint). The two archive locks are independent so stage-in archives
(on PRIMARY) and stage-out archives (on SECONDARY) can proceed
concurrently.

**ImportJobs are NOT serialized** — AMLFS supports multiple concurrent
ImportJobs per filesystem, and the Python helper relies on that to
overlap data hydration across in-flight jobs.

Multiple jobs queue at `flock` for archives; the second job blocks until
the first releases its lock.

## Retry & resilience

- `tenacity`-based exponential backoff on `HttpResponseError` 429/5xx
  (8 attempts, 4–120s backoff).
- ImportJob LRO continuation tokens are persisted to
  `/sched/slurm-amlfs-flow/job-<id>-<phase>.token`. If `slurmctld` restarts
  mid-stage, the next invocation can resume by `from_continuation_token()`.
- Archive polling uses `aml_filesystems.get(...).hsm.archive_status[]`
  filtered by `filesystem_path` — incremental and idempotent.

## Identity & RBAC

Scheduler VM uses **System-Assigned Managed Identity**;
`DefaultAzureCredential()` picks it up automatically.

Required role assignments:

| Scope                    | Role                                     |
|--------------------------|------------------------------------------|
| primary AMLFS (resource)       | `Azure Managed Lustre Contributor`       |
| secondary AMLFS (resource)       | `Azure Managed Lustre Contributor`       |
| HSM Blob container       | `Storage Blob Data Contributor`          |
| Subscription / both RGs  | `Reader`                                 |

## File layout (installed)

```
/opt/slurm-amlfs-flow/
├── venv/                          isolated Python environment
└── amlfs_flow/
    ├── __init__.py
    ├── __main__.py                CLI: stage-in / stage-out / teardown / selftest
    └── config.py                  cluster-specific (auto-generated)

/etc/slurm/
├── burst_buffer.lua               Slurm BB hook
├── burst_buffer.conf              BB pool + timeouts
└── slurm.conf                     (must include BurstBufferType=burst_buffer/lua)

/sched/slurm-amlfs-flow/                   shared (NFS) state dir
├── archive-stagein.lock           flock — primary AMLFS archive serialization
├── archive-stageout.lock          flock — secondary AMLFS archive serialization
└── job-<id>-<phase>.token         LRO continuation tokens

/var/log/slurm/amlfs_flow.log        Python helper structured log
```
