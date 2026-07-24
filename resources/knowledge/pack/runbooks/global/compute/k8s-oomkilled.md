---
id: k8s-oomkilled
title: "Kubernetes Container OOMKilled"
domain: compute
service: kubernetes
symptom_class:
  - oom
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
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

**Chain:**
- root: `resources.limits.memory` is sized below the application's steady-state working set.
- s1: the kubelet writes that undersized limit into the cgroup `memory.max` (v2) / `memory.limit_in_bytes` (v1).
- s2: normal-load RSS plus anonymous memory exceeds the cgroup limit, triggering memory pressure.
- s3: the cgroup OOM killer sends SIGKILL to the highest-`oom_score` process; container exits 137, kubelet records OOMKilled.
- s4: the working set is structural, so every restart re-enters the same allocation pattern and is killed again (CrashLoopBackOff).
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- s2: [Step 3] `kubectl top` shows the killed container running at or above the configured `limits.memory` during normal load
- root: [Step 6] application logs show no allocation-burst pattern preceding the kill — usage is flat at the ceiling
- s4: [Symptom] restart counter climbs steadily without correlation to traffic spikes

**Interventions:**
- **remediation** (root): size the limit from observed peak over 7 days plus 25-30% headroom for non-heap allocations.

  ```bash
  # PromQL: max_over_time(container_memory_working_set_bytes{pod=~"<deployment>-.*",namespace="<namespace>",container="<container>"}[7d])
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<peak_bytes_times_1.25> --requests=memory=<peak_bytes>
  ```

  **Verification:** After rollout, run `kubectl top pod -l app=<label> -n <namespace>` every 5 minutes for 30 minutes; working-set memory should stabilize at least 20% below the new limit and `kubectl get pod -l app=<label> -n <namespace>` should show `RESTARTS=0`.
- **mitigation** (s1): bump the limit immediately to stop the kills while the right size is being confirmed.

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<new-limit> --requests=memory=<new-request>
  ```

  **Risk:** Increasing the limit consumes more node capacity and can starve other pods on the same node. Over-allocating also masks future leaks. **Duration:** Safe to leave in place permanently once sized from observed peak usage; revisit if traffic patterns change. **Verification:** re-run Step 3; `usage / limit` for the killed container falls below 0.8 under normal load.

### Cause B: Application memory leak driving unbounded growth

**Statement:** Application code retains references (or fails to free native allocations), so working-set memory grows monotonically until it crosses the container limit.

**Chain:**
- root: application code retains references (unbounded caches, accumulating listeners, stuck pools, leaked native buffers) or fails to free native allocations.
- s1: the heap grows every request or background tick and GC cannot reclaim the retained space.
- s2: resident memory rises monotonically until it crosses `limits.memory`.
- s3: the cgroup OOM killer sends SIGKILL; container exits 137.
- s4: the container restarts with a fresh heap and the cycle repeats on the same time scale (predictable intervals).
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- s2: [Step 9] working-set memory shows a monotonic upward slope over hours/days, uncorrelated with traffic
- s1: [Step 6] application logs show GC overhead warnings (`GC overhead limit exceeded`, `Mark-sweep ... allocation failed`) or `OutOfMemoryError` shortly before termination
- s4: [Symptom] restart interval is roughly constant for a constant workload

**Interventions:**
- **remediation** (root): capture a heap dump/snapshot, find the retention path, fix it in code, and ship a new image.

  ```bash
  # Java: heap dump on OOM, then pull it off the pod
  kubectl set env deployment/<deployment-name> -n <namespace> \
    JAVA_TOOL_OPTIONS="-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof"
  kubectl exec <pod-name> -n <namespace> -- jcmd 1 GC.heap_dump /tmp/live-heap.hprof
  kubectl cp <namespace>/<pod-name>:/tmp/live-heap.hprof ./live-heap.hprof
  # Analyse in Eclipse MAT/VisualVM; fix the retention path.
  ```

  ```bash
  # Node.js: V8 heap snapshot
  kubectl exec <pod-name> -n <namespace> -- node --inspect=0.0.0.0:9229 -e "require('v8').writeHeapSnapshot('/tmp/heap.heapsnapshot')"
  kubectl cp <namespace>/<pod-name>:/tmp/heap.heapsnapshot ./heap.heapsnapshot
  # Load in Chrome DevTools, inspect retainers, fix and redeploy.
  ```

  **Verification:** After deploy, run `kubectl top pod -l app=<label> -n <namespace> --containers` hourly for 24h; working-set must plateau below 75% of the limit, not trend upward.
- **mitigation** (s2): periodically restart the deployment to reset the heap before it crosses the limit.

  ```bash
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Risk:** Scheduled restarts hide the bug and can mask data loss if the app holds in-memory state; use only as a holding pattern. **Duration:** Hours, not days — a stopgap CronJob while the leak fix is in flight. **Verification:** re-run Step 9 after a restart; working-set drops to baseline then climbs again.

