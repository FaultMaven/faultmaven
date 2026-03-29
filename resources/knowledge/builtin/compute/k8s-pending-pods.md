---
id: k8s-pending-pods
title: "Kubernetes Pods Stuck in Pending State: Diagnosis and Resolution"
domain: compute
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - scheduling
  - pending
  - resources
  - affinity
  - taints
  - pvc
difficulty: intermediate
---

# Kubernetes Pods Stuck in Pending State: Diagnosis and Resolution

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution. Requires `kubectl` access with permissions to get, describe pods, nodes, PVCs, and resource quotas. The `metrics-server` add-on is needed for `kubectl top` commands. Control plane access is needed to check scheduler health.

A pod enters the `Pending` phase when it has been accepted by the Kubernetes API server but the scheduler cannot assign it to a node. The pod remains in this state indefinitely until the underlying constraint is resolved. The scheduler evaluates all nodes against the pod's requirements (resource requests, node affinity, tolerations, volume topology) and only places the pod on a node that satisfies all constraints.

Common root causes include insufficient cluster resources (no node has enough allocatable CPU or memory to satisfy the pod's resource requests), node affinity or nodeSelector mismatch (the pod specifies label constraints that no available node satisfies), taints without matching tolerations (nodes carry `NoSchedule` taints and the pod lacks corresponding tolerations), PersistentVolumeClaim binding failure (the PVC cannot bind due to capacity, StorageClass, or topology issues), ResourceQuota exhaustion (the namespace has hit its CPU, memory, or object-count quota), scheduler unavailability (the kube-scheduler pod itself is down or misconfigured), hostPort conflicts (the pod requests a hostPort already occupied on every eligible node), and pod scheduling gates preventing the scheduler from considering the pod.

Typical presentation:

```text
NAME                  READY   STATUS    RESTARTS   AGE
my-app-abc123-xyz    0/1     Pending   0          15m
```

## Diagnostic Steps

### Step 1: Identify Pending Pods

**What this checks:** Which pods are stuck in Pending across the cluster.

```bash
kubectl get pods --all-namespaces --field-selector=status.phase=Pending
```

**Expected output:** A list of pods in Pending state with their namespace, name, and age.

**What the finding means:** Pods that have been Pending for more than a few minutes have a scheduling constraint that is not being resolved automatically. The age indicates how long the pod has been waiting.

### Step 2: Inspect Pod Events

**What this checks:** The scheduler's reason for failing to place the pod, which directly identifies the constraint.

```bash
kubectl describe pod <pod-name> -n <namespace>
```

**Expected output:** The `Events` section contains messages from the scheduler explaining why scheduling failed.

**What the finding means:** The scheduler message categorizes the failure:

| Event Message | Likely Cause |
| ------------- | ------------ |
| `0/N nodes are available: N Insufficient cpu` | CPU requests exceed allocatable capacity |
| `0/N nodes are available: N Insufficient memory` | Memory requests exceed allocatable capacity |
| `0/N nodes are available: N node(s) didn't match Pod's node affinity/selector` | Node affinity or nodeSelector mismatch |
| `0/N nodes are available: N node(s) had taints that the pod didn't tolerate` | Missing tolerations |
| `persistentvolumeclaim "<name>" not found` | PVC does not exist |
| `pod has unbound immediate PersistentVolumeClaims` | PVC stuck in Pending |
| `0/N nodes are available: N node(s) exceed max volume count` | Volume attachment limit reached |
| `exceeded quota` | ResourceQuota exhaustion |

### Step 3: Check Node Resources

**What this checks:** Available capacity on each node to determine if the cluster has room for the pod.

```bash
# Check node resource allocation vs capacity
kubectl describe nodes | grep -A 5 "Allocated resources"

# Check actual usage (requires metrics-server)
kubectl top nodes

# Detailed view of a specific node
kubectl describe node <node-name>
```

**Expected output:** Allocation percentages for CPU and memory on each node, and actual usage figures.

**What the finding means:** If all nodes show CPU or memory allocation near 100%, the cluster lacks capacity. The pod's resource requests must fit within the remaining allocatable space on at least one node.

### Step 4: Check Taints on All Nodes

**What this checks:** Which taints are applied to nodes that may prevent the pod from being scheduled.

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
```

**Expected output:** A list of nodes with their taints (key, value, effect).

**What the finding means:** If all nodes have `NoSchedule` taints and the pod lacks matching tolerations, the scheduler cannot place it. Common taints include `node-role.kubernetes.io/control-plane:NoSchedule` on control plane nodes and custom taints from maintenance operations.

### Step 5: Check Node Labels

**What this checks:** Whether any node matches the pod's nodeSelector or node affinity requirements.

```bash
# List all node labels
kubectl get nodes --show-labels

# Check if any node matches a specific label
kubectl get nodes -l <key>=<value>
```

**Expected output:** Nodes with their labels, and whether any node matches the required labels.

**What the finding means:** If the pod specifies a `nodeSelector` or `requiredDuringSchedulingIgnoredDuringExecution` affinity and no node has the matching labels, the pod cannot be scheduled. Either label a node or relax the affinity constraint.

### Step 6: Check PVC Status

**What this checks:** Whether PersistentVolumeClaims referenced by the pod are bound to PersistentVolumes.

```bash
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>
```

**Expected output:** PVC status (Bound, Pending, or Lost) and events explaining binding failures.

**What the finding means:** If the PVC is `Pending`, the scheduler cannot place the pod because the storage is not available. Common causes include no matching PV, StorageClass not found, provisioner failure, or zone topology mismatch.

### Step 7: Check ResourceQuota

**What this checks:** Whether the namespace has hit its resource quota limits.

```bash
kubectl describe resourcequota -n <namespace>
kubectl describe limitrange -n <namespace>
```

**Expected output:** Quota usage showing used vs. hard limits for CPU, memory, and pod count.

**What the finding means:** If `used` equals `hard` for any resource, the namespace is at its quota limit. New pods cannot be created until existing resources are freed or the quota is increased.

### Step 8: Check Scheduler Health

**What this checks:** Whether the kube-scheduler is running and processing scheduling decisions.

```bash
kubectl get pods -n kube-system -l component=kube-scheduler
kubectl logs -n kube-system -l component=kube-scheduler --tail=50
```

**Expected output:** The scheduler pod should be `Running` with no error logs.

**What the finding means:** If the scheduler pod is not running or shows errors, no pods can be scheduled cluster-wide. If the scheduler is healthy but no scheduling events appear for the pod, check for scheduling gates or webhook interference.

## Mitigation

### Option 1: Reduce Resource Requests

Use when pod resource requests exceed available capacity but actual usage would be lower.

- **Risk:** Low. The pod may experience throttling or OOM if actual usage exceeds the reduced request, but scheduling will succeed.
- **Command:**
  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/cpu", "value": "100m"},
         {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "128Mi"}]'
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
  The pod should transition from Pending to Running within seconds.
- **Duration:** Immediate.

### Option 2: Add Tolerations or Remove Taints

Use when the scheduler message indicates taints are blocking placement.

- **Risk:** Medium. Removing a taint may allow unwanted pods to schedule on that node. Adding a toleration to the pod spec is safer.
- **Command:**
  ```bash
  # Option A: Add toleration to the deployment
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op": "add", "path": "/spec/template/spec/tolerations", "value": [{"key": "<taint-key>", "operator": "Equal", "value": "<taint-value>", "effect": "NoSchedule"}]}]'

  # Option B: Remove the taint from the node
  kubectl taint nodes <node-name> <taint-key>=<taint-value>:NoSchedule-
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
- **Duration:** Immediate once the change is applied.

### Option 3: Free Resources by Deleting Low-Priority Pods

Use when the cluster is at capacity and non-critical pods can be evicted.

- **Risk:** Medium. Evicted pods may themselves become Pending. Choose pods that are non-critical or have low PriorityClass.
- **Command:**
  ```bash
  kubectl top pods --all-namespaces --sort-by=memory | head -20
  kubectl delete pod <pod-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> --field-selector=status.phase=Pending
  ```
- **Duration:** Immediate after pod deletion frees resources.

### Option 4: Uncordon a Drained Node

Use when a healthy node was cordoned for maintenance and is ready to accept workloads again.

- **Risk:** Low. Only risky if the node was cordoned intentionally for ongoing maintenance.
- **Command:**
  ```bash
  kubectl get nodes | grep SchedulingDisabled
  kubectl uncordon <node-name>
  ```
- **Verify:**
  ```bash
  kubectl get nodes
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
- **Duration:** Immediate. The scheduler considers the node within seconds.

## Root Cause Resolution

**If** `kubectl describe pod` shows `Insufficient cpu` or `Insufficient memory` **then** right-size resource requests based on actual usage, add nodes, or enable the Cluster Autoscaler:

```bash
# Check actual usage to right-size requests
kubectl top pods -n <namespace> --containers

# Enable or verify Cluster Autoscaler
kubectl get pods -n kube-system -l app=cluster-autoscaler
kubectl logs -n kube-system -l app=cluster-autoscaler --tail=20

# Clean up unused deployments consuming resources
kubectl get deployments --all-namespaces --sort-by=.spec.replicas
```

**If** `kubectl describe pod` shows `didn't match Pod's node affinity/selector` **then** label a node to match or relax the affinity:

```bash
# Check required labels from the pod spec
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.nodeSelector}'
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.affinity.nodeAffinity}'

