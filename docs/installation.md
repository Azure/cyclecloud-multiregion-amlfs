---
title: Quick start guide
description: Deploy and operate the cyclecloud-multiregion-amlfs plugin in five steps on an existing Azure CycleCloud server.
---

# Quick start guide

Create a new Slurm cluster with the `cyclecloud-multiregion-amlfs` Azure CycleCloud
plugin on an existing Azure CycleCloud server. This guide follows the complete
deployment sequence, from uploading the cluster-init project to running a
staged workload.

| Step | Outcome |
|---|---|
| [1. Prepare and upload the plugin](quickstart/prepare.md) | Confirm prerequisites, connect the administration tools, and upload the project to a locker |
| [2. Provision storage and permissions](quickstart/infrastructure.md) | Create regional networks and AMLFS resources, and assign HSM and scheduler access |
| [3. Configure and import the cluster](quickstart/configure.md) | Generate and review a complete Slurm cluster definition, then import it |
| [4. Start and verify staging](quickstart/activate.md) | Initialize nodes, activate the hooks, and verify a workload's input and returned output |
| [5. Run and monitor jobs](usage.md) | Submit workloads, monitor staging and execution, and inspect returned results |

Use the same administration-host shell throughout the steps so `LOCKER`,
`INFRA_DIR`, `DEPLOY_DIR`, and `CLUSTER_NAME` remain available. Commands that
run on the Slurm scheduler are identified separately.

The procedure preserves native Slurm integration and `/sched` and `/shared`
storage, and adds regional AMLFS mounts for workload data. Infrastructure
deployment and cluster start create billable Azure resources.

For an existing cluster, follow the
[controlled update procedure](cyclecloud-import.md#update-an-existing-cluster)
to retain its authoritative definition and site-specific settings.

Start with [Prepare and upload the plugin](quickstart/prepare.md).
