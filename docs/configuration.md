---
title: Generate cluster configuration
description: Use the interactive configuration wizard to generate a complete Slurm template and regional JSON configuration for Azure CycleCloud.
---

# Generate cluster configuration

Use `scripts/configure.py` to generate a complete Slurm template and regional
JSON configuration. The interactive wizard supports any number of regions,
including the home region, and preserves supported existing settings.

> **Note**
>
> The wizard writes local files only. It doesn't provision resources, grant roles,
> upload a project, import a cluster, or start nodes. For the complete installation
> sequence, see the [Quick start guide](installation.md).

## Prerequisites

- Python 3.8 or later and the fetched project directory from
  [Import the cluster-init project into Azure CycleCloud](project-import.md).
- The exact [Azure CycleCloud Slurm project release](https://github.com/Azure/cyclecloud-slurm/releases)
  that matches your installation. This is different from the Slurm daemon version.
- Provisioned regional resources, mount sources, compute subnets, and compatible
  images. Obtain these values from the provisioning output or your manual inventory.
- The existing managed identity, Azure CycleCloud credential name, and project locker.
- A new output directory outside the project. Don't create it before running the wizard.

## Run the configuration wizard

1. From the fetched project directory, run the following command. Replace the
   version and region count with your values:

   ```bash
   python3 scripts/configure.py --slurm-version 4.0.11 --regions 4 \
     --output-dir "$HOME/cyclecloud-deploy/multi-region-new"
   ```

2. Answer the prompts using the following table. The count includes home:
   Region A, followed by Regions B, C, D, and so on. Omit the command-line options
   if you prefer to enter them interactively.

3. Review the [generated files](#review-the-generated-files) before importing the cluster.

The wizard downloads the selected release's complete template and pins native
Slurm project references. It doesn't upgrade the Slurm daemon or node images.

| Prompt | Input |
|---|---|
| Credentials | Azure CycleCloud credential name, not a password or token |
| Managed identity | Existing identity ARM ID; normally `ccwLockerManagedIdentity` for Azure CycleCloud Workspace for Slurm |
| Project locker | Locker to which you uploaded `cyclecloud-multiregion-amlfs` |
| Regional resources | Actual Azure location, subscription, resource group, and AMLFS name from the provisioning outputs |
| Mount source | Full MGS address and file system, for example `10.0.0.4@tcp:/lustrefs` |
| Compute subnet | `resource-group/vnet/subnet`, not the dedicated AMLFS subnet or an ARM resource ID |
| Remote compute | Partition name, VM size, Lustre-capable image, node limit, and HPC/CPU options |
| Enable plugin | Defaults to **no**; select **yes** only when resources, permissions, and clients are ready |

Remote arrays start at zero nodes and default to one maximum node. Each mounts
only its regional file system. Existing home limits and native Slurm settings
are retained. Region/partition labels are configurable.

## Preserve an existing template and parameters

Supply a complete, unextended, version-matched template and original parameter
JSON when using offline sources or retaining site-specific settings:

```bash
python3 scripts/configure.py --slurm-version 4.0.11 --regions 4 \
  --template-file "$HOME/cyclecloud-deploy/original/slurm.txt" \
  --parameters-file "$HOME/cyclecloud-deploy/original/parameters.json" \
  --output-dir "$HOME/cyclecloud-deploy/multi-region-revised"
```

The parameters file must contain a JSON object, not an ARM parameter envelope.
Identity, credentials, HA, NFS, images, VM settings, and other values are retained,
except for home location/subnet settings explicitly confirmed in the prompts.
Missing identity and credential names are prompted.

Fragments, duplicate sections, already extended templates, conflicting project
versions, and unsupported regional overrides are rejected rather than guessed.
For unsupported layouts, use [manual template editing](slurm-template.md) in
**Advanced guide**. Updating a deployed cluster also requires the
[controlled import procedure](cyclecloud-import.md#update-an-existing-cluster);
do not replace its custom definition with a stock template.

## Review the generated files

The output directory must be new. It is created with mode `0700`, and files
with mode `0600`; keep it outside the project directory.

| File | Contents |
|---|---|
| `slurm-upstream.txt` | Original input template for comparison |
| `slurm-multi-region.txt` | Complete extended cluster definition |
| `parameters.json` | Preserved parameters and generated plugin settings |
| `regions.json` | Runtime region and partition map |
| `mounts-regionA.json`, `mounts-regionB.json`, ... | Regional Lustre mount lists |
| `source.json` | Selected project version, template source, and checksum |

`MultiRegionConfigJson` and mount parameters are already serialized JSON strings.
`MultiRegionEnabled` is a boolean. Do not serialize the generated values again.
Compare the generated definition with the original and review the parameters
privately before import.

> **Important**
>
> Exported parameters can contain sensitive values. Keep the output directory
> protected and don't commit its contents.

## Next steps

- [Import cluster configuration](import-configuration.md).
- [Update an existing cluster](cyclecloud-import.md#update-an-existing-cluster).
