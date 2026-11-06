---
title: Prepare and upload the plugin
description: Confirm deployment prerequisites, authenticate the administration tools, and upload the cluster-init project to a locker.
---

# 1. Prepare and upload the plugin

Run this step on the Azure CycleCloud VM or another authorized administration
host. The `cyclecloud-multi-region` cluster-init project supplies the plugin
runtime, Slurm hooks, node installer, and deployment tools.

## Confirm prerequisites

| Requirement | Details |
|---|---|
| Administration host | Python 3.8 or later, Azure CLI, and Azure CycleCloud CLI |
| Existing infrastructure | One subscription/resource group containing the parent VNet, Blob account, and data/log containers |
| Regions | Home is the parent VNet's location; choose the remote Azure locations and confirm AMLFS SKU availability, capacity, and quota |
| Network ranges | An IPAM-approved remote VNet address pool and free parent space for AMLFS/private-endpoint subnets |
| Permissions | Resource provisioning rights, HSM storage roles, scheduler runtime roles, and RBAC administration rights for role assignments |
| Node images | A kernel-matched Lustre client, with access to the scheduler, NFS, locker, and Azure APIs |

Retain the native Slurm integration and `/sched` and `/shared` mounts. Regional
AMLFS provides the workload filesystem, normally mounted at `/lustre`.

## Connect to Azure and Azure CycleCloud

On the administration host:

```bash
az login
cyclecloud initialize
cyclecloud locker list
```

On an authorized Azure VM, use `az login --identity` in place of `az login`.
An already initialized Azure CycleCloud CLI can retain its existing
configuration. Keep TLS certificate verification enabled.

Select your existing writable project locker:

```bash
LOCKER="azure-storage"
cyclecloud locker show "$LOCKER"
```

Replace `azure-storage` with your locker name. The locker stores installation
assets; the shared HSM data and log containers serve the AMLFS transfer path.

## Fetch and upload the project

Choose a published project version. This example uses `0.2.0`; substitute
your selected release and fetch into a new directory:

```bash
cyclecloud project fetch \
  https://github.com/Azure/cyclecloud-multiregion-amlfs/releases/0.2.0 \
  cyclecloud-multi-region
cd cyclecloud-multi-region
(cd blobs && sha256sum --check cyclecloud-multi-region.tar.gz.sha256)
cyclecloud project upload "$LOCKER"
```

The uploaded project is now available for download during node initialization.
Run the following steps' `scripts/...` commands from this fetched project
directory, using the same shell.

For locker selection and verification details, see
[Import the cluster-init project](../project-import.md).

**Next:** [Provision storage and permissions](infrastructure.md).
