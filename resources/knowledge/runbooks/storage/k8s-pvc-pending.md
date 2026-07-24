---
id: k8s-pvc-pending
title: "Kubernetes PersistentVolumeClaim Stuck in Pending"
domain: storage
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pvc
  - persistent-volume
  - storage-class
  - csi
  - dynamic-provisioning
  - wait-for-first-consumer
  - ebs-csi
difficulty: intermediate
---

# Kubernetes PersistentVolumeClaim Stuck in Pending

## Symptom Recognition

- `kubectl get pvc -n <ns>` shows the claim with `STATUS=Pending`, an empty `VOLUME` column, and a non-zero `AGE`; the claim never transitions to `Bound`.
- Pods that reference the claim are themselves stuck `Pending`; `kubectl describe pod` shows the scheduler event `FailedScheduling` with message `pod has unbound immediate PersistentVolumeClaims` or `0/N nodes are available: N pod has unbound immediate PersistentVolumeClaims`.
- `kubectl describe pvc <name>` reports one of the following event reasons and messages (verbatim strings emitted by `kube-controller-manager` or the CSI `external-provisioner` sidecar): `ProvisioningFailed` with `failed to provision volume with StorageClass "<class>": rpc error: code = <X> desc = <Y>`; `ProvisioningFailed` with `storageclass.storage.k8s.io "<class>" not found`; `WaitForPodScheduled` / `WaitForFirstConsumer` with `waiting for first consumer to be created before binding`; `ExternalProvisioning` with `waiting for a volume to be created, either by external provisioner "<driver>" or manually created by system administrator`; `ProvisioningFailed` with `failed to get target node: nodes "<node>" not found`.
- CSI-driver-specific `ProvisioningFailed` strings observed in the field include: `rpc error: code = ResourceExhausted desc = ... VolumeLimitExceeded`, `rpc error: code = InvalidArgument desc = ... InvalidVolumeSize`, `rpc error: code = Internal desc = ... UnauthorizedOperation`, `rpc error: code = Internal desc = could not create volume in EC2: VolumeLimitExceeded: You have reached the maximum number of EBS volumes`, `rpc error: code = DeadlineExceeded desc = context deadline exceeded`, and `rpc error: code = Aborted desc = an operation with the given Volume ID already exists`.
- Metrics: `kube_persistentvolumeclaim_status_phase{phase="Pending"} == 1` for the claim; `csi_sidecar_operations_seconds_count{operation_name="CreateVolume",grpc_status_code!="0"}` increases; cloud-provider logs (CloudTrail `CreateVolume` events for EBS, GCE `instances.attachDisk` errors for PD) show `Client.VolumeLimitExceeded`, `RequestLimitExceeded`, or `UnauthorizedOperation` correlated in time.

## Applicability

- Kubernetes 1.20+ clusters using dynamic provisioning via a StorageClass with either an in-tree provisioner or a CSI driver (`ebs.csi.aws.com`, `disk.csi.azure.com`, `pd.csi.storage.gke.io`, `driver.longhorn.io`, `rook-ceph.rbd.csi.ceph.com`, `cephfs.csi.ceph.com`, `vsphere.csi.vmware.com`, etc.).
- Diagnostic permissions required: `kubectl get/describe` on `persistentvolumeclaims`, `persistentvolumes`, `storageclasses`, `csidrivers`, `csistoragecapacities`, `events`, `pods`, `nodes`; `kubectl logs` on the CSI controller namespace (commonly `kube-system`, `longhorn-system`, `rook-ceph`); read access to the cloud provider's volume API (`ec2:DescribeVolumes`, `compute.disks.list`, `Microsoft.Compute/disks/read`).
- Required client tooling: `kubectl` v1.20+, `jq`, optional `aws`/`gcloud`/`az` CLI to inspect cloud-provider state and quotas.
- Out of scope: in-tree GCEPersistentDisk/AWSElasticBlockStore provisioners (removed in 1.25+); statically pre-provisioned PVs where dynamic provisioning is intentionally disabled by `storageClassName: ""` — those follow a different troubleshooting path documented under static binding.

## Diagnostic Steps

### Step 1: Capture the PVC events and message

```bash
kubectl describe pvc <pvc-name> -n <namespace> | sed -n '/Events:/,$p'
```

Expected output: an `Events` table from `kube-controller-manager` or `external-provisioner`. The Reason column should contain one of `WaitForPodScheduled`, `WaitForFirstConsumer`, `ExternalProvisioning`, `ProvisioningFailed`, `Provisioning`, or `ProvisioningSucceeded`. The Message column carries the verbatim error text used to pin the cause — capture the most recent `ProvisioningFailed` or `WaitForFirstConsumer` line exactly.

### Step 2: Inspect the PVC spec for the StorageClass and access modes

```bash
kubectl get pvc <pvc-name> -n <namespace> -o jsonpath='{"storageClassName="}{.spec.storageClassName}{"\naccessModes="}{.spec.accessModes}{"\nrequestedStorage="}{.spec.resources.requests.storage}{"\nvolumeName="}{.spec.volumeName}{"\nvolumeMode="}{.spec.volumeMode}{"\n"}'
```

