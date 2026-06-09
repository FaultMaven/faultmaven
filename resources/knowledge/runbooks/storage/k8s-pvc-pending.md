---
id: k8s-pvc-pending
title: "Kubernetes PersistentVolumeClaim Stuck in Pending"
domain: storage
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

**Mechanism:** When the PVC carries `storageClassName: <name>` the controller looks up that exact StorageClass; if the lookup returns NotFound, the controller emits `ProvisioningFailed` with `storageclass.storage.k8s.io "<name>" not found` and the PVC stays `Pending`. When the PVC omits the field, Kubernetes falls back to the StorageClass annotated `storageclass.kubernetes.io/is-default-class: "true"`; if no class carries the annotation, the PVC is admitted with an empty `storageClassName`, dynamic provisioning is skipped entirely, and the PVC remains `Pending` until a matching pre-existing PV appears.

**Indicator:**

- [Step 1] event message contains `storageclass.storage.k8s.io` and `not found`
<!-- match: {"step": 1, "predicate": "contains", "target": "not found"} -->
- [Step 3] `kubectl get storageclass <class-name>` returns `Error from server (NotFound)`
<!-- match: {"step": 3, "predicate": "contains", "target": "NotFound"} -->
- [Step 2] `storageClassName` is empty AND [Step 3] no StorageClass has the `is-default-class` annotation set to `true`

**Mitigation:**

- **Risk:** Patching the PVC to use a different class requires deleting and recreating the PVC (the field is immutable), which loses any pre-bound state; coordinate the recreate window with the workload owner.
- **Command:**

  ```bash
  kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.provisioner}{"\n"}{end}'
  ```

- **Duration:** Read-only inventory; use the output to pick a known-good class for the durable resolution.

**Resolution:**

```bash
# Option A: create the missing StorageClass (example for AWS EBS gp3)
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

# Option B: mark an existing class as the default
kubectl patch storageclass <existing-class> \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

# Option C: recreate the PVC pointing at an existing class
kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc.yaml
# edit /tmp/pvc.yaml to set spec.storageClassName, then:
kubectl delete pvc <pvc-name> -n <namespace>
kubectl apply -f /tmp/pvc.yaml
```

**Impact:** Cluster-wide for Options A and B (every PVC that omits `storageClassName` will start using the new default; verify that legacy workloads are not surprised). Option C is scoped to the single PVC; deleting a `Pending` PVC is safe because no PV is yet bound.

**Rollback:** `kubectl delete storageclass <new-class>` for Option A; `kubectl annotate storageclass <existing-class> storageclass.kubernetes.io/is-default-class-` for Option B; redeploy the previous PVC manifest for Option C.

**Verification:** `kubectl get storageclass` shows the target class and `(default)` next to it where intended. `kubectl get pvc <pvc-name> -n <namespace>` transitions from `Pending` to `Bound` within 30 seconds (Immediate binding) or once a consumer pod schedules (WaitForFirstConsumer).

### Cause B: CSI controller pods are not Running

**Statement:** The CSI driver responsible for the StorageClass is uninstalled, crashed, or has no Ready controller pod, so the `CreateVolume` RPC cannot be dispatched and the PVC sits with repeating `ExternalProvisioning` events.

**Mechanism:** The `external-provisioner` sidecar in the CSI controller deployment watches PVCs whose StorageClass references the driver's name and issues `CreateVolume` gRPC calls to the driver-plugin container. If the controller pod is `CrashLoopBackOff`, `Pending`, or `ImagePullBackOff`, no sidecar is alive to take the lease; if the pod is missing entirely (driver never installed), the PVC controller surfaces `ExternalProvisioning` indefinitely with `waiting for a volume to be created, either by external provisioner "<driver>" or manually created by system administrator`.

**Indicator:**

- [Step 4] one or more CSI controller/node pods are in `CrashLoopBackOff`, `ImagePullBackOff`, `Pending`, or `Error` state
<!-- match: {"step": 4, "predicate": "contains", "target": "CrashLoopBackOff"} -->
- [Step 1] PVC event reason is `ExternalProvisioning` with message `waiting for a volume to be created, either by external provisioner`
<!-- match: {"step": 1, "predicate": "contains", "target": "ExternalProvisioning"} -->
- [Step 4] `kubectl get csidrivers` does not list the provisioner named in the StorageClass

**Mitigation:**