### Cause C: Runtime heap sized larger than the container memory limit

**Statement:** A JVM, V8, or other managed runtime's max heap (`-Xmx`, `--max-old-space-size`) approaches or exceeds the container memory limit, leaving no headroom for non-heap memory.

**Chain:**
- root: the managed runtime's max heap (`-Xmx` / `--max-old-space-size` / `MaxRAMPercentage` near 100) is set at or near the full container limit.
- s1: total RSS = heap + off-heap (thread stacks, metaspace/code cache, JIT, native libs, direct/external buffers, GC scratch).
- s2: even a heap within `-Xmx` drives total RSS past `limits.memory`.
- s3: the cgroup OOM killer sends SIGKILL; the runtime logs no `OutOfMemoryError` because, from its view, the heap was not full.
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- s3: [Step 6] application logs show no `OutOfMemoryError` or GC overhead warning despite OOMKilled status
- s2: [Step 3] `kubectl top` shows the container at the limit while heap-fill metrics are well below `-Xmx`
- root: [Symptom] killed processes are JVM (`java`) or Node.js (`node`) and the runtime was launched with explicit `-Xmx <limit>` or `--max-old-space-size=<limit_in_mb>` equal to or near `limits.memory`

**Interventions:**
- **remediation** (root): use container-aware heap flags so the heap leaves headroom for off-heap memory.

  ```bash
  # Java 11+: UseContainerSupport default on; do not pin -Xmx
  kubectl set env deployment/<deployment-name> -n <namespace> \
    JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0 -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof"
  ```

  ```bash
  # Node.js: ~75% of limit (768 MB for a 1Gi limit) leaves headroom for V8 internals
  kubectl set env deployment/<deployment-name> -n <namespace> \
    NODE_OPTIONS="--max-old-space-size=768"
  ```

  **Verification:** After rollout, run `kubectl exec <pod-name> -n <namespace> -- jcmd 1 VM.flags | grep -E 'MaxHeapSize|MaxRAM'` (Java) or the equivalent `node -e "console.log(require('v8').getHeapStatistics().heap_size_limit)"` (Node.js); the reported heap ceiling must be ≤ 80% of `limits.memory`.
- **mitigation** (s1): apply container-aware percentage flags as a quick reconfiguration to cap heap below the limit.

  ```bash
  kubectl set env deployment/<deployment-name> -n <namespace> \
    JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0"
  ```

  **Risk:** Heap set too low causes legitimate `OutOfMemoryError`; start ~75% then tune from GC pressure. **Duration:** Permanent — container-aware flags auto-adjust if `limits.memory` changes. **Verification:** re-run Step 3; total RSS stays below `limits.memory` under load, no new OOMKilled.

### Cause D: Memory-backed emptyDir or tmpfs volume consuming RAM against the limit

**Statement:** A `medium: Memory` `emptyDir` volume (or other tmpfs mount inside the container) holds files in RAM, and that memory counts toward the container's cgroup limit even though the application is not aware of it.