# Label a node to match
kubectl label nodes <node-name> <key>=<value>
```

If the affinity rule is overly restrictive, switch from `required` to `preferred` affinity in the deployment spec to allow fallback scheduling.

**If** `kubectl describe pod` shows `had taints that the pod didn't tolerate` **then** add the required toleration to the pod spec or remove the taint from the node:

```bash
kubectl describe node <node-name> | grep -A 3 Taints
kubectl taint nodes <node-name> <key>:<effect>-
```

**If** `kubectl describe pod` shows `unbound immediate PersistentVolumeClaims` **then** check PVC status and the StorageClass:

```bash
kubectl describe pvc <pvc-name> -n <namespace>
kubectl get storageclass
```

If dynamic provisioning fails, check the provisioner logs. If volume topology prevents binding, set `volumeBindingMode: WaitForFirstConsumer` in the StorageClass so binding defers until scheduling.

**If** `kubectl describe pod` shows `exceeded quota` **then** review and adjust the ResourceQuota:

```bash
kubectl describe resourcequota -n <namespace>
# Increase quota if justified
kubectl edit resourcequota <quota-name> -n <namespace>
# Or clean up completed/failed pods consuming quota
kubectl delete pods -n <namespace> --field-selector=status.phase=Succeeded
kubectl delete pods -n <namespace> --field-selector=status.phase=Failed
```

