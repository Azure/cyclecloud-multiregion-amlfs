---
title: Blob namespace and path mapping
description: Follow a workload path from a regional Lustre mount through archive and import prefixes to the shared Blob namespace.
---

# Blob namespace and path mapping

The plugin uses a shared **Blob namespace** to connect regional AMLFS file
systems. Every participating AMLFS is integrated with the same data container
and a compatible HSM path layout. A workload's path relative to the file
system root identifies the same data in every region.

## Three views of the same data

| View | Example | Meaning |
|---|---|---|
| Client path | `/lustre/data/run42/input/model.dat` | Absolute path an application uses on a mounted AMLFS |
| Archive/import prefix | `/data/run42/input` | Subtree supplied to the AMLFS management API, relative to the filesystem or HSM namespace root |
| Blob key | `data/run42/input/model.dat` | Object name within the shared data container |

For this example, the data container is named `data`, and its root corresponds
to the AMLFS root. The full Blob URL is
`https://<account>.blob.core.windows.net/data/data/run42/input/model.dat`:
the first `data` is the container name; the second is part of the object key.

The archive/import prefix uses a leading `/` to identify a path from the
namespace root. The Blob key is shown relative to its container. The local
mount root `/lustre` belongs to the client view and is the same in every region.

## Mapping across regional mounts

[![Both regions use /lustre/data/run42/input/model.dat. Stage-in transfers input through Blob key data/run42/input/model.dat while preserving the application's absolute path. Stage-out returns output to the same /lustre/data/run42/output/result.dat path at home.](assets/path-mapping.svg)](assets/path-mapping.svg)

Every region mounts its own AMLFS at **`/lustre`**. The application sees the
same absolute paths wherever its job runs:

| Setting | Region A: home | Region B: target |
|---|---|---|
| `REGIONS[...]["mountpoint"]` | `/lustre` | `/lustre` |
| Input file | `/lustre/data/run42/input/model.dat` | `/lustre/data/run42/input/model.dat` |
| Output file | `/lustre/data/run42/output/result.dat` | `/lustre/data/run42/output/result.dat` |

During **stage-in**, the source is home. The runtime validates
`/lustre/data/run42/input` against the home mountpoint, removes `/lustre`, and
passes `/data/run42/input` to the source export and destination import.
AMLFS HSM maps the exported files to Blob keys under `data/run42/input/`.
The imported files appear at `/lustre/data/run42/input` in the target region,
ready for the application to use its original paths.

During **stage-out**, the source is the selected target region. The runtime
removes `/lustre` from `/lustre/data/run42/output`, exports
`/data/run42/output`, and imports that same prefix into home AMLFS.

This preserves a **seamless application path across regions**. Configure all
regions with the same mountpoint and workload layout. The provisioning tool sets HSM
`importPrefix` to `/`; per-job ImportJobs then select the workload prefixes
declared in the job.

## Express the paths in a job

The same input and output paths work on home and remote compute nodes:

```bash
#SBATCH --partition=multi-regionB
#BB_LUA stage_in source=/lustre/data/run42/input type=directory
#BB_LUA stage_out source=/lustre/data/run42/output type=directory

mkdir -p /lustre/data/run42/output
./solver --in /lustre/data/run42/input \
  --out /lustre/data/run42/output
```

Stage-in directives use the **home source path**. Stage-out directives use
the **target source path**. Both use `/lustre`, backed by the local region's
AMLFS. The plugin preserves the relative path through Blob Storage, so the
absolute application path remains unchanged at the destination.

Use literal absolute paths beneath the source mountpoint and keep directive
paths free of whitespace. Keep `/lustre` consistent in the regional mount
configuration and job scripts.

## Namespace ownership and refresh policy

Give each workload a dedicated subtree, such as `data/run42/`, and coordinate
every writer to that subtree across all regions. The shared Blob key names
identify common objects, so overlapping exports can replace each other's data.
Keep source files stable for the duration of their export.

Regional AMLFS contents are refreshed through explicit import operations.
The default stage-in policy, `Skip`, retains an existing destination file;
the default stage-out policy, `OverwriteAlways`, replaces existing home
files with returned results. Choose `STAGEIN_CONFLICT_MODE` and
`STAGEOUT_CONFLICT_MODE` to match the workload's ownership and freshness needs.

The runtime's filesystem locks coordinate its own transfers. Coordinate
external HSM operations and writers to shared Blob paths as part of workload
ownership, including workloads using different file system pairs.

Continue with [Routing and transfer coordination](staging.md) or
[Run and monitor jobs](usage.md).
