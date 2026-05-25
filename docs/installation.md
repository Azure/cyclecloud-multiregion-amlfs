# Installation

## Prerequisites

- Azure CycleCloud + Slurm cluster (tested with Slurm 25.05)
- Two AMLFS resources, one per region
- Both AMLFS instances **HSM-linked to the same Blob container**
- AMLFS mount path `/lustre` on all compute nodes (both regions)
- Scheduler VM has a System-Assigned Managed Identity
- Python 3.10+ on the scheduler

## RBAC

Grant the scheduler's Managed Identity:

```bash
SUB=<subscription-id>
SP_ID=<scheduler-managed-identity-object-id>

az role assignment create --assignee-object-id $SP_ID --assignee-principal-type ServicePrincipal \
    --role "Azure Managed Lustre Contributor" \
    --scope /subscriptions/$SUB/resourceGroups/<RG_A>/providers/Microsoft.StorageCache/amlFilesystems/<FS_A>

az role assignment create --assignee-object-id $SP_ID --assignee-principal-type ServicePrincipal \
    --role "Azure Managed Lustre Contributor" \
    --scope /subscriptions/$SUB/resourceGroups/<RG_B>/providers/Microsoft.StorageCache/amlFilesystems/<FS_B>

az role assignment create --assignee-object-id $SP_ID --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" \
    --scope /subscriptions/$SUB/resourceGroups/<RG_STORAGE>/providers/Microsoft.Storage/storageAccounts/<SA>/blobServices/default/containers/<CONTAINER>
```

## Install

```bash
git clone https://github.com/<org>/slurm-amlfs-flow.git
cd slurm-amlfs-flow

sudo ./scripts/install.sh \
    --subscription          <SUB_ID> \
    --amlfs-primary-rg            <RG_A>   \
    --amlfs-primary-name          <FS_A>   \
    --amlfs-secondary-rg            <RG_B>   \
    --amlfs-secondary-name          <FS_B>   \
    --primary-partition    hpc      \
    --secondary-partition    htc    \
    --stagein-conflict-mode  Skip            \
    --stageout-conflict-mode OverwriteAlways
```

The last two flags are optional; defaults shown.

### ImportJob conflict resolution

Each ImportJob reconciles incoming blobs against any pre-existing file on
the destination AMLFS. Choose how that's handled per direction:

| Mode                | Behavior                                              |
|---------------------|-------------------------------------------------------|
| `Skip`              | Keep the existing file; ignore the blob               |
| `OverwriteAlways`   | Replace destination unconditionally                   |
| `OverwriteIfDirty`  | Replace only if the existing file diverges from blob  |
| `Fail`              | Abort the ImportJob on any conflict                   |

Defaults are tuned for typical HPC use:

- **Stage-in: `Skip`** — preserves any working files already on the
  compute side from a previous job; the ImportJob only hydrates new data.
- **Stage-out: `OverwriteAlways`** — guarantees fresh results overwrite
  any stale copy on the primary AMLFS.

The installer:

- creates `/opt/slurm-amlfs-flow/venv/` and installs Azure SDK + tenacity
- copies the Python helper to `/opt/slurm-amlfs-flow/amlfs_flow/`
- renders `amlfs_flow/config.py` from the CLI args
- copies the Lua hook + conf to `/etc/slurm/`
- creates `/sched/slurm-amlfs-flow/` and pre-creates `flock` lock files
  owned by user `slurm`

## Slurm configuration

Add to `/etc/slurm/slurm.conf`:

```conf
BurstBufferType=burst_buffer/lua
DebugFlags=BurstBuffer
```

The `burst_buffer.conf` already installed at `/etc/slurm/burst_buffer.conf`
ships with sane defaults:

```conf
Flags=EnablePersistent
StageInTimeout=10800
StageOutTimeout=10800
OtherTimeout=600
```

Then reload:

```bash
sudo scontrol reconfigure
```

## Verify

```bash
sudo ./scripts/selftest.sh
```

Expected output:

```
INFO [amlfs_flow] primary AMLFS: OK (<FS_A>, location=<region>, state=Succeeded)
INFO [amlfs_flow] secondary AMLFS: OK (<FS_B>, location=<region>, state=Succeeded)
INFO [amlfs_flow] === SELFTEST PASSED ===
SELFTEST PASSED
```

If selftest fails, check:

1. Managed Identity is enabled on the VM (`az vm identity show`)
2. RBAC assignments propagated (can take up to 5 min)
3. `DefaultAzureCredential()` is picking the Managed Identity (no
   stale `AZURE_*` env vars)