Expected output: five labelled lines. `storageClassName` is either a named class, the empty string `""` (which disables dynamic provisioning), or absent (which triggers the default class lookup). `accessModes` is one of `["ReadWriteOnce"]`, `["ReadOnlyMany"]`, `["ReadWriteMany"]`, `["ReadWriteOncePod"]`. A non-empty `volumeName` means the claim is pre-bound to a named PV; the binding then depends on that PV's state, not on dynamic provisioning.

### Step 3: Confirm the StorageClass exists and identify its provisioner

```bash
kubectl get storageclass
kubectl get storageclass <class-name> -o jsonpath='{"provisioner="}{.provisioner}{"\nvolumeBindingMode="}{.volumeBindingMode}{"\nreclaimPolicy="}{.reclaimPolicy}{"\nallowedTopologies="}{.allowedTopologies}{"\nparameters="}{.parameters}{"\n"}' 2>&1
```

Expected output: the list of all StorageClasses (the one with annotation `storageclass.kubernetes.io/is-default-class: "true"` is the default), followed by five labelled lines for the referenced class. A `NotFound` error from the second command means the class referenced in Step 2 does not exist. `provisioner` names the controller responsible for `CreateVolume` (for example `ebs.csi.aws.com`, `kubernetes.io/no-provisioner` for purely-static classes). `volumeBindingMode` is either `Immediate` (the default; the PVC is bound as soon as it is created) or `WaitForFirstConsumer` (binding deferred until a pod that references the PVC is scheduled).

### Step 4: Verify the CSI controller and node pods are Running

```bash
PROVISIONER=$(kubectl get storageclass <class-name> -o jsonpath='{.provisioner}')
echo "Provisioner: $PROVISIONER"
# Common controller deployments per provisioner:
kubectl get pods -A -l 'app in (ebs-csi-controller,ebs-csi-node,csi-azuredisk-controller,csi-azuredisk-node,csi-gce-pd-controller,csi-gce-pd-node,longhorn-csi-plugin,csi-rbdplugin,csi-cephfsplugin,vsphere-csi-controller,vsphere-csi-node)' -o wide
kubectl get csidrivers
```

Expected output: every controller and node-plugin pod in `Running` state with all containers Ready, and an entry in `csidrivers` whose name equals the StorageClass provisioner from Step 3. Pods in `CrashLoopBackOff`, `ImagePullBackOff`, or `Pending` mean the driver itself is broken — the PVC cannot be provisioned until the driver pods are healthy.

### Step 5: Tail the CSI controller logs for the failing CreateVolume call

```bash
# Replace label/namespace per Step 4 output; for AWS EBS:
kubectl logs -n kube-system -l app=ebs-csi-controller -c csi-provisioner --tail=200 | grep -iE 'createvolume|failed|error|denied|limit'
kubectl logs -n kube-system -l app=ebs-csi-controller -c ebs-plugin --tail=200 | grep -iE 'createvolume|failed|error|denied|limit'
```

Expected output: the `csi-provisioner` sidecar log lines for the `CreateVolume` RPC and the driver-plugin container's response. A successful provision logs `successfully provisioned volume <id>`. Failures show the gRPC status code and the underlying cloud API error (`UnauthorizedOperation`, `VolumeLimitExceeded`, `InvalidParameterValue: ... requested size`, `InvalidVolumeType`, `MissingParameter: KMS key`, etc.). The exact error string is the strongest cause signal.

### Step 6: Check cloud-provider quota and capacity

```bash
# AWS EBS volume count and quota in the cluster region
aws ec2 describe-volumes --filters "Name=status,Values=available,in-use,creating" --query 'length(Volumes)'
aws service-quotas get-service-quota --service-code ebs --quota-code L-D18FCD1D --query 'Quota.Value'
# Alternative: GCP persistent disks
gcloud compute regions describe <region> --format='value(quotas)' | tr ';' '\n' | grep -i disk
# Alternative: Azure managed disks per region
az vm list-usage --location <region> --query "[?contains(name.value,'Disk')]" -o table
```

Expected output: the count of existing volumes in the cluster region, and the soft/hard quota for that volume class. If the count is at or above the quota, every new `CreateVolume` returns `VolumeLimitExceeded`/`QUOTA_EXCEEDED` and the CSI driver re-emits `ProvisioningFailed`. Insufficient capacity in a specific Availability Zone (less common but possible for unusual disk sizes/IOPS combinations) surfaces as `InsufficientVolumeCapacity` or `InsufficientResourceCapacity` from the cloud API.

