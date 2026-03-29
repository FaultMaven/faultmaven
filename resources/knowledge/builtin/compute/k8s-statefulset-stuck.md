---
id: k8s-statefulset-stuck
title: "Kubernetes StatefulSet Stuck During Rolling Update: Diagnosis and Resolution"
domain: compute
service: kubernetes
symptom_class:
  - deployment_failure
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - statefulset
  - rolling-update
  - pvc
  - deployment
difficulty: advanced
---

# Kubernetes StatefulSet Stuck During Rolling Update: Diagnosis and Resolution

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution. Requires `kubectl` access with permissions to get, describe, and manage StatefulSets, pods, PVCs, PVs, and ControllerRevisions. Understanding of StatefulSet ordered update semantics is assumed. Force-deleting stateful pods carries data corruption risk and should be used with caution.

A StatefulSet rolling update becomes stuck when one or more pods fail to reach the `Ready` state during an ordered update, blocking the entire rollout. StatefulSets use the `OrderedReady` pod management policy by default, which means the controller updates pods in reverse ordinal order (highest to lowest) and waits for each pod to become Ready before proceeding to the next. If any pod fails, the update halts indefinitely until manual intervention resolves the failing pod.

When updating pod `N`, the controller terminates it, creates a new pod with the updated spec, and waits for it to become Ready. If the new pod fails (crash, readiness probe failure, PVC binding issue, or init container failure), the controller does not proceed to pod `N-1`. The rollout remains stuck until the pod is fixed, deleted, or the StatefulSet is rolled back.

Common root causes include application startup failure (the new image or configuration causes crashes or readiness probe failures), PersistentVolumeClaim issues (PVC fails to bind due to storage class unavailability, capacity exhaustion, or access mode incompatibility), volume attachment stuck (the PV is still attached to the old node and has not been released), init container failure (database migration, schema validation, or dependency check fails), ordered startup dependencies (the updated pod depends on a peer pod that has not yet been updated, creating a circular dependency), resource constraints (insufficient CPU, memory, or ephemeral storage on the node), partition misconfiguration (the `rollingUpdate.partition` field prevents some pods from being updated), and headless Service missing or misconfigured (StatefulSets require a headless Service for stable DNS identities).

Typical presentation:

```text
$ kubectl rollout status statefulset/my-db -n <namespace>
Waiting for 1 pods to be ready...
```

The rollout hangs at this message. Checking pods:

```text
NAME      READY   STATUS             RESTARTS     AGE
my-db-0   1/1     Running            0            2d
my-db-1   1/1     Running            0            2d
my-db-2   0/1     CrashLoopBackOff   5 (30s ago)  10m
```

Pod `my-db-2` (highest ordinal) was updated first but is failing, blocking the update of `my-db-1` and `my-db-0`.

## Diagnostic Steps

### Step 1: Check StatefulSet Status

**What this checks:** The overall rollout status including how many pods have been updated, the target revision, and partition settings.

```bash
kubectl describe statefulset <statefulset-name> -n <namespace>

# Get a concise status view
kubectl get statefulset <statefulset-name> -n <namespace> -o jsonpath='{.status}' | jq .
```

**Expected output:** Fields including `updateRevision`, `currentRevision`, `replicas`, `readyReplicas`, `updatedReplicas`, and `partition` (if set).

**What the finding means:** If `updatedReplicas` is less than `replicas`, the rollout is incomplete. If `currentRevision` differs from `updateRevision`, some pods are still on the old revision. If `partition` is set to a value greater than 0, only pods with ordinals >= partition are updated (intentional for canary deployments but can appear stuck if unintended).

### Step 2: Identify the Stuck Pod

**What this checks:** Which specific pod is blocking the rollout.

```bash
# List pods with their status, sorted by ordinal
kubectl get pods -n <namespace> -l app=<statefulset-label> --sort-by=.metadata.name

# Check which pods are NOT ready
kubectl get pods -n <namespace> -l app=<statefulset-label> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}'
```

**Expected output:** A list of pods showing their phase and readiness status.

**What the finding means:** The stuck pod is typically the one with the highest ordinal that is not Ready. This is the pod that was most recently updated and is blocking further progress.

### Step 3: Diagnose the Failing Pod

