---
id: k8s-oomkilled
title: "Kubernetes Container OOMKilled"
domain: compute
service: kubernetes
symptom_class:
  - oom
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - oom
  - memory
  - cgroup
  - jvm
  - nodejs
difficulty: intermediate
---

# Kubernetes Container OOMKilled

## Symptom Recognition

- Pod status shows `Reason: OOMKilled` and `Exit Code: 137` in `kubectl describe pod` output (the `State` or `Last State` block of a container).
- Container exits with code 137, which is `128 + 9` (SIGKILL delivered by the kernel OOM killer).
- `kubectl get pods` shows climbing `RESTARTS` counter; with `restartPolicy: Always` the pod eventually enters `CrashLoopBackOff` if the cause is persistent.
- Kernel log on the node contains entries such as `Memory cgroup out of memory: Killed process <pid> (<comm>) total-vm:<kb>kB anon-rss:<kb>kB`.
- Events stream shows `Warning OOMKilling` from `kubelet` referencing the container ID.
- `container_memory_working_set_bytes` reaches or exceeds `container_spec_memory_limit_bytes` immediately before termination.
- For pods evicted by node memory pressure (not container-limit OOM), pod phase becomes `Failed` with `Reason: Evicted` and message `The node was low on resource: memory`.

## Applicability

- Kubernetes 1.24 or newer on any distribution (vanilla, EKS, GKE, AKS, OpenShift).
- Linux nodes using cgroup v1 or v2 with the memory controller enabled. Windows nodes are out of scope; their OOM semantics differ.
- Requires `kubectl` access with `get`, `list`, `describe`, and `logs` verbs on `pods` and `nodes` in the target namespace.
- `kubectl top` requires the `metrics-server` add-on to be installed and healthy.
- `kubectl debug node/<node>` requires the `EphemeralContainers` feature (GA since 1.25) and cluster permission to create node debug pods.
- Historical memory analysis benefits from Prometheus with cAdvisor metrics (`container_memory_working_set_bytes`, `container_spec_memory_limit_bytes`).
- JVM heap-dump capture requires `jmap`/`jcmd` in the container image plus a JDK (not just JRE).

## Diagnostic Steps

### Step 1: Confirm OOMKilled termination reason and exit code

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"  exitCode="}{.lastState.terminated.exitCode}{"  reason="}{.lastState.terminated.reason}{"\n"}{end}'
```

Expected output: a line per container with `exitCode=137  reason=OOMKilled` for the killed container. If `lastState` is empty, query `.state.terminated` instead for a currently-terminated container.

### Step 2: Read pod description for limits, restart count, and events

```bash
kubectl describe pod <pod-name> -n <namespace>
```

Expected output: `State` / `Last State` block with `Reason: OOMKilled` and `Exit Code: 137`; `Limits` and `Requests` per container; `Restart Count`; `QoS Class`; Events at the bottom referencing `OOMKilling`.

### Step 3: Measure current memory usage versus configured limit

```bash
kubectl top pod <pod-name> -n <namespace> --containers
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  limit="}{.resources.limits.memory}{"  request="}{.resources.requests.memory}{"\n"}{end}'
```

Expected output: actual `MEMORY(bytes)` per container from `kubectl top`, plus the configured `limit` and `request` strings. Compute `usage / limit` for the killed container.

### Step 4: Verify memory limit is declared on every container in the pod

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  limits.memory="}{.resources.limits.memory}{"\n"}{end}'
```

Expected output: one line per container. A missing or empty value indicates the container has no memory limit set.

### Step 5: Check node-level memory pressure and eviction signals

```bash
kubectl describe node <node-name> | sed -n '/Conditions:/,/Addresses:/p'
kubectl top node <node-name>
```

Expected output: the `MemoryPressure` row under `Conditions` (`Status: True` indicates the kubelet is evicting pods to reclaim memory) and the node-wide memory usage percentage.

### Step 6: Read application logs from the previous (killed) container instance

```bash
kubectl logs <pod-name> -n <namespace> --previous --tail=200
```

