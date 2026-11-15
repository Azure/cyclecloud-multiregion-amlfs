---
title: Import cluster configuration
description: Review and import a generated Slurm template and parameter file to create a new cluster on an existing Azure CycleCloud server.
---

# Import cluster configuration

This article shows you how to import the files produced by
[Generate cluster configuration](configuration.md) into Azure CycleCloud.
You create a new Slurm cluster definition from the complete template and its
parameters, then review it before starting any nodes.

Importing the configuration is separate from
[uploading the cluster-init project](project-import.md). Both must be complete
before nodes can install the plugin.

> **Warning**
>
> This procedure is for a new cluster. To modify an existing cluster, use the
> [controlled update procedure](cyclecloud-import.md#update-an-existing-cluster).
> Don't add `--force` to replace an existing definition with unreviewed configuration.

## Prerequisites

- An existing Azure CycleCloud server and an initialized, compatible Azure
  CycleCloud CLI on your administration host.
- Permission to create and inspect clusters on that server.
- A complete generated `slurm-multi-region.txt` and matching `parameters.json`,
  kept in the protected output directory from the configuration wizard.
- The referenced cluster-init project version uploaded to the selected locker.
- Regional infrastructure, managed identity permissions, and Lustre-capable
  images ready before enabling the plugin and starting nodes.
- A new cluster name that isn't already in use.

## Select the configuration and cluster name

On the administration host, set the following values in a Bash session:

```bash
DEPLOY_DIR="$HOME/cyclecloud-deploy/multi-region-new"
TEMPLATE_CLUSTER="Slurm"
CLUSTER_NAME="slurm-multi-region"
```

Replace `DEPLOY_DIR` with the existing wizard output directory.
`TEMPLATE_CLUSTER` identifies the `[cluster ...]` section inside the template;
use its actual name if it isn't `Slurm`. `CLUSTER_NAME` is the name of the
new deployed cluster, not a template catalog name.

Keep the same shell open for the remaining commands.

## Review the generated configuration

1. Compare `slurm-multi-region.txt` with `slurm-upstream.txt` in the output
   directory. Confirm that native Slurm projects, existing node definitions,
   shared NFS mounts, identities, and scheduler settings are preserved.

2. Privately review `parameters.json` using the following checklist:

   | Setting | What to verify |
   |---|---|
   | Credentials and identity | The Azure CycleCloud credential name and existing managed identity are correct. |
   | Project reference | The version and source locker match the uploaded cluster-init project. |
   | Regional resources | Every AMLFS resource, Azure location, compute subnet, and mount source is correct. |
   | Images and mounts | Each region uses a kernel-compatible Lustre client image and its own local AMLFS. |
   | Partitions and limits | Partition names match the runtime map. Remote arrays start at zero nodes, with reviewed maximum limits. |
   | Plugin state | `MultiRegionEnabled` is intentional. The wizard defaults to disabled; enable only when prerequisites are ready. |

3. Confirm that `MultiRegionConfigJson` and the regional mount parameters remain
   serialized JSON strings. `MultiRegionEnabled` must be a JSON boolean. Don't
   serialize the generated values a second time.

> **Important**
>
> Use the complete generated template and parameter file. `regions.json`,
> individual mount files, and `examples/region-nodearrays.txt` aren't substitutes
> for these inputs. Parameter files can contain sensitive values; keep them
> private and don't commit them.

## Import the new cluster

Run the following command on the administration host:

```bash
cyclecloud import_cluster "$CLUSTER_NAME" \
  -f "$DEPLOY_DIR/slurm-multi-region.txt" \
  -c "$TEMPLATE_CLUSTER" \
  -p "$DEPLOY_DIR/parameters.json"
```

Wait for the command to complete successfully. If the cluster name already
exists, choose a new name or follow the existing-cluster update procedure.
Don't delete the existing cluster or force an import to resolve a name conflict.

This operation creates the cluster definition. It doesn't start nodes, create
the regional AMLFS infrastructure, or upload missing project artifacts.

## Verify the imported definition

1. Inspect the cluster:

   ```bash
   cyclecloud show_cluster "$CLUSTER_NAME"
   ```

2. Open the cluster in the Azure CycleCloud portal. Confirm the nodearrays and
   parameters match the reviewed files, including regional locations, subnets,
   images, identities, project references, and node limits.

3. Leave the cluster stopped until the configuration and prerequisites are ready.
   Keep the generated files as the source for future reviews and controlled updates.

Import success verifies that Azure CycleCloud accepted the definition. It
doesn't establish that Lustre mounts, runtime permissions, or data staging work
on the nodes.

## Next steps

- [Start the cluster and verify staging](quickstart/activate.md).
- [Run and monitor multi-region Slurm jobs](usage.md) after installation completes.

For reusable catalog templates or existing-cluster updates, see
[Import or update a Slurm cluster definition](cyclecloud-import.md).
