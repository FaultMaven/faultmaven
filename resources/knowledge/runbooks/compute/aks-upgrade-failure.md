---
id: "aks-upgrade-failure"
title: "AKS cluster/node-pool upgrade fails: PDB eviction, quota, subnet, or allocation"
domain: compute
service: azure-aks
symptom_class: [deployment_failure]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-17"
verified_by: "kb-researcher"
status: draft
tags: [upgrade-failed, pod-drain-failure, quota-exceeded, subnet-is-full, allocation-failed, max-surge]
difficulty: advanced
---

## Symptom Recognition

An `az aks upgrade` or `az aks nodepool upgrade` operation fails and the cluster/node pool enters a `Failed` provisioning state. The operation surfaces one of these error strings:

- `(UpgradeFailed) Drain node aks-<nodepool-name>-xxxxxxxx-vmssxxxxxx failed when evicting pod <pod-name> failed with Too Many Requests error. This error is often caused by a restrictive Pod Disruption Budget (PDB) policy. ... Original error: Cannot evict pod as it would violate the pod's disruption budget.. PDB debug info: <namespace>/<pod-name> blocked by pdb <pdb-name> with 0 unready pods.`
- `Code: QuotaExceeded Message: Operation could not be completed as it results in exceeding approved standardDSv3Family Cores quota. ... Current Limit: 1500, Current Usage: 1500, Additional Required: 16, (Minimum) New Limit Required: 1516.`
- `Code: ErrCode_InsufficientVCPUQuota Message: Insufficient vcpu quota requested 48, remaining 32 for family standardDSv5Family for region qatarcentral`
- `Error: VMSSAgentPoolReconciler retry failed: Code='SubnetIsFull' Message='<SUBNET NAME> with address prefix <PREFIX> doesn't have enough capacity for IP addresses.'`
- `Code: ZonalAllocationFailed Message: Allocation failed. We do not have sufficient capacity for the requested VM size in this zone.`
- `Code: AllocationFailed Message: The VM allocation failed due to an internal error. Please retry later or try deploying to a different location.`

Kubernetes events on the cluster show: `Warning Drain node/aks-... Eviction blocked by Too Many Requests (usually a pdb): <pod-name>`.

## Applicability

- Azure Kubernetes Service (AKS) clusters and node pools, any supported Kubernetes version, using VMSS-backed node pools.
- Azure CLI 2.67.0 or later for the PDB drain workflow; 2.0.65 or later for subnet/NSG workflows. Check with `az --version`.
- Required access: `Microsoft.ContainerService/managedClusters/agentPools/write` on the cluster, plus read on the node resource group (`MC_<rg>_<cluster>_<region>`), the VNet/subnet, and subscription quota. `kubectl` configured against the target cluster (`az aks get-credentials`).
- Tools: `az`, `kubectl`.

## Diagnostic Steps

### Step 1: Read the failed upgrade's provisioning state and error message

```bash
az aks show \
  --resource-group <ResourceGroupName> \
  --name <AKSClusterName> \
  --query "{provisioningState:provisioningState, k8sVersion:kubernetesVersion}" -o table
```

Expected output: `provisioningState` is `Succeeded` when healthy. A failed upgrade shows `Failed`. Capture the full error from the original `az aks upgrade` output or from `az aks nodepool show ... --query provisioningState`.

### Step 2: Inspect PodDisruptionBudgets and their allowed disruptions

```bash
kubectl get pdb --all-namespaces
kubectl get events --all-namespaces | grep -i drain
```

Expected output: a table of PDBs with columns `MIN AVAILABLE`, `MAX UNAVAILABLE`, and `ALLOWED DISRUPTIONS`. A PDB with `ALLOWED DISRUPTIONS` of `0` blocks eviction. The events grep shows `Eviction blocked by Too Many Requests (usually a pdb)` when a PDB stalled the drain.

### Step 3: Compare current vCPU usage against the regional and family quota

```bash
az vm list-usage --location <your-region> --output table | grep -Ei "Total Regional vCPUs|<VMFamily>"
```