**If** no scheduling events appear at all for the pending pod **then** verify the scheduler is running:

```bash
kubectl get pods -n kube-system -l component=kube-scheduler
kubectl logs -n kube-system -l component=kube-scheduler --tail=100
```

## Verification

After applying the fix, confirm the pod transitions out of Pending.

```bash
# 1. Watch the pod transition to Running
kubectl get pod <pod-name> -n <namespace> -w
```

```bash
# 2. Confirm the pod is scheduled to a node
kubectl get pod <pod-name> -n <namespace> -o wide
kubectl describe pod <pod-name> -n <namespace> | grep "Node:"
```

```bash
# 3. Verify no more Pending pods
kubectl get pods --all-namespaces --field-selector=status.phase=Pending
```

```bash
# 4. If part of a Deployment or StatefulSet, verify rollout
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

## Prevention

**Set realistic resource requests and limits.** Base requests on observed usage via `kubectl top` or Prometheus metrics, not guesswork. Over-requesting wastes capacity; under-requesting risks OOM kills.

**Enable Cluster Autoscaler.** Configure automatic node scaling to handle demand spikes without manual intervention:

```bash
kubectl get deployment cluster-autoscaler -n kube-system
```

**Use PodDisruptionBudgets.** Prevent too many pods from being evicted simultaneously during node maintenance:

```bash
kubectl get pdb --all-namespaces
```

**Monitor ResourceQuota usage.** Set alerts when namespace quota usage exceeds 80% to proactively adjust limits before pods fail to schedule.

**Use `WaitForFirstConsumer` volume binding mode.** Prevent PVC-node topology mismatches by deferring volume binding until a pod is scheduled:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fast-ssd
provisioner: kubernetes.io/aws-ebs
volumeBindingMode: WaitForFirstConsumer
```

**Audit taints and node labels regularly.** Ensure taints added during maintenance windows are removed once the node is returned to service:

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
```

**Use PriorityClasses.** Assign priority classes to workloads so that critical pods can preempt lower-priority ones when resources are scarce.

**Set up Pending pod alerts.** Create alerts for pods stuck in Pending state longer than a threshold:

```yaml
- alert: PodPendingTooLong
  expr: kube_pod_status_phase{phase="Pending"} > 0
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} has been Pending for more than 10 minutes"
```

## Sources

- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) -- Official guide for diagnosing pod scheduling failures
- [Kubernetes: Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) -- Pending phase and scheduling process
- [Kubernetes: Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) -- Resource requests, limits, and their effect on scheduling
- [Kubernetes: Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) -- How taints prevent scheduling
- [Kubernetes: Assigning Pods to Nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) -- Node affinity, nodeSelector, and scheduling constraints
- [Kubernetes: Pod Scheduling Readiness](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-scheduling-readiness/) -- Scheduling gates that can prevent pod placement
- [Kubernetes: Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) -- PVC binding lifecycle and dynamic provisioning
