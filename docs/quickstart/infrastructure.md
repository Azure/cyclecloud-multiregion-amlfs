---
title: Provision storage and permissions
description: Plan and deploy regional AMLFS infrastructure, then grant the scheduler the management access used by the plugin.
---

# 2. Provision storage and permissions

Continue in the administration shell and fetched project directory from
[step 1](prepare.md). Prepare an AMLFS in every participating region and
connect them to the shared Blob namespace.

## Create and review the plan

Create a protected directory outside the project for deployment-specific files:

```bash
umask 077
INFRA_DIR="$HOME/cyclecloud-deploy/infrastructure"
mkdir -p "$INFRA_DIR"
```

Replace the placeholders and example address pool with approved values:

```bash
python3 scripts/provision.py plan \
  --subscription '<subscription-id>' \
  --resource-group '<resource-group>' \
  --parent-vnet '<parent-vnet-name>' \
  --regions '<region-b-location>' '<region-c-location>' '<region-d-location>' \
  --sku AMLFS-Durable-Premium-500 --capacity-tib 4 \
  --storage-account '<storage-account-name>' \
  --data-container '<data-container-name>' \
  --log-container '<log-container-name>' \
  --address-pool 10.64.0.0/12 \
  --hsm-principal-id '<HPC-Cache-Resource-Provider-object-id>' \
  --assign-hsm-roles \
  --output "$INFRA_DIR/plan.json"
```

Planning writes a local plan for review. The example authorizes missing HSM
storage grants during apply; omit `--assign-hsm-roles` when the required
grants already exist. Supply the verified tenant object ID of the **HPC Cache
Resource Provider** service principal for HSM access.

## Apply the plan

```bash
python3 scripts/provision.py apply "$INFRA_DIR/plan.json"
```

Review the Azure Resource Manager what-if output. Enter `apply` when ready to
create billable infrastructure.

The plan provisions regional VNets, dedicated subnets, bidirectional
peerings, outbound networking, private Blob access, and an AMLFS per region,
including the parent's region. Use actual Azure location identifiers for the
remote regions.

After completion, use `"$INFRA_DIR/plan.outputs.json"` for regional file
system IDs, compute subnets, and mount sources. If provisioning is interrupted,
inspect the Azure deployment and resume the **same saved plan** so its
resource ownership remains consistent.

See [Provision regional infrastructure](../provisioning.md) for additional
settings, or [Manual infrastructure provisioning](../manual-infrastructure.md)
for an operator-managed deployment.

## Grant scheduler runtime access

Assign the AMLFS runtime permissions to the **scheduler node's managed
identity** on every participating AMLFS, including home. Use the built-in
CCWS identity, **`ccwLockerManagedIdentity`**, or attach a new managed identity
through the Azure CycleCloud cluster definition.

Follow [Configure scheduler identity](../managed-identity.md) for the two
options, the import/export role, and asynchronous-operation status access.

**Next:** [Configure and import the cluster](configure.md).
