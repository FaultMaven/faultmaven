---
id: "k8s-evicted-pods"
title: "Kubernetes Pod Eviction Due to Node Resource Pressure"
domain: compute
service: kubernetes
symptom_class: [oom, disk_full]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [eviction, resource-pressure, disk, memory, ephemeral-storage, qos, priority-class]
difficulty: intermediate
---

## Symptom Recognition

- Pods show `STATUS: Evicted` with `READY: 0/1` when running `kubectl get pods`.
- `kubectl describe pod <name>` shows `Status: Failed`, `Reason: Evicted`, and a `Message` field naming the triggering resource: `The node was low on resource: memory. Threshold quantity: 100Mi, available: 56Mi.`
- Node conditions report `True` for `MemoryPressure`, `DiskPressure`, or `PIDPressure` in `kubectl describe node`.
- Kubelet logs contain lines such as `eviction_manager: attempting to reclaim memory` or `Evicting pod <name>`.
- Prometheus alert `kube_pod_status_reason{reason="Evicted"} > 0` fires.
- Multiple pods from the same Deployment or StatefulSet fail simultaneously and reschedule on other nodes or remain Pending.

## Applicability

Applies to Kubernetes 1.24+ on any distribution (self-managed, EKS, GKE, AKS). Requires `kubectl` access with `get` and `describe` permissions on pods and nodes. Step 4 requires SSH access to the node or `kubectl debug node`. Step 3 requires `metrics-server` to be installed for `kubectl top`. Step 6 requires SSH or `kubectl debug node` to read kubelet config.

## Diagnostic Steps

### Step 1: Identify evicted pods and read the eviction message

```bash
kubectl get pods --all-namespaces --field-selector=status.phase=Failed | grep Evicted
kubectl describe pod <evicted-pod-name> -n <namespace>
```

Expected output: The `Message:` line names the triggering resource signal — `memory`, `ephemeral-storage`, `nodefs`, or `imagefs` — and states the threshold and actual available quantity.

### Step 2: Check node pressure conditions

```bash
kubectl get pod <evicted-pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}'
kubectl describe node <node-name> | grep -A 15 "Conditions:"
```

Expected output: `MemoryPressure`, `DiskPressure`, and `PIDPressure` entries each showing `True` or `False` with the last transition time.

### Step 3: Check node resource allocation and actual usage

```bash
kubectl describe node <node-name> | grep -A 20 "Allocated resources:"
kubectl top node <node-name>
kubectl top pods --all-namespaces --sort-by=memory | head -20
```

Expected output: Allocation percentages for CPU and memory versus capacity, and per-pod memory usage ranked highest first.

### Step 4: Check disk usage on the node

```bash
df -h
df -hi
du -sh /var/log/pods/* 2>/dev/null | sort -rh | head -10
du -sh /var/lib/containerd/* 2>/dev/null | sort -rh | head -10
du -sh /var/lib/kubelet/pods/* 2>/dev/null | sort -rh | head -10
```

Expected output: Filesystem usage percentages per mount point and the top directories by size. Any filesystem above 90% is a candidate for disk-pressure eviction.

### Step 5: Check pod QoS class and ephemeral storage limits

```bash
kubectl get pod <evicted-pod-name> -n <namespace> -o jsonpath='{.status.qosClass}'
kubectl get pod <evicted-pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].resources}' | jq .
kubectl get pod <evicted-pod-name> -n <namespace> \
  -o jsonpath='{.spec.containers[*].resources.limits.ephemeral-storage}'
```

Expected output: QoS class of `BestEffort`, `Burstable`, or `Guaranteed`, plus memory/CPU/ephemeral-storage request and limit values or empty strings if unset.

### Step 6: Check kubelet eviction thresholds and eviction log

```bash
sudo cat /var/lib/kubelet/config.yaml | grep -A 10 eviction
sudo journalctl -u kubelet --since "1 hour ago" --no-pager | grep -i evict | tail -40
```

Expected output: `evictionHard` and `evictionSoft` keys with their threshold values, plus log lines listing pod names and the resource signal that triggered each eviction.

### Step 7: Check pod priority class

```bash
kubectl get priorityclass
kubectl get pod <evicted-pod-name> -n <namespace> \
  -o jsonpath='{.spec.priorityClassName}'
```

