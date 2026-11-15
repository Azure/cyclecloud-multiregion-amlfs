---
title: Configure and import the cluster
description: Generate a complete multi-region Slurm template, review regional settings, and import a new cluster definition.
---

# 3. Configure and import the cluster

Continue in the same administration shell and fetched project directory.
Use the regional resource inventory and permissions prepared in
[step 2](infrastructure.md).

## Generate the configuration

Run the interactive wizard with the **Azure CycleCloud Slurm project release**
that matches your installation. This version identifies the CycleCloud
integration project; the Slurm daemon has its own version.

```bash
DEPLOY_DIR="$HOME/cyclecloud-deploy/multi-region-new"
python3 scripts/configure.py --slurm-version 4.0.11 --regions 4 \
  --output-dir "$DEPLOY_DIR"
```

Choose the total participating region count, including home. Supply a fresh
output-directory path for the wizard to create.

Provide your credential name, existing identity ID, selected locker, and
regional values from `"$INFRA_DIR/plan.outputs.json"`. Retain the home's
existing **compute subnet**, and choose regional images with compatible
Lustre clients.

Enable the plugin when infrastructure, identity grants, and client images are
ready. Remote arrays start at zero nodes and default to one maximum node.
Review these limits against your intended workload capacity.

The wizard writes `slurm-multi-region.txt`, `parameters.json`, `regions.json`,
and regional mount JSON files. See [Generate cluster configuration](../configuration.md)
for offline templates, parameter preservation, and prompt details.

## Review the generated files

Compare the generated template with `slurm-upstream.txt`. Check the
parameters, images, mounts, identity, network settings, and node limits.
Exported parameters may contain sensitive values; keep the directory private.

Use a new cluster name for this creation procedure. Existing-cluster changes
follow the [controlled update procedure](../cyclecloud-import.md#update-an-existing-cluster),
including its definition-preservation and review steps.

## Import the cluster definition

```bash
CLUSTER_NAME="slurm-multi-region"
cyclecloud import_cluster "$CLUSTER_NAME" \
  -f "$DEPLOY_DIR/slurm-multi-region.txt" \
  -c Slurm \
  -p "$DEPLOY_DIR/parameters.json"
cyclecloud show_cluster "$CLUSTER_NAME"
```

Replace `Slurm` with the template's actual `[cluster ...]` section name if
different. The import registers the complete definition for review and the
explicit start step that follows.

See [Import cluster configuration](../import-configuration.md) for the full
review checklist.

**Next:** [Start and verify staging](activate.md).