- **Risk:** A blanket restart of the controller deployment briefly drops the `external-provisioner` lease; in-flight CreateVolume calls may be retried. Acceptable during incident windows.
- **Command:**

  ```bash
  kubectl rollout restart deployment -n kube-system ebs-csi-controller
  kubectl rollout status deployment -n kube-system ebs-csi-controller --timeout=120s
  ```

- **Duration:** Up to 5 minutes while pods reschedule.

**Resolution:**

```bash
# 1) If the driver is missing, install the official Helm chart (AWS EBS shown)
helm repo add aws-ebs-csi-driver https://kubernetes-sigs.github.io/aws-ebs-csi-driver
helm repo update
helm upgrade --install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
  --namespace kube-system

# 2) If the driver is installed but crashing, inspect logs for the root failure
kubectl logs -n kube-system -l app=ebs-csi-controller -c ebs-plugin --tail=200
# Typical fixes:
#   - missing IAM permissions (Cause D)
#   - missing IRSA role annotation on the controller ServiceAccount
#   - wrong image tag pinned in a chart values override

# 3) For IRSA on EKS, attach the role on the controller SA
kubectl annotate -n kube-system serviceaccount ebs-csi-controller-sa \
  eks.amazonaws.com/role-arn=arn:aws:iam::<account>:role/<ebs-csi-irsa-role> --overwrite
kubectl rollout restart deployment -n kube-system ebs-csi-controller
```

**Impact:** Cluster-wide for the driver — every PVC bound to this provisioner depends on it. Restart causes a brief leader-election gap (seconds); already-bound PVs are unaffected because the kubelet-side node plugin handles mount/unmount independently of the controller's CreateVolume path.

**Rollback:** `helm rollback aws-ebs-csi-driver <previous-revision>` for chart upgrades; remove the IRSA annotation with `kubectl annotate ... eks.amazonaws.com/role-arn-` if it was misapplied.

**Verification:** `kubectl get pods -n kube-system -l app=ebs-csi-controller` shows all pods `Running` and Ready. A fresh `kubectl describe pvc <pvc-name>` shows a new `Provisioning`/`ProvisioningSucceeded` event pair within 60 seconds, and the PVC transitions to `Bound`.

### Cause C: StorageClass uses WaitForFirstConsumer and no consumer pod is scheduled

**Statement:** The StorageClass has `volumeBindingMode: WaitForFirstConsumer`, so the controller intentionally defers `CreateVolume` until a pod that references the PVC is scheduled to a node — and no such pod is yet schedulable.

**Mechanism:** Under `WaitForFirstConsumer`, the controller emits `WaitForPodScheduled` / `WaitForFirstConsumer` events and waits. Once a referencing pod is scheduled, the controller selects the pod's node zone and topology, then calls `CreateVolume` with `accessibility_requirements` matching that node. If the pod is itself unschedulable (insufficient CPU/memory, missing tolerations, no PVC consumer at all), no zone is chosen and the PVC remains `Pending` forever. This is normal operating behavior, not a fault of the provisioner — but it blocks every workload that assumes Immediate binding.

**Indicator:**

- [Step 1] PVC event reason is `WaitForPodScheduled` or `WaitForFirstConsumer` with message `waiting for first consumer to be created before binding`
<!-- match: {"step": 1, "predicate": "contains", "target": "waiting for first consumer to be created before binding"} -->
- [Step 3] `volumeBindingMode` is `WaitForFirstConsumer`
<!-- match: {"step": 3, "predicate": "contains", "target": "WaitForFirstConsumer"} -->
- [Step 8] no pod references the PVC, or every referencing pod is `Pending` with `reason: Unschedulable` / `SchedulingGated`

**Mitigation:**

- **Risk:** Patching the PVC to a class with `Immediate` binding pre-provisions a volume in an arbitrary zone, which can later collide with a pod that needs a different zone; only use when no consumer pod is expected and the PVC must be pre-bound for inspection.
- **Command:**

  ```bash
  kubectl get pods -n <namespace> -o wide \
    | grep -E "Pending|ContainerCreating"
  kubectl describe pod <consumer-pod> -n <namespace> \
    | sed -n '/Events:/,$p'
  ```

- **Duration:** Read-only; the next step is to make the consumer pod schedulable.

**Resolution:**

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

**Impact:** Scoped to the consumer Deployment/StatefulSet. Once the pod schedules, the CSI driver provisions the volume in the same zone as the chosen node and the PVC binds within ~30 seconds. Pre-existing pods on other nodes are not affected.