**Chain:**
- root: a `medium: Memory` `emptyDir` (tmpfs mount) is present, with no `sizeLimit`.
- s1: writes to the tmpfs allocate page cache backed by RAM in the same cgroup as the container process.
- s2: the cgroup memory controller accounts those tmpfs pages against `memory.max`, so working-set rises even though the app heap is small.
- s3: cgroup memory crosses the limit; the kernel kills the largest process in the cgroup — typically the application, not the file-cache holder.
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] pod spec contains an `emptyDir` volume with `medium: Memory` and no `sizeLimit`
- s1: [Step 6] application logs show no allocation pressure (heap is healthy) yet the container was OOMKilled
- s2: [Step 7] kernel `oom-kill` log identifies the application process with anon-rss far below the cgroup limit, indicating other accounted pages (page cache from tmpfs) consumed the budget

**Interventions:**
- **remediation** (root): bound the tmpfs volume with `sizeLimit` or switch to disk-backed `emptyDir`.

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
- **mitigation** (s2): patch a `sizeLimit` onto the existing volume to cap tmpfs RAM consumption immediately.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' -p='[{"op":"add","path":"/spec/template/spec/volumes/0/emptyDir/sizeLimit","value":"256Mi"}]'
  ```

  **Risk:** Adding `sizeLimit` causes writes to fail with `ENOSPC` instead of OOM; the application must handle write errors. Switching to disk-backed `emptyDir` adds I/O latency. **Duration:** Permanent once the application has been verified to handle `ENOSPC` gracefully. **Verification:** re-run Step 8; the volume now reports a non-empty `sizeLimit` and working-set stays under the container limit.

### Cause E: Sidecar or init container consuming memory not budgeted into the pod limit

**Statement:** A sidecar container (Istio/Envoy proxy, logging agent, secrets injector) has no memory limit or a small one, and its memory growth — together with the application — pushes the pod over its aggregate memory ceiling.

**Chain:**
- root: a sidecar (`istio-proxy`, `fluent-bit`, `vault-agent`, etc.) has no `resources.limits.memory`, or the main container's limit was sized assuming the sidecar is "free."
- s1: pod-level QoS and node scheduling sum every container's memory; the unbudgeted sidecar grows to consume node memory.
- s2: the sidecar's RSS plus the application's RSS exceed available node memory (or a container crosses its own individual limit).
- s3: the kernel kills whichever cgroup is over its limit — often the application, which has the larger working set; under node pressure the kubelet evicts the whole pod.
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] one or more containers in the pod (typically `istio-proxy`, `fluent-bit`, `vault-agent`) have no `limits.memory` set
- s3: [Step 2] `kubectl describe pod` shows multiple containers and the killed container is not the highest memory consumer
- s1: [Step 3] sidecar working-set is non-trivial (>100Mi) relative to the application

**Interventions:**
- **remediation** (root): make memory limits explicit on every container, including auto-injected sidecars, in the deployment and mesh-injection templates.

  ```bash
  # Make limits explicit on every container in the pod template, including auto-injected sidecars.
  # For Istio, configure the mesh injection template to set proxy resources:
  kubectl get configmap istio-sidecar-injector -n istio-system -o yaml
  # Edit "proxy.resources" under values.global.proxy to set memory limits, then re-roll workloads.
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** After rollout, run `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  "}{.resources.limits.memory}{"\n"}{end}'`; every container in the list must have a non-empty memory limit. `kubectl top pod <pod-name> --containers` should show each container below its own limit.
- **mitigation** (s1): set an explicit memory limit on the offending sidecar to cap its growth.

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --containers='istio-proxy' --limits=memory=256Mi --requests=memory=128Mi
  ```

  **Risk:** Setting a sidecar memory limit too low triggers OOMKilled in the sidecar itself, which can break service-mesh data-plane connectivity for the whole pod. **Duration:** Permanent. Bake explicit sidecar limits into the deployment template and any mesh-injection templates. **Verification:** re-run Step 4; the sidecar now reports a non-empty `limits.memory` and `kubectl top pod --containers` shows it below its limit.

### Cause F: Node-level memory pressure triggering pod eviction

**Statement:** The node hosting the pod has crossed the kubelet's `memory.available` eviction threshold, and the kubelet has evicted the pod to reclaim node memory even though the container itself was below its individual limit.

**Chain:**
- root: the node's `memory.available` (= `node.capacity[memory] - node.stats.memory.workingSet`) crosses the kubelet `--eviction-hard=memory.available<...` threshold (default 100Mi).
- s1: the kubelet sets the node `MemoryPressure` condition to `True` and begins evicting pods (BestEffort first, then Burstable over request, then Guaranteed).
- s2: hard eviction uses a 0-second grace period; the pod is killed without graceful shutdown, status `Reason: Evicted`.
- s3: workloads scheduled back onto the same node are evicted again, reproducing the operator-visible terminate/restart/CrashLoopBackOff symptom.
- D: container is terminated/evicted (points at Symptom Recognition).

**Indicators:**
- s1: [Step 5] `MemoryPressure` node condition is `Status: True` at or near the time of termination
- s2: [Step 2] pod-level `Reason: Evicted` with message `The node was low on resource: memory`
- root: [Step 3] container's own working-set is well below its configured limit at the time of the kill

**Interventions:**
- **remediation** (root): reduce node overcommitment or add node capacity so `memory.available` stays above the eviction threshold.

  ```bash
  # Two durable paths:
  # 1) Reduce node overcommitment by lowering replica count or raising pod memory requests so the scheduler stops packing the node.
  kubectl scale deployment <deployment-name> -n <namespace> --replicas=<lower>
  # 2) Add node capacity (cluster autoscaler, manual node pool resize, or larger instance types).
  kubectl get nodes -o custom-columns=NAME:.metadata.name,ALLOCATABLE_MEM:.status.allocatable.memory,REQUESTS:.status.capacity.memory
  ```

  **Verification:** After remediation, run `kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"  MemoryPressure="}{.status.conditions[?(@.type=="MemoryPressure")].status}{"\n"}{end}'`; every node must report `MemoryPressure=False`. No new `Evicted` pods should appear within 1 hour: `kubectl get pods -A --field-selector=status.phase=Failed | grep -i evicted`.