Expected output: `CurrentValue` and `Limit` columns per quota. When `CurrentValue` equals (or is within `maxSurge` nodes of) `Limit`, the surge nodes cannot be allocated. The error text also names the family (e.g. `standardDSv3Family`) and the `(Minimum) New Limit Required`.

### Step 4: Check available IP addresses in the node subnet

```bash
az network vnet subnet show \
  --resource-group <VNetResourceGroup> \
  --vnet-name <VNetName> \
  --name <SubnetName> \
  --query "{prefix:addressPrefix, ipConfigs:length(ipConfigurations)}" -o table
```

Expected output: the subnet's `addressPrefix` (e.g. `10.240.0.0/24` = 251 usable IPs) and the count of consumed IP configurations. When consumed IPs plus `(maxSurge nodes) × (1 + maxPods)` exceeds the prefix capacity, surge nodes fail to provision.

### Step 5: Read the node pool's max-surge / max-unavailable upgrade settings

```bash
az aks nodepool show \
  --resource-group <ResourceGroupName> \
  --cluster-name <AKSClusterName> \
  --name <NodePoolName> \
  --query "upgradeSettings" -o json
```

Expected output: a JSON object with `maxSurge` (default `"1"`) and optionally `maxUnavailable`. A high `maxSurge` (e.g. `33%`/`50%`) increases the surge-node count, which multiplies the vCPU quota and subnet-IP demand from Steps 3 and 4.

### Step 6: Confirm the target SKU is available in the cluster's region and zones

```bash
az vm list-skus \
  --location <your-region> \
  --size <VMSize> \
  --all \
  --output table
```

Expected output: rows for the VM size with a `Restrictions` column. An entry of `NotAvailableForSubscription` or a zone listed under restrictions means the surge node's SKU cannot be allocated in that zone/region.

## Causes

### Cause A: A restrictive PodDisruptionBudget blocks pod eviction during node drain
**Statement:** A PodDisruptionBudget on a workload sets `ALLOWED DISRUPTIONS` to 0 (minAvailable equals running replicas, or maxUnavailable is 0), so AKS cannot evict the protected pod and the node drain phase of the upgrade fails after repeated retries.
**Chain:**
- root: PDB allows 0 disruptions for a workload on a node being upgraded
- s1: kubelet/API rejects the eviction request with "Too Many Requests (usually a pdb)"
- s2: AKS retries the drain, exhausts attempts, and marks the node pool `Failed`
- D: the upgrade operation fails with UpgradeFailed / PodDrainFailure
**Indicators:**
- root: [Step 2] a PDB shows `ALLOWED DISRUPTIONS` of `0`
- s1: [Step 2] events contain "Eviction blocked by Too Many Requests (usually a pdb)"
- s2: [Symptom] error contains "Cannot evict pod as it would violate the pod's disruption budget"
- D: [Step 1] provisioningState is `Failed`
**Interventions:**
- **remediation** (root): Raise the workload's replica count or relax the PDB so `ALLOWED DISRUPTIONS` is at least 1, then re-run the upgrade to trigger reconciliation of the failed cluster.

  ```bash
  kubectl patch pdb <pdb-name> -n <pdb-namespace> \
    --type merge -p '{"spec":{"maxUnavailable":1}}'
  az aks upgrade --name <AKSClusterName> --resource-group <ResourceGroupName>
  ```

  **Verification:** `kubectl get pdb <pdb-name> -n <pdb-namespace>` shows `ALLOWED DISRUPTIONS` >= 1, and Step 1 returns `provisioningState: Succeeded`.
- **mitigation** (s1): Back up then delete the blocking PDB so the drain can proceed, re-run the upgrade, then re-apply the PDB.

  ```bash
  kubectl get pdb <pdb-name> -n <pdb-namespace> -o yaml > pdb-name-backup.yaml
  kubectl delete pdb <pdb-name> -n <pdb-namespace>
  az aks upgrade --name <AKSClusterName> --resource-group <ResourceGroupName>
  kubectl apply -f pdb-name-backup.yaml
  ```

  **Risk:** the workload loses disruption protection while the PDB is absent; a concurrent node failure could take all replicas down. **Duration:** only for the length of the upgrade; re-apply immediately after. **Verification:** `kubectl get pdb <pdb-name> -n <pdb-namespace>` shows the PDB restored after `kubectl apply`.

