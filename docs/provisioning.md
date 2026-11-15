---
title: Provision regional networks and Azure Managed Lustre
description: Plan and provision regional VNets, peering, private Blob access, and Azure Managed Lustre with the infrastructure provisioning script.
---

# Provision regional networks and Azure Managed Lustre

Use `scripts/provision.py` to create the regional infrastructure for Azure
CycleCloud Multi-region. The script provisions networks and one Azure
Managed Lustre (AMLFS) file system per region, including the home region.

The script has separate `plan` and `apply` commands. Planning inspects Azure and
writes a local JSON file without creating Azure resources. Applying creates
billable resources after an Azure Resource Manager (ARM) what-if review and confirmation.

For the complete installation sequence, see the [deployment guide](installation.md).
To create resources without the script, use [manual infrastructure provisioning](manual-infrastructure.md).

## Prerequisites

Run on an administration host with Python 3.8 or later and Azure CLI installed.
Authenticate explicitly using `az login`, or `az login --identity` on an
authorized Azure VM. The script does not change the default subscription or
perform a login.

The current implementation targets **Azure public cloud**, one subscription,
and one existing resource group. The supplied parent VNet, storage account,
and both existing containers must be in that resource group. It does not create
the storage account or containers. Register `Microsoft.Network`,
`Microsoft.Storage`, and `Microsoft.StorageCache` in the subscription beforehand.

The operator needs resource read permissions for planning, including connected
parent peer VNets, the SKU/subnet-sizing API, and HSM role assignments.
Applying requires deployment and resource-write permissions in the group,
including peering and private endpoint connection approval. Creating missing
HSM role assignments additionally requires `roleAssignments/write` on the
storage account; **Contributor alone cannot grant roles**.

### Network and storage requirements

Confirm these prerequisites:

- The parent VNet has free address space for a dedicated AMLFS subnet and,
  in private-endpoint mode, a separate `/27` endpoint subnet. The script never
  expands the parent's address space or reuses its compute subnet for AMLFS.
- The new VNet address pool is approved by your IPAM administrator. The script
  avoids existing group VNets, parent peers, and explicit exclusions, but cannot
  discover every on-premises, VPN, ExpressRoute, or other-subscription network.
- Existing parent NSGs/routes allow scheduler, NFS, Azure CycleCloud, and Lustre
  access from the new regional CIDRs. Custom DNS servers must resolve the Blob
  private endpoint, normally by forwarding the relevant zone to Azure DNS.
- The Blob account has shared-key access enabled, as required by AMLFS HSM.
  The script refuses an incompatible account rather than weakening its settings.
- The **HPC Cache Resource Provider** has Storage Account Contributor and Storage
  Blob Data Contributor on the storage account. These are separate from the
  scheduler runtime roles described in [managed identity](managed-identity.md).

Review [AMLFS prerequisites](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/amlfs-prerequisites)
and organizational networking policies. NAT gateways, public IPs, private
endpoints, global peering/data transfer, and AMLFS all incur charges.

If the existing Blob DNS record points to another endpoint that cannot be reused
in the parent VNet, planning stops rather than replacing that record and
potentially disrupting other clients. Prepare an approved parent-VNet endpoint
integrated with the same-group `privatelink.blob.core.windows.net` zone first.
Cross-group/private-DNS resolver designs need a separately reviewed deployment;
the script does not migrate those existing arrangements.

## Resources created

| Resource | Behavior |
|---|---|
| Parent VNet | Reused, not replaced. Its existing address spaces and compute subnets are retained. |
| Home AMLFS | Always created in the parent VNet's region, even if that location is omitted from the requested list. Uses a new dedicated subnet. |
| Regional VNets | One per additional unique region, with separate compute and AMLFS subnets. |
| VNet peering | Both directions between each regional VNet and the parent. No gateway transit, forwarded traffic, or full mesh. |
| AMLFS instances | One per region, all linked to the supplied data and log containers. |
| Outbound networking | One Standard NAT gateway and public egress IP per region, attached to the new AMLFS subnet and new regional compute subnet. Existing parent compute egress is unchanged. |
| NSGs | New NSGs with Azure's default rules: virtual-network traffic allowed, unsolicited Internet inbound denied, and outbound allowed. Existing parent NSGs/routes are not edited. |
| Blob private access | By default, one private endpoint in a separate parent subnet, a private DNS zone, and links for every participating VNet. An approved parent-VNet Blob endpoint integrated with the same-group zone is reused, along with existing zone/links. |

> **Note**
>
> Peering isn't transitive. Regional compute reaches the scheduler and shared NFS
> in the parent VNet, its local AMLFS, and the Blob private endpoint in the parent
> VNet. Data movement uses HSM, not direct spoke-to-spoke transfers.

## Create a plan

From the fetched project directory, create a protected deployment directory and
generate a plan. Replace the placeholders with your approved values:

```bash
umask 077
DEPLOY_DIR="$HOME/cyclecloud-deploy/infrastructure"
mkdir -p "$DEPLOY_DIR"
chmod 700 "$DEPLOY_DIR"

python3 scripts/provision.py plan \
  --subscription '<subscription-id>' \
  --resource-group '<resource-group>' \
  --parent-vnet '<existing-parent-vnet-name>' \
  --regions '<region-b-azure-location>' '<region-c-azure-location>' '<region-d-azure-location>' \
  --sku AMLFS-Durable-Premium-500 \
  --capacity-tib 4 \
  --storage-account '<existing-storage-account-name>' \
  --data-container '<existing-data-container-name>' \
  --log-container '<existing-log-container-name>' \
  --address-pool 10.64.0.0/12 \
  --hsm-principal-id '<HPC-Cache-Resource-Provider-object-id>' \
  --assign-hsm-roles \
  --prefix ccmr \
  --output "$DEPLOY_DIR/plan.json"
```

Replace the example address pool with an IPAM-approved range and use real Azure
location identifiers. Region A is automatically the parent's location. Repeated
regions, including the parent, are deduplicated.

The example explicitly authorizes missing HSM storage grants during apply.
Omit `--assign-hsm-roles` if an administrator has already assigned them.
Use the provider's tenant **object ID**, not the scheduler identity or an
application ID. Neither command grants scheduler runtime roles.

### Planning checks and options

The planner checks SKU regional availability and restrictions, and calls the
non-provisioning `getRequiredAmlFSSubnetsSize` API for the requested SKU/capacity.
By default, AMLFS gets at least a `/24`, enlarged if the service requires more
addresses. The first advertised zone is selected for each region when the SKU
advertises zones. SKU visibility is not a capacity reservation or a guarantee
that policy/quota permits deployment.

Use these options to adjust the plan:

| Option | Purpose |
|---|---|
| `--exclude-cidr 10.80.0.0/16` | Reserve an additional range; repeat for VPN/on-premises/IPAM exclusions. |
| `--vnet-prefix 20` | Prefix length for each new regional VNet; default `/20`. New compute subnets are `/24`. |
| `--amlfs-subnet-prefix 23` | Request a larger dedicated file system subnet; checked against service requirements. |
| `--hsm-principal-id '<object-id>'` | Supply the tenant object ID of HPC Cache Resource Provider when Graph lookup is unavailable. This is **not** its application/client ID or the scheduler identity. |
| `--assign-hsm-roles` | Explicitly authorize apply to create missing storage roles for that service principal. Requires role-administration permissions. |
| `--blob-network public` | Skip private endpoint/DNS creation only when the account already permits public access from all networks. Does not widen the firewall. |

Without `--hsm-principal-id`, the script resolves the service principal using
application ID `4392ab71-2ce2-4b0d-8770-b352745c73f5`. Directory read access may be
required. Without `--assign-hsm-roles`, missing required grants stop planning.
Planning never creates the grants, even when that flag is supplied.

The plan contains the complete ARM template, selected CIDRs, resource names,
regions, and references. It is written with mode `0600` and refuses to overwrite
an existing file.

> **Important**
>
> The saved plan is executable deployment input. Review it before applying it,
> and don't apply a plan from an untrusted source.

## Review and apply the plan

1. Apply the saved plan:

   ```bash
   python3 scripts/provision.py apply "$DEPLOY_DIR/plan.json"
   ```

2. Review the ARM what-if output. Apply rechecks collisions and parent subnet
   configuration before deployment.

3. Enter `apply` to confirm resource creation. Use `--yes` only when intentionally
   skipping confirmation in automation; what-if still runs.

If explicitly authorized, missing HSM roles are assigned first, followed by a
five-minute propagation wait. The infrastructure is then deployed in
**Incremental** mode. File system creation waits for subnets, peerings, and the
private endpoint/DNS resources where applicable. Expect AMLFS creation to take
time; it is not an instantaneous operation.

### Resume a partial deployment

> **Warning**
>
> A failure doesn't trigger deletion or automatic rollback. Billable resources
> can remain, and an Azure deployment can continue after the client is interrupted.

Inspect the Azure deployment record, resolve the issue, and rerun the same saved
plan. The plan retains resource names, address allocations, and ownership tags.
Don't generate a new plan to resume partially created infrastructure.

Existing unrelated named resources are not adopted or replaced. Changes to
owned resources can be reapplied by ARM; keep custom changes in a reviewed
deployment definition rather than modifying these resources out of band.
Existing parent subnets with conflicting settings or custom route tables are
rejected instead of reset.

## Review the deployment output

After all file system deployments succeed, `plan.outputs.json` contains each
region's file system resource ID, VNet/subnet details, MGS address, mount source,
and Azure-reported client information. A repeat apply refreshes only the output
belonging to that plan.

Use those values with the [configuration wizard](configuration.md#run-the-configuration-wizard).
The parent compute subnet is intentionally not guessed: retain the existing
cluster's correct compute subnet. Remote compute subnet paths are included.
Choose kernel-matched Lustre client images before enabling mounts, grant the
scheduler runtime roles, and follow [installation](installation.md).

This script does not change Slurm, import an Azure CycleCloud template, attach
identities to cluster nodes, grant scheduler runtime roles, or install clients.

## Next steps

- [Configure managed identities and permissions](managed-identity.md).
- [Generate cluster configuration](configuration.md).