- **mitigation** (s1): cordon and drain the pressured node to move pods off it while capacity is fixed.

  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
  ```

  **Risk:** Cordoning the node forces re-scheduling and can cascade pressure to other nodes if cluster headroom is tight. **Duration:** Until the node has been replaced or its memory expanded; uncordon once the underlying capacity issue is resolved. **Verification:** re-run Step 5 on the target node; `MemoryPressure` returns to `False` after pods drain.

### Cause Z: Unidentified

**Statement:** Diagnostic steps did not converge on a specific cause; the OOMKill cannot be attributed to a known pattern from Causes A–F.

**Chain:**
- root: the available evidence (limits, working-set trend, sidecar inventory, node pressure, application logs) does not isolate which path drove memory across the threshold.
- s1: the kernel killed the container's main process via SIGKILL after cgroup memory accounting exceeded `memory.max`, but the driving path is unknown.
- D: container is OOMKilled (points at Symptom Recognition).

**Indicators:**
- root: [Default] OOMKilled is confirmed (Step 1, Step 2) but Causes A–F indicators do not match the gathered evidence

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the application owner / platform on-call.

  ```bash
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector reason=OOMKilling -o wide
  ```

  **Risk:** Restarting buys time but does not address the cause; if the OOM recurs within minutes, escalate immediately to avoid alert fatigue. **Duration:** Use only as a holding action while engaging the application owner with the gathered diagnostic artefacts. **Verification:** Capture the artefacts from Steps 1–9 (pod description, kernel `dmesg` excerpt, working-set time series, container logs from the previous instance) and hand off; the receiving engineer acknowledges and an incident ticket is opened with the artefacts attached and a follow-up owner assigned.

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