**What this checks:** The specific reason the pod is not becoming Ready.

```bash
# Describe the stuck pod
kubectl describe pod <stuck-pod-name> -n <namespace>

# Check container logs
kubectl logs <stuck-pod-name> -n <namespace>
kubectl logs <stuck-pod-name> -n <namespace> --previous

# Check init container logs (if applicable)
kubectl logs <stuck-pod-name> -n <namespace> -c <init-container-name>
```

**Expected output:** Pod events showing the failure reason, and container logs showing application errors.

**What the finding means:** Look for CrashLoopBackOff (application crash), readiness probe failures (application not ready), ImagePullBackOff (wrong image), init container failures (migration or dependency issue), or volume mount errors (PVC not bound).

### Step 4: Check PVC Status

**What this checks:** Whether PersistentVolumeClaims for the StatefulSet pods are bound.

```bash
# List PVCs for the StatefulSet
kubectl get pvc -n <namespace> -l app=<statefulset-label>

# Check for pending or lost PVCs
kubectl get pvc -n <namespace> | grep -v Bound

# Describe a problematic PVC
kubectl describe pvc <pvc-name> -n <namespace>
```

**Expected output:** PVC status (Bound, Pending, or Lost) and events.

**What the finding means:** `Pending` PVCs indicate no matching PersistentVolume is available or the StorageClass cannot provision. `Lost` PVCs indicate the underlying PV was deleted. Either condition prevents the pod from starting.

### Step 5: Check Volume Attachment

**What this checks:** Whether PersistentVolumes are stuck in a released or attached state from the previous pod.

```bash
# Check if volumes are still attached to other nodes
kubectl get volumeattachments | grep <pv-name>

# Check PersistentVolume status
kubectl get pv <pv-name> -o jsonpath='{.status.phase}'
```

**Expected output:** Volume attachment status and PV phase.

**What the finding means:** If the PV shows `Released` instead of `Available`, it may need to be reclaimed before the new pod can use it. If a VolumeAttachment still exists for a deleted pod, the volume may be stuck attached to the old node.

### Step 6: Check ControllerRevision History

**What this checks:** The revision history to understand what changed between the old and new versions.

```bash
# View revision history
kubectl rollout history statefulset/<statefulset-name> -n <namespace>

# Compare current and update revisions
kubectl get controllerrevision -n <namespace> -l app=<statefulset-label> --sort-by=.revision
```

**Expected output:** A list of revisions with their ordinal numbers.

**What the finding means:** The revision history shows how many updates have been applied and enables rollback to a specific revision.

### Step 7: Check Partition Setting

**What this checks:** Whether the `partition` field is set, which controls which pods receive the update.

```bash
kubectl get statefulset <statefulset-name> -n <namespace> \
  -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'
```

**Expected output:** A numeric value or empty (default 0).

**What the finding means:** If `partition` is set to a value greater than 0, pods with ordinals below the partition value are intentionally not updated. This is used for canary deployments but can appear as a stuck rollout if set unintentionally.

### Step 8: Check Headless Service

**What this checks:** Whether the required headless Service exists and is correctly configured for stable DNS identities.

```bash
# Verify the headless Service exists
kubectl get svc <headless-service-name> -n <namespace>

# Confirm it is headless (clusterIP: None)
kubectl get svc <headless-service-name> -n <namespace> -o jsonpath='{.spec.clusterIP}'

# Verify DNS resolution for StatefulSet pods
kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- \
  nslookup <pod-name>.<headless-service-name>.<namespace>.svc.cluster.local
```

**Expected output:** The headless Service with `clusterIP: None` and successful DNS resolution for individual pod names.

**What the finding means:** If the headless Service does not exist or has a ClusterIP assigned, StatefulSet pods will not get stable DNS names. Applications that rely on DNS-based peer discovery (databases, message brokers) will fail health checks.

## Mitigation

### Option 1: Delete the Stuck Pod to Force Retry

Use when the failure is transient (temporary network issue, race condition during startup).

- **Risk:** Low. The StatefulSet controller recreates the pod with the same identity and PVC.
- **Command:**
  ```bash
  kubectl delete pod <stuck-pod-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<statefulset-label> -w
  kubectl rollout status statefulset/<statefulset-name> -n <namespace>
  ```
  The pod should be recreated and, if the issue was transient, become Ready.