Expected output: application stdout/stderr from the terminated container. Look for `OutOfMemoryError`, `FATAL ERROR: Reached heap limit Allocation failed`, `MemoryError`, GC overhead warnings, or large-allocation log lines in the seconds before the kill.

### Step 7: Inspect kernel OOM-killer log entries on the node

```bash
kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c "dmesg -T | grep -iE 'oom|killed process' | tail -40"
```

Expected output: kernel lines such as `Memory cgroup out of memory: Killed process <pid> (<comm>) total-vm:<kb>kB anon-rss:<kb>kB ...` identifying the exact process killed and its RSS at time of death.

### Step 8: Inventory memory-backed emptyDir volumes in the pod

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.volumes[*]}{.name}{"  medium="}{.emptyDir.medium}{"  sizeLimit="}{.emptyDir.sizeLimit}{"\n"}{end}'
```

Expected output: one line per volume. `medium=Memory` indicates a tmpfs volume that consumes RAM and counts toward the container's memory limit; an empty `sizeLimit` means no ceiling is enforced.

### Step 9: Plot working-set memory over time (leak versus spike)

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
# Then query: container_memory_working_set_bytes{namespace="<ns>",pod="<pod>",container!="POD"}
# Compare against: container_spec_memory_limit_bytes{namespace="<ns>",pod="<pod>",container!="POD"}
```

Expected output: a working-set time series. A monotonic upward slope across hours/days indicates a leak; a near-vertical spike just before termination indicates a request-driven allocation burst; a flat line near the limit indicates the limit is undersized for normal operation.

## Causes

### Cause A: Memory limit set below the application's normal working set

**Statement:** The container's `resources.limits.memory` is configured below the application's steady-state working-set size, so normal operation exceeds the limit and the kernel kills the container.

**Mechanism:** The kubelet writes the configured limit into the container's `memory.max` (cgroup v2) or `memory.limit_in_bytes` (cgroup v1). When the cgroup's RSS plus anonymous memory exceeds the limit and the kernel detects memory pressure, the cgroup OOM killer sends SIGKILL to the process with the highest `oom_score`. The container exits with code 137 and the kubelet records `Reason: OOMKilled`. Because the working set is structural rather than transient, every restart re-enters the same allocation pattern and is killed again, producing CrashLoopBackOff.

**Indicator:**

- [Step 3] `kubectl top` shows the killed container running at or above the configured `limits.memory` during normal load
<!-- match: {"step": 3, "predicate": "threshold", "target": "memory_pct", "op": ">", "value": 0.95} -->
- [Step 6] application logs show no allocation-burst pattern preceding the kill — usage is flat at the ceiling
- [Symptom] restart counter climbs steadily without correlation to traffic spikes

**Mitigation:**

- **Risk:** Increasing the limit consumes more node capacity and can starve other pods on the same node. Over-allocating also masks future leaks.
- **Command:**

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<new-limit> --requests=memory=<new-request>
  ```

- **Duration:** Safe to leave in place permanently once sized from observed peak usage; revisit if traffic patterns change.

**Resolution:**

```bash
# Size from observed peak over 7 days plus 25-30% headroom for non-heap allocations
# PromQL: max_over_time(container_memory_working_set_bytes{pod=~"<deployment>-.*",namespace="<namespace>",container="<container>"}[7d])
kubectl set resources deployment/<deployment-name> -n <namespace> \
  --limits=memory=<peak_bytes_times_1.25> --requests=memory=<peak_bytes>
