---
id: k8s-evicted-pods
title: "Kubernetes Pod Eviction"
domain: compute
service: kubernetes
symptom_class:
  - oom
  - disk_full
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pods
  - eviction
  - resource-pressure
  - disk
  - memory
  - priority-class
difficulty: intermediate
---

# Kubernetes Pod Eviction

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution. Requires `kubectl` access with permissions to get, describe, and delete pods. Node-level diagnostics require SSH access or `kubectl debug node`. The `metrics-server` add-on is needed for `kubectl top` commands.

Pod eviction occurs when the kubelet proactively terminates pods to reclaim resources on a node under pressure. Evicted pods show a `Failed` phase with reason `Evicted` and a message indicating which resource threshold was exceeded. Unlike voluntary disruptions (drains, rolling updates), node-pressure eviction does not respect PodDisruptionBudgets or the pod's full `terminationGracePeriodSeconds`.

The kubelet continuously monitors node resource usage against configured eviction thresholds. When a resource signal crosses a threshold, the kubelet begins evicting pods to bring usage below the threshold. The following signals and default hard eviction thresholds apply:

| Signal | Default Hard Threshold | Description |
| ------ | ---------------------- | ----------- |
| `memory.available` | < 100Mi | Available memory on the node |
| `nodefs.available` | < 5% | Available space on the node root filesystem |
| `nodefs.inodesFree` | < 4% | Available inodes on the node root filesystem |
| `imagefs.available` | < 5% | Available space on the image filesystem |
| `pid.available` | < 4% | Available process IDs on the node |

Pods are selected for eviction by QoS class: BestEffort pods (no resource requests or limits) are evicted first, then Burstable pods exceeding their requests, then Guaranteed pods (requests equal limits). Within each QoS tier, pods consuming the most resources relative to their requests are evicted first. PriorityClass further influences order: lower-priority pods are evicted before higher-priority ones regardless of QoS.

Common root causes include memory pressure from workloads consuming more than nodes can provide, disk pressure from container logs or ephemeral storage filling the filesystem, inode exhaustion from many small files, pods exceeding their `ephemeral-storage` limit, PID exhaustion from leaked processes, pods without resource requests running as BestEffort, and insufficient overall cluster capacity.

Typical presentation:

```text
$ kubectl get pods -n <namespace>
NAME                  READY   STATUS    RESTARTS   AGE
my-app-abc123-xyz    0/1     Evicted   0          2h
my-app-abc123-def    0/1     Evicted   0          2h
my-app-abc123-ghi    1/1     Running   0          5m
```

Describing an evicted pod shows:

```text
Status:   Failed
Reason:   Evicted
Message:  The node was low on resource: memory. Threshold quantity: 100Mi,
          available: 56Mi.
```

## Diagnostic Steps

### Step 1: Identify Evicted Pods and Read the Eviction Message

**What this checks:** Which pods were evicted and which resource signal triggered the eviction.

```bash
# Find all evicted pods across the cluster
kubectl get pods --all-namespaces --field-selector status.phase=Failed | grep Evicted

# Get details on a specific evicted pod
kubectl describe pod <evicted-pod-name> -n <namespace>
```

**Expected output:** The `Message` field in the describe output states which resource triggered eviction (e.g., `memory`, `ephemeral-storage`, `nodefs`).

**What the finding means:** The message directly identifies the resource under pressure. Memory eviction means the node's available memory dropped below the threshold. Ephemeral-storage eviction means the pod itself exceeded its ephemeral storage limit. DiskPressure eviction means the node filesystem is nearly full.

### Step 2: Check Node Conditions

**What this checks:** Whether the node that hosted the evicted pod is currently reporting resource pressure conditions.

```bash
# Find which node the pod was running on
kubectl get pod <evicted-pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}'

# Check node conditions
kubectl describe node <node-name> | grep -A 10 "Conditions:"
```

**Expected output:** Condition flags showing `True` or `False` for each pressure type.