**Rollback:** Revert the deployment with `kubectl rollout undo deployment/<deployment> -n <namespace>`; the previous unschedulable pod spec returns and the PVC reverts to `Pending`.

**Verification:** `kubectl get pod <consumer-pod> -n <namespace>` reaches `Running`, `kubectl get pvc <pvc-name> -n <namespace>` shows `Bound` with a non-empty `VOLUME` column, and `kubectl describe pvc` shows a `ProvisioningSucceeded` event with the new PV name.

### Cause D: CSI driver lacks cloud-provider IAM permissions

**Statement:** The CSI controller's service account has no IAM/identity grant for the cloud-provider volume APIs (such as `ec2:CreateVolume`, `compute.disks.insert`, `Microsoft.Compute/disks/write`), so every `CreateVolume` call returns an authorization error and the PVC stays `Pending`.

**Mechanism:** The CSI controller authenticates to the cloud provider via instance profile, IRSA (EKS), Workload Identity (GKE), or AAD Pod Identity / Managed Identity (AKS). When the bound identity is missing the required permissions, the controller-plugin container logs the cloud-API error verbatim (`UnauthorizedOperation`, `AccessDenied`, `PERMISSION_DENIED`) and the `external-provisioner` sidecar surfaces it on the PVC as `ProvisioningFailed` with `rpc error: code = Internal desc = ...`. On EKS this most commonly means the `ebs-csi-controller-sa` ServiceAccount lacks the `eks.amazonaws.com/role-arn` annotation or the role lacks `AmazonEBSCSIDriverPolicy` / `AmazonEBSCSIDriverPolicyV2`.

**Indicator:**

- [Step 1] event message contains `UnauthorizedOperation`, `AccessDenied`, or `PERMISSION_DENIED`
<!-- match: {"step": 1, "predicate": "contains", "target": "UnauthorizedOperation"} -->
- [Step 5] controller logs contain `is not authorized to perform: ec2:CreateVolume` or `googleapi: Error 403: Required '<permission>' permission`
<!-- match: {"step": 5, "predicate": "contains", "target": "is not authorized to perform"} -->
- [Step 4] the controller ServiceAccount is missing the cloud-identity annotation (for example `eks.amazonaws.com/role-arn` on EKS, `iam.gke.io/gcp-service-account` on GKE)

**Mitigation:**

- **Risk:** Attaching a broad managed policy (for example `AmazonEBSCSIDriverPolicy`) grants the role permissions across every EBS volume in the account; acceptable as a short-term unblock but pair with a restricted custom policy in the durable resolution.
- **Command:**

  ```bash
  aws iam attach-role-policy \
    --role-name <ebs-csi-irsa-role> \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
  ```

- **Duration:** Up to 24 hours, replaced by the scoped policy below.

**Resolution:**

```bash
# AWS EKS: attach the v2 managed policy (scoped via tag-based conditions)
aws iam attach-role-policy \
  --role-name <ebs-csi-irsa-role> \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicyV2
# Confirm IRSA annotation
kubectl annotate -n kube-system serviceaccount ebs-csi-controller-sa \
  eks.amazonaws.com/role-arn=arn:aws:iam::<account>:role/<ebs-csi-irsa-role> --overwrite
# Restart so the controller picks up new credentials immediately
kubectl rollout restart deployment -n kube-system ebs-csi-controller
# GKE: bind a Google service account via Workload Identity
gcloud projects add-iam-policy-binding <project> \
  --member="serviceAccount:<gsa>@<project>.iam.gserviceaccount.com" \
  --role="roles/compute.storageAdmin"
kubectl annotate serviceaccount -n kube-system csi-gce-pd-controller \
  iam.gke.io/gcp-service-account=<gsa>@<project>.iam.gserviceaccount.com --overwrite
```

**Impact:** Cluster-wide for the driver — every future provisioning succeeds against the cloud provider. No effect on already-bound PVs. STS token rotation propagates within ~60 seconds after the controller restart.

**Rollback:** `aws iam detach-role-policy --role-name <ebs-csi-irsa-role> --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicyV2` (or remove the GCP IAM binding), then `kubectl rollout restart deployment -n kube-system ebs-csi-controller`.

**Verification:** `kubectl logs -n kube-system -l app=ebs-csi-controller -c ebs-plugin --tail=50` shows a fresh `CreateVolume` succeed with no `UnauthorizedOperation` line. The original PVC transitions to `Bound`; `kubectl describe pvc` includes `ProvisioningSucceeded` referencing the new PV.