```

**Verification:** After rollout, run `kubectl top pod -l app=<label> -n <namespace>` every 5 minutes for 30 minutes; working-set memory should stabilize at least 20% below the new limit and `kubectl get pod -l app=<label> -n <namespace>` should show `RESTARTS=0`.

### Cause B: Application memory leak driving unbounded growth

**Statement:** Application code retains references that prevent garbage collection (or fails to free native allocations), causing working-set memory to grow monotonically until it crosses the container limit.

**Mechanism:** Unbounded caches, accumulating event listeners, stuck connection pools, or leaked native buffers cause the process's heap to grow with every request or background tick. The garbage collector eventually cannot reclaim space; resident memory rises past `limits.memory`; the cgroup OOM killer sends SIGKILL. The container restarts with a fresh heap and the cycle restarts on roughly the same time scale, producing predictable restart intervals.

**Indicator:**

- [Step 9] working-set memory shows a monotonic upward slope over hours or days, not correlated with traffic
- [Step 6] application logs show GC overhead warnings (`GC overhead limit exceeded`, `Mark-sweep ... allocation failed`) or `OutOfMemoryError` shortly before termination
<!-- match: {"step": 6, "predicate": "contains", "target": "OutOfMemoryError"} -->
- [Symptom] restart interval is roughly constant for a constant workload (each restart takes the same time to climb back to the limit)

**Mitigation:**

- **Risk:** Scheduled restarts hide the underlying bug and can mask data loss if the application has in-memory state. Use only as a holding pattern while a fix is developed.
- **Command:**

  ```bash
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

- **Duration:** Hours, not days. Schedule a recurring restart via a CronJob only as a stopgap while leak investigation is in flight.

**Resolution:**

```bash
# Java: capture a heap dump on OOM and pull it off the pod for analysis
kubectl set env deployment/<deployment-name> -n <namespace> \
  JAVA_TOOL_OPTIONS="-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof"
kubectl exec <pod-name> -n <namespace> -- jcmd 1 GC.heap_dump /tmp/live-heap.hprof
kubectl cp <namespace>/<pod-name>:/tmp/live-heap.hprof ./live-heap.hprof
# Analyse the dump with Eclipse MAT or VisualVM, find the dominator tree, fix the retention path in code, ship a new image.
```

```bash
# Node.js: generate a heap snapshot via the V8 inspector
kubectl exec <pod-name> -n <namespace> -- node --inspect=0.0.0.0:9229 -e "require('v8').writeHeapSnapshot('/tmp/heap.heapsnapshot')"
kubectl cp <namespace>/<pod-name>:/tmp/heap.heapsnapshot ./heap.heapsnapshot
# Load the snapshot in Chrome DevTools, look for retainers in the dominator view, fix and redeploy.
```

**Verification:** After deploying the fixed image, run `kubectl top pod -l app=<label> -n <namespace> --containers` once per hour for 24 hours; working-set memory must plateau and remain below 75% of the limit instead of trending upward.

### Cause C: Runtime heap sized larger than the container memory limit

**Statement:** A JVM, V8, or other managed runtime is configured with a maximum heap (`-Xmx`, `--max-old-space-size`) that approaches or exceeds the container's memory limit, leaving no headroom for non-heap memory.

**Mechanism:** Managed runtimes also consume off-heap memory: thread stacks, metaspace/code cache, JIT-compiled code, native libraries, direct byte buffers, V8 external memory (Buffers, native modules), and GC scratch space. Total RSS equals heap + off-heap. When `-Xmx` is set to the full container limit (or `MaxRAMPercentage` near 100), even a heap that stays within `-Xmx` causes total RSS to exceed `limits.memory`, triggering the cgroup OOM killer. The JVM/V8 itself does not log an `OutOfMemoryError` because, from its perspective, the heap was not full.

**Indicator:**

- [Step 6] application logs show no `OutOfMemoryError` or GC overhead warning despite OOMKilled status
- [Step 3] `kubectl top` shows the container at the limit while heap-fill metrics (if exposed) are well below `-Xmx`
- [Symptom] killed processes are JVM (`java`) or Node.js (`node`) and the runtime was launched with explicit `-Xmx <limit>` or `--max-old-space-size=<limit_in_mb>` equal to or near `limits.memory`

**Mitigation:**

- **Risk:** Setting heap too low causes legitimate `OutOfMemoryError` from the runtime. Aim for ~75% heap allocation initially, then tune from observed GC pressure.
- **Command:**

  ```bash
  kubectl set env deployment/<deployment-name> -n <namespace> \
    JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0"
  ```

- **Duration:** Permanent. Container-aware runtime flags auto-adjust if `limits.memory` is changed later.

**Resolution:**

```bash
# Java 11+ (UseContainerSupport is default on; do not pin -Xmx in container images)
kubectl set env deployment/<deployment-name> -n <namespace> \
  JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0 -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof"
```

