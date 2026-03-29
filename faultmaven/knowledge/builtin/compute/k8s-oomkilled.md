---
id: k8s-oomkilled
title: "Kubernetes OOMKilled: Diagnosis and Resolution"
domain: compute
service: kubernetes
symptom_class:
  - oom
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - oom
  - memory
  - container
  - resource-limits
difficulty: intermediate
---

# Kubernetes OOMKilled: Diagnosis and Resolution

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution. Requires `kubectl` access with permissions to get, describe, and log pods in the target namespace. The `metrics-server` add-on is needed for `kubectl top` commands. Prometheus with `container_memory_working_set_bytes` metric is recommended for historical memory analysis. JVM heap dump analysis requires `jmap` and a tool such as Eclipse MAT or VisualVM.

OOMKilled is a container termination reason indicating that the Linux kernel's OOM (Out of Memory) killer terminated the container process because it exceeded its cgroup memory limit. The container exits with code 137 (SIGKILL = 128 + 9). Unlike CPU limits which throttle, memory limits result in immediate process termination when exceeded. The kernel enforces memory limits via the cgroup memory controller, and the kubelet reports the termination reason as `OOMKilled`.

This occurs when the container's resident memory (working set) exceeds the value set in `resources.limits.memory`. The working set includes anonymous memory (heap, stack), memory-mapped files actively in use, and tmpfs/memory-backed volumes. It excludes inactive file-backed pages that the kernel can reclaim.

Common scenarios include application memory usage growing beyond the configured limit, memory leaks causing gradual consumption increase, traffic spikes allocating more memory for request handling, JVM/Python/Node.js runtime heap sized larger than the container memory limit, memory-backed `emptyDir` volumes (`medium: Memory`) consuming memory that counts toward the container limit, and sidecar containers (Istio/Envoy proxies, log agents) consuming memory not accounted for in limits.

If the pod has `restartPolicy: Always` (default), the kubelet restarts the container. If the root cause persists, the pod enters CrashLoopBackOff with exponential backoff delays, causing prolonged downtime.

## Diagnostic Steps

### Step 1: Confirm OOMKilled Status

**What this checks:** Whether the container was terminated specifically by the OOM killer, as opposed to other termination reasons.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.state}{"\t"}{.lastState}{"\n"}{end}'
```

**Expected output:** JSON showing `"reason":"OOMKilled"` and `"exitCode":137` in the current or last state.

**What the finding means:** The presence of `OOMKilled` confirms the container exceeded its memory limit. Exit code 137 is SIGKILL from the kernel. If the exit code is 137 but the reason is not `OOMKilled`, the container may have been force-deleted rather than OOM-killed.

### Step 2: Get Full Pod Description

**What this checks:** The complete pod state including configured limits, restart count, and events.

```bash
kubectl describe pod <pod-name> -n <namespace>
```

**Expected output:** The `State` / `Last State` section showing `Reason: OOMKilled`, the `Restart Count`, the `Limits` and `Requests` under each container, and the `Events` section.

**What the finding means:** A high restart count indicates the OOM is recurring. The configured memory limit shows the ceiling the container hit. Events may show additional context such as `OOMKilling` messages from the kubelet.

### Step 3: Check Current Memory Usage

**What this checks:** How much memory pods in the namespace are currently consuming relative to their limits.

```bash
kubectl top pods -n <namespace> --sort-by=memory
```

**Expected output:** Memory usage in Mi for each pod, sorted from highest to lowest.

**What the finding means:** If a running instance of the same workload is near its limit, the OOM is likely to recur. If usage is well below the limit, the OOM may have been caused by a transient spike or a different container in the pod.

### Step 4: Inspect Resource Specification

**What this checks:** The configured memory requests and limits for each container in the pod.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"\t requests.memory="}{.resources.requests.memory}{"\t limits.memory="}{.resources.limits.memory}{"\n"}{end}'
```

**Expected output:** Memory request and limit values for each container.

**What the finding means:** If no limits are set, the container can consume unbounded memory until node-level pressure triggers eviction. If limits are set very low relative to the application's needs, the OOM is expected. A large gap between request and limit indicates Burstable QoS, which is vulnerable to eviction under node pressure.

### Step 5: Check Node-Level Memory Pressure

**What this checks:** Whether the node itself is under memory pressure, which can trigger OOM kills even for pods within their limits.

```bash
kubectl describe node <node-name> | grep -A 5 "Conditions:"
kubectl top nodes
```

**Expected output:** Node condition flags and actual memory usage.

**What the finding means:** If `MemoryPressure: True`, the node is low on memory and the kubelet is evicting pods. In this case, BestEffort pods are killed first, then Burstable pods exceeding their requests. The OOM may be a node-level issue rather than a pod-level one.

### Step 6: Review Application Logs Before the Kill

**What this checks:** Application output from the terminated container instance, which may reveal the memory allocation pattern before the kill.

```bash
kubectl logs <pod-name> -n <namespace> --previous --tail=100
```

**Expected output:** Application logs from the crashed container. Look for memory allocation errors, heap dump messages, GC pressure indicators, or error messages about running out of memory.

