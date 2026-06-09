---
id: "k8s-statefulset-stuck"
title: "Kubernetes StatefulSet Stuck During Rolling Update"
domain: compute
service: kubernetes
symptom_class: [deployment_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [statefulset, rolling-update, pvc, partition, orderedready]
difficulty: advanced
---

## Symptom Recognition

- `kubectl rollout status statefulset/<name>` hangs at `Waiting for 1 pods to be ready...` without completing
- One or more pods stuck in `CrashLoopBackOff`, `Pending`, `Terminating`, or `Init:Error` with the highest ordinal(s) affected first
- `kubectl get statefulset <name>` shows `updatedReplicas` less than `replicas` for more than 10–15 minutes
- `currentRevision` and `updateRevision` differ in StatefulSet status, indicating an incomplete rollout
- Pods with lower ordinals remain on the old image/config; the update has not progressed past the failing pod
- Alert: `StatefulSetUpdateStalled` — `kube_statefulset_status_observed_generation != kube_statefulset_metadata_generation` and `changes(kube_statefulset_status_replicas_updated[15m]) == 0` for 15+ minutes

## Applicability

Kubernetes 1.24+ on any distribution (self-managed, EKS, GKE, AKS). Requires `kubectl` access with `get`, `describe`, `patch`, `delete`, and `logs` permissions on StatefulSets, pods, PVCs, PersistentVolumes, VolumeAttachments, and ControllerRevisions in the target namespace. The `jq` CLI is assumed available for JSON extraction.

## Diagnostic Steps

### Step 1: Check overall StatefulSet rollout state

```bash
kubectl get statefulset <name> -n <namespace> \
  -o jsonpath='{.status}' | jq '{replicas, readyReplicas, updatedReplicas, currentRevision, updateRevision}'
```

Expected output: JSON object. If `updatedReplicas < replicas` and `currentRevision != updateRevision`, a rollout is in progress or stuck.

### Step 2: Check partition setting

```bash
kubectl get statefulset <name> -n <namespace> \
  -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'
```

Expected output: A number (default `0`). A value greater than `0` means only pods with ordinal >= partition receive the update.

### Step 3: Identify the stuck pod

```bash
kubectl get pods -n <namespace> -l <statefulset-selector> \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}'
```

Expected output: One row per pod showing phase and readiness. The stuck pod is the unready one with the highest ordinal.

### Step 4: Describe the stuck pod for failure reason

```bash
kubectl describe pod <stuck-pod> -n <namespace>
```

Expected output: `Events:` section at the bottom shows the specific failure: `Back-off restarting failed container`, `Readiness probe failed`, `Unschedulable`, `FailedMount`, or `Failed to pull image`.

### Step 5: Check container and init-container logs

```bash
kubectl logs <stuck-pod> -n <namespace> --previous 2>/dev/null || \
  kubectl logs <stuck-pod> -n <namespace>
kubectl logs <stuck-pod> -n <namespace> -c <init-container-name> 2>/dev/null
```

Expected output: Application error messages, stack traces, or migration failure output in the init container.

### Step 6: Check PVC binding status

```bash
kubectl get pvc -n <namespace> | grep -E 'NAME|Pending|Lost'
kubectl describe pvc <pvc-name> -n <namespace>
```

Expected output: PVCs in `Bound` status are healthy. `Pending` means no matching PV found; `Lost` means the underlying PV was deleted.

### Step 7: Check volume attachment for stuck PV

```bash
kubectl get volumeattachments | grep <pv-name>
kubectl get pv <pv-name> -o jsonpath='{.status.phase}'
```

Expected output: VolumeAttachment should be absent for a pod that has been deleted. Phase `Released` means the PV needs manual reclamation before it can be reused.

### Step 8: Check headless Service DNS

```bash
kubectl get svc <headless-svc-name> -n <namespace> \
  -o jsonpath='{.spec.clusterIP}'
kubectl run dns-test --rm -it --restart=Never --image=busybox:1.36 -- \
  nslookup <pod-name>.<headless-svc-name>.<namespace>.svc.cluster.local
```

Expected output: `clusterIP` must be `None`. `nslookup` must return the pod IP. Any other result indicates a misconfigured headless Service.

## Causes

### Cause A: Application crash or readiness probe failure in the new image

**Statement:** The updated container image crashes on startup or fails its readiness probe, preventing the pod from reaching Ready state and blocking the ordered rollout.

**Mechanism:** StatefulSet uses `OrderedReady` pod management by default, waiting for each pod to become Ready before proceeding to the next lower ordinal. If the new image has a bug, misconfigured environment variable, or a readiness probe with insufficient `initialDelaySeconds` for the new startup time, the pod never transitions to Ready and the controller halts indefinitely.

**Indicator:**

- [Step 4] Events show `Back-off restarting failed container` or `Readiness probe failed: ...`
- [Step 5] Container logs show application exception, panic, or non-zero exit on startup

<!-- match: {"step": 4, "predicate": "contains", "target": "Back-off restarting failed container"} -->
<!-- match: {"step": 4, "predicate": "contains", "target": "Readiness probe failed"} -->

**Mitigation:**

- **Risk:** Rolling back resets all pods to the previous revision; any schema changes in the new image may cause incompatibility on rollback.
- **Command:**

  ```bash
  kubectl rollout undo statefulset/<name> -n <namespace>
  kubectl rollout status statefulset/<name> -n <namespace> --timeout=300s
  ```

- **Duration:** 5–15 minutes until all pods are back on the previous revision.

**Resolution:**

```bash
# Fix the image or readiness probe, then re-apply
kubectl set image statefulset/<name> <container>=<registry>/<image>:<fixed-tag> -n <namespace>
# Or patch the readiness probe initialDelaySeconds
kubectl patch statefulset <name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":60}]'
kubectl rollout status statefulset/<name> -n <namespace> --timeout=600s
```

**Impact:** Patch updates the StatefulSet spec and triggers a new rolling update for all pods. **Rollback:** `kubectl rollout undo statefulset/<name> -n <namespace>`

**Verification:** `kubectl rollout status statefulset/<name> -n <namespace>` exits with `statefulset rolling update complete`. All pods show `1/1 Running`.

---

### Cause B: PVC stuck in Pending — no matching PersistentVolume

**Statement:** The StatefulSet's volumeClaimTemplate cannot bind a PersistentVolumeClaim because no PersistentVolume with matching StorageClass, capacity, and access mode is available.

**Mechanism:** When a StatefulSet creates a new pod during a rolling update, it also creates or reuses the associated PVC. If the StorageClass provisioner has no capacity, the StorageClass no longer exists, or the access mode is incompatible, the PVC stays `Pending` indefinitely. The pod cannot mount the volume and remains `Pending` or fails to start, blocking the rollout.

**Indicator:**

- [Step 6] `kubectl get pvc` shows a PVC in `Pending` state for the stuck pod
- [Step 6] `kubectl describe pvc <pvc>` Events show `no persistent volumes available for this claim` or `storageclass not found`

<!-- match: {"step": 6, "predicate": "contains", "target": "Pending"} -->
<!-- match: {"step": 6, "predicate": "contains", "target": "no persistent volumes available"} -->

**Mitigation:**

- **Risk:** Low. Temporarily freezing the rollout at the current partition prevents further PVC creation attempts while storage is provisioned.
- **Command:**

  ```bash
  kubectl patch statefulset <name> -n <namespace> \
    -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":<stuck-pod-ordinal>}}}}'
  ```

- **Duration:** Until storage capacity is restored.

**Resolution:**

```bash
# Verify StorageClass exists and provisioner is healthy
kubectl get storageclass
kubectl describe storageclass <sc-name>

# If using static provisioning, create a matching PV
# If using dynamic provisioning, add storage capacity or clean up unused PVCs
kubectl get pvc -n <namespace> --sort-by=.metadata.creationTimestamp

# After storage is ready, reset partition to 0
kubectl patch statefulset <name> -n <namespace> \
  -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":0}}}}'
```

**Verification:** `kubectl get pvc -n <namespace>` shows all PVCs in `Bound` state. Rollout resumes and completes.

---

### Cause C: PersistentVolume stuck in Released state

**Statement:** A PersistentVolume from a previous pod remains in `Released` phase because its reclaim policy is `Retain`, preventing the new pod from binding to it.

**Mechanism:** When a StatefulSet pod is deleted during a rolling update, the associated PV is released from the old pod's claim. With a `Retain` reclaim policy, the PV transitions to `Released` rather than `Available`, and its `claimRef` still points to the old claim object. The new pod's PVC cannot bind to a `Released` PV, leaving the pod `Pending`.

**Indicator:**

- [Step 7] `kubectl get pv <pv-name>` shows phase `Released`
- [Step 7] `kubectl get volumeattachments` shows a stale attachment for a deleted pod

<!-- match: {"step": 7, "predicate": "contains", "target": "Released"} -->

**Mitigation:**

- **Risk:** Low. Removing `claimRef` makes the PV available; it will be immediately bound by the pending PVC.
- **Command:**

  ```bash
  kubectl patch pv <pv-name> --type='json' \
    -p='[{"op":"remove","path":"/spec/claimRef"}]'
  ```

- **Duration:** Immediate; the pending PVC binds within seconds.

**Resolution:** Same as Mitigation.

**Verification:** `kubectl get pv <pv-name> -o jsonpath='{.status.phase}'` returns `Bound`. The stuck pod starts and becomes Ready.

---

### Cause D: Init container failure (database migration or dependency check)

**Statement:** An init container performing database migration, schema validation, or peer-dependency checking fails on the new pod, preventing the main container from starting.

**Mechanism:** StatefulSet pods often include init containers that must complete successfully before the main container starts. If a new image version changes the migration logic, requires a schema that does not yet exist, or checks for a peer pod that is not yet updated, the init container exits with a non-zero code. The pod enters `Init:CrashLoopBackOff` or `Init:Error` and is never Ready, halting the rollout.

**Indicator:**

- [Step 4] Events show `Init:CrashLoopBackOff` or `Init:Error` in pod status
- [Step 5] Init container logs show migration failure, connection refused to peer, or schema mismatch

<!-- match: {"step": 4, "predicate": "contains", "target": "Init:CrashLoopBackOff"} -->
<!-- match: {"step": 4, "predicate": "contains", "target": "Init:Error"} -->

**Mitigation:**

- **Risk:** Medium. Rolling back restores previous init container image; verify the old init container is compatible with current data state before rollback.
- **Command:**

  ```bash
  kubectl rollout undo statefulset/<name> -n <namespace>
  ```

- **Duration:** 5–10 minutes.

**Resolution:**

```bash
# Fix the init container image or command, then re-deploy
kubectl patch statefulset <name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/initContainers/0/image","value":"<fixed-init-image>"}]'
kubectl rollout status statefulset/<name> -n <namespace> --timeout=600s
```

**Verification:** `kubectl describe pod <new-pod>` shows all init containers in `Completed` state. Main container starts and pod becomes Ready.

---

### Cause E: Partition set too high — intentional or accidental canary freeze

**Statement:** The `spec.updateStrategy.rollingUpdate.partition` is set to a value greater than 0, intentionally or accidentally blocking pods with lower ordinals from receiving the update.

**Mechanism:** The partition field is a canary mechanism: pods with ordinal >= partition are updated; pods below partition retain the old spec. If partition was set for a staged rollout and never lowered to 0, or was set accidentally, the rollout appears stuck because the controller will not update lower-ordinal pods. `kubectl rollout status` waits forever because `updatedReplicas < replicas`.

**Indicator:**

- [Step 2] `kubectl get statefulset ... -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'` returns a value > 0
- [Step 1] `updatedReplicas` equals `replicas - partition`, not `replicas`

<!-- match: {"step": 2, "predicate": "threshold", "target": "partition", "op": ">", "value": 0} -->

**Mitigation:**

- **Risk:** Low. Lowering partition triggers the update of remaining pods in ordinal order.
- **Command:**

  ```bash
  kubectl patch statefulset <name> -n <namespace> \
    -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":0}}}}'
  ```

- **Duration:** Update completes after remaining pods cycle through; typically 5–20 minutes.

**Resolution:** Same as Mitigation.

**Verification:** `kubectl get statefulset <name> -n <namespace>` shows `updatedReplicas == replicas`. `currentRevision == updateRevision`.

---

### Cause F: Pod stuck in Terminating due to node failure or finalizer

**Statement:** The previous pod revision is stuck in `Terminating` state because its node is unreachable or a finalizer is blocking deletion, preventing the StatefulSet controller from creating the updated replacement pod.

**Mechanism:** StatefulSets maintain "at-most-one" semantics: the controller will not create the new pod until the old pod with the same identity is fully deleted. If the node hosting the old pod goes offline, kubelet cannot send the deletion acknowledgment, and the pod stays `Terminating` indefinitely. Similarly, a custom finalizer on the pod (e.g., from a storage driver or admission webhook) can block deletion until the finalizer is removed.

**Indicator:**

- [Step 3] Stuck pod shows phase `Terminating` for more than 10 minutes
- [Step 4] `kubectl describe pod` shows `DeletionTimestamp` is set but pod is not gone; node may show `NotReady`

<!-- match: {"step": 3, "predicate": "contains", "target": "Terminating"} -->

**Mitigation:**

- **Risk:** High. Force deletion bypasses graceful shutdown. For databases, this risks data corruption if in-flight writes were not flushed. Confirm the node is truly unreachable before force-deleting.
- **Command:**

  ```bash
  # Option 1: Remove blocking finalizer (safer)
  kubectl patch pod <stuck-pod> -n <namespace> \
    -p '{"metadata":{"finalizers":null}}'

  # Option 2: Force delete (only after confirming node is dead)
  kubectl delete pod <stuck-pod> -n <namespace> --grace-period=0 --force
  ```

- **Duration:** Immediate; controller creates replacement pod within seconds.

**Resolution:**

```bash
# If caused by a dead node, delete the Node object to unblock
kubectl delete node <node-name>
# Then verify the StatefulSet resumes the rollout
kubectl rollout status statefulset/<name> -n <namespace> --timeout=600s
```

**Rollback:** Not applicable; the pod identity will be recreated by the controller.

**Verification:** `kubectl get pods -n <namespace>` shows the replacement pod created and progressing toward `Running`. No duplicate pod with the same name exists after ~60 seconds.

---

### Cause G: Headless Service missing or misconfigured

**Statement:** The StatefulSet's required headless Service does not exist or has `clusterIP` set to a real IP rather than `None`, breaking DNS identity for pods that rely on peer discovery during startup.

**Mechanism:** Each StatefulSet pod gets a stable DNS entry `<pod>.<svc>.<ns>.svc.cluster.local` only when a headless Service (clusterIP: None) with a matching selector exists. If the Service is absent, has a real ClusterIP, or has a selector mismatch, pods cannot resolve peers. Applications that validate peer connectivity during startup (e.g., Elasticsearch, Cassandra, ZooKeeper) fail readiness probes or crash, blocking the rollout.

**Indicator:**

- [Step 8] `kubectl get svc <svc-name>` returns `Not Found`, or `clusterIP` is not `None`
- [Step 8] `nslookup <pod-name>.<svc>.<ns>.svc.cluster.local` fails with `NXDOMAIN` or returns no address

<!-- match: {"step": 8, "predicate": "absent", "target": "clusterIP: None"} -->

**Mitigation:**

- **Risk:** Low. Creating or patching the headless Service has no effect on running pods' data.
- **Command:**

  ```bash
  kubectl apply -f - <<EOF
  apiVersion: v1
  kind: Service
  metadata:
    name: <headless-svc-name>
    namespace: <namespace>
  spec:
    clusterIP: None
    selector:
      <statefulset-label-key>: <statefulset-label-value>
    ports:
      - port: <app-port>
  EOF
  ```

- **Duration:** DNS propagation takes 5–30 seconds after Service is created.

**Resolution:** Same as Mitigation. Verify the StatefulSet `spec.serviceName` matches the Service name exactly.

**Verification:** `kubectl run dns-test --rm -it --restart=Never --image=busybox:1.36 -- nslookup <pod-name>.<svc>.<ns>.svc.cluster.local` returns the pod's IP. Stuck pod restarts and passes readiness probe.

---

### Cause Z: Unidentified

**Statement:** The StatefulSet rollout is stuck for a reason not matched by the diagnostic steps above.

**Mechanism:** StatefulSet update failures can arise from admission webhook rejections, custom controllers conflicting with the built-in StatefulSet controller, resource quota exhaustion, or image registry connectivity issues not surfaced by the steps above.

**Indicator:**

- [Default] None of the specific cause indicators match after completing all diagnostic steps.

**Mitigation:**

- **Risk:** Low. Setting partition to the stuck ordinal freezes the rollout without affecting running pods.
- **Command:**

  ```bash
  kubectl patch statefulset <name> -n <namespace> \
    -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":<stuck-pod-ordinal>}}}}'
  # Collect full diagnostics for escalation
  kubectl describe statefulset <name> -n <namespace> > statefulset-describe.txt
  kubectl get events -n <namespace> --sort-by=.metadata.creationTimestamp >> statefulset-describe.txt
  ```

- **Duration:** Until root cause is identified and resolved.

**Resolution:** Out of runbook scope. Escalate with the collected diagnostic bundle.

**Verification:** After escalation fix is applied, `kubectl rollout status statefulset/<name> -n <namespace>` completes successfully and `updatedReplicas == replicas`.

## Prevention

Configure readiness and startup probes with sufficient timeouts for the application's actual startup time:

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

Use partition for canary deployments before rolling out to all replicas:

```bash
# Update only the highest-ordinal pod first
REPLICAS=$(kubectl get statefulset <name> -n <namespace> -o jsonpath='{.spec.replicas}')
kubectl patch statefulset <name> -n <namespace> \
  -p "{\"spec\":{\"updateStrategy\":{\"rollingUpdate\":{\"partition\":$((REPLICAS-1))}}}}"
# After canary validates, complete the rollout
kubectl patch statefulset <name> -n <namespace> \
  -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":0}}}}'
```

Set up a Prometheus alert for stalled rollouts:

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
    summary: "StatefulSet {{ $labels.namespace }}/{{ $labels.statefulset }} update stalled"
```

Verify StorageClass provisioner health and available capacity before initiating an update:

```bash
kubectl get storageclass
kubectl get pv --sort-by=.spec.capacity.storage | grep Available
```

Maintain sufficient revision history to enable rapid rollback:

```yaml
spec:
  revisionHistoryLimit: 10
```

## Sources

- [Kubernetes: StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) — Rolling update mechanics, partition semantics, pod management policies, and known broken-state recovery. Priority 1.
- [Kubernetes: Debug a StatefulSet](https://kubernetes.io/docs/tasks/debug/debug-application/debug-statefulset/) — Official debugging workflow for pods in Unknown and Terminating states. Priority 1.
- [Kubernetes: Force Delete StatefulSet Pods](https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/) — At-most-one semantics, force deletion risks, finalizer removal, node deletion approach. Priority 1.
- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) — General pod debugging commands applicable to StatefulSet pods. Priority 1.