```bash
# Node.js: leave 25% headroom for V8 internals and external buffers
# For a 1Gi limit set --max-old-space-size to ~768 MB
kubectl set env deployment/<deployment-name> -n <namespace> \
  NODE_OPTIONS="--max-old-space-size=768"
```

**Verification:** After rollout, run `kubectl exec <pod-name> -n <namespace> -- jcmd 1 VM.flags | grep -E 'MaxHeapSize|MaxRAM'` (Java) or `kubectl exec <pod-name> -n <namespace> -- node -e "console.log(require('v8').getHeapStatistics().heap_size_limit)"` (Node.js); the reported heap ceiling must be ≤ 80% of `limits.memory` in bytes.

### Cause D: Memory-backed emptyDir or tmpfs volume consuming RAM against the limit

**Statement:** A `medium: Memory` `emptyDir` volume (or other tmpfs mount inside the container) holds files in RAM, and that memory counts toward the container's cgroup limit even though the application is not aware of it.

**Mechanism:** Kubernetes implements `emptyDir.medium: Memory` as a tmpfs mount inside the pod. Writes to the volume allocate page cache backed by RAM in the same cgroup as the container process. The cgroup memory controller accounts these pages against `memory.max`. When the application writes large files (logs, caches, scratch data) to the tmpfs volume, working-set memory rises even though the application heap is small. Once cgroup memory crosses the limit, the kernel kills the largest process in the cgroup — typically the application, not the file-cache holder.

**Indicator:**

- [Step 8] pod spec contains an `emptyDir` volume with `medium: Memory` and no `sizeLimit`
<!-- match: {"step": 8, "predicate": "contains", "target": "medium=Memory"} -->
- [Step 6] application logs show no allocation pressure (heap is healthy) yet the container was OOMKilled
- [Step 7] kernel `oom-kill` log identifies the application process with anon-rss far below the cgroup limit, indicating other accounted pages (page cache from tmpfs) consumed the budget

**Mitigation:**

- **Risk:** Adding `sizeLimit` causes writes to fail with `ENOSPC` instead of OOM; the application must handle write errors. Switching to disk-backed `emptyDir` adds I/O latency.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' -p='[{"op":"add","path":"/spec/template/spec/volumes/0/emptyDir/sizeLimit","value":"256Mi"}]'
  ```

- **Duration:** Permanent once the application has been verified to handle `ENOSPC` gracefully.

**Resolution:**

```yaml
# Edit the deployment spec: either bound the tmpfs volume or switch to disk-backed emptyDir
volumes:
  - name: scratch
    emptyDir:
      medium: Memory
      sizeLimit: 256Mi   # Hard ceiling enforced by tmpfs; writes beyond return ENOSPC
# OR remove medium: Memory entirely to use node-local disk:
  - name: scratch
    emptyDir: {}
```

**Verification:** After applying the spec change, run `kubectl exec <pod-name> -n <namespace> -- df -h /<volume-mountpath>`; tmpfs should now show a bounded `Size` matching `sizeLimit`, and `kubectl top pod` working-set should stay below `limits.memory` even under sustained writes.

### Cause E: Sidecar or init container consuming memory not budgeted into the pod limit

**Statement:** A sidecar container (Istio/Envoy proxy, logging agent, secrets injector) has no memory limit or a small one, and its memory growth — together with the application — pushes the pod over its aggregate memory ceiling.

**Mechanism:** Pod-level QoS and node-level scheduling sum the memory limits of every container in the pod. When a sidecar lacks `resources.limits.memory`, it can grow to consume node memory; under node memory pressure the kubelet evicts the entire pod. When sidecars do have limits but the main container's limit was sized assuming the sidecar is "free," the sidecar's RSS plus the application's RSS exceed available node memory and the kernel kills whichever cgroup is over its individual limit — often the application, which has the larger working set.

**Indicator:**

- [Step 4] one or more containers in the pod (typically `istio-proxy`, `fluent-bit`, `vault-agent`) have no `limits.memory` set
<!-- match: {"step": 4, "predicate": "absent", "target": "spec.containers[].resources.limits.memory"} -->
- [Step 2] `kubectl describe pod` shows multiple containers and the killed container is not the highest memory consumer
- [Step 3] sidecar working-set is non-trivial (>100Mi) relative to the application

**Mitigation:**

- **Risk:** Setting a sidecar memory limit too low triggers OOMKilled in the sidecar itself, which can break service-mesh data-plane connectivity for the whole pod.
- **Command:**

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --containers='istio-proxy' --limits=memory=256Mi --requests=memory=128Mi
  ```

