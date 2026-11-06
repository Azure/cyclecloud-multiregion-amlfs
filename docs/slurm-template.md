---
title: Edit a Slurm cluster template manually
description: Add the multi-region cluster-init project, runtime configuration, and regional nodearrays to a complete Azure CycleCloud Slurm template.
---

# Edit a Slurm cluster template manually

Use this procedure to add Azure CycleCloud Multi-region plugin for AMLFS to a customized
Slurm template that the configuration wizard doesn't support. For supported
templates, use the [configuration wizard](configuration.md) instead. Don't repeat
these edits on generated files.

## Prerequisites

- A complete, authoritative Slurm cluster definition and its existing parameter values.
- A protected deployment directory and reviewed backups of the original files.
- The project version uploaded to an accessible Azure CycleCloud locker.
- Regional AMLFS, compute subnets, mount sources, and compatible Lustre client images.
- The scheduler's [managed identity and runtime permissions](managed-identity.md).

> **Warning**
>
> Every INI block below and [region-nodearrays.txt](../examples/region-nodearrays.txt)
> is a fragment, not an importable cluster template. Merge sections into existing
> definitions without duplicating them. Importing an incomplete definition,
> especially with `--force`, can discard settings and nodearrays.

## Preserve the version-matched Slurm definition

Start with your complete deployed definition or a complete template from the
[Azure CycleCloud Slurm project](https://github.com/Azure/cyclecloud-slurm), selecting
the release that matches your installation. The Azure CycleCloud Slurm project version
and Slurm daemon version are separate. Keep the Slurm project/image versions
matched to your installation; do not replace an older cluster with a newer
template merely to add this plugin.

Preserve:

- `[[node defaults]]`, existing parameters, image/VM settings, credentials,
  `Azure.Identities`, `cluster.identities.default`, and network configuration.
- Native `cyclecloud/slurm:default`, `:scheduler`, `:login`, and `:execute`
  specs and their `slurm.role` settings. Keep `nodearraybase` and its inheritance.
- `/sched` and `/shared` mounts/exports, scheduler persistent volumes, HA
  configuration, and the native Slurm autoscaler. Regional AMLFS at `/lustre`
  does **not** replace the scheduler's shared NFS paths.
- Existing site-specific cluster-init.

The plugin stages data; it neither installs a replacement scheduler nor
creates partitions from `PARTITION_REGIONS`. The hooks target the Slurm
25.x/26.x callback contracts; confirm compatibility with your installed version.

## Add the plugin parameters

Add this new parameter group to the complete template, alongside its existing
top-level `[parameters ...]` sections:

```ini
[parameters Multi-Region]
    [[parameters Multi-region configuration]]
        [[[parameter MultiRegionEnabled]]]
        Label = Enable multi-region
        DefaultValue = false
        Widget.Plugin = pico.form.BooleanCheckBox

        [[[parameter MultiRegionProjectLocker]]]
        Label = Uploaded cyclecloud-multi-region source locker
        ParameterType = String

        [[[parameter MultiRegionConfigJson]]]
        Label = Scheduler runtime settings as JSON
        ParameterType = String
        DefaultValue = "{}"

        [[[parameter MultiRegionAMountsJson]]]
        ParameterType = String
        DefaultValue = "[]"

        [[[parameter MultiRegionBMountsJson]]]
        ParameterType = String
        DefaultValue = "[]"

        [[[parameter MultiRegionCMountsJson]]]
        ParameterType = String
        DefaultValue = "[]"

        [[[parameter MultiRegionDMountsJson]]]
        ParameterType = String
        DefaultValue = "[]"

        [[[parameter MultiRegionBLocation]]]
        Label = Azure location for regionB
        ParameterType = String

        [[[parameter MultiRegionCLocation]]]
        Label = Azure location for regionC
        ParameterType = String

        [[[parameter MultiRegionDLocation]]]
        Label = Azure location for regionD
        ParameterType = String
```

The `*Json` parameters are JSON strings, not nested objects or filenames. Populate
`MultiRegionConfigJson` with the serialized contents of an edited
[examples/regions.json](../examples/regions.json). Populate each mounts parameter
with a serialized list such as:

```json
[{"source":"REGION_MGS_IP@tcp0:/lustrefs","mountpoint":"/lustre","options":"defaults,_netdev"}]
```

Replace `REGION_MGS_IP` with that file system's actual MGS endpoint, and use its
actual file system name and network suffix. Do not copy one region's endpoint into
another region's value. Use the recipe below to serialize JSON into the
deployment parameter file without manual double-escaping.
> **Important**
>
> Empty defaults aren't a working enabled configuration. Fill in the runtime
> settings and mounts before setting `MultiRegionEnabled = true`.

`regionA`, `regionB`, `regionC`, and `regionD` are logical labels. Set each
`MultiRegion*Location` to the real Azure location in its matching `REGIONS`
entry. Keep the original template's home `Region` and subnet settings aligned
with `regionA`; do not use the logical labels as Azure location values.

### Prepare the deployment parameter file

Use the operator-owned `DEPLOY_DIR` from the [import guide](cyclecloud-import.md),
outside this checkout. Keep its complete `slurm-multi-region.txt` and
`parameters.json`; the latter must contain the existing upstream parameter
values as a JSON object. Preserve a reviewed backup before updating it.

Create `regions.json` there from the example, replacing resource identifiers,
and four mount-list files using the JSON list above:

| Deployment file | Parameter | Local file system |
|---|---|---|
| `mounts-regionA.json` | `MultiRegionAMountsJson` | Region A (home) AMLFS |
| `mounts-regionB.json` | `MultiRegionBMountsJson` | Region B AMLFS |
| `mounts-regionC.json` | `MultiRegionCMountsJson` | Region C AMLFS |
| `mounts-regionD.json` | `MultiRegionDMountsJson` | Region D AMLFS |

After editing all five inputs, run this from your operator workstation. It
only updates local parameters; it neither uploads nor deploys anything.
`MULTI_REGION_PROJECT_LOCKER` must name the same locker as `LOCKER` in the import guide.

```bash
export DEPLOY_DIR='<absolute-deployment-directory>'
export MULTI_REGION_PROJECT_LOCKER='<uploaded-project-locker-name>'
(
  cd "$DEPLOY_DIR" || exit 1
  python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path("parameters.json")
parameters = json.loads(path.read_text())
if not isinstance(parameters, dict):
    raise ValueError("parameters.json must be the complete parameter object")
inputs = {
    "MultiRegionConfigJson": ("regions.json", dict),
    "MultiRegionAMountsJson": ("mounts-regionA.json", list),
    "MultiRegionBMountsJson": ("mounts-regionB.json", list),
    "MultiRegionCMountsJson": ("mounts-regionC.json", list),
    "MultiRegionDMountsJson": ("mounts-regionD.json", list),
}
for parameter, (filename, expected_type) in inputs.items():
    value = json.loads(Path(filename).read_text())
    if not isinstance(value, expected_type) or not value:
        raise ValueError(f"{filename} must contain a nonempty {expected_type.__name__}")
    parameters[parameter] = json.dumps(value, separators=(",", ":"))
runtime = json.loads(parameters["MultiRegionConfigJson"])
for letter in "BCD":
    parameters[f"MultiRegion{letter}Location"] = runtime["REGIONS"][f"region{letter}"]["location"]
parameters["MultiRegionProjectLocker"] = os.environ["MULTI_REGION_PROJECT_LOCKER"]
parameters["MultiRegionEnabled"] = True
path.write_text(json.dumps(parameters, indent=2) + "\n")
PY
)
```

Review the result. `MultiRegionEnabled` is a JSON boolean, while `MultiRegionConfigJson`
and all four mounts values are JSON **strings**. The three location parameters
come from the corresponding runtime region entries. This performs only structural
checks; it does not discover MGS addresses, validate live resources, or grant
permissions. Preserve all original credentials/region/VM/image/autoscale
parameter values, adjusting only those your reviewed deployment requires.

## Attach the project after native Slurm

Merge into the existing defaults node:

```ini
[[node defaults]]
    [[[cluster-init cyclecloud-multi-region:default:0.2.0]]]
    SourceLocker = $MultiRegionProjectLocker
    Order = 10000

    [[[configuration]]]
    cyclecloud.multi_region.enabled = $MultiRegionEnabled
    cyclecloud.multi_region.role = compute
    cyclecloud.multi_region.mounts_json = "[]"
    cyclecloud.multi_region.reconfigure = false
```

The project name is `cyclecloud-multi-region`, version **0.2.0**, spec **default**.
Use **no `cyclecloud/` prefix** for this custom project. Set `MultiRegionProjectLocker`
to the existing user locker to which you uploaded this exact project version.

The official [cluster-init reference](https://learn.microsoft.com/en-us/azure/cyclecloud/cluster-references/cluster-init-reference?view=cyclecloud-8)
defines both `SourceLocker` and `Locker`: `SourceLocker` stages the uploaded
project from that user locker into the node's target locker, including for
other regions; `Locker` names the locker from which the node downloads it.
This guide explicitly selects **SourceLocker** and retains Azure CycleCloud's target
locker selection rather than guessing a global `Locker` setting. Check source
and target locker access for each region. Do not change built-in Slurm references
to use the custom locker.

Specs normally run in declaration order, with defaults inherited first; simply
adding this spec to defaults could otherwise run it before the scheduler spec.
`Order = 10000` explicitly places it after the stock Slurm specs, whose default
orders begin at 1000. Check any site-specific explicit orders: native Slurm
configuration must exist, shared `/sched` must be available, and required
Lustre client preparation must have completed **before** this installer runs.
The script's `20-` filename orders scripts within this project; it does not
establish ordering against another project's specs.

The inherited plugin role `compute` is deliberate: it configures mounts on
execute **and login** nodes without changing their native `slurm.role`. Override
the plugin role for the scheduler next. Leave unrelated infrastructure nodes
disabled if your template has additional roles.

## Configure the scheduler and runtime region map

Merge into the existing scheduler:

```ini
[[node scheduler]]
EnableTerminateNotification = true
    [[[configuration]]]
    cyclecloud.multi_region.role = scheduler
    cyclecloud.multi_region.config_json = $MultiRegionConfigJson
    cyclecloud.multi_region.mounts_json = $MultiRegionAMountsJson
    cyclecloud.multi_region.reconfigure = false
```

`enabled` and the custom spec are inherited from defaults. Preserve
`slurm.role = scheduler` and the native scheduler spec. The scheduler home mount
is useful for preparing/checking files; it does not need all remote mounts to
make ARM requests. Omit its mounts override if it needs no local data access.
Check HA inheritance separately: upstream `scheduler-ha` extends `scheduler`;
peers need the same runtime configuration and a shared `STATE_DIR` supporting
file locks and writable by the Slurm service account.

The runtime JSON connects Azure resources to Slurm:

| Key | Required relationship |
|---|---|
| `SUBSCRIPTION_ID` | Default subscription; a region may override it with `subscription_id`. |
| `REGIONS` | Arbitrary region keys, each with `resource_group`, `filesystem`, Azure `location`, and `mountpoint`. Use actual existing AMLFS resources. |
| `HOME_REGION` | One key from `REGIONS`, such as `regionA`, not an Azure location string. |
| `PARTITION_REGIONS` | Every partition used for staged jobs maps to a `REGIONS` key. For example `multi-regionB` maps to `regionB`, whose `location` contains the actual Azure location identifier. |
| `STATE_DIR` | Shared scheduler-owned state path; example `/sched/cyclecloud-multi-region`. Preserve it across scheduler operation and job cleanup. |

Keep home mappings for the actual `hpc`, `htc`, `gpu`, and `dynamic` partitions
your cluster uses. All example mounts are `/lustre`, but each node mounts its
local file system there. The shared HSM Blob layout supplies data movement
between them; a shared mountpoint name alone does not share data. Do not change
a job's partition/region route while staging or cleanup is outstanding.

Preserve the template's existing identity attachment. Follow
[managed identity](managed-identity.md) to grant the scheduler AMLFS runtime
permissions, select the intended identity, and retain locker permissions.
For Azure CycleCloud Workspace for Slurm, normally keep the attached
`ccwLockerManagedIdentity` and add the required role assignments; an identity
override is not needed for that path.
`MultiRegionSchedulerIdentity`, if used there, is an additional explicitly defined
parameter, not one supplied automatically by the upstream template.

## Configure home mounts and regional nodearrays

Merge [examples/region-nodearrays.txt](../examples/region-nodearrays.txt) into
the full definition. It contains home login/compute mount overrides and three
remote nodearrays, using the parameters above; do not import the file alone.

For each remote nodearray:

1. Retain `Extends = nodearraybase` to inherit the native execute spec, role,
   autoscaler, and network-interface settings. Select a region-available image
   with a kernel-matched Lustre client; the example reuses `$HPCImageName`.
2. Set both Azure `Region` and `SubnetId`. The latter uses
   **`resource-group/vnet/computeSubnet`**, not a raw subnet resource ID, and
   must point at the compute subnet rather than the dedicated AMLFS subnet.
   The illustrative `rg-region-*`, `vnet-region-*`, and `computeSubnet` names need replacing.
3. Set `slurm.partition` to the exact partition key in `PARTITION_REGIONS`.
   Preserve cluster `Autoscale` and inherited `slurm.autoscale = true`.
   The examples use non-HPC `Standard_D4as_v5` VMs, with
   `slurm.hpc = false` and `slurm.use_pcpu = false`; adapt these together for
   your production hardware.
4. Start bounded: `InitialCount = 0`, `MaxCount = 1` for each example remote
   array. A per-array cap is not a cluster-wide budget; review all other
   arrays' limits too. Do not force allocation just by adding the definition.
5. Supply only that region's `mounts_json`. Home login and all participating
   home compute arrays use `MultiRegionAMountsJson`; remote arrays explicitly
   select their own parameters. Keep existing NFS mounts intact.

Installing the plugin does not install a Lustre kernel/client package or
repair networking. The installer refuses conflicting `/etc/fstab` entries or
an existing different file system at `/lustre`; reconcile those definitions
before enabling it. Enable and review termination notifications/unmount handling
for these Lustre clients: upstream Slurm only auto-enables notifications for
its own Lustre mount parameters, not this plugin's `mounts_json`. The example
sets `EnableTerminateNotification = true` on the participating arrays; keep the
upstream timeout and clean unmount handling.

### Add more regions

For every additional region:

- Provision an AMLFS with the compatible shared HSM container/path layout,
  dedicated AMLFS subnet, and separate compute subnet.
- Establish scheduler/NFS/Azure CycleCloud/locker reachability, regional Lustre
  access, private Blob DNS/routes, and required outbound access.
- Add one `REGIONS` entry and runtime identity grants on its file system.
- Add its mounts JSON parameter and one or more bounded native execute
  nodearrays with the correct `Region`, `SubnetId`, image, and partition.
- Add every new partition to `PARTITION_REGIONS` and check the native
  autoscaler's generated Slurm partitions before submitting jobs.
- Review the new region's configuration before raising limits. Retain the
  single intended `HOME_REGION`.

## Activate the plugin

> **Warning**
>
> Don't set `BurstBufferType=burst_buffer/lua` in `slurm.additional.config`,
> an upstream Slurm parameter, or `slurm.conf` on a fresh scheduler before the
> hook files exist. Native Slurm can start before the plugin. Enabling the plugin
> without `burst_buffer.lua` and `burst_buffer.conf` can prevent controller startup.

The plugin installer creates its runtime, dependencies, state/log ownership,
and hook files first, then adds `BurstBufferType=burst_buffer/lua` to the
existing Slurm configuration (preserving its symlink). An already running
controller still needs a reconfigure to activate the change. The installer
does not restart Slurm and defaults to **no reconfigure**.

After confirming successful cluster-init and installed hooks, explicitly run
`sudo scontrol reconfigure` while the controller
is running. Alternatively, set `cyclecloud.multi_region.reconfigure = true`
**on the scheduler only**, when your provisioning order guarantees that the
controller is already running. The flag is unconditional on the scheduler:
do not enable it where `scontrol` cannot contact a controller. Subsequent native
Slurm regeneration must retain/reapply the plugin setting after hook
installation; review it after upgrades rather than enabling it prematurely.

### Installer settings

| `cyclecloud.multi_region.*` | Installer behavior/default |
|---|---|
| `enabled` | Boolean, default `false`; explicitly enable mounts and scheduler hooks. |
| `role` | `scheduler` or `compute`. Default `auto` maps native `scheduler` to scheduler and `execute` to compute. Other enabled nodes, including login nodes, need an explicit plugin role, as in these fragments. |
| `config_json` | JSON **object string**, not a path; provide the runtime configuration on the enabled scheduler. |
| `config_path` | Alternative trusted Python config file already on the node. Mutually exclusive with `config_json`; absent both, an existing installed config may be reused. |
| `mounts_json` | JSON **list string**; default `[]`. Each object requires `source` and `mountpoint`, with optional `options` (default `defaults,_netdev`). |
| `reconfigure` | Boolean, default `false`; when true on a scheduler, runs `scontrol reconfigure` after installation. |

Editing the cluster definition or uploading version `0.2.0` again does
**not** refresh a running node's startup configuration snapshot or rerun
completed cluster-init scripts. Use the reviewed deployment/update procedure in
[Azure CycleCloud import](cyclecloud-import.md); do not clear completion markers or
assume an ordinary converge installs new settings.

## Next steps

- [Import or update the complete Slurm cluster definition](cyclecloud-import.md).
- [Run and monitor multi-region Slurm jobs](usage.md).