### Cause B: Surge nodes exceed the subscription's VM vCPU quota
**Statement:** The VM-family or regional vCPU quota has no headroom for the temporary surge nodes the upgrade creates (current nodes + surge nodes exceed the limit), so Azure Compute refuses to allocate them and the upgrade fails with QuotaExceeded / InsufficientVCPUQuota.
**Chain:**
- root: VM-family or regional vCPU quota is at/near its limit for current + surge nodes
- s1: Azure Resource Manager rejects the surge-node VMSS scale request as quota-exceeding
- s2: the node pool cannot create surge capacity and enters `Failed`
- D: the upgrade operation fails with QuotaExceeded / ErrCode_InsufficientVCPUQuota
**Indicators:**
- root: [Step 3] `az vm list-usage` shows `CurrentValue` at/near `Limit` for the node's VM family or Total Regional vCPUs
- s1: [Symptom] error contains "exceeding approved" cores quota or "Insufficient vcpu quota"
- s2: [Step 5] a high `maxSurge` increases the surge-node vCPU demand
- D: [Symptom] error code is `QuotaExceeded` or `ErrCode_InsufficientVCPUQuota`
**Interventions:**
- **remediation** (root): Request a VM-family or regional vCPU quota increase to cover current + surge nodes, then re-run the upgrade.

  ```bash
  # Request via Azure portal: Subscriptions > Usage + quotas > request increase
  # for the family/region from the error (Minimum New Limit Required), then:
  az aks upgrade --name <AKSClusterName> --resource-group <ResourceGroupName>
  ```

  **Verification:** `az vm list-usage --location <your-region> -o table` shows `Limit` raised above `CurrentValue + surge`, and Step 1 returns `provisioningState: Succeeded`.
- **defensive_fix** (s2): Upgrade in place using existing capacity (no surge nodes) by setting max-surge to 0 and max-unavailable to 1.

  ```bash
  az aks nodepool update \
    --resource-group <ResourceGroupName> \
    --cluster-name <AKSClusterName> \
    --name <NodePoolName> \
    --max-surge 0 \
    --max-unavailable 1
  ```

  **Verification:** `az aks nodepool show ... --query upgradeSettings` shows `maxSurge: "0"` / `maxUnavailable: "1"`; re-running the upgrade no longer requests surge vCPUs.

### Cause C: The node subnet has insufficient IP addresses for surge nodes
**Statement:** The node pool's subnet does not have enough free IP addresses to host the surge nodes (and their pods) that the upgrade provisions, so VMSS reconciliation fails with SubnetIsFull.
**Chain:**
- root: node subnet free IP count is less than (surge nodes) × (1 + maxPods)
- s1: the surge-node VMSS cannot obtain IP configurations from the subnet
- s2: VMSSAgentPoolReconciler retries and fails the node-pool scale
- D: the upgrade operation fails with SubnetIsFull
**Indicators:**
- root: [Step 4] consumed IPs leave fewer than the surge requirement free in the subnet prefix
- s1: [Symptom] error contains "doesn't have enough capacity for IP addresses"
- s2: [Symptom] error contains "VMSSAgentPoolReconciler retry failed"
- D: [Symptom] error code is `SubnetIsFull`
**Interventions:**
- **remediation** (root): Add a new user node pool on a larger subnet (with enough IP headroom), shift workloads to it, and retire the original pool; or expand the subnet's address space (for example `/24` to `/22`) before re-running the upgrade.

  ```bash
  az aks nodepool add \
    --resource-group <ResourceGroupName> \
    --cluster-name <AKSClusterName> \
    --name <NewNodePoolName> \
    --vnet-subnet-id <LargerSubnetResourceId> \
    --mode User
  ```

  **Verification:** `az network vnet subnet show ... --query addressPrefix` on the new subnet confirms ample IPs; re-running the upgrade against the relocated workload succeeds (Step 1 `Succeeded`).
