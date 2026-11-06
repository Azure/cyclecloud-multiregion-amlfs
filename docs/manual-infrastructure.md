---
title: Provision multi-region infrastructure manually
description: Prepare prerequisites and manually create regional networks, peering, private Blob access, and Azure Managed Lustre file systems.
---

# Provision multi-region infrastructure manually

This article shows you how to create the infrastructure for Azure CycleCloud Multi-region plugin for AMLFS in the Azure portal. You can apply the same requirements
through your organization's approved infrastructure tooling. For the scripted
path, use the [deployment guide](installation.md).

The target is one Azure Managed Lustre (AMLFS) file system per participating
region, including home, backed by the **same Blob data container**. Region A is
home; Regions B, C, and D illustrate additional locations.
Replace these labels with actual Azure location identifiers when creating
resources.

> **Important**
>
> Don't run `scripts/provision.py` over these manually created resources. The script
> doesn't adopt unrelated resources.

## Prerequisites

Before creating billable resources, confirm:

| Area | Prerequisite |
|---|---|
| Azure CycleCloud | An existing server, writable project locker, and complete version-matched Slurm template. Preserve native Slurm, identities, scheduler storage, and shared NFS. |
| Subscription | AMLFS is offered in each selected region and zone, with sufficient file system capacity quota and regional compute-family quota. SKU availability doesn't guarantee capacity. |
| Providers | Register `Microsoft.StorageCache`, `Microsoft.Network`, and `Microsoft.Storage`; also `Microsoft.Compute` and `Microsoft.ManagedIdentity` where you create nodes or identities. Wait for registration to complete. |
| Resource ownership | Approved subscriptions/resource groups, naming, tags, administrators, and cleanup ownership. A resource group's location does not restrict its resources to that region. |
| Permissions | Rights to create networks, peer both VNets, approve private endpoints, and create storage and AMLFS resources. Role assignment requires separate RBAC administration rights; Contributor alone is insufficient. |
| Existing parent VNet | Retain its address spaces, compute subnet, routes, NSGs, DNS, and peerings. Reserve free space for home AMLFS and private endpoints. |
| IP address plan | Non-overlapping IPv4 ranges across all regional VNets, existing peers, VPN/ExpressRoute networks, and on-premises networks. AMLFS does not support IPv6. |
| Policy | Approve permitted regions, SKUs, public egress IPs, private endpoint design, storage shared-key access, and encryption requirements. Resolve policy restrictions before deployment. |
| Images and software | A Lustre client matched to each node's OS/kernel, native Slurm compatibility, package/locker access, and consistent workload UID/GID mappings. |
| Cost and lifecycle | Budget for every AMLFS, NAT gateway, public IP, private endpoint, Blob capacity/transactions, peering, and cross-region transfer. Set ownership and a recovery/retention plan. |

Use one subscription and resource group for the simplest layout. A manual
cross-group or cross-subscription design needs explicit peering, DNS, identity,
and RBAC coverage for every resource; it is not covered by the standalone
provisioner's single-group assumptions.

### Record the deployment design

Record each region's Azure location, VNet and address range, compute subnet,
AMLFS subnet, intended file system name, SKU and capacity, and zone. Also record
the shared storage account and its data/log container names.

## Create regional VNets and subnets

In the Azure portal, open **Virtual networks**:

1. Reuse the parent VNet in Region A. Do not recreate it or replace its compute subnet.
2. Create a VNet in each additional region using the approved non-overlapping ranges.
3. Create a separate **compute subnet** and **AMLFS subnet** in each remote VNet.
   Add a dedicated AMLFS subnet in the parent VNet as well.
4. Reserve a separate subnet for the Blob private endpoint in the parent VNet.
   Keep it separate from the AMLFS and compute subnets in this design.