### Cause E: Cloud-provider quota or capacity is exhausted

**Statement:** The cloud provider rejects new volume creation in the cluster region because the account quota for that volume class is reached or the requested AZ has insufficient capacity.

**Mechanism:** Each cloud provider enforces per-region limits on the number of volumes, total provisioned capacity (TiB), provisioned IOPS, and throughput. When the CSI driver's `CreateVolume` call exceeds any of these, the cloud API returns `VolumeLimitExceeded`, `RequestLimitExceeded`, `QUOTA_EXCEEDED`, `InsufficientResourceCapacity`, or `OperationNotAllowed: QuotaExceeded`. The CSI driver surfaces the error verbatim as `ProvisioningFailed`; the `external-provisioner` retries with exponential backoff, so the PVC stays `Pending` until the quota is raised or volumes are freed.

**Indicator:**

- [Step 1] event message contains `VolumeLimitExceeded`, `RequestLimitExceeded`, `QUOTA_EXCEEDED`, or `InsufficientResourceCapacity`
<!-- match: {"step": 1, "predicate": "contains", "target": "VolumeLimitExceeded"} -->
- [Step 5] controller logs contain `You have reached the maximum number of EBS volumes` or `Quota '<resource>' exceeded` from the cloud API
<!-- match: {"step": 5, "predicate": "contains", "target": "maximum number"} -->
- [Step 6] current volume count is at or above the configured service quota

**Mitigation:**

- **Risk:** Deleting orphaned volumes recovers quota immediately but is destructive — confirm each volume is unattached and not referenced by any Bound PV before deletion.
- **Command:**

  ```bash
  # AWS: list available (unattached) EBS volumes for cleanup candidates
  aws ec2 describe-volumes \
    --filters "Name=status,Values=available" \
    --query 'Volumes[].{Id:VolumeId,Size:Size,Created:CreateTime,Tags:Tags}' \
    --output table
  # GCP: list unattached PDs
  gcloud compute disks list --filter='-users:*' --format='table(name,sizeGb,zone,creationTimestamp)'
  ```

- **Duration:** Minutes; only delete clearly orphaned volumes that are not referenced by any cluster PV.

**Resolution:**

```bash
# AWS: request a quota increase for EBS volume count in the region
aws service-quotas request-service-quota-increase \
  --service-code ebs \
  --quota-code L-D18FCD1D \
  --desired-value <new-limit>
# GCP: request quota in the console or via gcloud
gcloud compute project-info update --quota-target='SSD_TOTAL_GB' --quota-limit=<new-limit>
# Azure: request a quota increase via support ticket or
az support tickets create --ticket-name "<name>" --issue-type quota \
  --quota-ticket-details file://quota-request.json
# Reduce demand: enable VolumeSnapshot lifecycle and delete orphaned PVs
kubectl get pv -o jsonpath='{range .items[?(@.status.phase=="Released")]}{.metadata.name}{"\n"}{end}' \
  | xargs -r -I{} kubectl delete pv {}
```

**Impact:** Quota-increase requests are processed by the cloud provider (typically minutes to hours, depending on the limit and region) and apply to every workload in the account. Deleting `Released` PVs reclaims storage immediately and is bounded to volumes the cluster previously owned.

**Rollback:** Quota increases are non-reversible at the workload layer (the higher limit simply remains). Restore deleted volumes from snapshot if the deletion was premature; CSI drivers do not auto-recreate.

**Verification:** `aws service-quotas get-service-quota --service-code ebs --quota-code L-D18FCD1D` returns the new limit and `aws ec2 describe-volumes | jq '.Volumes | length'` is below it. A re-test PVC binds within 60 seconds and the original PVC transitions to `Bound` once the controller's exponential-backoff retry fires (up to 5 minutes).

### Cause F: PVC access mode is not supported by the provisioner

**Statement:** The PVC requests an access mode (typically `ReadWriteMany`) that the CSI driver behind the StorageClass does not support, so the controller cannot select any topology that satisfies both the access mode and the requested storage class.