Expected output: A list of PriorityClass objects with their numeric values, and the priority class name assigned to the evicted pod (empty string if none).

## Causes

### Cause A: Node memory exhausted by Burstable or BestEffort pods

**Statement:** Pods without a memory limit or with a limit far above their request consume node memory until the kubelet's `memory.available` hard threshold is crossed, triggering eviction starting with BestEffort then Burstable pods.

**Mechanism:** The kubelet computes `memory.available` as node capacity minus working set. When this falls below the threshold (default 100Mi), it evicts pods by QoS class: BestEffort first, then Burstable pods exceeding their requests, then Guaranteed. Pods without memory requests run as BestEffort and are always first in line regardless of how little memory they actually use.

**Indicator:**

- [Step 1] `Message` contains `low on resource: memory`
- [Step 2] `MemoryPressure` condition is `True`
- [Step 5] `qosClass` is `BestEffort` or `Burstable` and memory limit is absent or much larger than request

<!-- match: {"step": 1, "predicate": "contains", "target": "low on resource: memory"} -->
<!-- match: {"step": 2, "predicate": "contains", "target": "MemoryPressure  True"} -->

**Mitigation:**

- **Risk:** Setting limits too low can cause OOMKill restarts; setting requests too high can block scheduling on already-pressured nodes.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"256Mi"},
         {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"512Mi"}]'
  ```

- **Duration:** Takes effect on next pod restart; nodes may continue evicting until memory headroom improves.

**Resolution:**

```bash
# Right-size memory requests and limits for all containers in the affected deployment
# Use observed peak usage from Step 3 as the baseline for requests
kubectl set resources deployment <deployment-name> -n <namespace> \
  --requests=memory=256Mi --limits=memory=512Mi
# Verify QoS class after rollout
kubectl get pods -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.qosClass}{"\n"}{end}'
```

- **Impact:** Cluster-wide improvement in scheduler bin-packing; requires rolling restart of the deployment.
- **Rollback:** Revert the resource patch by setting values back to previous or removing limits.

**Verification:** Watch `kubectl top nodes` for 15 minutes after rollout. `MemoryPressure` condition must return to `False` on affected nodes. No new `Evicted` pods appear in `kubectl get pods --all-namespaces --field-selector=status.phase=Failed`.

---

### Cause B: Pod exceeds its ephemeral-storage limit

**Statement:** A container writing to its filesystem, emptyDir volumes, or container logs exceeds the `ephemeral-storage` limit set in its resource spec, causing the kubelet to immediately evict the pod.

**Mechanism:** The kubelet tracks each pod's ephemeral storage consumption including writable container layers, emptyDir volumes, and log files. When a container's usage exceeds its declared `limits.ephemeral-storage`, the kubelet evicts the entire pod with a message referencing `ephemeral-storage`. Unlike memory OOMKill which kills a single container, ephemeral storage violation removes all containers in the pod.

**Indicator:**

- [Step 1] `Message` contains `ephemeral-storage`
- [Step 5] `limits.ephemeral-storage` is set and actual disk usage in Step 4 shows the pod's volume directory is near that size
- [Step 2] `DiskPressure` may be `False` (this is a per-pod limit, not a node-level signal)

<!-- match: {"step": 1, "predicate": "contains", "target": "ephemeral-storage"} -->

**Mitigation:**

- **Risk:** Increasing the ephemeral-storage limit may allow the pod to consume more disk, worsening node-level disk pressure if many pods do this simultaneously.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/ephemeral-storage","value":"4Gi"}]'
  ```

- **Duration:** Takes effect on next pod restart; immediate relief for the evicted pod.

**Resolution:**

```bash
# Identify what is consuming ephemeral storage — add to the running pod before restart
kubectl exec -n <namespace> <running-pod-name> -- du -sh /tmp /var/log /data 2>/dev/null | sort -rh

# For emptyDir volumes that grow unbounded, add sizeLimit via a Deployment patch:
# Edit spec.template.spec.volumes[].emptyDir.sizeLimit in the deployment manifest
kubectl edit deployment <deployment-name> -n <namespace>
```

