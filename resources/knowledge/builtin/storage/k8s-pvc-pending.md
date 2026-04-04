---
id: k8s-pvc-pending
title: "Kubernetes PVC Stuck in Pending"
domain: storage
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pvc
  - persistent-volume
  - storage-class
  - provisioner
  - csi
  - pending
difficulty: intermediate
---

# Kubernetes PVC Stuck in Pending

## Problem Definition

Applies to Kubernetes clusters v1.13+ with dynamic provisioning enabled. Requires read access to PVCs, PVs, StorageClasses, Events, and CSI driver resources. Affects all CSI drivers (EBS CSI, EFS CSI, Longhorn, Ceph, local-path, etc.) and in-tree provisioners.

A PersistentVolumeClaim (PVC) stuck in Pending state means the cluster cannot bind the claim to a PersistentVolume (PV). Pods referencing the PVC remain in Pending state as well, unable to start:

```
$ kubectl get pvc my-data -n production
NAME      STATUS    VOLUME   CAPACITY   ACCESS MODES   STORAGECLASS   AGE
my-data   Pending                                      gp3-csi        15m
```

```
$ kubectl describe pod my-app -n production
Events:
  Warning  FailedScheduling  pod/my-app  0/3 nodes are available:
  3 pod has unbound immediate PersistentVolumeClaims.
```

Common causes:

- **No matching StorageClass** — the PVC references a StorageClass that does not exist or is misconfigured.
- **Provisioner not running** — the CSI driver or in-tree provisioner pods are crashed, not scheduled, or not installed.
- **WaitForFirstConsumer binding** — the StorageClass uses `volumeBindingMode: WaitForFirstConsumer` and no pod has been scheduled yet, so provisioning is intentionally deferred.
- **Capacity exhausted** — the underlying storage backend (cloud provider volume limits, disk pool capacity) cannot create new volumes.
- **Access mode mismatch** — the PVC requests an access mode (e.g., ReadWriteMany) that the provisioner does not support.
- **Zone/topology mismatch** — the PVC must be provisioned in a specific zone, but no nodes exist in that zone or the topology constraints conflict.
- **Pre-existing PV mismatch** — a PVC with a specific `volumeName` references a PV that does not exist or has incompatible attributes (size, access mode, StorageClass).

## Diagnostic Steps

### Step 1. Check PVC events

Retrieves the events on the PVC, which contain the provisioner's error messages explaining why binding failed.

```bash
kubectl describe pvc my-data -n production
```

Look at the `Events` section. Common event messages include:
- `waiting for first consumer to be created before binding` — normal for WaitForFirstConsumer; wait for a pod to be scheduled.
- `storageclass "gp3-csi" not found` — the StorageClass does not exist.
- `failed to provision volume with StorageClass "gp3-csi": rpc error` — the CSI driver encountered an error.
- `no persistent volumes available for this claim and no storage class is set` — no StorageClass specified and no default exists.

### Step 2. Verify the StorageClass exists and is configured correctly

Confirms the StorageClass referenced by the PVC exists and has a valid provisioner.

```bash
kubectl get storageclass
kubectl get storageclass gp3-csi -o yaml
```

Expected output shows the StorageClass with a `provisioner` field (e.g., `ebs.csi.aws.com`, `efs.csi.aws.com`, `driver.longhorn.io`), `volumeBindingMode`, and `parameters`. If the StorageClass does not exist, this is the root cause. If no default StorageClass is set (annotated with `storageclass.kubernetes.io/is-default-class: "true"`), PVCs without an explicit `storageClassName` will remain Pending.

### Step 3. Check the CSI driver / provisioner health

Verifies that the CSI driver pods are running and healthy. A crashed or unscheduled provisioner cannot create volumes.

```bash
# List CSI driver pods (common namespaces: kube-system, longhorn-system, etc.)
kubectl get pods -n kube-system -l app=ebs-csi-controller
kubectl get pods -n kube-system -l app=ebs-csi-node

# Check CSI driver registration
kubectl get csidriver
kubectl get csistoragecapacity -A
```

Expected output shows all CSI controller and node pods in `Running` state with all containers ready. If pods are in CrashLoopBackOff or Pending, examine their logs:

```bash
kubectl logs -n kube-system -l app=ebs-csi-controller --tail=50
```

Common CSI driver failures include expired credentials, missing RBAC permissions, and node driver registration failures.

### Step 4. Check volume binding mode