- **Duration:** 1 to 5 minutes depending on startup time.

### Option 2: Roll Back to the Previous Revision

Use when the new image or configuration is fundamentally broken and you need to restore service.

- **Risk:** Medium. Rolls back all updated pods to the previous revision. Any pods already updated with the new revision will be restarted with the old configuration.
- **Command:**
  ```bash
  kubectl rollout history statefulset/<statefulset-name> -n <namespace>
  kubectl rollout undo statefulset/<statefulset-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl rollout status statefulset/<statefulset-name> -n <namespace> --timeout=300s
  kubectl get pods -n <namespace> -l app=<statefulset-label>
  ```
  All pods should return to Running/Ready with the previous image.
- **Duration:** 2 to 15 minutes depending on replica count and startup time.

### Option 3: Use Partition to Isolate the Failure

Use when you want to stop the rollout from affecting more pods while investigating.

- **Risk:** Low. Only controls which pods receive the update. Does not affect already-running pods.
- **Command:**
  ```bash
  kubectl patch statefulset <statefulset-name> -n <namespace> \
    -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":<stuck-pod-ordinal>}}}}'
  ```
- **Verify:**
  ```bash
  kubectl get statefulset <statefulset-name> -n <namespace> \
    -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'
  ```
  Only pods with ordinal >= partition will be updated. Lower ordinals remain on the old revision.
- **Duration:** Immediate.

### Option 4: Force Delete a Stuck Terminating Pod

Use when the old pod is stuck in `Terminating` state, preventing the new pod from being created.

- **Risk:** Medium. Force deletion skips graceful shutdown. For stateful workloads (databases), this may cause data corruption if the application does not handle abrupt termination. Verify data integrity after force deletion.
- **Command:**
  ```bash
  kubectl delete pod <stuck-pod-name> -n <namespace> --grace-period=0 --force
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<statefulset-label> -w
  ```
  The controller should create a replacement pod.
- **Duration:** Immediate, but the new pod still needs time to start.

## Root Cause Resolution

**If** the pod crashes or fails readiness probes with the new image **then** fix the application and push a corrected image:

```bash
# Check what changed in the new image
kubectl get statefulset <statefulset-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].image}'

# Update to a fixed image
kubectl set image statefulset/<statefulset-name> \
  <container-name>=<registry>/<image>:<fixed-tag> -n <namespace>

# Monitor the rollout
kubectl rollout status statefulset/<statefulset-name> -n <namespace>
```

**If** the readiness probe configuration does not match the new application behavior **then** update the probe:

```bash
kubectl patch statefulset <statefulset-name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":30}]'
```

**If** the PVC is stuck in `Pending` **then** check the StorageClass and available capacity:

```bash
kubectl get storageclass
kubectl describe storageclass <storage-class-name>
kubectl get pv --sort-by=.spec.capacity.storage
```

If the StorageClass provisioner is out of capacity, add storage or clean up unused PVCs:

```bash
kubectl delete pvc <unused-pvc-name> -n <namespace>
```

**If** the PersistentVolume shows `Released` instead of `Available` **then** reclaim the PV:

```bash
kubectl get pv <pv-name> -o jsonpath='{.spec.persistentVolumeReclaimPolicy}'

# For Retain policy, manually make the PV available
kubectl patch pv <pv-name> --type='json' \
  -p='[{"op":"remove","path":"/spec/claimRef"}]'
```

**If** init containers are failing **then** debug them individually:

```bash
kubectl describe pod <stuck-pod-name> -n <namespace> | grep -A 20 "Init Containers:"
kubectl logs <stuck-pod-name> -n <namespace> -c <init-container-name>
```

Common fixes: database migration init containers may need the previous pod running first (ordered dependency). Fix the init container image or command, then the StatefulSet controller retries automatically.

**If** the updated pod requires peer pods that are not yet updated (circular dependency) **then** consider using `Parallel` pod management or breaking the dependency:

```yaml
spec:
  podManagementPolicy: Parallel  # Removes ordered startup constraint
```

Alternatively, use partition for a phased rollout:

```bash
# Update one pod at a time by adjusting partition
kubectl patch statefulset <statefulset-name> -n <namespace> \
  -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":2}}}}'
# Wait for pod 2 to be Ready, then lower partition
kubectl patch statefulset <statefulset-name> -n <namespace> \
  -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":1}}}}'
# Continue until partition is 0
```

**If** the pod is stuck in `Pending` due to insufficient resources **then** free up resources or add nodes:

```bash
kubectl describe pod <stuck-pod-name> -n <namespace> | grep -A 5 "Events:"
kubectl describe nodes | grep -A 10 "Allocated resources:"
```

## Verification

After resolving the issue, confirm the StatefulSet rollout completes and the workload is healthy.

```bash
# 1. Confirm rollout completes
kubectl rollout status statefulset/<statefulset-name> -n <namespace> --timeout=600s
# Should exit with "statefulset rolling update complete"
```

```bash
# 2. Verify all pods are Ready
kubectl get pods -n <namespace> -l app=<statefulset-label>
# All pods should show Running with READY at full count (e.g., 1/1)
```

```bash
# 3. Verify all pods are on the updated revision
kubectl get statefulset <statefulset-name> -n <namespace> \
  -o jsonpath='currentRevision={.status.currentRevision} updateRevision={.status.updateRevision}'
# Both revisions should match
```

```bash
# 4. Verify PVCs are bound
kubectl get pvc -n <namespace> -l app=<statefulset-label>
# All PVCs should show Bound status
```

```bash
# 5. Validate application health across all replicas
for i in $(seq 0 $((REPLICAS-1))); do
  kubectl exec <statefulset-name>-$i -n <namespace> -- <health-check-command>
done
# For databases, verify replication status and data consistency
```

## Prevention

**Test updates in a staging environment.** Always test StatefulSet image and configuration changes in a non-production environment before applying to production. StatefulSets manage stateful workloads where failures can cause data loss.

**Use partition for canary deployments.** Test the update on a single pod before rolling out to all pods:

```bash
# Set partition to replicas-1 to only update the highest ordinal pod
kubectl patch statefulset <statefulset-name> -n <namespace> \
  -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":'$((REPLICAS-1))'}}}}'
```

Validate the canary pod, then lower partition to 0 to complete the rollout.

**Configure appropriate readiness and startup probes.** StatefulSet rollouts depend on readiness probes to determine when a pod is ready. Ensure probes have sufficient timeouts for application startup:

```yaml
readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 6
  timeoutSeconds: 5
startupProbe:
  httpGet:
    path: /ready
    port: 8080
  failureThreshold: 30
  periodSeconds: 10
```

**Monitor StatefulSet rollout progress.** Set up alerts for stalled rollouts:

```yaml
- alert: StatefulSetUpdateStalled
  expr: |
    (kube_statefulset_status_observed_generation != kube_statefulset_metadata_generation)
    and
    (changes(kube_statefulset_status_replicas_updated[15m]) == 0)
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "StatefulSet {{ $labels.namespace }}/{{ $labels.statefulset }} update is stalled"
```

**Pre-validate storage availability.** Before initiating a StatefulSet update, verify that the StorageClass provisioner is healthy and has sufficient capacity:

```bash
kubectl get storageclass
kubectl get pv --sort-by=.spec.capacity.storage
```

**Maintain rollout history.** Keep sufficient revision history to enable rollbacks:

```yaml
spec:
  revisionHistoryLimit: 10
```

**Avoid updating multiple StatefulSets simultaneously.** Update one StatefulSet at a time in production to limit blast radius and simplify debugging if an update fails.

## Sources

- [Kubernetes: StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) -- Rolling updates, partition, pod management policies, and PVC retention
- [Kubernetes: StatefulSet Basics](https://kubernetes.io/docs/tutorials/stateful-application/basic-stateful-set/) -- Tutorial covering StatefulSet lifecycle and update mechanics
- [Kubernetes: Force Delete StatefulSet Pods](https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/) -- Handling pods stuck in Terminating state
- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) -- General pod debugging techniques applicable to StatefulSet pods
- [Kubernetes: Maximum Unavailable Replicas for StatefulSet](https://kubernetes.io/blog/2022/05/27/maxunavailable-for-statefulset/) -- Concurrent update support for faster rollouts