- **defensive_fix** (s1): Reduce surge-node IP demand by lowering max-pods per node (and/or max-surge) on the node pool.

  ```bash
  az aks nodepool update \
    --resource-group <ResourceGroupName> \
    --cluster-name <AKSClusterName> \
    --name <NodePoolName> \
    --max-pods 50
  ```

  **Verification:** recompute `(current nodes + maxSurge) × (1 + maxPods)` against the subnet free IPs from Step 4; the value now fits, and the upgrade no longer returns SubnetIsFull.

### Cause D: Azure cannot allocate the surge node's VM SKU in the target zone or region
**Statement:** The requested VM SKU has insufficient capacity in the cluster's zone/region (or is over-constrained), so the surge-node VMSS allocation fails with ZonalAllocationFailed / AllocationFailed / OverconstrainedAllocationRequest.
**Chain:**
- root: target VM SKU lacks allocatable capacity (or is over-constrained) in the cluster's zone/region
- s1: the surge-node VMSS allocation request is rejected by Azure Compute
- s2: AKS cannot create surge capacity and the node pool enters `Failed`
- D: the upgrade operation fails with an allocation error code
**Indicators:**
- root: [Step 6] `az vm list-skus` shows the size restricted (e.g. `NotAvailableForSubscription`) or zone-restricted in the region
- s1: [Symptom] error contains "We do not have sufficient capacity for the requested VM size in this zone"
- s2: [Step 1] provisioningState is `Failed`
- D: [Symptom] error code is `ZonalAllocationFailed`, `AllocationFailed`, or `OverconstrainedAllocationRequest`
**Interventions:**
- **remediation** (root): Provision a new node pool using an available SKU (or a different zone/region), or remove the over-constraining proximity placement group, then migrate the workload and retire the failing pool.

  ```bash
  az aks nodepool add \
    --resource-group <ResourceGroupName> \
    --cluster-name <AKSClusterName> \
    --name <NewNodePoolName> \
    --node-vm-size <AvailableVMSize> \
    --zones <AvailableZone> \
    --mode User
  ```

  **Verification:** `az vm list-skus --location <region> --size <AvailableVMSize> -o table` shows no blocking restriction; the new pool reaches `provisioningState: Succeeded` and the upgrade completes.
- **defensive_fix** (s1): Upgrade in place without surge nodes (use existing capacity) so no new SKU allocation is needed.

  ```bash
  az aks nodepool update \
    --resource-group <ResourceGroupName> \
    --cluster-name <AKSClusterName> \
    --name <NodePoolName> \
    --max-surge 0 \
    --max-unavailable 1
  ```

  **Verification:** `az aks nodepool show ... --query upgradeSettings` shows `maxSurge: "0"`; re-running the upgrade cordons and drains existing nodes one at a time with no surge allocation.

### Cause E: A network security group rule blocks nodes from downloading upgrade resources
**Statement:** A custom NSG rule on the node subnet or node resource group blocks the outbound internet traffic that upgrading nodes need to fetch required packages/images, so newly surged or re-imaged nodes cannot complete bootstrap and the upgrade fails.
**Chain:**
- root: a custom NSG rule denies required outbound traffic from the AKS node subnet
- s1: upgrading/surge nodes cannot download required resources and fail to become Ready
- s2: AKS marks the node pool upgrade `Failed`
- D: the upgrade operation fails with an NSG-related error
**Indicators:**
- root: [Step 1] provisioningState is `Failed` and the error references an NSG rule
- s1: [Step 1] the upgrade error shows nodes cannot reach required endpoints, and a non-default deny rule appears in `az network nsg rule list` for the MC_ NSG
- D: [Symptom] error message indicates an NSG rule is involved
**Interventions:**
- **remediation** (root): List the node NSG rules, remove or correct the rule that blocks outbound internet egress, then re-run the upgrade to trigger reconciliation.

  ```bash
  az network nsg list -o table
  az network nsg rule list \
    --resource-group MC_<ResourceGroupName>_<AKSClusterName>_<location> \
    --nsg-name <nsg-name> --include-default -o table
  az aks upgrade \
    --resource-group <ResourceGroupName> \
    --name <AKSClusterName> \
    --kubernetes-version <KUBERNETES_VERSION>
  ```

  **Verification:** `az network nsg rule list ... --include-default -o table` shows only the default rules (or no blocking custom rule), and Step 1 returns `provisioningState: Succeeded`.