**Mechanism:** Block-storage CSI drivers (`ebs.csi.aws.com`, `disk.csi.azure.com`, `pd.csi.storage.gke.io`) only implement `ReadWriteOnce` and `ReadWriteOncePod`; shared-filesystem drivers (`efs.csi.aws.com`, `file.csi.azure.com`, `cephfs.csi.ceph.com`, NFS) implement `ReadWriteMany`. When a PVC asks for `ReadWriteMany` against a block driver, validation may pass at admission time but `CreateVolume` returns `INVALID_ARGUMENT` with `multi-attach not supported` or the provisioner refuses to attempt the call. Symptoms can be subtle when an admission webhook accepts the PVC but the driver rejects it later.

**Indicator:**

- [Step 1] event message contains `multi-attach`, `not supported`, or `INVALID_ARGUMENT`
<!-- match: {"step": 1, "predicate": "contains", "target": "INVALID_ARGUMENT"} -->
- [Step 2] `accessModes` contains `ReadWriteMany` AND [Step 3] provisioner is a block-storage driver (`ebs.csi.aws.com`, `disk.csi.azure.com`, `pd.csi.storage.gke.io`, `driver.longhorn.io` without RWX add-on)
- [Step 5] controller logs contain `accessMode <mode> not supported by driver`

**Mitigation:**

- **Risk:** Recreating the PVC with a different access mode invalidates any pre-bound state and any workload pod-volume bindings; perform during a maintenance window for the consuming workload.
- **Command:**

  ```bash
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc-orig.yaml
  ```

- **Duration:** Backup only — apply the durable fix immediately.

**Resolution:**

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

**Impact:** Scoped to the single PVC and its consumer workload. The volume's underlying storage moves from block to shared-filesystem, which changes performance and pricing characteristics — validate with the workload owner before deploying.

**Rollback:** `kubectl apply -f /tmp/pvc-orig.yaml` to recreate the original PVC. The original `Pending` state returns.

**Verification:** `kubectl get pvc <pvc-name> -n <namespace>` reaches `Bound` against the new StorageClass; `kubectl describe pvc` shows `ProvisioningSucceeded`. Consumer pods that mount the PVC reach `Running` and shared writes from multiple pods succeed (`kubectl exec` on two pods writing to the same path).

### Cause G: PVC requests a size, type, or zone the provisioner cannot satisfy

**Statement:** The PVC requests a storage size below the provisioner's minimum, an invalid volume type for the cloud, or a topology that conflicts with the StorageClass's `allowedTopologies`, so the cloud API rejects `CreateVolume` with a validation error.

**Mechanism:** EBS minimum volume size is 1 GiB for gp2/gp3 and 4 GiB for io1/io2; GCE PD minimum is 10 GiB for `pd-standard`, 200 GiB for `pd-extreme`. Below the floor, the cloud API returns `InvalidParameterValue: requested size is below the minimum` and the driver surfaces it as `ProvisioningFailed`. Similarly, `allowedTopologies` on the StorageClass restricts where the provisioner may place the volume; under `WaitForFirstConsumer`, if the consumer pod schedules to a node in a zone not listed in `allowedTopologies`, the driver cannot provision and returns `INVALID_ARGUMENT` with a topology mismatch.

**Indicator:**

- [Step 1] event message contains `InvalidVolumeSize`, `requested size`, `InvalidParameterValue`, `topology`, or `accessibility requirements`
<!-- match: {"step": 1, "predicate": "contains", "target": "InvalidParameterValue"} -->
- [Step 2] `requestedStorage` is below the provisioner-class minimum (1 GiB EBS gp3, 10 GiB GCE pd-standard, etc.)
- [Step 7] StorageClass `allowedTopologies` excludes every zone where a candidate consumer node lives

**Mitigation:**

- **Risk:** Increasing the requested size is non-reversible without a volume migration; verify the workload tolerates the larger volume and the new cost.
- **Command:**

  ```bash
  kubectl get pvc <pvc-name> -n <namespace> -o yaml > /tmp/pvc-resize.yaml
  ```

- **Duration:** Backup only.

**Resolution:**

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

**Impact:** PVC re-creation scopes the change to the single claim. Editing `allowedTopologies` on the StorageClass affects every future PVC bound to it; widen with care and confirm the cloud provider supports volumes in the added zones.

**Rollback:** Restore `/tmp/pvc-resize.yaml` from backup with `kubectl apply -f /tmp/pvc-resize.yaml.bak`; revert the StorageClass with the prior YAML; remove the pod-affinity patch via `kubectl rollout undo deployment/<deployment>`.

**Verification:** `kubectl get pvc <pvc-name> -n <namespace>` reaches `Bound` with a non-empty `VOLUME` column. `kubectl describe pvc` shows `ProvisioningSucceeded`. The consumer pod schedules to a node in a zone that the StorageClass permits and the volume mounts successfully.

