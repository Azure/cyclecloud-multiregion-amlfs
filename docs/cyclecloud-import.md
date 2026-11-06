---
title: Import or update a Slurm cluster definition
description: Upload the cluster-init project, import a complete Azure CycleCloud Slurm template, or update an existing cluster with a rollback plan.
---

# Import or update a Slurm cluster definition

This article explains how to upload the application project, register a reusable
template, or update an existing Azure CycleCloud Slurm cluster. Uploading the
project and importing the complete cluster definition are separate operations.

For a new deployment with generated configuration, use the
[deployment guide](installation.md). This advanced procedure covers manual imports,
activation on existing nodes, and rollback.

## Prerequisites

- Bash, Python 3.8 or later, and an Azure CycleCloud CLI compatible with your server.
- Azure CycleCloud project and cluster management access, plus locker upload access.
- A fetched project directory and a complete, version-matched Slurm template.
- Provisioned regional infrastructure and [identity permissions](managed-identity.md).
- For an existing cluster, its authoritative template, operational backups, and
  an approved maintenance window. An upstream template isn't a backup of a customized cluster.

## Deployment artifacts

| Artifact | Purpose | Deployment operation |
|---|---|---|
| `project.ini` and `specs/default` | Versioned node installation scripts and payload | `cyclecloud project upload` to a locker |
| Complete Slurm template | Nodes, regional nodearrays, configuration, project references, and parameters | `import_template` or `import_cluster` |
| Deployment parameters | Environment-specific identifiers and JSON strings | `-p parameters.json` or the template's UI fields |
| AMLFS and shared source Blob container | Data storage and HSM integration | Provision separately before enabling the plugin |

> **Note**
>
> The project locker isn't the HSM data container. Preserve existing locker
> access and configure HSM storage permissions separately. Uploading to a locker
> doesn't require a Git commit or push.

## Prepare the administration environment

Run the following commands in the same Bash session, from the fetched project
directory, with `cyclecloud` on `PATH`.

Authenticate interactively and select an existing writable locker:

```bash
cyclecloud initialize
cyclecloud locker list
LOCKER="azure-storage"
cyclecloud locker show "$LOCKER"
```

Use your deployment's locker name, not necessarily `azure-storage`. Keep TLS
certificate verification enabled and configure trust for your server's CA.
Do not put passwords, tokens, or storage keys in template files or command-line
arguments. The operator needs Azure CycleCloud project/cluster-management access and
locker upload access; those are not the scheduler's AMLFS runtime permissions.