### Cause Z: Unidentified
**Statement:** The upgrade fails for a reason not captured by Causes A–E (for example a control-plane reconciliation defect, a transient platform incident, or an addon/extension failure).
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the AKS SME / Azure Support.

  ```bash
  az aks show -g <ResourceGroupName> -n <AKSClusterName> -o json > aks-snapshot.json
  az aks nodepool list -g <ResourceGroupName> --cluster-name <AKSClusterName> -o json > nodepools-snapshot.json
  kubectl get nodes -o wide > nodes.txt
  kubectl get events --all-namespaces --sort-by=.lastTimestamp > events.txt
  kubectl get pdb --all-namespaces -o yaml > pdbs.yaml
  ```

  **Risk:** snapshot may contain cluster metadata; share only over an approved support channel. **Duration:** until the SME responds. **Verification:** snapshot files exist and are attached to the escalation ticket.

## Prevention

- Set realistic PDBs: ensure every workload's PDB leaves `ALLOWED DISRUPTIONS` >= 1 (minAvailable below replica count, or maxUnavailable >= 1). Audit with `kubectl get pdb --all-namespaces` before every upgrade.
- Pre-flight capacity: before upgrading, confirm `az vm list-usage --location <region> -o table` has headroom for current + surge nodes, and verify subnet free IPs against `(current nodes + maxSurge) × (1 + maxPods)`.
- Tune upgrade settings to the cluster: keep `maxSurge=1` (default) for cost-constrained clusters, `maxSurge=33%` for production speed/cost balance; use `--max-surge 0 --max-unavailable 1` when quota/SKU capacity is tight.
- Use Planned Maintenance windows and schedule upgrades during off-peak hours so surge SKUs are more likely to allocate.
- Add monitoring alerts on AKS `provisioningState=Failed` and on regional/family vCPU usage approaching quota limits.

## Sources

- [Troubleshoot UpgradeFailed errors due to eviction failures caused by PDBs (PodDrainFailure)](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/create-upgrade-delete/error-code-poddrainfailure) — exact PDB drain error strings, `kubectl get pdb` / events workflow, and the three solutions (adjust PDB, back up/delete/redeploy, scale to zero).
- [QuotaExceeded or InsufficientVCPUQuota error during creation or upgrade](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/create-upgrade-delete/quota-exceeded-during-creation-upgrade) — exact family/regional quota error strings and quota-increase resolution.
- [Capacity and cost planning for AKS upgrades](https://learn.microsoft.com/en-us/azure/aks/upgrade-capacity-cost-planning) — surge-node behavior, maxSurge defaults, `az vm list-usage`, IP formula, and `az aks nodepool update --max-surge/--max-unavailable/--max-pods` commands.
- [Troubleshoot a SubnetIsFull error code during an AKS cluster upgrade](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/create-upgrade-delete/error-code-subnetisfull-upgrade) — exact SubnetIsFull error string and the larger-subnet node-pool remediation.
- [Troubleshoot ZonalAllocationFailed, AllocationFailed, or OverconstrainedAllocationRequest](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/error-codes/zonalallocation-allocationfailed-error) — exact allocation error strings, SKU/zone causes, and maxUnavailable / different-SKU resolutions.
- [AKS cluster upgrade fails because of NSG rules](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/create-upgrade-delete/upgrade-fails-because-of-nsg-rules) — NSG-rule cause and the `az network nsg list` / `az network nsg rule list` resolution.