### Step 7: Compare PVC topology requirements with available node zones

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone,REGION:.metadata.labels.topology\\.kubernetes\\.io/region,SCHEDULABLE:.spec.unschedulable
kubectl get storageclass <class-name> -o jsonpath='{.allowedTopologies}'
# If a pod consumes the PVC, find its zone constraint:
kubectl get pod <consumer-pod> -n <namespace> -o jsonpath='{.spec.nodeSelector}{"\n"}{.spec.affinity.nodeAffinity}'
```

Expected output: every node's zone and region, the StorageClass `allowedTopologies` (a JSON object listing zones the provisioner may use, or `null`/missing for no constraint), and the consumer pod's node-affinity/nodeSelector. If `allowedTopologies` lists zones with no schedulable nodes, or the pod constrains itself to a zone not in `allowedTopologies`, the CSI driver cannot place the volume.

### Step 8: Check whether the consumer pod is scheduled (WaitForFirstConsumer case)

```bash
kubectl get pods -n <namespace> -o json | jq -r --arg pvc "<pvc-name>" '.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName==$pvc) | {pod:.metadata.name,phase:.status.phase,node:.spec.nodeName,reason:(.status.conditions[]? | select(.type=="PodScheduled") | .reason)}'
```

Expected output: a JSON line per pod referencing the PVC. If the consumer pod has `phase: Pending` and `reason: Unschedulable`, the pod's own scheduling failure is what is blocking volume binding under `WaitForFirstConsumer`. If `phase: Pending` and `reason: SchedulingGated` or the consumer pod simply does not exist yet, the PVC will stay `Pending` until a referencing pod is created and is itself schedulable.

### Step 9: Check ResourceQuota for PVC and storage limits

```bash
kubectl describe resourcequota -n <namespace>
```

Expected output: `Resource`, `Used`, `Hard` columns. If `used` equals `hard` for `persistentvolumeclaims`, `requests.storage`, or `<storage-class>.storageclass.storage.k8s.io/persistentvolumeclaims`, new PVCs in the namespace are admitted by the API server but cannot be provisioned beyond the cap — typically the API server rejects them at creation, but mismatches surface here as well when a quota was lowered after the PVC was created.

### Step 10: Inspect a pre-bound PV when `volumeName` is set

```bash
PV=$(kubectl get pvc <pvc-name> -n <namespace> -o jsonpath='{.spec.volumeName}')
if [ -n "$PV" ]; then
  kubectl get pv "$PV" -o jsonpath='{"phase="}{.status.phase}{"\nstorageClassName="}{.spec.storageClassName}{"\naccessModes="}{.spec.accessModes}{"\ncapacity="}{.spec.capacity.storage}{"\nclaimRef.namespace/name="}{.spec.claimRef.namespace}{"/"}{.spec.claimRef.name}{"\n"}'