For a new cluster, use the [configuration wizard](configuration.md#run-the-configuration-wizard)
to prepare the complete template and JSON files outside the project. Don't
create the output directory in advance:

```bash
umask 077
DEPLOY_DIR="$HOME/cyclecloud-deploy/multi-region-new"
python3 scripts/configure.py --slurm-version 4.0.11 --regions 4 \
  --output-dir "$DEPLOY_DIR"
```

Retain this protected directory for configuration and rollback.
Exported parameters can contain sensitive
values. The wizard generates:

| File in `$DEPLOY_DIR` | Contents |
|---|---|
| `slurm-multi-region.txt` | Complete, version-matched Slurm template with the plugin merged in |
| `parameters.json` | Complete original parameters plus the plugin's declared parameters |
| `regions.json` | Runtime configuration with real resource identifiers |
| `mounts-regionA.json`, `mounts-regionB.json`, `mounts-regionC.json`, `mounts-regionD.json` | Regional mount lists, using actual AMLFS mount sources |

The mount filenames correspond to the regions in your configuration.
Neither `examples/region-nodearrays.txt` nor `examples/regions.json` is a
complete cluster template or complete cluster parameter file.

For an existing cluster, [preserve its original definition and export its
parameters](#update-an-existing-cluster) before preparing replacement files.
Set `DEPLOY_DIR` to an existing, operator-owned deployment directory with mode
`0700`; don't run the new-cluster generation command above for this path.
Use [manual template editing](slurm-template.md), or supply the supported
original template and parameters to the wizard. Don't substitute a stock
template for the existing definition.

## Upload the application project

Fetch and verify the published project as described in
[Import the cluster-init project into Azure CycleCloud](project-import.md).
From its directory, upload to your chosen locker:

```bash
cyclecloud project upload "$LOCKER"
```

The merged template must reference the uploaded project's version:

```ini
[[[cluster-init cyclecloud-multi-region:default:0.2.0]]]
```

Custom projects do **not** get the built-in `cyclecloud/` prefix. Match the
project's source locker and the node's locker access as described in the
[template guide](slurm-template.md). Keep the existing Slurm cluster-init
references. Uploading a new payload does not update a cluster definition,
enable staging, or rerun scripts on existing nodes.

## Choose an import method

`TEMPLATE_CLUSTER` identifies the `[cluster ...]` section **inside the file**.
`CLUSTER_NAME` is the new or existing deployed cluster instance. A catalog
template name is a third, separate name. Replace all example values accordingly:

```bash
TEMPLATE_CLUSTER="Slurm"
CLUSTER_NAME="slurm-multi-region"
```

### Create a new cluster

```bash
cyclecloud import_cluster "$CLUSTER_NAME" \
  -f "$DEPLOY_DIR/slurm-multi-region.txt" \
  -c "$TEMPLATE_CLUSTER" \
  -p "$DEPLOY_DIR/parameters.json"
```

Review the resulting definition in Azure CycleCloud before starting it. Starting
allocates infrastructure and incurs charges:

```bash
cyclecloud start_cluster "$CLUSTER_NAME"
```

Use conservative regional nodearray limits when introducing new regions.

### Register a reusable template

To make the template available for repeated use in the Azure CycleCloud portal
or CLI, import it into the template catalog instead:

```bash
CATALOG_TEMPLATE="Slurm-MultiRegion"
cyclecloud import_template "$CATALOG_TEMPLATE" \
  -f "$DEPLOY_DIR/slurm-multi-region.txt" \
  -c "$TEMPLATE_CLUSTER"
```

Select that template when creating a cluster in the Azure CycleCloud portal and populate
its parameters. Alternatively:

```bash
cyclecloud create_cluster "$CATALOG_TEMPLATE" "$CLUSTER_NAME" \
  -p "$DEPLOY_DIR/parameters.json"
```

Review and start the new cluster as described in [Create a new cluster](#create-a-new-cluster).
Updating a catalog template doesn't update previously created cluster instances.

### Update an existing cluster

Use a maintenance window appropriate to your workload. Obtain the **complete
authoritative template used by this cluster**, including local customizations.
An upstream Slurm template is not a backup of your customized deployment.
The CLI has no `export_cluster` command that reconstructs that complete file.

1. Save the original template as `$DEPLOY_DIR/slurm.before.txt`.

2. Export the current parameters before editing:

   ```bash
   CLUSTER_NAME="your-existing-cluster"
   cyclecloud export_parameters "$CLUSTER_NAME" --format=json \
     -o "$DEPLOY_DIR/parameters.before.json"
   ```

   This exports parameters only, not the complete cluster definition. Preserve
   the node and nodearray inventory and existing operational backups as well.

3. Merge the plugin into a copy of the authoritative template and its parameters
   into a copy of the exported parameters. Review both diffs privately:

   ```bash
   diff -u "$DEPLOY_DIR/slurm.before.txt" "$DEPLOY_DIR/slurm-multi-region.txt"
   diff -u "$DEPLOY_DIR/parameters.before.json" "$DEPLOY_DIR/parameters.json"
   ```

   Expected differences include regional arrays, plugin configuration, identity
   attachment if needed, and project references. Stop if existing nodes, networks,
   mounts, identities, or Slurm settings are unexpectedly removed or replaced.

4. Only after reviewing the complete replacement, apply it:

   ```bash
   cyclecloud import_cluster "$CLUSTER_NAME" \
     -f "$DEPLOY_DIR/slurm-multi-region.txt" \
     -c "$TEMPLATE_CLUSTER" \
     -p "$DEPLOY_DIR/parameters.json" \
     --force
   ```

5. Compare the resulting nodearrays and parameters against the preserved inventory.

> **Warning**
>
> `--force` replaces the existing definition; omitted settings can be lost.
> If you can't obtain the complete definition, use your supported complete-definition
> update process instead. Don't force-import a fragment or delete and recreate the cluster.

## Activate the plugin on new and existing nodes

For a **fresh scheduler**, the installer must create the hook files before
Slurm loads `BurstBufferType=burst_buffer/lua`. Follow the ordering and
persistent Slurm configuration instructions in the
[template guide](slurm-template.md); do not pre-enable the plugin against
missing files. New regional compute nodes must receive their own regional
mount settings.

For **already running nodes**, uploading a project or changing a cluster
definition is not enough. Cluster-init completion markers and the node's
startup-configuration snapshot can prevent the new settings from taking effect.
Even `jetpack converge --cluster-init` is not proof that configuration refreshed
or that a previously completed script reran. Do not blanket-rerun unrelated
cluster-init projects on a production scheduler.

Use a supported rolling node-update/recreation procedure, or the
[manual scheduler installation](manual-installation.md)
for an explicitly reviewed in-place update. Keep the complete Azure CycleCloud
definition aligned so replacement and future nodes receive the same settings.
Inspect effective node configuration using the tools supported by your Jetpack
version. Keep configuration files private and do not create obsolete files to
work around stale startup settings.

## Roll back an update

Complete the [activation steps](quickstart/activate.md#inspect-and-activate-the-scheduler-hooks)
before using the plugin.

Before rollback, stop new staged submissions and allow active staging operations
and jobs to finish. Restore the reviewed original complete template/parameters
through the same controlled update process, and restore the corresponding
scheduler configuration backups before reconfiguring Slurm. Setting
`enabled=false` is not a promise to uninstall existing hook files or undo a
previous Slurm configuration edit. Do not delete AMLFS, Blob data, or shared
state as an installation rollback.

## Next steps

Continue with [Run and monitor multi-region Slurm jobs](usage.md).

## Related content

- [Azure CycleCloud projects and lockers](https://learn.microsoft.com/en-us/azure/cyclecloud/how-to/projects?view=cyclecloud-8).
- [Azure CycleCloud cluster templates and parameters](https://learn.microsoft.com/en-us/azure/cyclecloud/how-to/cluster-templates?view=cyclecloud-8).

Use your installed CLI's help for version-specific options.