**What the finding means:** `MemoryPressure: True` means the node is actively low on memory. `DiskPressure: True` means the node filesystem is nearly full. `PIDPressure: True` means the node is running out of process IDs. If all conditions show `False`, the pressure was transient and has since resolved.

### Step 3: Check Node Resource Usage

**What this checks:** Current resource allocation versus capacity on the affected node, and actual usage.

```bash
# Check node resource allocation vs capacity
kubectl describe node <node-name> | grep -A 15 "Allocated resources:"

# Check actual usage (requires metrics-server)
kubectl top node <node-name>

# Check pod-level resource usage on the node
kubectl top pods --all-namespaces --sort-by=memory | head -20
```

**Expected output:** Allocation percentages for CPU and memory, and actual usage figures.

**What the finding means:** If allocated resources approach 100% of capacity, the node is overcommitted. If actual usage significantly exceeds requests, Burstable pods are consuming more than they reserved, leaving insufficient headroom.

### Step 4: Check Disk Usage on the Node

**What this checks:** Filesystem space and inode consumption on the node to identify what is consuming disk.

```bash
# SSH to the node or use kubectl debug node
df -h                    # Filesystem space
df -hi                   # Inode usage
du -sh /var/log/* | sort -rh | head -10        # Log directory sizes
du -sh /var/lib/containerd/* | sort -rh | head -10  # Container runtime storage
du -sh /var/lib/kubelet/pods/* | sort -rh | head -10  # Pod volumes
```

**Expected output:** Filesystem usage percentages and sizes of the largest directories.

**What the finding means:** If `/var/log` is large, container or system logs are unbounded. If `/var/lib/containerd` is large, unused container images or layers need cleanup. If specific pod volumes are large, those pods are writing excessive ephemeral data.

### Step 5: Check Pod Resource Configuration and QoS Class

**What this checks:** The QoS class and resource configuration of the evicted pod, which determines eviction priority.

```bash
# Check QoS class of the evicted pod
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.qosClass}'

# Check resource requests and limits
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].resources}' | jq .

# Check ephemeral storage limits
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].resources.limits.ephemeral-storage}'
```

**Expected output:** QoS class (BestEffort, Burstable, or Guaranteed) and resource specifications.

**What the finding means:** BestEffort pods (no requests or limits) are always evicted first. Burstable pods exceeding their requests are evicted next. Guaranteed pods are evicted last. If the pod has no resource requests, adding them will reduce eviction risk.

### Step 6: Check Kubelet Eviction Configuration

**What this checks:** The kubelet's configured eviction thresholds, which may differ from defaults.

```bash
# On the node, check kubelet configuration
sudo cat /var/lib/kubelet/config.yaml | grep -A 5 eviction

# Check kubelet logs for eviction events
sudo journalctl -u kubelet -n 200 --no-pager | grep -i evict
```

**Expected output:** Configured `evictionHard` and `evictionSoft` thresholds, and log entries showing which pods were evicted and why.

**What the finding means:** Custom thresholds may be more aggressive than defaults. Soft eviction thresholds include a grace period before eviction occurs. Hard thresholds trigger immediate eviction with 0s grace period.

### Step 7: Check Pod Priority Classes

**What this checks:** Whether the evicted pod has a low PriorityClass that made it a preferential eviction target.

```bash
# List priority classes in the cluster
kubectl get priorityclass

# Check which priority class the evicted pod uses
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.priorityClassName}'
```

**Expected output:** The PriorityClass name and its numeric value.

**What the finding means:** Lower-priority pods are evicted before higher-priority ones regardless of QoS class. If the pod has no PriorityClass or a low-value one, it is an early eviction candidate.

## Mitigation

### Option 1: Clean Up Evicted Pod Objects

Evicted pods remain in `Failed` state and consume API server resources. Clean them up first.

- **Risk:** None. Evicted pods are not running and cannot be restarted. This only removes stale pod objects.
- **Command:**
  ```bash
  kubectl get pods --all-namespaces --field-selector status.phase=Failed -o json | \
    jq -r '.items[] | select(.status.reason=="Evicted") | "\(.metadata.namespace) \(.metadata.name)"' | \
    while read ns name; do kubectl delete pod "$name" -n "$ns"; done
  ```