fi
```

Expected output: empty (no pre-binding — the PVC is using dynamic provisioning), or the named PV's `phase`, `storageClassName`, `accessModes`, `capacity`, and `claimRef`. If `phase` is `Released`, the PV holds a stale `claimRef` from a deleted PVC and will not re-bind until the `claimRef` is cleared or the PV is recreated.

## Causes

### Cause A: StorageClass does not exist or is not the cluster default

**Statement:** The PVC references a StorageClass name that is not present in the cluster, or omits `storageClassName` while no default StorageClass is configured, so the controller has no provisioner to invoke.

**Chain:**
- root: the PVC's referenced StorageClass is missing, or no default class exists when `storageClassName` is omitted.
- s1: the controller cannot resolve a provisioner — it either emits `ProvisioningFailed` `... not found` or admits an empty `storageClassName` and skips dynamic provisioning entirely.
- D: no `CreateVolume` is ever issued, so the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `storageClassName` is empty AND [Step 3] no StorageClass carries the `storageclass.kubernetes.io/is-default-class: "true"` annotation.
- s1: [Step 1] event message contains `storageclass.storage.k8s.io` and `not found`.
- s1: [Step 3] `kubectl get storageclass <class-name>` returns `Error from server (NotFound)`.

**Interventions:**
- **remediation** (root): create the missing StorageClass, mark an existing class as default, or recreate the PVC pointing at an existing class.

  ```bash
  # Option A: create the StorageClass (AWS EBS gp3)
  cat > /tmp/sc.yaml <<'EOF'
  apiVersion: storage.k8s.io/v1
  kind: StorageClass
  metadata:
    name: gp3
    annotations:
      storageclass.kubernetes.io/is-default-class: "true"
  provisioner: ebs.csi.aws.com
  volumeBindingMode: WaitForFirstConsumer
  reclaimPolicy: Delete
  allowVolumeExpansion: true
  parameters:
    type: gp3
    fsType: ext4
  EOF
  kubectl apply -f /tmp/sc.yaml

  # Option B: mark an existing class default
  kubectl patch storageclass <existing-class> \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

  # Option C: recreate the PVC at an existing class
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc.yaml
  # set spec.storageClassName, then:
  kubectl delete pvc <pvc-name> -n <namespace>
  kubectl apply -f /tmp/pvc.yaml
  ```

  **Verification:** `kubectl get storageclass` shows the target class as `(default)`; the PVC moves from `Pending` to `Bound` within 30s (Immediate) or once a consumer pod schedules (WaitForFirstConsumer).
- **mitigation** (root): inventory existing classes to pick a known-good one for the durable fix.

  ```bash
  kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.provisioner}{"\n"}{end}'
  ```

  **Risk:** Patching to a different class means delete+recreate (the field is immutable), losing pre-bound state; coordinate the window with the workload owner. **Duration:** Read-only. **Verification:** the chosen class appears with a known provisioner.

### Cause B: CSI controller pods are not Running

**Statement:** The CSI driver for the StorageClass is uninstalled, crashed, or has no Ready controller pod, so the `CreateVolume` RPC cannot be dispatched and the PVC repeats `ExternalProvisioning` events.

**Chain:**
- root: the CSI controller deployment is uninstalled, `CrashLoopBackOff`, `ImagePullBackOff`, or `Pending`.
- s1: no live `external-provisioner` holds the lease, so no `CreateVolume` call reaches the driver plugin.
- D: the controller surfaces `ExternalProvisioning` indefinitely and the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] one or more CSI controller/node pods are `CrashLoopBackOff`, `ImagePullBackOff`, `Pending`, or `Error`.
- root: [Step 4] `kubectl get csidrivers` does not list the provisioner named in the StorageClass.
- s1: [Step 1] PVC event reason is `ExternalProvisioning` with message `waiting for a volume to be created, either by external provisioner`.

**Interventions:**
- **remediation** (root): install the driver if missing, fix the crashing controller's root failure, or wire the IRSA role so it becomes Ready.

  ```bash
  # 1) If missing, install the official Helm chart (AWS EBS shown)
  helm repo add aws-ebs-csi-driver https://kubernetes-sigs.github.io/aws-ebs-csi-driver
  helm repo update
  helm upgrade --install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
    --namespace kube-system

  # 2) If installed but crashing, inspect logs (typical: missing IAM (Cause D),
  #    missing IRSA SA annotation, or a wrong image tag in chart values)
  kubectl logs -n kube-system -l app=ebs-csi-controller -c ebs-plugin --tail=200

  # 3) For IRSA on EKS, attach the role on the controller SA
  kubectl annotate -n kube-system serviceaccount ebs-csi-controller-sa \
    eks.amazonaws.com/role-arn=arn:aws:iam::<account>:role/<ebs-csi-irsa-role> --overwrite
  kubectl rollout restart deployment -n kube-system ebs-csi-controller
  ```

  **Verification:** `kubectl get pods -n kube-system -l app=ebs-csi-controller` shows all pods `Running`/Ready; a fresh `kubectl describe pvc <pvc-name>` shows a `Provisioning`/`ProvisioningSucceeded` pair within 60s.
- **mitigation** (s1): restart the controller deployment to recover a wedged but installed sidecar.

  ```bash
  kubectl rollout restart deployment -n kube-system ebs-csi-controller
  kubectl rollout status deployment -n kube-system ebs-csi-controller --timeout=120s
  ```

  **Risk:** A blanket restart briefly drops the `external-provisioner` lease; in-flight CreateVolume calls may be retried — acceptable in incident windows. **Duration:** Up to 5 minutes while pods reschedule. **Verification:** controller pods return to `Running`/Ready and a new `Provisioning` event appears on the PVC.

### Cause C: StorageClass uses WaitForFirstConsumer and no consumer pod is scheduled

**Statement:** The StorageClass has `volumeBindingMode: WaitForFirstConsumer`, so the controller intentionally defers `CreateVolume` until a pod that references the PVC is scheduled to a node — and no such pod is yet schedulable.

**Chain:**
- root: the StorageClass uses `volumeBindingMode: WaitForFirstConsumer`, deferring provisioning until a consumer pod schedules.
- s1: no referencing pod is schedulable (none exists, or every one is `Pending`/`Unschedulable`/`SchedulingGated`), so no node zone is selected.
- D: the controller emits `WaitForFirstConsumer` and never calls `CreateVolume`, leaving the PVC `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 3] `volumeBindingMode` is `WaitForFirstConsumer`.
- s1: [Step 1] PVC event reason is `WaitForPodScheduled` or `WaitForFirstConsumer` with message `waiting for first consumer to be created before binding`.
- s1: [Step 8] no pod references the PVC, or every referencing pod is `Pending` with `reason: Unschedulable` / `SchedulingGated`.

**Interventions:**
- **remediation** (s1): make the consumer pod schedulable (reduce requests, add tolerations, or relax affinity) so binding can proceed.

  ```bash
  # Fix the consumer pod's scheduling issue so it can land on a node.
  # Common patterns:
  #   - reduce resource requests so the pod fits an existing node
  kubectl set resources deployment/<deployment> -n <namespace> \
    --requests=cpu=100m,memory=128Mi
  #   - add a toleration if all candidate nodes are tainted
  kubectl patch deployment/<deployment> -n <namespace> --type=json -p='[{"op":"add","path":"/spec/template/spec/tolerations/-","value":{"key":"workload","operator":"Equal","value":"general","effect":"NoSchedule"}}]'
  #   - relax node affinity / nodeSelector that no node satisfies
  kubectl edit deployment/<deployment> -n <namespace>
  # Verify the pod transitions and the PVC binds
  kubectl get pvc <pvc-name> -n <namespace> -w
  ```

  **Verification:** `kubectl get pod <consumer-pod> -n <namespace>` reaches `Running`; `kubectl get pvc <pvc-name> -n <namespace>` shows `Bound` with a non-empty `VOLUME`; `kubectl describe pvc` shows `ProvisioningSucceeded` with the new PV name.
- **mitigation** (s1): inspect the consumer pod's scheduling events to find the blocker before fixing it.

  ```bash
  kubectl get pods -n <namespace> -o wide \
    | grep -E "Pending|ContainerCreating"
  kubectl describe pod <consumer-pod> -n <namespace> \
    | sed -n '/Events:/,$p'
  ```

  **Risk:** Patching the PVC to an `Immediate`-binding class instead pre-provisions a volume in an arbitrary zone that can later collide with a pod needing a different zone; only do so when no consumer pod is expected. **Duration:** Read-only; the next step is to make the consumer pod schedulable. **Verification:** the pod's `Events` name the precise scheduling constraint (insufficient resources, taint, affinity).