Determines whether the StorageClass uses `WaitForFirstConsumer`, which defers provisioning until a pod is scheduled.

```bash
kubectl get storageclass gp3-csi -o jsonpath='{.volumeBindingMode}'
```

If the output is `WaitForFirstConsumer`, the PVC stays Pending until a pod referencing it is scheduled to a node. This is normal behavior, not an error. Check whether the pod itself is schedulable:

```bash
kubectl get pods -n production -l app=my-app
kubectl describe pod my-app -n production
```

If the pod is also Pending for reasons unrelated to PVCs (insufficient CPU/memory, node taints), fix the scheduling issue first.

### Step 5. Check available capacity and cloud provider limits

Verifies whether the underlying storage backend has capacity to create new volumes.

For AWS EBS:

```bash
# Check current EBS volume count in the region
aws ec2 describe-volumes --query 'Volumes | length(@)'

# Check the service quota (default: 5000 volumes per region)
aws service-quotas get-service-quota \
  --service-code ebs \
  --quota-code L-D18FCD1D \
  --query 'Quota.Value'
```

For Longhorn:

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,LONGHORN_DISK:.metadata.annotations.longhorn-disk
kubectl -n longhorn-system get volumes.longhorn.io
```

If the volume count approaches the quota or the storage pool is full, new volumes cannot be created.

### Step 6. Check access mode compatibility

Verifies that the requested access mode is supported by the provisioner.

```bash
kubectl get pvc my-data -n production -o jsonpath='{.spec.accessModes}'
```

Common restrictions:
- EBS CSI supports `ReadWriteOnce` only (single-node attachment).
- EFS CSI supports `ReadWriteMany`.
- Longhorn supports `ReadWriteOnce` and `ReadWriteMany` (with NFS).

If the PVC requests `ReadWriteMany` but the provisioner only supports `ReadWriteOnce`, the volume cannot be created.

### Step 7. Check topology and zone constraints

Verifies whether topology constraints prevent the volume from being provisioned in the required zone.

```bash
# Check node zones
kubectl get nodes -o custom-columns=NAME:.metadata.name,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone

# Check PVC topology requirements (if using WaitForFirstConsumer)
kubectl get storageclass gp3-csi -o jsonpath='{.allowedTopologies}'
```

If `allowedTopologies` restricts provisioning to specific zones but no nodes exist in those zones, provisioning fails. With `WaitForFirstConsumer`, the volume is provisioned in the zone of the scheduled node — ensure nodes exist in zones where the pod can schedule.

### Step 8. Check for pre-bound PV mismatches

If the PVC specifies a `volumeName`, verifies that the referenced PV exists and has compatible attributes.

```bash
kubectl get pvc my-data -n production -o jsonpath='{.spec.volumeName}'

# If a volumeName is set, check the PV
kubectl get pv my-static-pv -o yaml
```

The PV must have matching `storageClassName`, compatible `accessModes`, sufficient `capacity`, and `claimRef` pointing to this PVC (or no `claimRef`). A Released PV must be manually cleared before it can be re-bound.

## Mitigation

### Option 1: Create a matching PV manually (for immediate unblock)

**Risk**: Manually created PVs bypass the dynamic provisioner and must be managed manually (including cleanup). The PV capacity and configuration must match the PVC exactly.

**Command**:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: manual-pv-for-my-data
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3-csi
  csi:
    driver: ebs.csi.aws.com
    volumeHandle: vol-0123456789abcdef0
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.kubernetes.io/zone
              operator: In
              values:
                - us-east-1a
EOF
```

**Verify**:

```bash
kubectl get pvc my-data -n production
```

Expected: STATUS changes from `Pending` to `Bound`.

**Duration**: Replace with dynamic provisioning after the CSI driver issue is resolved. Delete the manual PV when no longer needed.

### Option 2: Switch to a working StorageClass

**Risk**: Different StorageClasses may have different performance characteristics (IOPS, throughput) or cost. Verify the alternative is acceptable for the workload.

**Command**:

```bash
kubectl patch pvc my-data -n production -p '{"spec":{"storageClassName":"gp2"}}'
# Note: storageClassName is immutable on existing PVCs. You must delete and recreate:
kubectl delete pvc my-data -n production
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-data
  namespace: production
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp2
  resources:
    requests:
      storage: 10Gi
EOF
```

**Verify**:

```bash
kubectl get pvc my-data -n production
```