**What the finding means:** If the application logged GC overhead or allocation failures before the kill, the application itself is aware it is running out of memory. If there are no memory-related messages, the kill was sudden (common for native memory leaks or memory-mapped files).

### Step 7: Check for Memory-Backed emptyDir Volumes

**What this checks:** Whether the pod uses tmpfs volumes that consume memory counting toward the container's limit.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.volumes[*]}{.name}{"\t"}{.emptyDir}{"\n"}{end}'
```

**Expected output:** Volume definitions showing `medium: Memory` for tmpfs-backed volumes.

**What the finding means:** Volumes with `medium: Memory` consume RAM that counts toward the pod's memory limit. Large files written to these volumes can push the container over its limit unexpectedly.

### Step 8: Inspect Kernel OOM Events on the Node

**What this checks:** The kernel's OOM kill log entries, showing exactly which process was killed and its memory consumption.

```bash
kubectl debug node/<node-name> -it --image=busybox -- sh -c "dmesg | grep -i 'oom\|killed process' | tail -20"
```

**Expected output:** Kernel log lines showing `oom-kill` events with process name, PID, and memory statistics.

**What the finding means:** The kernel log confirms which process within the container was killed and how much memory it was using at the time. This is the definitive record of the OOM event.

### Step 9: Check QoS Class

**What this checks:** The pod's Quality of Service class, which determines its eviction priority under node memory pressure.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.qosClass}'
```

**Expected output:** One of `Guaranteed`, `Burstable`, or `BestEffort`.

**What the finding means:** `Guaranteed` (requests equal limits) has the highest priority and is last to be evicted. `Burstable` (requests less than limits) is medium priority. `BestEffort` (no requests or limits) is first to be evicted. For critical workloads, Guaranteed QoS provides the most protection.

### Step 10: Profile Memory Usage Over Time

**What this checks:** Historical memory usage trends to distinguish between gradual leaks and sudden spikes.

```bash
# If Prometheus is available, query container memory usage
# PromQL: container_memory_working_set_bytes{pod="<pod-name>", namespace="<namespace>"}
kubectl port-forward svc/prometheus -n monitoring 9090:9090
```

**Expected output:** A time-series graph showing memory consumption over time.

**What the finding means:** A steadily increasing line indicates a memory leak. A sudden spike followed by OOM indicates a traffic-driven allocation burst. A flat line near the limit indicates the limit is too low for normal operation.

## Mitigation

### Option 1: Increase Memory Limit

Use when the application needs more memory than currently allocated.

- **Risk:** Low. May increase resource consumption on the node. Over-provisioning wastes cluster resources and may mask a memory leak.
- **Command:**
  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' -p='[
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "1Gi"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "512Mi"}
  ]'
  ```
- **Verify:**
  ```bash
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  kubectl top pod -l app=<app-label> -n <namespace>
  ```
- **Duration:** Immediate. Rolling update completes within the deployment strategy timeout.

### Option 2: Restart the Pod

Use when the OOM was caused by a transient memory spike and the normal working set fits within the limit.

- **Risk:** Low. Causes brief downtime for the specific pod. Only effective if the OOM was caused by a transient spike, not a persistent leak.
- **Command:**
  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl get pod -l app=<app-label> -n <namespace> -w
  ```
- **Duration:** Seconds to minutes depending on image pull and startup time.

### Option 3: Scale Out to Distribute Memory Load

Use when per-request memory allocation is driving the OOM and spreading load reduces per-pod consumption.

- **Risk:** Low. Increases total resource usage across the cluster but reduces per-pod memory pressure.
- **Command:**
  ```bash
  kubectl scale deployment <deployment-name> -n <namespace> --replicas=<new-count>
  ```
- **Verify:**
  ```bash
  kubectl get pods -l app=<app-label> -n <namespace>
  kubectl top pods -l app=<app-label> -n <namespace>
  ```
- **Duration:** New pods are ready within the readiness probe threshold (typically 30-120 seconds).

### Option 4: Set QoS to Guaranteed

Use for critical workloads that must not be evicted under node memory pressure.

- **Risk:** Medium. Reserves exact resources, reducing scheduling flexibility. May prevent the pod from being scheduled if the node lacks capacity.
- **Command:**
  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' -p='[
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "1Gi"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "1Gi"}
  ]'
  ```
- **Verify:**
  ```bash
  kubectl get pod <new-pod-name> -n <namespace> -o jsonpath='{.status.qosClass}'
  # Should return: Guaranteed
  ```
- **Duration:** Rolling update completes within the deployment strategy timeout.

## Root Cause Resolution

**If** the memory limit is too low for normal application operation **then** right-size the limit based on observed peak usage plus 20-30% headroom:

```bash
# Observe peak memory over 7 days (PromQL)
# max_over_time(container_memory_working_set_bytes{pod=~"<deployment>.*", namespace="<namespace>"}[7d])
# Set limit = observed_peak * 1.25
kubectl set resources deployment <deployment-name> -n <namespace> \
  --limits=memory=<new-limit> --requests=memory=<new-request>
