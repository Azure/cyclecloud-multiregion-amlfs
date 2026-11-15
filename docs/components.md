---
title: Components and ownership
description: Separate compute orchestration, Slurm job scheduling, plugin installation, and AMLFS data movement.
---

# Components and ownership

`cyclecloud-multiregion-amlfs` connects three cooperating systems: Azure CycleCloud
for compute orchestration, Slurm for job scheduling, and AMLFS HSM for data
movement. The plugin supplies the installation and coordination layer that
connects storage operations to the Slurm job lifecycle.

## Compute orchestration: Azure CycleCloud

Azure CycleCloud owns the cluster definition and the lifecycle of Azure
compute resources. Its native Slurm integration translates demand into
nodearray capacity, provisions nodes, and removes capacity according to the
configured autoscaling policy.

The operator defines regional nodearrays with their Azure locations, compute
subnets, VM sizes, images, identities, and scaling limits. Azure CycleCloud
applies that definition and runs cluster-init on the resulting nodes. Existing
Slurm projects, scheduler storage, and shared NFS mounts remain part of the
complete cluster definition.

## Job scheduling: Slurm

Slurm owns the queue, partition policies, priorities, resource allocation,
job execution, and job state. Users submit jobs to a configured partition and
declare input and output subtrees using `#BB_LUA` directives.

Slurm invokes the Lua burst-buffer callbacks at staging and cleanup boundaries.
The callbacks obtain the job's resolved partition and invoke the plugin's
Python runtime. Successful stage-in makes the job eligible to proceed under
Slurm's normal scheduling rules; stage-out returns the declared results.

The plugin's `PARTITION_REGIONS` setting associates that resolved partition
with its regional AMLFS. Keep this data map aligned with the partition's
regional compute placement in the cluster definition.

## Data movement: plugin coordination and AMLFS HSM

The plugin runs on the scheduler as the Slurm service account. It resolves
the source and destination file systems, converts source paths into
filesystem-relative prefixes, and acquires locks for both resources.

Using the scheduler's Azure identity, the runtime creates and monitors scoped
auto-export jobs and ImportJobs through Azure Resource Manager. It verifies
data-transfer completion and cleans up the operations recorded for that job.
Logs, route records, and lock files provide the operational state.

**AMLFS HSM moves the file bytes.** Export writes files into the shared Blob
container; import brings the matching Blob objects into the destination
AMLFS namespace. The application performs its working I/O against the local
Lustre mount. Scheduler traffic consists of management requests and status
responses.

## Installation: the cluster-init project

The `cyclecloud-multiregion-amlfs` project packages the installer, runtime, Slurm
hooks, and dependencies for distribution through an Azure CycleCloud project
locker. The cluster definition references the project and supplies settings
under `cyclecloud.multi_region.*`.

| Node role | Cluster-init work |
|---|---|
| Scheduler | Install the staging runtime and Python environment, write runtime configuration, prepare Slurm-owned state and logs, install Lua hooks, and apply the enabled Slurm integration |
| Regional compute | Configure and mount the explicitly declared regional Lustre file system using the image's Lustre client |
| Enabled login or other client nodes | Apply the explicitly selected plugin role and mount configuration |

The installation lifecycle follows native Jetpack cluster-init execution.
For an existing cluster, apply definition changes and node updates through the
[controlled update procedure](cyclecloud-import.md#update-an-existing-cluster).

## Infrastructure and workload ownership

| Owner | Preparation and ongoing responsibility |
|---|---|
| Infrastructure operator | Regional VNets, dedicated subnets, routes, DNS, private Blob access, AMLFS resources, and service capacity |
| Cluster operator | Compatible node images, cluster definition, partitions, mount sources, shared scheduler state, and activation of Slurm hooks |
| Identity administrator | Grant the scheduler node's managed identity access to the participating AMLFS resources |
| Workload owner | Input preparation, declared staging paths, stable source data during export, unique run subtrees, and output retention |

Use the [provisioning tools](provisioning.md) or an approved
[manual infrastructure process](manual-infrastructure.md) to prepare Azure
resources before enabling the plugin.

## Scheduler node identity

The **managed identity attached to the scheduler node** is the target for
AMLFS permission assignments. The plugin uses that identity to trigger,
monitor, and clean up export jobs and ImportJobs on each participating file
system, including home.

Choose one of two options:

1. Reuse the built-in CCWS managed identity, `ccwLockerManagedIdentity`.
2. Create a managed identity and attach it to the scheduler through the Azure
   CycleCloud cluster definition.

See [Configure scheduler identity](managed-identity.md) for these options
and the AMLFS runtime permissions.
