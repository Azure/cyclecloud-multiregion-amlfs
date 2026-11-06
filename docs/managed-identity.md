---
title: Configure scheduler identity and permissions
description: Choose the scheduler node's managed identity and grant it access to trigger and monitor AMLFS import and export jobs.
---

# Configure scheduler identity and permissions

Assign AMLFS permissions to the **managed identity attached to the scheduler
node**. The plugin uses this identity to trigger, monitor, and clean up export
jobs and ImportJobs on the home and remote file systems.

Choose one of the following two options, then assign the same AMLFS runtime
permissions to the selected identity.

## Option 1: use the built-in CCWS identity

Azure CycleCloud Workspace for Slurm (CCWS) provides
**`ccwLockerManagedIdentity`**, normally attached to the scheduler already.
Reuse that identity and preserve its existing Azure CycleCloud configuration
and locker access.

Confirm the scheduler's identity attachment in Azure CycleCloud or the VM's
Identity page. Permissions granted to this identity apply wherever the
identity is used.

## Option 2: add a new managed identity through Azure CycleCloud

Create a **user-assigned managed identity** in Azure, then reference its
resource ID in the scheduler node's `Azure.Identities` property. Azure
CycleCloud attaches the identity when it provisions the node.

Merge the following into the complete cluster definition, using your
scheduler node's actual name:

```ini
[[node scheduler]]
Azure.Identities = $MultiRegionSchedulerIdentity

    [[[configuration]]]
    cluster.identities.default = $MultiRegionSchedulerIdentity

[parameters Multi-region Identity]
[[parameter MultiRegionSchedulerIdentity]]
ParameterType = Azure.ManagedIdentity
Label = Scheduler managed identity
Description = Managed identity resource ID to attach to the scheduler
```

Set `MultiRegionSchedulerIdentity` to the new identity's Azure resource ID.
Configure it as the scheduler's single user-assigned runtime identity and
preserve the project-locker access needed for node initialization. The
`cluster.identities.default` setting aligns Jetpack's locker identity selection
with that attachment.

Apply the definition through [Azure CycleCloud cluster import](cyclecloud-import.md).
For an existing scheduler, follow the controlled node-update procedure there.
Microsoft's [managed identity guide](https://learn.microsoft.com/en-us/azure/cyclecloud/how-to/managed-identities?view=cyclecloud-8#assigning-roles-to-cluster-vms-with-managed-identity)
describes the native `Azure.Identities` configuration.

## Resolve the scheduler identity's principal ID

On an authenticated administration host, use the identity chosen above:

```bash
identity_subscription='<identity-subscription-id>'
identity_group='<identity-resource-group>'
identity_name='ccwLockerManagedIdentity'

principal_id=$(az identity show \
  --subscription "$identity_subscription" \
  --resource-group "$identity_group" \
  --name "$identity_name" --query principalId -o tsv)
```

For option 2, replace `identity_name` with your new identity's name. The
**principal ID** is the assignee for Azure role assignments; the **resource
ID** is the value used in the Azure CycleCloud cluster definition.

## AMLFS import/export runtime role

The [Azure CycleCloud Multi-region Runtime role](../roles/cyclecloud-multi-region-runtime.json)
contains the management operations used by the plugin:

```text
Microsoft.StorageCache/amlFilesystems/read
Microsoft.StorageCache/amlFilesystems/autoExportJobs/read
Microsoft.StorageCache/amlFilesystems/autoExportJobs/write
Microsoft.StorageCache/amlFilesystems/autoExportJobs/delete
Microsoft.StorageCache/amlFilesystems/importJobs/read
Microsoft.StorageCache/amlFilesystems/importJobs/write
Microsoft.StorageCache/amlFilesystems/importJobs/delete
Microsoft.StorageCache/locations/ascOperations/read
```

These permissions let the scheduler inspect each AMLFS, create and monitor
job-scoped exports and imports, and clean up completed operations. Have your
RBAC administrator create or approve the supplied custom role with
`AssignableScopes` covering the resource groups that contain your AMLFS
resources.

Assign that role to the scheduler identity on **each participating AMLFS**,
including home:

```bash
amlfs_subscription='<AMLFS-subscription-id>'
amlfs_group='<AMLFS-resource-group>'
amlfs_name='<AMLFS-name>'
runtime_role_id='<approved-runtime-role-id>'
amlfs_scope="/subscriptions/$amlfs_subscription/resourceGroups/$amlfs_group/providers/Microsoft.StorageCache/amlFilesystems/$amlfs_name"

az role assignment create --subscription "$amlfs_subscription" \
  --assignee-object-id "$principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role "$runtime_role_id" --scope "$amlfs_scope"
```

Repeat for every configured file system. Existing assignments on a containing
resource group also cover its AMLFS resources.

### Allow import/export completion checks

Azure returns asynchronous operation-status URLs at subscription scope.
Grant the same scheduler identity
`Microsoft.StorageCache/locations/ascOperations/read` there through an
approved read-only operation-status role:

```bash
poll_role_id='<approved-operation-status-reader-role-id>'
az role assignment create --subscription "$amlfs_subscription" \
  --assignee-object-id "$principal_id" \
  --assignee-principal-type ServicePrincipal \
  --role "$poll_role_id" --scope "/subscriptions/$amlfs_subscription"
```

Apply this read-only grant once per AMLFS subscription. Keep the export/import
write permissions scoped to the participating file systems or their resource
groups.

## Verify scheduler access

Allow role-assignment propagation, then run on the scheduler as its Slurm
service account:

```bash
sudo -u slurm /opt/cyclecloud-multi-region/venv/bin/python -I -m amlfs_flow selftest
```

Selftest verifies AMLFS read access. Complete a
[staged workload](usage.md#submit-an-example-workload) to verify export,
import, completion checks, and returned output. For authorization failures,
check the scheduler identity, requested action, and target resource scope in
[Troubleshooting](troubleshooting.md).

**Next:** [Generate cluster configuration](configuration.md).