### Cause D: CSI driver lacks cloud-provider IAM permissions

**Statement:** The CSI controller's service account has no IAM/identity grant for the cloud volume APIs (`ec2:CreateVolume`, `compute.disks.insert`), so every `CreateVolume` returns an auth error and the PVC stays `Pending`.

**Chain:**
- root: the controller's bound identity (instance profile, IRSA, Workload/Managed Identity) lacks the required volume-API permissions.
- s1: each `CreateVolume` is rejected with `UnauthorizedOperation`/`AccessDenied`/`PERMISSION_DENIED`, logged by the controller plugin.
- s2: the `external-provisioner` surfaces it as `ProvisioningFailed` with `rpc error: code = Internal desc = ...`.
- D: no volume is created and the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] the controller ServiceAccount is missing the cloud-identity annotation (`eks.amazonaws.com/role-arn`, `iam.gke.io/gcp-service-account`).
- s1: [Step 5] controller logs contain `is not authorized to perform: ec2:CreateVolume` or `googleapi: Error 403: Required '<permission>' permission`.
- s2: [Step 1] event message contains `UnauthorizedOperation`, `AccessDenied`, or `PERMISSION_DENIED`.

**Interventions:**
- **remediation** (root): grant the controller identity the scoped volume permissions, confirm the annotation, and restart.

  ```bash
  # AWS EKS: attach the v2 policy, confirm annotation, then restart
  aws iam attach-role-policy --role-name <ebs-csi-irsa-role> \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicyV2
  kubectl annotate -n kube-system serviceaccount ebs-csi-controller-sa \
    eks.amazonaws.com/role-arn=arn:aws:iam::<account>:role/<ebs-csi-irsa-role> --overwrite
  kubectl rollout restart deployment -n kube-system ebs-csi-controller
  # GKE: bind a GSA via Workload Identity, then link the controller KSA to it
  gcloud projects add-iam-policy-binding <project> \
    --member="serviceAccount:<gsa>@<project>.iam.gserviceaccount.com" \
    --role="roles/compute.storageAdmin"
  kubectl annotate serviceaccount -n kube-system csi-gce-pd-controller \
    iam.gke.io/gcp-service-account=<gsa>@<project>.iam.gserviceaccount.com --overwrite
  ```

  **Verification:** `kubectl logs -n kube-system -l app=ebs-csi-controller -c ebs-plugin --tail=50` shows a fresh `CreateVolume` succeed with no `UnauthorizedOperation`; the PVC binds (`ProvisioningSucceeded`).
- **mitigation** (root): attach the broad managed policy as a short-term unblock until the scoped policy lands.

  ```bash
  aws iam attach-role-policy --role-name <ebs-csi-irsa-role> \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
  ```

  **Risk:** `AmazonEBSCSIDriverPolicy` grants access across every EBS volume in the account; acceptable short-term but pair with a restricted custom policy. **Duration:** Up to 24h. **Verification:** a fresh `CreateVolume` in the logs succeeds and the PVC binds.

### Cause E: Cloud-provider quota or capacity is exhausted

**Statement:** The cloud provider rejects new volume creation in the cluster region because the account quota for that volume class is reached or the requested AZ has insufficient capacity.

**Chain:**
- root: the account's per-region limit (volume count, capacity, IOPS, or throughput) is reached, or the AZ lacks capacity.
- s1: each `CreateVolume` cloud-API call returns `VolumeLimitExceeded`/`RequestLimitExceeded`/`QUOTA_EXCEEDED`/`InsufficientResourceCapacity`.
- s2: the CSI driver surfaces `ProvisioningFailed` and the `external-provisioner` retries with backoff.
- D: no volume is created and the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 6] current volume count is at or above the configured service quota.
- s1: [Step 1] event message contains `VolumeLimitExceeded`, `RequestLimitExceeded`, `QUOTA_EXCEEDED`, or `InsufficientResourceCapacity`.
- s2: [Step 5] controller logs contain `You have reached the maximum number of EBS volumes` or `Quota '<resource>' exceeded` from the cloud API.

**Interventions:**
- **remediation** (root): request a quota increase for the volume class in the region (and reduce demand by deleting orphaned `Released` PVs).

  ```bash
  # AWS: request a quota increase for EBS volume count in the region
  aws service-quotas request-service-quota-increase \
    --service-code ebs --quota-code L-D18FCD1D --desired-value <new-limit>
  # GCP:
  gcloud compute project-info update --quota-target='SSD_TOTAL_GB' --quota-limit=<new-limit>
  # Azure: request via support ticket
  az support tickets create --ticket-name "<name>" --issue-type quota \
    --quota-ticket-details file://quota-request.json
  # Reduce demand: delete orphaned Released PVs
  kubectl get pv -o jsonpath='{range .items[?(@.status.phase=="Released")]}{.metadata.name}{"\n"}{end}' \
    | xargs -r -I{} kubectl delete pv {}
  ```

  **Verification:** `aws service-quotas get-service-quota --service-code ebs --quota-code L-D18FCD1D` returns the new limit; the PVC binds once the backoff retry fires (up to 5 min).