- **Duration:** Permanent. Bake explicit sidecar limits into the deployment template and any mesh-injection templates.

**Resolution:**

```bash
# Make limits explicit on every container in the pod template, including auto-injected sidecars.
# For Istio, configure the mesh injection template to set proxy resources:
kubectl get configmap istio-sidecar-injector -n istio-system -o yaml
# Edit "proxy.resources" under values.global.proxy to set memory limits, then re-roll workloads.
kubectl rollout restart deployment/<deployment-name> -n <namespace>
```

**Verification:** After rollout, run `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  "}{.resources.limits.memory}{"\n"}{end}'`; every container in the list must have a non-empty memory limit. `kubectl top pod <pod-name> --containers` should show each container below its own limit.

### Cause F: Node-level memory pressure triggering pod eviction

**Statement:** The node hosting the pod has crossed the kubelet's `memory.available` eviction threshold, and the kubelet has evicted the pod to reclaim node memory even though the container itself was below its individual limit.

**Mechanism:** The kubelet tracks `memory.available = node.capacity[memory] - node.stats.memory.workingSet` and compares it against `--eviction-hard=memory.available<...` (default 100Mi). When the threshold is crossed, the kubelet sets the `MemoryPressure` node condition to `True` and begins evicting pods, preferring `BestEffort` first, then `Burstable` pods that exceed their memory request, then `Guaranteed` pods. Hard eviction uses a 0-second grace period; the pod is killed without graceful shutdown. The pod status reflects `Reason: Evicted` rather than `Reason: OOMKilled` on the container, but the operator-visible symptom (container terminated, restart, possible CrashLoopBackOff for workloads scheduled back onto the same node) is the same.

**Indicator:**

- [Step 5] `MemoryPressure` node condition is `Status: True` at or near the time of termination
<!-- match: {"step": 5, "predicate": "contains", "target": "MemoryPressure       True"} -->
- [Step 2] pod-level `Reason: Evicted` with message `The node was low on resource: memory`
- [Step 3] container's own working-set is well below its configured limit at the time of the kill

**Mitigation:**

- **Risk:** Cordoning the node forces re-scheduling and can cascade pressure to other nodes if cluster headroom is tight.
- **Command:**

  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
  ```

- **Duration:** Until the node has been replaced or its memory expanded; uncordon once the underlying capacity issue is resolved.

**Resolution:**

```bash
# Two durable paths:
# 1) Reduce node overcommitment by lowering replica count or raising pod memory requests so the scheduler stops packing the node.
kubectl scale deployment <deployment-name> -n <namespace> --replicas=<lower>
# 2) Add node capacity (cluster autoscaler, manual node pool resize, or larger instance types).
kubectl get nodes -o custom-columns=NAME:.metadata.name,ALLOCATABLE_MEM:.status.allocatable.memory,REQUESTS:.status.capacity.memory
```

**Verification:** After remediation, run `kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"  MemoryPressure="}{.status.conditions[?(@.type=="MemoryPressure")].status}{"\n"}{end}'`; every node must report `MemoryPressure=False`. No new `Evicted` pods should appear within 1 hour: `kubectl get pods -A --field-selector=status.phase=Failed | grep -i evicted`.

### Cause Z: Unidentified

**Statement:** Diagnostic steps did not converge on a specific cause; the OOMKill cannot be attributed to a known pattern from Causes A–F.

**Mechanism:** The kernel killed the container's main process via SIGKILL after cgroup memory accounting exceeded `memory.max`, but the available evidence (limits, working-set trend, sidecar inventory, node pressure, application logs) does not isolate which path drove memory across the threshold. Further investigation needs richer signals — process-level RSS over time, off-heap allocation profiling, kernel `oom_score` ranking, or correlation with deployment/configuration changes.