Size the AMLFS subnet for all storage servers, metadata servers, HSM service
nodes, Azure's five reserved addresses, and planned expansion. Do not size it
only by the number of compute nodes. The current service guidance starts at
`/27` for 4–16 TiB, but that is only minimum guidance: use the service's sizing
API or Azure CLI for the exact SKU/capacity, and leave headroom.
The [AMLFS network prerequisites](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/amlfs-prerequisites#network-prerequisites)
describe how to query the required size.

> **Important**
>
> Don't use a subnet that contains or previously contained Azure NetApp Files.
> You can't move an AMLFS to another VNet or subnet after creation.

Keep existing `/sched` and `/shared` storage separate from regional `/lustre`.

## Peer the regions and configure routing

For each remote VNet, select **Peerings** > **Add** and create both directions:
parent-to-remote and remote-to-parent. Allow virtual-network access and verify
that both peerings report **Connected**.

Use the parent as the hub. Peering is **not transitive**: connecting Regions B
and C to Region A does not connect B directly to C. This plugin does not
require direct spoke-to-spoke Lustre mounts; data movement uses the shared Blob
account. Enable gateway transit or forwarded traffic only when your approved
VPN/NVA design requires it.

Review effective routes and security rules, not just peering status:

| Path | Required access |
|---|---|
| Each regional client subnet ↔ its AMLFS subnet | Lustre traffic to all file system nodes |
| Remote compute ↔ parent scheduler | Native Slurm communication, authentication, and name resolution |
| Remote compute ↔ shared NFS | Existing `/sched` and `/shared` exports and the NFS/RPC ports used by your server |
| Nodes → Azure CycleCloud and locker | Native provisioning/management endpoints and project downloads |
| Scheduler → Azure management APIs | Managed-identity authentication and AMLFS archive/import/status operations |
| All AMLFS subnets → shared Blob endpoint | Reachable private IP and correct storage DNS resolution |

Include remote compute CIDRs in approved parent firewall rules and NFS export
access. Check host firewalls as well as subnet/NIC NSGs and user-defined routes.
Custom DNS must resolve the scheduler, NFS, Azure CycleCloud, and storage names
from every participating VNet.

## Configure NSGs and outbound access

Create and associate NSGs before deploying AMLFS. Allow bidirectional TCP
**988 and 1019–1023** between the regional clients and AMLFS subnet, and the
required intra-AMLFS-subnet communication, including deployment SSH.
Ensure no client service reserves the Lustre ports.

Follow the complete current
[AMLFS NSG guidance](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/configure-network-security-group)
for Azure service access, monitoring, authentication, storage/queue access,
time synchronization, and telemetry. Its sample regional service tags must be
adapted to your locations. A short Lustre-only allowlist is not sufficient.
Review rule priorities so custom deny rules do not override required traffic.

Provide **explicit outbound connectivity** in every region. One option is a
regional Standard NAT gateway with a public egress IP, associated with the
AMLFS subnet and new compute subnet. Alternatively, use an approved firewall
or other supported egress design with the required destinations permitted.
Preserve working parent compute egress.

> **Important**
>
> Don't rely on implicit default outbound access for new VNets. A NAT gateway
> provides egress, not unsolicited inbound access. A Blob private endpoint doesn't
> replace the other outbound service connections AMLFS needs.

## Prepare the shared Blob account and private access

In **Storage accounts**, create or select a compatible account. Standard
StorageV2 with an appropriate redundancy option is a common choice; confirm
supported account types in the
[AMLFS Blob integration prerequisites](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/amlfs-prerequisites#blob-integration-prerequisites).

1. Keep anonymous Blob access disabled and require secure transport.
2. Enable storage account key access, as required by AMLFS HSM. Do not retrieve
   or place keys in scripts/templates. If policy prohibits this setting, obtain
   an approved resolution rather than bypassing policy.
3. Create two distinct private containers: one for **data**, one for **import/export logs**.
   Use the same data container for every regional file system.
4. Under **Networking**, create a private endpoint for the **blob** subresource
   in the parent endpoint subnet and approve the connection.
5. Set **Integrate with private DNS zone** to **Yes**, using
   `privatelink.blob.core.windows.net`. Link the zone to all participating VNets.
6. Confirm private connectivity, then keep the storage account's public network
   access disabled for this private design.

If an approved endpoint/zone already exists, reuse or integrate it rather than
overwriting its account DNS record. Peering does not automatically configure
private DNS links. For custom DNS, configure forwarding/resolution to the
appropriate Azure private DNS service and verify it from every client network.

`<account>.blob.core.windows.net` must resolve to the intended reachable private
endpoint from every AMLFS VNet. An administrator's successful public connection
does not prove that the private service path works. The storage account can be
shared across regions, but cross-region transfer latency and charges still apply.

## Grant HSM storage access

On the storage account's **Access control (IAM)** page, assign these roles to
the verified **HPC Cache Resource Provider** tenant service principal:

| Role | Scope |
|---|---|
| Storage Account Contributor | Shared HSM storage account |
| Storage Blob Data Contributor | Shared HSM storage account |

These are not assignments to the scheduler or locker identity. If the service
principal is not visible, obtain its verified object ID from the directory
administrator; do not create a lookalike application. Allow role propagation
before file system creation. See Microsoft's
[AMLFS Blob integration prerequisites](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/amlfs-prerequisites#access-roles-for-blob-integration)
for HSM identity and assignment guidance.

## Create an HSM-enabled AMLFS in every region

In the Azure portal, select **Azure Managed Lustre** > **Create**. Repeat the following for Region A and
every remote region:

1. Select the intended subscription and resource group, actual region, file system
   resource name, and supported availability zone.
2. Select the throughput tier and capacity. For the four-region example,
   `AMLFS-Durable-Premium-500` with **4 TiB per file system** is the intended
   starting configuration, where available. Other tiers have different minimum
   capacities/increments; inspect the final capacity and cost before accepting.
3. Select that region's VNet and dedicated AMLFS subnet, not the compute subnet.
4. On **Advanced**, enable **Blob integration** and select the shared account,
   data container, and separate log container.
5. Use a consistent import-prefix/path layout across all regions so the same
   relative workload path maps to the same Blob object. Review the initial import
   selection before attaching an existing container with production data.
6. Choose the maintenance window and encryption policy. Microsoft-managed keys
   are the simpler option; customer-managed keys require the service's additional
   identity/Key Vault prerequisites. Encryption mode cannot be changed after creation.
7. Review and create. Wait for provisioning to report **Succeeded**, then record
   the file system resource ID, client mount instructions, and MGS address.

Follow the current
[portal creation guide](https://learn.microsoft.com/en-us/azure/azure-managed-lustre/create-file-system-portal)
for available tiers, capacity increments, encryption, and maintenance settings.
HSM must be configured during creation for this plugin's data-staging path;
don't omit it on the home file system.

> **Warning**
>
> File systems sharing a Blob container aren't live replicas. Later Blob changes
> require import operations. Concurrent workloads can overwrite shared paths
> unless you coordinate writes.

Don't alter the service-managed AMLFS NICs or disable their accelerated networking.

## Prepare node identities, clients, and access

Reuse `ccwLockerManagedIdentity` for Azure CycleCloud Workspace for Slurm unless
your design explicitly requires scheduler-only isolation. Assign the scheduler
runtime role to every AMLFS resource group and the separate operation-status
reader at the required subscription scopes, as described in
[managed identities](managed-identity.md). Retain locker download permissions.

Prepare each node image with the supported Lustre client for its **exact
OS/kernel**, including the `mount.lustre` helper and loadable kernel module.
Confirm this for scheduler/home nodes and each regional compute image.
The plugin configures mounts; it does not install or upgrade Lustre clients.

Keep UID/GID ownership consistent across nodes and shared data. Ensure the
Slurm service account can use its shared state directory, and that native Slurm
authentication, time synchronization, NFS exports, and regional name resolution
are ready. Mount each compute node's local AMLFS, not the home file system
over a cross-region link.

## Verify readiness and record the configuration

Before enabling the plugin, confirm that all file systems are ready, peerings
are connected, DNS resolves privately, required routes and ports work, and the
intended identities have access. Perform client and mount checks from the relevant
regional networks; a successful ARM deployment alone does not establish these.

Keep a protected inventory containing:

| Value per region | Used by the configuration wizard |
|---|---|
| Logical key and Azure location | Region/partition mapping |
| Subscription, resource group, and AMLFS name/resource ID | Runtime API target |
| MGS mount source from the service | Full source such as `10.0.0.4@tcp:/lustrefs`, not just an IP |
| Compute subnet | `resource-group/vnet/compute-subnet`; retain the existing home compute subnet |
| Local mountpoint and compatible image | Regional mounts and nodearray image selection |

Manual creation doesn't produce `plan.outputs.json`. Use this inventory instead
when answering the configuration wizard.

## Recover or clean up resources

On a partial failure, inspect the deployment and repair the owned resources
before retrying. Do not delete the parent VNet, shared Blob data, or unrelated
resources as rollback. Stop workloads and retain required data before any
approved teardown; infrastructure remains billable until removed.

## Next steps

Fetch and upload the project if needed, skip the scripted infrastructure step,
and continue with [scheduler runtime permissions](quickstart/infrastructure.md#grant-scheduler-runtime-access).
Then [generate the Slurm template and regional configuration](configuration.md).