- **Impact:** Single deployment; requires rolling restart.
- **Rollback:** Revert the `sizeLimit` or `limits.ephemeral-storage` value if the application cannot tolerate the cap.

**Verification:** After rollout, `kubectl describe pod <new-pod-name> -n <namespace>` should show no eviction. Monitor with `kubectl exec` to confirm the pod's ephemeral usage stays below the new limit over a 30-minute window.

---

### Cause C: Node filesystem full (DiskPressure from container logs or images)

**Statement:** Accumulated container logs, dangling image layers, or exited container writable layers fill the node filesystem beyond the kubelet's `nodefs.available` hard eviction threshold (default 5%), triggering DiskPressure and mass pod eviction.

**Mechanism:** The kubelet monitors `nodefs.available` (the filesystem hosting pod volumes, logs, and kubelet data) and `imagefs.available` (the container runtime's image store). When either drops below the hard threshold, the kubelet first attempts to garbage-collect unused images and stopped containers. If that does not reclaim enough space, it evicts pods in QoS order. Container logs that are not size-limited grow continuously and are one of the most common causes of unexpected DiskPressure.

**Indicator:**

- [Step 2] `DiskPressure` condition is `True`
- [Step 4] Filesystem at `/var/lib/containerd` or `/var/log/pods` is above 90% full
- [Step 1] `Message` contains `nodefs` or `imagefs`

<!-- match: {"step": 2, "predicate": "contains", "target": "DiskPressure  True"} -->
<!-- match: {"step": 4, "predicate": "threshold", "target": "disk_pct", "op": ">", "value": 0.90} -->

**Mitigation:**

- **Risk:** Deleting logs removes diagnostic evidence; pruning images is safe but cannot be reversed for layers not in a registry.
- **Command:**

  ```bash
  # Prune unused container images
  sudo crictl rmi --prune

  # Remove exited containers
  sudo crictl rm $(sudo crictl ps -aq --state exited) 2>/dev/null || true

  # Vacuum systemd journal
  sudo journalctl --vacuum-size=500M

  # Remove container log files older than 7 days
  sudo find /var/log/containers -name "*.log" -mtime +7 -delete
  ```

- **Duration:** 2–10 minutes; DiskPressure condition clears once available space rises above threshold.

**Resolution:**

```yaml
# Set kubelet log rotation limits in /var/lib/kubelet/config.yaml
# containerLogMaxSize: "50Mi"
# containerLogMaxFiles: 3
# Then restart kubelet: sudo systemctl restart kubelet
```

```bash
# Verify log rotation took effect
sudo cat /var/lib/kubelet/config.yaml | grep -i log
sudo journalctl -u kubelet --since "5 min ago" | grep -i "log"
```

- **Impact:** Node-wide; kubelet restart causes a brief disruption to pod status reporting but does not terminate running pods.
- **Rollback:** Increase `containerLogMaxSize` or `containerLogMaxFiles` if applications depend on longer log retention.

**Verification:** `df -h` shows filesystem below 80%. `kubectl describe node <node-name> | grep DiskPressure` shows `False`. No new eviction events for 10 minutes: `kubectl get events --all-namespaces --field-selector reason=Evicted`.

---

### Cause D: Inode exhaustion on node filesystem

**Statement:** A workload that creates a very large number of small files exhausts the filesystem's inode table, triggering `nodefs.inodesFree` eviction even though disk space in bytes appears available.

**Mechanism:** Each filesystem has a fixed number of inodes allocated at format time. When all inodes are consumed, no new files can be created even if bytes remain free. `df -h` shows available space but `df -ih` shows 0 inodes free. The kubelet monitors `nodefs.inodesFree` (default threshold: 4%) and triggers DiskPressure and eviction when the inode pool is exhausted.

**Indicator:**

- [Step 4] `df -hi` shows inode usage at or near 100% while `df -h` shows available space
- [Step 2] `DiskPressure` condition is `True`
- [Step 1] `Message` contains `inodes`

<!-- match: {"step": 1, "predicate": "contains", "target": "inodes"} -->

**Mitigation:**

- **Risk:** Deleting files from /tmp or application cache directories may remove data the application needs; confirm the source before bulk deleting.
- **Command:**

  ```bash
  # Find the directory with the most files
  find /var/lib/kubelet/pods -xdev -printf '%h\n' 2>/dev/null | sort | uniq -c | sort -rn | head -10
  find /tmp -xdev -printf '%h\n' 2>/dev/null | sort | uniq -c | sort -rn | head -10

  # Delete small temporary files to recover inodes
  sudo find /tmp -type f -atime +1 -delete
  ```

- **Duration:** Immediate once files are removed; DiskPressure clears within one kubelet monitoring interval (~10 seconds).

**Resolution:**

```bash
# Identify the pod or process producing the files and fix the root cause
# Common culprits: logging frameworks writing per-request files, unmanaged temp dirs
# After cleanup, verify with:
df -ih
kubectl describe node <node-name> | grep DiskPressure
```

**Verification:** `df -ih` shows inode usage below 80%. `kubectl describe node <node-name>` shows `DiskPressure: False`. No new eviction events in 5 minutes.

---

### Cause E: PID exhaustion from process-leaking workload

**Statement:** A container that spawns child processes without reaping them exhausts the node's PID pool, triggering `pid.available` eviction and preventing any new processes from starting on the node.

**Mechanism:** Linux nodes have a kernel-level PID limit (`/proc/sys/kernel/pid_max`). The kubelet monitors `pid.available` (available PIDs) and triggers PIDPressure when it falls below the threshold (default 4%). Once PIDs are exhausted, all fork/exec calls fail with EAGAIN, affecting the entire node — not just the offending pod. The kubelet evicts pods by QoS class to release PIDs held by their processes.

**Indicator:**

- [Step 2] `PIDPressure` condition is `True`
- [Step 1] `Message` contains `pid`
- [Symptom] System-wide process creation failures or fork errors in application logs

<!-- match: {"step": 2, "predicate": "contains", "target": "PIDPressure  True"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "pid"} -->

**Mitigation:**

- **Risk:** Increasing PID limits is a stopgap; the leaking workload will exhaust the higher limit without a fix.
- **Command:**

  ```bash
  # On the node, find the process with the most children
  ps -eo user,pid,ppid,nlwp,cmd --sort=-nlwp | head -20

  # Temporary increase of node PID limit
  echo 131072 | sudo tee /proc/sys/kernel/pid_max

  # Evict the offending pod to release its PIDs
  kubectl delete pod <offending-pod-name> -n <namespace>
  ```

- **Duration:** Temporary increase lasts until next reboot; fix the application and revert.

**Resolution:**

```bash
# Fix the process-leaking application (ensure it reaps child processes)
# Then set pod-level PID limits via LimitRange or pod security context:
kubectl apply -f - <<EOF
apiVersion: v1
kind: LimitRange
metadata:
  name: pid-limit
  namespace: <namespace>
spec:
  limits:
  - type: Container
    default:
      pids: 1000
    defaultRequest:
      pids: 500
EOF
```

**Verification:** `cat /proc/sys/kernel/pid_max` shows a safe headroom. `kubectl describe node <node-name> | grep PIDPressure` shows `False`. The offending pod's replacement does not re-create the leak (monitor with `ps -eo nlwp | awk '{sum += $1} END {print sum}'` on the node).

---

### Cause F: Low-priority pod evicted due to PriorityClass assignment

**Statement:** A pod with a low or absent PriorityClass is preferentially evicted before higher-priority pods when the node is under any resource pressure, even if its resource usage is modest.

**Mechanism:** When the kubelet selects candidates for eviction within a QoS tier, pods with lower `priority` values (from PriorityClass) are selected before higher-priority ones. A pod without a PriorityClass gets priority 0 by default, making it the first eviction candidate among peers. This means a low-priority but memory-efficient pod is evicted ahead of a high-priority but memory-hungry pod at the same QoS level.

**Indicator:**

- [Step 7] Evicted pod has no PriorityClass or a low numeric value (below 1000)
- [Step 7] Other pods on the same node have higher PriorityClass values
- [Step 5] `qosClass` is `Burstable` or `Guaranteed` but the pod was still evicted

<!-- match: {"step": 7, "predicate": "absent", "target": "spec.priorityClassName"} -->

**Mitigation:**

- **Risk:** Assigning high priority to all pods defeats the purpose of priority classes; set priorities intentionally.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"add","path":"/spec/template/spec/priorityClassName","value":"high-priority"}]'
  ```

- **Duration:** Takes effect on next pod restart.

**Resolution:**

```yaml
# Create a PriorityClass hierarchy and assign appropriately
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: service-critical
value: 1000
globalDefault: false
description: "Production-facing services"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-low
value: 100
globalDefault: false
description: "Background batch jobs — safe to evict"
```

```bash
kubectl apply -f priority-classes.yaml
```

**Verification:** `kubectl get pod <new-pod-name> -n <namespace> -o jsonpath='{.spec.priorityClassName}'` returns the expected class. The pod survives node pressure events that evict lower-priority batch pods.

---

### Cause Z: Unidentified eviction cause

**Statement:** The eviction trigger cannot be conclusively identified from available diagnostic data.

**Mechanism:** Eviction messages may be missing if the pod object was deleted before inspection, or the triggering signal may have resolved by the time diagnostics run. Transient pressure spikes below threshold dwell time may not leave a permanent node condition.

**Indicator:**

- [Default] None of the above causes match the observed eviction message or node conditions

**Mitigation:**

- **Risk:** Escalating without diagnosis may lead to unnecessary node replacements.
- **Command:**

  ```bash
  # Collect kubelet logs from the time of eviction for escalation
  sudo journalctl -u kubelet --since "2 hours ago" --no-pager > /tmp/kubelet-$(hostname).log
  kubectl get events --all-namespaces --sort-by='.lastTimestamp' | grep -i evict | tail -30
  kubectl describe node <node-name> > /tmp/node-describe-$(hostname).txt
  ```

- **Duration:** Diagnostic collection only; no runtime changes.

**Resolution:** Out of runbook scope. Escalate with kubelet logs and node describe output to the infrastructure team for deeper analysis.

**Verification:** Escalation ticket created with log artifacts attached. Monitor the node for 30 minutes for recurrence of eviction events.

## Prevention

Set memory and CPU requests and limits on every container. Use `Guaranteed` QoS (requests equal limits) for production services to minimize eviction risk. Enforce defaults cluster-wide with a `LimitRange`:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: <namespace>
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "100m"
      memory: "128Mi"
    default:
      cpu: "500m"
      memory: "512Mi"
```