**Duration**: Permanent if the original StorageClass is broken. Migrate back after fixing the provisioner.

## Root Cause Resolution

**If** the StorageClass does not exist → create it:

```bash
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-csi
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
  fsType: ext4
EOF
```

**If** the CSI driver is not installed or is crashed → install or fix the driver:

```bash
# For AWS EBS CSI driver via Helm
helm repo add aws-ebs-csi-driver https://kubernetes-sigs.github.io/aws-ebs-csi-driver
helm upgrade --install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
  --namespace kube-system
```

If the driver pods are crashing, check logs for credential or permission errors:

```bash
kubectl logs -n kube-system deployment/ebs-csi-controller --tail=100
```

**If** the CSI driver lacks IAM permissions (AWS) → attach the required policy to the node instance profile or IRSA role:

```bash
aws iam attach-role-policy \
  --role-name EBSCSIDriverRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
```

**If** the access mode is unsupported → change the PVC to a supported access mode, or switch to a provisioner that supports the required mode (e.g., EFS for ReadWriteMany on AWS):

```bash
# Delete and recreate with correct access mode
kubectl delete pvc my-data -n production
kubectl apply -f pvc-with-correct-access-mode.yaml
```

**If** cloud provider capacity is exhausted → request a quota increase or provision in a different availability zone:

```bash
aws service-quotas request-service-quota-increase \
  --service-code ebs \
  --quota-code L-D18FCD1D \
  --desired-value 10000
```

**If** a PV is in Released state and needs re-binding → clear the claimRef to make it Available:

```bash
kubectl patch pv my-static-pv -p '{"spec":{"claimRef":null}}'
```

**If** topology constraints conflict → add nodes in the required zone or relax the topology constraints in the StorageClass.

## Verification

1. Confirm the PVC transitions from Pending to Bound:

```bash
kubectl get pvc my-data -n production -w
```

Expected output shows STATUS changing to `Bound` with a VOLUME name assigned.

2. Confirm the pod referencing the PVC starts successfully:

```bash
kubectl get pod -n production -l app=my-app
```

Expected: pod transitions from Pending to Running.

3. Verify the volume is mounted and writable inside the pod:

```bash
kubectl exec -n production deployment/my-app -- df -h /mnt/data
kubectl exec -n production deployment/my-app -- touch /mnt/data/test-write && echo "Write OK"
```

4. Check that no warning events remain on the PVC:

```bash
kubectl get events -n production --field-selector involvedObject.name=my-data,type=Warning
```

Expected: no recent warning events.

## Prevention

1. **Always verify StorageClass availability before deploying workloads**. Include StorageClass resources in Helm charts or Kustomize bases:

```bash
kubectl get storageclass
# Ensure the required StorageClass exists and has a running provisioner
```

2. **Set a default StorageClass** in every cluster to prevent PVCs without an explicit class from remaining Pending:

```bash
kubectl patch storageclass gp3-csi -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

3. **Monitor CSI driver health** with liveness probes and Prometheus metrics. Set up alerts for CSI controller pod restarts:

```bash
# Prometheus alert rule
# alert: CSIControllerDown
# expr: kube_deployment_status_replicas_available{deployment="ebs-csi-controller"} == 0
# for: 5m
```

4. **Set resource quotas for PVCs** per namespace to prevent a single namespace from exhausting storage capacity:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: storage-quota
  namespace: production
spec:
  hard:
    persistentvolumeclaims: "20"
    requests.storage: "500Gi"
EOF
```

5. **Use WaitForFirstConsumer binding mode** for zone-aware provisioners (EBS, GCE PD) to avoid zone mismatches between volumes and pods.

6. **Pre-provision PVs for critical workloads** in production to avoid dependency on dynamic provisioning during incident recovery.

7. **Document required IAM permissions for CSI drivers** and include them in cluster bootstrapping automation to prevent permission-related provisioning failures.

## Sources

- [Persistent Volumes - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [Storage Classes - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [Dynamic Volume Provisioning - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/dynamic-provisioning/)
- [CSI Drivers - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/volumes/#csi)
- [AWS EBS CSI Driver - GitHub](https://github.com/kubernetes-sigs/aws-ebs-csi-driver)
- [Troubleshoot PVC Pending - Kubernetes Documentation](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/#my-pod-is-pending-with-event-failedscheduling)
- [Volume Binding Mode - Kubernetes Documentation](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