### Cause H: Pre-bound PV is missing or incompatible

**Statement:** The PVC sets `spec.volumeName` referencing a specific PV, but that PV does not exist, is in `Released` state, or has incompatible attributes (StorageClass, size, access mode), so the PVC cannot bind to it.

**Mechanism:** When `volumeName` is set, the controller skips dynamic provisioning entirely and tries to bind directly to the named PV. The controller checks that the PV exists, is `Available` (or `Bound` to this same PVC), has matching `storageClassName`, supports the requested access modes, and has at least the requested capacity. Any mismatch keeps the PVC `Pending` with no `ProvisioningFailed` event (because no provisioning is attempted). A `Released` PV retains a stale `claimRef` from a deleted PVC and cannot be re-bound until the `claimRef` is manually cleared.

**Indicator:**

- [Step 2] `volumeName` is set to a non-empty value
<!-- match: {"step": 2, "predicate": "contains", "target": "volumeName="} -->
- [Step 10] the named PV does not exist (NotFound) OR its `phase` is `Released` OR its `storageClassName`/`accessModes`/`capacity` differs from the PVC
- [Step 1] PVC events show no `ProvisioningFailed` and no `Provisioning` activity — only the implicit Pending state

**Mitigation:**

- **Risk:** Clearing a PV `claimRef` makes the PV immediately re-bindable; if another `Pending` PVC matches its size and class, that PVC may grab it before the intended one. Apply during a low-traffic window or pre-bind explicitly.
- **Command:**

  ```bash
  kubectl get pv <pv-name> -o yaml > /tmp/pv-backup.yaml
  ```

- **Duration:** Backup only.

**Resolution:**

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

**Impact:** Scoped to the targeted PV and PVC. Clearing `claimRef` makes the PV immediately bindable cluster-wide for any matching PVC; create the new PVC promptly to avoid an unintended bind. PV creation does not affect other workloads.

**Rollback:** `kubectl apply -f /tmp/pv-backup.yaml` restores the previous `claimRef`; `kubectl delete pv <pv-name>` removes a PV created in error (only safe when no PVC is bound).

**Verification:** `kubectl get pv <pv-name>` shows `STATUS=Bound` and `CLAIM=<namespace>/<pvc-name>`. `kubectl get pvc <pvc-name> -n <namespace>` shows `STATUS=Bound` with the matching `VOLUME`. The consumer pod schedules and mounts the volume successfully (`kubectl exec ... -- mount | grep <mount-path>`).

### Cause Z: Unidentified

**Statement:** The PVC is confirmed Pending and dynamic provisioning is enabled, but none of the indicators for Causes A through H match the gathered evidence.

**Mechanism:** Less common paths include: PVC admission webhooks silently mutating the spec; cluster-scoped `LimitRange` rejecting the requested storage size; OPA/Gatekeeper policies denying creation of PVs in the target namespace; CNI failures preventing the CSI controller from reaching the cloud-provider API; an `external-provisioner` sidecar pinned to an old API version incompatible with the in-cluster Kubernetes; bug states in specific CSI driver versions; or partial cluster upgrades that left in-flight provisioning state stranded. The PVC may also be in an explicit `LostVolume` scenario where the bound PV's backing storage was deleted out-of-band.

**Indicator:**

- [Default] PVC is `Pending` (Step 1 confirmed) but none of Causes A-H indicators match the gathered evidence

**Mitigation:**

- **Risk:** Capturing additional diagnostic context is read-only and safe; enabling verbose CSI logging may surface secrets in logs — redact before sharing externally.
- **Command:**

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

- **Duration:** Minutes. Hand the bundle to the platform team, the CSI driver maintainers, or the cloud provider's support channel.

**Resolution:** Out of runbook scope. Open a support case with the cloud provider (for cloud-CSI drivers) or file a bug against the CSI driver repository (for OSS drivers like Longhorn, Rook-Ceph). Include the diagnostic bundle, the Kubernetes minor version, the CSI driver version (`kubectl get pods -n kube-system -l app=ebs-csi-controller -o jsonpath='{.items[0].spec.containers[*].image}'`), and the exact PVC event message timeline.

**Verification:** Support case is acknowledged with a ticket ID. Once a workaround or fix is provided, re-test on the original PVC and confirm transition to `Bound`.

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