**Indicator:**

- [Default] OOMKilled is confirmed (Step 1, Step 2) but Causes A–F indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Restarting buys time but does not address the cause; if the OOM recurs within minutes, escalate immediately to avoid alert fatigue.
- **Command:**

  ```bash
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector reason=OOMKilling -o wide
  ```

- **Duration:** Use only as a holding action while engaging the application owner with the gathered diagnostic artefacts.

**Resolution:** Out of runbook scope. Capture the artefacts from Steps 1–9 (pod description, kernel `dmesg` excerpt, working-set time series, container logs from the previous instance) and escalate to the application owner or platform on-call with the failure-mode summary.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Set both `requests.memory` and `limits.memory` on every container — including sidecars and init containers. Enforce defaults cluster-wide with a `LimitRange` per namespace so pods cannot be deployed without limits.
- Use Guaranteed QoS (requests equal limits) for tier-1 services. Avoid `BestEffort` (no requests, no limits) in production — those pods are evicted first under node pressure.
- For managed runtimes, always use container-aware heap flags. Java 11+: `-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0`. Node.js: `--max-old-space-size=<~75% of limit in MB>`. Never pin `-Xmx` to the full container limit; reserve 25% for non-heap memory.
- Size limits from observed peak, not from guesses. Capture 7-day `max_over_time(container_memory_working_set_bytes[7d])` and set `limits.memory = peak * 1.25`. The Vertical Pod Autoscaler in `updateMode: "Off"` can produce recommendations automatically.
- Alert before kill: fire a Prometheus alert at `container_memory_working_set_bytes / container_spec_memory_limit_bytes > 0.85` for 5m so the operator has time to investigate before the cgroup OOM fires.
- Bound memory-backed volumes. Every `emptyDir` with `medium: Memory` must declare `sizeLimit`. Prefer disk-backed `emptyDir` for non-latency-critical scratch space.
- Enforce namespace-wide ceilings with `ResourceQuota` (`limits.memory` and `requests.memory`) to prevent a single team from overcommitting a node.
- Track `MemoryPressure` node condition in dashboards and page on transition to `True`. Combine with cluster-autoscaler so capacity grows before eviction is necessary.

## Sources

- [Kubernetes — Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) — Priority 1. Cgroup memory limit enforcement, exit code 137, reactive OOM kills, QoS classes.
- [Kubernetes — Assign Memory Resources to Containers and Pods](https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/) — Priority 1. Detecting OOMKilled via `kubectl describe pod`, right-sizing requests and limits.
- [Kubernetes — Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) — Priority 1. Container states, OOMKilled termination reason, restartPolicy, exit code 137 in containerStatuses.
- [Kubernetes — Pod Quality of Service Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/) — Priority 1. Guaranteed/Burstable/BestEffort criteria, eviction priority, `kubectl get pod ... -o jsonpath='{.status.qosClass}'`.
- [Kubernetes — Node-Pressure Eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/) — Priority 1. MemoryPressure condition, `memory.available` signal, hard/soft eviction thresholds, eviction order by QoS.
- [Kubernetes — Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/) — Priority 1. `kubectl debug` for pods and nodes, ephemeral containers, dmesg inspection on the host.
- [Kubernetes — Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) — Priority 1. `kubectl describe pod`, previous-container logs, events for OOMKilling diagnosis.
- [Red Hat — Java 17 OpenJDK container awareness](https://developers.redhat.com/articles/2022/04/19/java-17-whats-new-openjdks-container-awareness) — Priority 1 (vendor). `UseContainerSupport` and `MaxRAMPercentage`/`InitialRAMPercentage` semantics for JVM heap inside cgroup limits.
- [Red Hat — Node.js 20+ memory management in containers](https://developers.redhat.com/articles/2025/10/10/nodejs-20-memory-management-containers) — Priority 1 (vendor). V8 container-aware heap defaults, `--max-old-space-size`, off-heap (external/Buffer) memory considerations.