- **Verify:**
  ```bash
  kubectl get pods --all-namespaces --field-selector status.phase=Failed | grep Evicted
  ```
  No evicted pods should remain.
- **Duration:** Seconds.

### Option 2: Free Disk Space on the Node

Use when eviction is caused by DiskPressure.

- **Risk:** Low to Medium. Cleaning unused images is safe; truncating logs may lose diagnostic data.
- **Command:**
  ```bash
  # Clean unused container images
  sudo crictl rmi --prune

  # Clean stopped containers
  sudo crictl rm $(sudo crictl ps -a -q --state exited)

  # Truncate large journal logs
  sudo journalctl --vacuum-size=500M

  # Remove old container logs
  sudo find /var/log/containers -name "*.log" -mtime +7 -delete
  ```
- **Verify:**
  ```bash
  df -h
  kubectl describe node <node-name> | grep DiskPressure
  ```
  DiskPressure should return to `False`.
- **Duration:** 2 to 10 minutes.

### Option 3: Increase Node Capacity

Use when the cluster has insufficient resources for the current workload.

- **Risk:** Low. Adding nodes does not disrupt existing workloads. Cost increases proportionally.
- **Command:**
  ```bash
  # For managed Kubernetes (EKS, GKE, AKS), scale the node group
  # Example for EKS:
  eksctl scale nodegroup --cluster=<cluster> --name=<nodegroup> --nodes=<new-count>

  # Verify cluster autoscaler is running
  kubectl get pods -n kube-system -l app=cluster-autoscaler
  ```
- **Verify:**
  ```bash
  kubectl get nodes
  kubectl top nodes
  ```
  New nodes should join and workloads should redistribute.
- **Duration:** 5 to 15 minutes for new nodes to become Ready.

### Option 4: Cordon the Pressured Node

Use to prevent new pods from landing on a node that is already under resource pressure.

- **Risk:** Low. Existing pods continue running; only new scheduling is blocked.
- **Command:**
  ```bash
  kubectl cordon <node-name>
  ```
- **Verify:**
  ```bash
  kubectl get node <node-name>
  ```
  The node should show `SchedulingDisabled`.
- **Duration:** Immediate.

## Root Cause Resolution

**If** the eviction message indicates memory pressure **then** right-size pod memory requests and limits based on observed usage:

```bash
# Check actual memory usage across pods on the node
kubectl top pods --all-namespaces --sort-by=memory --no-headers | head -20

# Set appropriate memory requests and limits
kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"256Mi"},
       {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"512Mi"}]'
```

**If** a single pod is consuming excessive memory (potential leak) **then** identify and fix the application memory leak, or set stricter limits and accept periodic OOMKill restarts until the fix is deployed.

**If** the eviction message indicates disk pressure **then** address the root cause of disk consumption:

```bash
# Identify top disk consumers on the node
sudo du -sh /var/lib/containerd/io.containerd.snapshotter/* | sort -rh | head -10
sudo du -sh /var/log/pods/* | sort -rh | head -10
```

Configure container log rotation in the kubelet:

```yaml
# kubelet config
containerLogMaxSize: "50Mi"
containerLogMaxFiles: 3
```

**If** pods use `emptyDir` volumes that grow unbounded **then** set `sizeLimit` on the volume to prevent unchecked growth:

```yaml
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: "1Gi"
```

**If** a pod exceeds its `ephemeral-storage` limit **then** increase the limit or reduce the pod's disk usage:

```yaml
resources:
  requests:
    ephemeral-storage: "1Gi"
  limits:
    ephemeral-storage: "2Gi"
```

**If** eviction is caused by PID pressure **then** identify the process-leaking workload:

```bash
# On the node, find which process has the most children
ps -eo user,pid,ppid,nlwp,cmd --sort=-nlwp | head -20

# Check PID limits
cat /proc/sys/kernel/pid_max
```