- **mitigation** (root): reclaim quota by deleting clearly orphaned, unattached volumes.

  ```bash
  # AWS: list available (unattached) EBS volumes as cleanup candidates
  aws ec2 describe-volumes --filters "Name=status,Values=available" \
    --query 'Volumes[].{Id:VolumeId,Size:Size,Created:CreateTime}' --output table
  # GCP: list unattached PDs
  gcloud compute disks list --filter='-users:*' --format='table(name,sizeGb,zone)'
  ```

  **Risk:** Deleting volumes is destructive — confirm each is unattached and not referenced by any Bound PV first. **Duration:** Minutes. **Verification:** the freed volume count drops below the quota and a re-test PVC provisions.

### Cause F: PVC access mode is not supported by the provisioner

**Statement:** The PVC requests an access mode (typically `ReadWriteMany`) that the CSI driver behind the StorageClass does not support, so the controller cannot select any topology that satisfies both the access mode and the requested storage class.

**Chain:**
- root: the PVC requests `ReadWriteMany` against a block-storage driver that implements only `ReadWriteOnce`/`ReadWriteOncePod`.
- s1: admission may accept the PVC but `CreateVolume` returns `INVALID_ARGUMENT` with `multi-attach not supported`, or the provisioner refuses to attempt the call.
- D: no compatible volume can be created and the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `accessModes` contains `ReadWriteMany` AND [Step 3] provisioner is a block-storage driver (`ebs.csi.aws.com`, `disk.csi.azure.com`, `pd.csi.storage.gke.io`, `driver.longhorn.io` without RWX add-on).
- s1: [Step 1] event message contains `multi-attach`, `not supported`, or `INVALID_ARGUMENT`.
- s1: [Step 5] controller logs contain `accessMode <mode> not supported by driver`.

**Interventions:**
- **remediation** (root): recreate the PVC against a shared-filesystem class that supports RWX (or switch the workload to `ReadWriteOnce` if shared access is not truly needed).

  ```bash
  # Pick a shared-filesystem class for RWX
  kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.provisioner}{"\n"}{end}' \
    | grep -E 'efs|file|nfs|cephfs'
  # Recreate the PVC pointing at that class
  cat > /tmp/pvc-rwx.yaml <<EOF
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: <pvc-name>
    namespace: <namespace>
  spec:
    accessModes: ["ReadWriteMany"]
    storageClassName: <efs-or-equivalent-class>
    resources:
      requests:
        storage: <size>
  EOF
  kubectl delete pvc <pvc-name> -n <namespace>
  kubectl apply -f /tmp/pvc-rwx.yaml
  # Alternative: change the workload to ReadWriteOnce when shared access is not actually required
  ```

  **Verification:** `kubectl get pvc <pvc-name> -n <namespace>` reaches `Bound` against the new StorageClass; `kubectl describe pvc` shows `ProvisioningSucceeded`; consumer pods reach `Running` and shared writes from multiple pods succeed.
- **mitigation** (root): back up the original PVC manifest before recreating it.

  ```bash
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc-orig.yaml
  ```

  **Risk:** Recreating the PVC invalidates any pre-bound state and workload pod-volume bindings; perform during a maintenance window. **Duration:** Backup only — apply the durable fix immediately. **Verification:** `/tmp/pvc-orig.yaml` exists and round-trips with `kubectl apply --dry-run=client`.

### Cause G: PVC requests a size, type, or zone the provisioner cannot satisfy

**Statement:** The PVC requests a storage size below the provisioner's minimum, an invalid volume type for the cloud, or a topology that conflicts with the StorageClass's `allowedTopologies`, so the cloud API rejects `CreateVolume` with a validation error.

**Chain:**
- root: the PVC requests a size below the provisioner minimum, an invalid volume type, or a zone excluded by the StorageClass `allowedTopologies`.
- s1: the cloud API rejects `CreateVolume` with `InvalidParameterValue`/`InvalidVolumeSize` or an `INVALID_ARGUMENT` topology mismatch.
- D: no volume is created and the PVC stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `requestedStorage` is below the provisioner-class minimum (1 GiB EBS gp3, 10 GiB GCE pd-standard, etc.).
- root: [Step 7] StorageClass `allowedTopologies` excludes every zone where a candidate consumer node lives.
- s1: [Step 1] event message contains `InvalidVolumeSize`, `requested size`, `InvalidParameterValue`, `topology`, or `accessibility requirements`.

**Interventions:**
- **remediation** (root): recreate the PVC with a valid size/type, widen the StorageClass topology, or constrain the consumer pod to an allowed zone.

  ```bash
  # Edit /tmp/pvc-resize.yaml to set spec.resources.requests.storage to a valid value
  # (e.g., 1Gi for EBS gp3, 10Gi for GCE pd-standard) and an accessMode the driver supports.
  kubectl delete pvc <pvc-name> -n <namespace>
  kubectl apply -f /tmp/pvc-resize.yaml
  # For topology mismatch, widen the StorageClass to cover the candidate zones
  kubectl get storageclass <class> -o yaml > /tmp/sc.yaml
  # edit /tmp/sc.yaml to remove allowedTopologies or add the missing zones, then:
  kubectl apply -f /tmp/sc.yaml
  # Or constrain the consumer pod to a zone the class already covers
  kubectl patch deployment <deployment> -n <namespace> \
    --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/affinity","value":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"topology.kubernetes.io/zone","operator":"In","values":["<allowed-zone>"]}]}]}}}}]'
  ```

  **Verification:** `kubectl get pvc <pvc-name> -n <namespace>` reaches `Bound` with a non-empty `VOLUME`; `kubectl describe pvc` shows `ProvisioningSucceeded`; the consumer pod schedules to a permitted zone and the volume mounts.