```

**If** the application has a memory leak **then** identify and fix the leak in application code:

```bash
# For Java applications, capture a heap dump before OOM
# Add to container env: -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof
kubectl exec <pod-name> -n <namespace> -- jmap -dump:live,format=b,file=/tmp/heapdump.hprof 1
kubectl cp <namespace>/<pod-name>:/tmp/heapdump.hprof ./heapdump.hprof
# Analyze with Eclipse MAT or VisualVM
```

```bash
# For Node.js applications, generate a diagnostic report
# Add to container env: NODE_OPTIONS="--max-old-space-size=<limit-in-mb>"
kubectl exec <pod-name> -n <namespace> -- node -e "process.report.writeReport('/tmp/report.json')"
kubectl cp <namespace>/<pod-name>:/tmp/report.json ./report.json
```

**If** JVM heap is misconfigured relative to the container limit **then** align JVM heap with container memory using container-aware flags (Java 11+):

```bash
kubectl set env deployment/<deployment-name> -n <namespace> \
  JAVA_OPTS="-XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0"
```

**If** memory-backed emptyDir volumes are consuming too much memory **then** add a sizeLimit or switch to disk-backed emptyDir:

```yaml
volumes:
- name: cache
  emptyDir:
    medium: Memory
    sizeLimit: 256Mi  # Enforce a ceiling
```

**If** sidecar containers consume unexpected memory **then** set explicit resource limits on all containers in the pod, including sidecars:

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"\t"}{.resources}{"\n"}{end}'
# Ensure every container has requests and limits set
```

**If** the node is overcommitted and many pods compete for memory **then** reduce replicas, add nodes, or use ResourceQuotas to prevent overcommitment:

```bash
kubectl describe node <node-name> | grep -A 10 "Allocated resources"
# If memory requests exceed node allocatable, reduce replicas or add nodes
```

## Verification

After applying a fix, verify the issue is resolved.

```bash
# 1. Confirm no OOMKilled events in the last hour
kubectl get events -n <namespace> --field-selector reason=OOMKilling --sort-by='.lastTimestamp'
```

```bash
# 2. Verify pod is running without restarts
kubectl get pod -l app=<app-label> -n <namespace>
# RESTARTS column should be 0 or stable
```

```bash
# 3. Check current memory usage is within limits
kubectl top pods -l app=<app-label> -n <namespace>
```

```bash
# 4. Verify no OOMKilled in container last state
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].lastState}'
# Should be empty or show a non-OOM termination
```

```bash
# 5. Monitor over time (run after 30 minutes)
kubectl get pod -l app=<app-label> -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\t restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}'
```

## Prevention

**Always set memory requests and limits.** Pods without limits can consume unbounded memory and destabilize the node. Use LimitRange objects to enforce defaults:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-memory-limits
  namespace: <namespace>
spec:
  limits:
  - default:
      memory: 512Mi
    defaultRequest:
      memory: 256Mi
    type: Container
```

**Use ResourceQuotas to prevent namespace-level overcommitment:**

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: memory-quota
  namespace: <namespace>
spec:
  hard:
    requests.memory: 8Gi
    limits.memory: 16Gi
```

**Right-size based on observed usage, not guesses.** Use metrics-server and Prometheus to baseline memory consumption before setting limits. The Vertical Pod Autoscaler (VPA) can recommend values automatically:

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: <deployment-name>-vpa
  namespace: <namespace>
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <deployment-name>
  updatePolicy:
    updateMode: "Off"  # Recommendation-only mode
```

**Align runtime memory settings with container limits.** For JVM workloads, use `-XX:MaxRAMPercentage=75.0` (Java 11+) so the JVM respects the container memory ceiling. For Node.js, set `--max-old-space-size` to 75% of the container limit. Reserve the remaining 25% for non-heap memory (native allocations, thread stacks, GC overhead).

**Monitor with alerts.** Create Prometheus alerts that fire before OOMKill occurs:

```yaml
groups:
- name: kubernetes-memory
  rules:
  - alert: ContainerMemoryNearLimit
    expr: |
      (container_memory_working_set_bytes / container_spec_memory_limit_bytes) > 0.85
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Container {{ $labels.container }} in pod {{ $labels.pod }} is using >85% of memory limit"
```

**Use Guaranteed QoS for critical services.** Set requests equal to limits for workloads that must not be evicted under node memory pressure.

**Avoid BestEffort QoS in production.** Pods without any resource specifications are the first to be killed during node memory pressure events.

## Sources

- [Kubernetes: Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) -- Memory requests, limits, QoS classes, and OOM enforcement via cgroup memory controller
- [Kubernetes: Assign Memory Resources to Containers and Pods](https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/) -- Step-by-step guide for configuring memory limits and observing OOMKilled behavior
- [Kubernetes: Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) -- Container states, OOMKilled reason, exit code 137, restart policies
- [Kubernetes: Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/) -- kubectl top, ephemeral debug containers, and node-level debugging
- [Kubernetes: Pod Quality of Service Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/) -- QoS class determination and effect on eviction priority