Fix the application that is leaking processes. As a stopgap, increase the PID limit: `echo 65536 | sudo tee /proc/sys/kernel/pid_max`.

**If** evicted pods have BestEffort QoS (no resource requests) **then** add resource requests to elevate them to Burstable or Guaranteed QoS:

```yaml
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

**If** important pods are being evicted in favor of less important ones **then** assign PriorityClasses to protect critical workloads:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high-priority
value: 1000
globalDefault: false
description: "For business-critical applications"
---
# In the pod spec:
spec:
  priorityClassName: high-priority
```

## Verification

After resolving the resource pressure, confirm the cluster is healthy.

```bash
# 1. Confirm node conditions are healthy
kubectl describe node <node-name> | grep -A 5 "Conditions:"
# All pressure conditions should be False: MemoryPressure, DiskPressure, PIDPressure
```

```bash
# 2. Watch for new evictions over several minutes
kubectl get events --all-namespaces --field-selector reason=Evicted --watch
# No new eviction events should appear
```

```bash
# 3. Verify workloads are running
kubectl get pods -n <namespace> -l app=<app-label>
# All expected pods should be Running with no recent restarts
```

```bash
# 4. Check resource headroom
kubectl top nodes
kubectl describe node <node-name> | grep -A 15 "Allocated resources:"
# Verify adequate headroom between allocated and capacity
```

## Prevention

**Set resource requests and limits on all pods.** Every pod should have explicit CPU and memory requests and limits. Use `Guaranteed` QoS (requests equal limits) for critical workloads to minimize eviction risk. Enforce defaults with LimitRange objects.

**Configure ephemeral storage limits.** Set `ephemeral-storage` requests and limits for pods that write temporary data to prevent a single pod from filling the node filesystem:

```yaml
resources:
  requests:
    ephemeral-storage: "500Mi"
  limits:
    ephemeral-storage: "2Gi"
```

**Reserve system resources on nodes.** Configure kubelet to reserve resources for the OS and Kubernetes system components so that workloads cannot consume all node capacity:

```yaml
# kubelet config
systemReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
kubeReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
```

**Use PriorityClasses strategically.** Define a hierarchy so that low-priority batch jobs are evicted before high-priority services:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-low
value: 100
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: service-high
value: 1000
```

**Enable Cluster Autoscaler.** Configure automatic node scaling to add nodes when resource pressure increases, preventing evictions due to insufficient capacity.

**Monitor eviction events with alerts.** Set up Prometheus alerts for eviction events and node pressure conditions:

```yaml
- alert: PodEvicted
  expr: kube_pod_status_reason{reason="Evicted"} > 0
  for: 1m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} was evicted"

- alert: NodeMemoryPressure
  expr: kube_node_status_condition{condition="MemoryPressure",status="true"} == 1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Node {{ $labels.node }} has memory pressure"
```

**Automate evicted pod cleanup.** Schedule a CronJob to clean up evicted pod objects that accumulate over time:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cleanup-evicted-pods
spec:
  schedule: "0 */6 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: pod-cleanup
          containers:
          - name: cleanup
            image: bitnami/kubectl:latest
            command: ["sh", "-c"]
            args:
            - >
              kubectl get pods --all-namespaces --field-selector status.phase=Failed -o json |
              jq -r '.items[] | select(.status.reason=="Evicted") | "\(.metadata.namespace) \(.metadata.name)"' |
              while read ns name; do kubectl delete pod "$name" -n "$ns"; done
          restartPolicy: OnFailure
```

## Sources

- [Kubernetes: Node-pressure Eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/) -- Eviction signals, thresholds, pod selection order, soft and hard eviction configuration
- [Kubernetes: Pod Quality of Service Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/) -- QoS class determination and its effect on eviction priority
- [Kubernetes: Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) -- Resource requests, limits, and ephemeral storage
- [Kubernetes: Pod Priority and Preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/) -- PriorityClass and its effect on scheduling and eviction
- [Kubernetes: Node Status](https://kubernetes.io/docs/reference/node/node-status/) -- Node conditions including MemoryPressure, DiskPressure, and PIDPressure