- **mitigation** (root): back up the PVC manifest before editing and recreating it.

  ```bash
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc-resize.yaml
  ```

  **Risk:** Increasing the requested size is non-reversible without a volume migration; verify the workload tolerates the larger volume and cost. **Duration:** Backup only. **Verification:** `/tmp/pvc-resize.yaml` exists and round-trips with `kubectl apply --dry-run=client`.

### Cause H: Pre-bound PV is missing or incompatible

**Statement:** The PVC sets `spec.volumeName` referencing a specific PV, but that PV does not exist, is in `Released` state, or has incompatible attributes (StorageClass, size, access mode), so the PVC cannot bind to it.

**Chain:**
- root: `spec.volumeName` references a PV that is missing, `Released` (stale `claimRef`), or has mismatched `storageClassName`/`accessModes`/`capacity`.
- s1: with `volumeName` set the controller skips dynamic provisioning and tries to bind directly, but the existence/compatibility check fails.
- D: no `ProvisioningFailed` event is emitted and the PVC simply stays `Pending` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `volumeName` is set to a non-empty value.
- s1: [Step 10] the named PV does not exist (NotFound) OR its `phase` is `Released` OR its `storageClassName`/`accessModes`/`capacity` differs from the PVC.
- s1: [Step 1] PVC events show no `ProvisioningFailed` and no `Provisioning` activity — only the implicit Pending state.

**Interventions:**
- **remediation** (root): clear a stale `claimRef`, create the missing PV, or recreate the PVC without `volumeName` to let dynamic provisioning handle it.

  ```bash
  # Case 1: PV is Released and needs to be made Available
  kubectl patch pv <pv-name> --type=json \
    -p='[{"op":"remove","path":"/spec/claimRef"}]'
  kubectl get pv <pv-name>  # should now show STATUS=Available
  # Case 2: PV does not exist and is needed — create it
  cat > /tmp/pv.yaml <<EOF
  apiVersion: v1
  kind: PersistentVolume
  metadata:
    name: <pv-name>
  spec:
    capacity:
      storage: <size>
    accessModes: ["ReadWriteOnce"]
    storageClassName: <matching-class>
    csi:
      driver: ebs.csi.aws.com
      volumeHandle: <existing-cloud-volume-id>
  EOF
  kubectl apply -f /tmp/pv.yaml
  # Case 3: PV exists but attributes mismatch — recreate the PVC without volumeName
  # and let dynamic provisioning handle it
  kubectl delete pvc <pvc-name> -n <namespace>
  # remove spec.volumeName from PVC manifest, then:
  kubectl apply -f <pvc-manifest>
  ```

  **Verification:** `kubectl get pv <pv-name>` shows `STATUS=Bound` and `CLAIM=<namespace>/<pvc-name>`; `kubectl get pvc <pvc-name> -n <namespace>` shows `STATUS=Bound` with the matching `VOLUME`; the consumer pod mounts the volume.
- **mitigation** (root): back up the PV before mutating its `claimRef`.

  ```bash
  kubectl get pv <pv-name> -o yaml > /tmp/pv-backup.yaml
  ```

  **Risk:** Clearing a PV `claimRef` makes it immediately re-bindable; another `Pending` PVC matching its size/class may grab it first. Apply during a low-traffic window or pre-bind explicitly. **Duration:** Backup only. **Verification:** `/tmp/pv-backup.yaml` exists and `kubectl apply -f /tmp/pv-backup.yaml` restores the prior `claimRef`.

### Cause Z: Unidentified

**Statement:** The PVC is confirmed Pending with dynamic provisioning enabled, but none of the indicators for Causes A through H match the gathered evidence.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the platform/CSI-driver SME or cloud provider.

  ```bash
  # Bundle all relevant state for handoff
  mkdir -p /tmp/pvc-diag
  kubectl describe pvc <pvc-name> -n <namespace> > /tmp/pvc-diag/pvc.txt
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc-diag/pvc.yaml
  kubectl get storageclass <class> -o yaml > /tmp/pvc-diag/storageclass.yaml 2>&1
  kubectl get csidriver -o yaml > /tmp/pvc-diag/csidrivers.yaml
  kubectl get events -n <namespace> --sort-by=.lastTimestamp > /tmp/pvc-diag/events.txt
  kubectl logs -n kube-system -l app=ebs-csi-controller --all-containers=true --tail=500 > /tmp/pvc-diag/csi-controller.log 2>&1
  kubectl get pods -n <namespace> -o yaml > /tmp/pvc-diag/pods.yaml
  kubectl version > /tmp/pvc-diag/cluster-version.txt
  tar czf /tmp/pvc-diag.tar.gz -C /tmp pvc-diag
  ```

  **Risk:** Capturing context is read-only and safe; enabling verbose CSI logging may surface secrets — redact before sharing externally. **Duration:** Minutes; hand the bundle to the platform team, CSI driver maintainers, or the cloud provider's support channel. **Verification:** the support case is acknowledged with a ticket ID; once a workaround is provided, re-test the original PVC and confirm transition to `Bound`.

