---
title: Import the cluster-init project into Azure CycleCloud
description: Fetch a published Multi-region cluster-init project, verify its payload, and upload it to an Azure CycleCloud project locker.
---

# Import the cluster-init project into Azure CycleCloud

This article shows you how to make the Azure CycleCloud Multi-region plugin for AMLFS
cluster-init project available to your Azure CycleCloud server. You fetch a
published project, verify its payload, and upload it to an existing project locker.

> **Note**
>
> Importing a cluster-init project means uploading its installation artifacts to
> a locker. It isn't the same as importing a Slurm cluster template.
> `cyclecloud import_cluster` and `cyclecloud import_template` don't upload the
> project's artifacts.

## Prerequisites

- An existing Azure CycleCloud server and a compatible Azure CycleCloud CLI.
- An administration host with Bash and `sha256sum`. You can use the Azure
  CycleCloud VM or another authorized host; this procedure doesn't run on each node.
- Azure CycleCloud access and permission to upload to an existing project locker.
- Network access to the project's GitHub release and the selected locker.
- A published project version and a new local directory for the fetched files.

The project locker is separate from the Blob data and log containers used by
Azure Managed Lustre HSM. Don't use the HSM data container as the project locker.

## Connect to Azure CycleCloud

1. If the CLI isn't initialized, connect it to your Azure CycleCloud server:

   ```bash
   cyclecloud initialize
   ```

   Retain an existing working configuration. Keep TLS certificate verification
   enabled and configure trust for your server's certificate authority.

2. List the available lockers:

   ```bash
   cyclecloud locker list
   ```

3. Select an existing writable project locker and inspect its settings:

   ```bash
   LOCKER="azure-storage"
   cyclecloud locker show "$LOCKER"
   ```

   Replace `azure-storage` with your locker name. Run the remaining commands in
   the same shell so `LOCKER` remains available.

## Fetch the published project

Choose a published release that you intend to deploy. The example uses `0.2.0`;
replace the version and destination directory together if you select another release.

```bash
cyclecloud project fetch \
  https://github.com/Azure/cyclecloud-multiregion-amlfs/releases/0.2.0 \
  cyclecloud-multi-region-0.2.0
cd cyclecloud-multi-region-0.2.0
```

Use a new destination directory rather than overwriting a working project.
The fetched source includes `project.ini`, the cluster-init specification, and
the configuration and provisioning scripts. Release payloads are downloaded to `blobs/`.

## Verify the project and payload

1. Inspect the local project metadata:

   ```bash
   cyclecloud project info
   ```

   Confirm the project name is `cyclecloud-multi-region` and the version matches your
   selected release. Inspect `project.ini` for the `[spec default]` declaration.
   This command inspects the local project; it doesn't verify a remote upload.

2. Verify the runtime payload checksum:

   ```bash
   (cd blobs && sha256sum --check cyclecloud-multi-region.tar.gz.sha256)
   ```

   The payload check must report `OK`. If the archive or checksum is missing,
   or verification fails, stop and resolve the download before uploading.

## Upload the project to the locker

From the fetched project directory, upload to the selected locker:

```bash
cyclecloud project upload "$LOCKER"
```

Wait for the command to complete successfully. Resolve authentication, permission,
or connectivity errors before configuring a cluster to use the project.
Keep the fetched directory for the configuration and provisioning scripts.

> **Important**
>
> Uploading doesn't create or start a cluster, enable staging, or rerun completed
> cluster-init on existing nodes. It only makes the project artifacts available
> for subsequent node installation.

## Reference the uploaded project

The configuration wizard adds the project to the complete Slurm template.
Select the same locker and confirm that the generated project reference matches
the version you uploaded. For version
`0.2.0`, the cluster-init reference is:

```ini
[[[cluster-init cyclecloud-multi-region:default:0.2.0]]]
```

This is a fragment, not a complete importable template. Custom projects don't
use the built-in `cyclecloud/` prefix. Preserve the native Slurm project references.

The generated template uses `MultiRegionProjectLocker` to select the source locker.
Ensure that regional nodes can reach their target lockers and that their
identities retain locker download permissions. See
[managed identities and permissions](managed-identity.md) for identity requirements,
or [manual template editing](slurm-template.md#attach-the-project-after-native-slurm)
for the `SourceLocker` configuration.

## Next steps

- [Provision regional infrastructure](provisioning.md) if it isn't ready.
- [Generate cluster configuration](configuration.md).
- [Import cluster configuration](import-configuration.md) after the
  complete template and parameters are ready.