Set `ephemeral-storage` requests and limits for pods writing temporary data:

```yaml
resources:
  requests:
    ephemeral-storage: "500Mi"
  limits:
    ephemeral-storage: "2Gi"
```

Reserve system resources in kubelet configuration so workloads cannot consume all node capacity:

```yaml
systemReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
kubeReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
```

Configure kubelet log rotation to prevent log accumulation from triggering DiskPressure:

```yaml
containerLogMaxSize: "50Mi"
containerLogMaxFiles: 3
```

Define a PriorityClass hierarchy so low-value batch workloads are evicted before production services. Enable Cluster Autoscaler to add nodes before resource pressure reaches eviction thresholds.

Monitor with Prometheus alerts:

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

- alert: NodeDiskPressure
  expr: kube_node_status_condition{condition="DiskPressure",status="true"} == 1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Node {{ $labels.node }} has disk pressure"
```

Schedule periodic cleanup of evicted pod objects to keep the API server free of stale `Failed` pod records:

```bash
kubectl get pods --all-namespaces --field-selector=status.phase=Failed -o json | \
  jq -r '.items[] | select(.status.reason=="Evicted") | "\(.metadata.namespace) \(.metadata.name)"' | \
  while read ns name; do kubectl delete pod "$name" -n "$ns"; done
```

## Sources

- [Kubernetes: Node-pressure Eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/) — Priority 1. Eviction signals, hard and soft thresholds, pod selection order, kubelet configuration, filesystem topology (nodefs/imagefs/containerfs), memory.available calculation, PID pressure.
- [Kubernetes: Pod Quality of Service Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/) — Priority 1. QoS class criteria (Guaranteed, Burstable, BestEffort), eviction order, cgroup v2 memory throttling, memory protection.
- [Kubernetes: Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) — Priority 1. Ephemeral-storage requests and limits, emptyDir sizeLimit, pod eviction on ephemeral-storage limit breach, memory-backed emptyDir volumes.