## Prevention

- Enforce a default StorageClass in every cluster: set `storageclass.kubernetes.io/is-default-class: "true"` on exactly one class, and add a CI check that fails any cluster manifest where the count of default classes is not 1.
- Prefer `volumeBindingMode: WaitForFirstConsumer` for zone-aware provisioners (EBS, GCE PD, Azure Disk). This eliminates the entire class of "volume in zone A, pod scheduled to zone B" failures that haunt Immediate binding.
- Monitor CSI controller health: alert on `kube_deployment_status_replicas_available{deployment=~"<csi>-controller"} == 0` for 5 minutes and on `csi_sidecar_operations_seconds_count{operation_name="CreateVolume",grpc_status_code!="0"}` increasing for 10 minutes. Both fire long before PVCs visibly stall in user workloads.
- Track cloud-provider quota headroom: page when EBS volume count is above 80% of the regional quota, or when total provisioned capacity exceeds 80% of the account limit. Quota raises take hours; do not wait for the saturation event.
- Set per-namespace `ResourceQuota` for `persistentvolumeclaims` and `requests.storage` so a runaway controller in one namespace cannot exhaust cluster-wide capacity. Add `<class>.storageclass.storage.k8s.io/persistentvolumeclaims` to scope by class for shared multi-tenant clusters.
- Pin a known-good CSI driver version in cluster bootstrap (Helm chart with explicit `image.tag`) and gate upgrades behind a canary cluster. Driver upgrades can introduce schema-incompatible changes that strand existing PVCs.
- Run `kubectl get pvc --all-namespaces --field-selector=status.phase=Pending` from a scheduled job and alert when any PVC stays Pending for more than 10 minutes (matches the typical exponential-backoff window for CSI retries).
- Document the IAM/IRSA/Workload-Identity role required by the CSI driver in the cluster bootstrap Terraform/Pulumi module, and validate the binding on every cluster create. The AWS `AmazonEBSCSIDriverPolicyV2` policy scopes by tag and is the recommended baseline for new clusters.
- Capture the bound PV name into Helm/Kustomize annotations on the consuming workload so post-mortem investigators can immediately correlate Pending PVCs with their consumer pods.
- For shared-filesystem use cases, standardize on a single RWX driver (EFS, Filestore, Azure Files) and a single StorageClass per cluster to avoid the Cause F drift where developers pick a block class by mistake.

## Sources

- [Persistent Volumes - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) — Priority 1. PVC lifecycle states (Pending/Bound/Released/Lost), dynamic vs static provisioning behavior, `DefaultStorageClass` admission controller requirement, capacity/access-mode matching rules, and the explicit-empty `storageClassName: ""` disable-dynamic semantic.
- [Storage Classes - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/storage-classes/) — Priority 1. StorageClass fields used in diagnosis: `provisioner`, `volumeBindingMode` (Immediate vs WaitForFirstConsumer behavior), `allowedTopologies`, `reclaimPolicy`, `allowVolumeExpansion`, `parameters`, the `storageclass.kubernetes.io/is-default-class` annotation, and the multiple-default tiebreaker rule.
- [Dynamic Volume Provisioning - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/dynamic-provisioning/) — Priority 1. End-to-end dynamic provisioning flow, controller responsibilities, and what triggers `ProvisioningFailed` versus `ProvisioningSucceeded` events.
- [CSI Volumes - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/volumes/#csi) — Priority 1. CSI driver registration via the `CSIDriver` resource, controller/node-plugin split, and accessibility requirement (topology) plumbing through `CreateVolume`.
- [Debugging Application: Pods - Kubernetes Documentation](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) — Priority 1. `FailedScheduling` scheduler event format, including `pod has unbound immediate PersistentVolumeClaims`.
- [AWS EBS CSI Driver - GitHub repository](https://github.com/kubernetes-sigs/aws-ebs-csi-driver) — Priority 1. Required IAM permissions (`AmazonEBSCSIDriverPolicy` / `AmazonEBSCSIDriverPolicyV2`), IRSA wiring via `eks.amazonaws.com/role-arn` annotation on `ebs-csi-controller-sa`, and the controller's `UnauthorizedOperation` / `VolumeLimitExceeded` error surfaces.
- [Amazon EKS Troubleshooting - AWS Documentation](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html) — Priority 1. EBS-CSI-driver-specific failure modes on EKS: missing IRSA permissions, AZ insufficient capacity, and the `IamNodeRoleNotFound` / `AccessDenied` failure surface that prevents CSI nodes from registering.
- [CSI external-provisioner - GitHub repository](https://github.com/kubernetes-csi/external-provisioner) — Priority 1. The `ExternalProvisioning` and `ProvisioningFailed` event emission contract, retry/backoff logic (`csi-provisioner --retry-interval-start`, `--retry-interval-max`), and gRPC error mapping for `CreateVolume`.
