---
id: k8s-crashloopbackoff
title: "Kubernetes Pod CrashLoopBackOff"
domain: compute
service: kubernetes
symptom_class:
  - crash_loop
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pods
  - crashloop
  - liveness-probe
  - configmap
difficulty: intermediate
---

# Kubernetes Pod CrashLoopBackOff

## Symptom Recognition

- `kubectl get pods` reports `STATUS: CrashLoopBackOff` for the affected pod and a steadily climbing `RESTARTS` column (e.g. `0/1   CrashLoopBackOff   14 (2m ago)   10m`).
- `kubectl describe pod` shows a container `State: Waiting` with `Reason: CrashLoopBackOff` and a `Last State: Terminated` block carrying a non-zero `Exit Code` and a `Reason` such as `Error`, `OOMKilled`, `ContainerCannotRun`, or `StartError`.
- Kubelet emits `Back-off restarting failed container` warning events on the pod, with backoff delays doubling between attempts (10s → 20s → 40s → 80s → 160s, capped at 300s) until the container runs successfully for at least 10 minutes.
- For probe-induced restarts the event log shows `Liveness probe failed: ...` followed by `Killing container ... Container failed liveness probe, will be restarted`.
- For missing references the pod status shows `Reason: CreateContainerConfigError` or `CreateContainerError` instead of CrashLoopBackOff, but the symptom (container never starts, pod cycles through Waiting) is operator-equivalent.
- For init-container failures the pod status reports `Init:CrashLoopBackOff` with `Init Containers` block showing the failing init step.

## Applicability

- Kubernetes 1.24 or newer on any distribution (vanilla, EKS, GKE, AKS, OpenShift, k3s).
- Pods with `restartPolicy: Always` (default for Deployments, StatefulSets, DaemonSets, ReplicaSets) or `restartPolicy: OnFailure` (default for Jobs). `restartPolicy: Never` pods enter `Failed` instead and are out of scope.
- Requires `kubectl` access with `get`, `list`, `describe`, and `logs` verbs on `pods` in the target namespace, plus `get` on `configmaps` and `secrets` referenced by the pod.
- `kubectl debug` (ephemeral containers / node debug) requires Kubernetes 1.25+ and cluster permission to create debug pods.
- `kubectl top pod` requires the `metrics-server` add-on to be installed and healthy in `kube-system`.
- Inspection of node-level kernel logs requires either SSH/SSM access to the node or `kubectl debug node/<node>` privilege.

## Diagnostic Steps

### Step 1: Confirm CrashLoopBackOff status and capture restart count

```bash
kubectl get pod <pod-name> -n <namespace> -o wide
```

Expected output: a row showing `STATUS: CrashLoopBackOff` (or `Init:CrashLoopBackOff` for init-container failures) and a non-zero, increasing `RESTARTS` count. Record the count for comparison after remediation.

### Step 2: Read pod description for last termination reason, exit code, and events

```bash
kubectl describe pod <pod-name> -n <namespace>
```

Expected output: a `Containers` section with `State: Waiting / Reason: CrashLoopBackOff`, a `Last State: Terminated` block containing `Reason`, `Exit Code`, `Started`, and `Finished` fields, the pod `Restart Count`, `Liveness` / `Readiness` / `Startup` probe definitions, and an `Events` table at the bottom citing `Back-off restarting failed container`, `Liveness probe failed`, `Failed to pull image`, or `Error: configmap "<name>" not found`.

### Step 3: Read the previous container's stdout/stderr

```bash
kubectl logs <pod-name> -n <namespace> --previous --tail=200
kubectl logs <pod-name> -n <namespace> --previous -c <container-name> --tail=200
```

Expected output: application output from the last (terminated) container instance. Look for stack traces, unhandled exceptions, "address already in use", "permission denied", "no such file or directory", "missing required environment variable", or framework startup messages cut off before "ready to accept connections".

### Step 4: Capture the structured exit code and reason

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"  exitCode="}{.lastState.terminated.exitCode}{"  reason="}{.lastState.terminated.reason}{"  signal="}{.lastState.terminated.signal}{"  message="}{.lastState.terminated.message}{"\n"}{end}'
```

Expected output: one line per container with the exit code, machine-readable reason, signal (if killed), and any termination message written to `/dev/termination-log`. Common combinations: `exitCode=137 reason=OOMKilled` (cgroup OOM), `exitCode=1 reason=Error` (application failure), `exitCode=127 reason=ContainerCannotRun` (missing entrypoint binary), `exitCode=0 reason=Completed` (process exited cleanly — usually a misconfigured daemon).

### Step 5: Verify referenced ConfigMaps and Secrets exist with expected keys

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*].envFrom[*]}{.configMapRef.name}{"\n"}{.secretRef.name}{"\n"}{end}{range .spec.containers[*].env[*]}{.valueFrom.configMapKeyRef.name}{"/"}{.valueFrom.configMapKeyRef.key}{"\n"}{.valueFrom.secretKeyRef.name}{"/"}{.valueFrom.secretKeyRef.key}{"\n"}{end}{range .spec.volumes[*]}{.configMap.name}{"\n"}{.secret.secretName}{"\n"}{end}'
kubectl get configmap,secret -n <namespace>
```

Expected output: list of every ConfigMap / Secret (and specific keys) the pod depends on, followed by what exists in the namespace. Any name in the first command absent from the second is a missing reference.

### Step 6: Confirm liveness, readiness, and startup probe configuration

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  liveness="}{.livenessProbe}{"  startup="}{.startupProbe}{"\n"}{end}'
```

Expected output: per-container JSON dump of probe definitions. Inspect `initialDelaySeconds`, `periodSeconds`, `timeoutSeconds`, `failureThreshold`, and whether a `startupProbe` is present. Compare `initialDelaySeconds * failureThreshold` against the application's known cold-start time.

### Step 7: Measure memory and CPU usage versus configured limits

```bash
kubectl top pod <pod-name> -n <namespace> --containers
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  cpu_lim="}{.resources.limits.cpu}{"  mem_lim="}{.resources.limits.memory}{"\n"}{end}'
```

Expected output: live `MEMORY(bytes)` and `CPU(cores)` per container with the configured limits on the next lines. A `kubectl top` row at or above the limit, combined with `exitCode=137` from Step 4, indicates OOMKilled.

### Step 8: Inspect init-container chain (only if Step 1 showed `Init:CrashLoopBackOff`)

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.initContainerStatuses[*]}{.name}{"  exitCode="}{.lastState.terminated.exitCode}{"  reason="}{.lastState.terminated.reason}{"\n"}{end}'
kubectl logs <pod-name> -n <namespace> -c <init-container-name> --previous --tail=200
```

Expected output: per-init-container exit codes (Step 4 only covers app containers) and the stdout/stderr of the failed init step.

### Step 9: Confirm the image matches the node CPU architecture

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].image}{"\n"}'
kubectl get node <node-name> -o jsonpath='{.status.nodeInfo.architecture}{"\n"}'
# Inspect image manifest:
crane manifest <image>:<tag> | jq '.manifests[]?.platform // .architecture'
```

Expected output: image reference, node CPU architecture (`amd64` or `arm64`), and the architectures published in the image manifest. Mismatch is a frequent CrashLoopBackOff cause on heterogeneous (Graviton + x86) clusters.

### Step 10: Pull node-level kernel log for the killed process

```bash
kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c "dmesg -T | grep -iE 'oom|killed process|segfault|traps:' | tail -40"
```

Expected output: kernel lines such as `Memory cgroup out of memory: Killed process ...` (cgroup OOM kill) or `segfault at ...` / `traps: ...` (native crash). Empty output means the kill was application-level, not kernel-level.

## Causes

### Cause A: Application throws an unhandled error during startup

**Statement:** The application process exits with a non-zero status during initialization because it hits an unhandled exception, a configuration parse error, or a failed downstream connection during boot.

**Mechanism:** When the container's PID 1 returns a non-zero exit status, the container runtime records `reason=Error` with the captured exit code on the pod's container status. The kubelet's restart manager sees a terminated container under `restartPolicy: Always`, schedules a restart after the current backoff window, and increments the restart count. Because the startup error is deterministic (same image, same config), each restart hits the same code path and exits the same way, producing an indefinite loop until either the configuration is fixed or a different image is deployed.

**Indicator:**

- [Step 4] container status reports `exitCode=1` (or any non-zero, non-137 code) with `reason=Error`
<!-- match: {"step": 4, "predicate": "contains", "target": "reason=Error"} -->
- [Step 3] previous-container logs show a stack trace, parse error, or "fatal" log line just before termination
- [Symptom] restart count climbs in lockstep with backoff intervals; container never reaches its normal "ready" log line

**Mitigation:**

- **Risk:** Forcing the deployment back to a known-good image rolls back any new feature work since that release; verify the rollback target is acceptable before issuing.
- **Command:**

  ```bash
  kubectl rollout undo deployment/<deployment-name> -n <namespace>
  ```

- **Duration:** Permanent until the failing change is corrected and re-rolled out.

**Resolution:**

```bash
# 1. Pull the failing log line locally for analysis
kubectl logs <pod-name> -n <namespace> --previous --tail=500 > /tmp/<pod>.log
# 2. Fix the application bug or configuration issue in source, build a new image, and roll out
kubectl set image deployment/<deployment-name> -n <namespace> <container-name>=<image>:<fixed-tag>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Impact:** Single deployment rolled forward in place; rolling-update strategy keeps prior replicas serving until new pods become ready.
**Rollback:** `kubectl rollout undo deployment/<deployment-name> -n <namespace>` reverts to the previous ReplicaSet.

**Verification:** After rollout, `kubectl get pod -l <selector> -n <namespace>` should show `STATUS: Running` and `RESTARTS: 0` for at least 10 minutes (the kubelet's reset threshold for the backoff timer).

### Cause B: Container OOMKilled because memory usage exceeds the configured limit

**Statement:** The container's working set exceeds `resources.limits.memory`, so the cgroup OOM killer sends SIGKILL to the main process every time it reaches the limit.

**Mechanism:** The kubelet writes `limits.memory` into the container's cgroup `memory.max` (cgroup v2) or `memory.limit_in_bytes` (cgroup v1). When the cgroup's anonymous RSS plus accounted page-cache crosses the limit, the kernel selects the highest-`oom_score` process in the cgroup — typically the application — and delivers SIGKILL. The container exits with code `137` (`128 + 9`) and `reason=OOMKilled`. Because the memory ceiling is structural, every restart re-enters the same allocation pattern and is killed again, surfacing as CrashLoopBackOff. See `k8s-oomkilled.md` for full OOM-specific diagnosis and tuning.

**Indicator:**

- [Step 4] container status reports `exitCode=137` and `reason=OOMKilled`
<!-- match: {"step": 4, "predicate": "exit_code", "target": 137} -->
- [Step 7] `kubectl top` shows the container's memory at or above its configured `limits.memory`
- [Step 10] node `dmesg` contains `Memory cgroup out of memory: Killed process ...` naming the container's main process

**Mitigation:**

- **Risk:** Raising `limits.memory` consumes more node capacity and can starve other pods on the node; if the underlying cause is a leak, this only delays the next kill.
- **Command:**

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<new-limit> --requests=memory=<new-request>
  ```

- **Duration:** Safe to leave permanently if sized from observed peak working-set; revisit if traffic patterns change.

**Resolution:**

```bash
# Right-size from observed peak over 7 days plus 25-30% headroom; for managed runtimes (JVM/V8) also set container-aware heap flags. See runbook k8s-oomkilled for memory-leak vs sidecar vs tmpfs differentiation.
kubectl set resources deployment/<deployment-name> -n <namespace> \
  --limits=memory=<peak_bytes_times_1.25> --requests=memory=<peak_bytes>
```

**Impact:** Cluster-wide capacity impact proportional to replica count; the scheduler reschedules pods with the new request, which can disturb bin-packing.
**Rollback:** `kubectl set resources deployment/<deployment-name> -n <namespace> --limits=memory=<previous-limit> --requests=memory=<previous-request>` restores the prior sizing.

**Verification:** After rollout, `kubectl top pod -l <selector> -n <namespace>` working-set should stabilize at least 20% below the new limit for 30 minutes and `RESTARTS` should remain 0.

### Cause C: Liveness probe kills the container before it finishes starting

**Statement:** The liveness probe's `initialDelaySeconds` plus `failureThreshold * periodSeconds` is shorter than the application's cold-start time, so the kubelet kills the container before it can serve its first healthy response.

**Mechanism:** The kubelet starts probing the container after `initialDelaySeconds`. Each failed probe increments a failure counter; on reaching `failureThreshold` the kubelet sends SIGTERM to the container, waits `terminationGracePeriodSeconds`, then sends SIGKILL. The kubelet records a `Liveness probe failed` event followed by `Container failed liveness probe, will be restarted`. The container restarts and hits the same probe window again, producing CrashLoopBackOff that is indistinguishable in logs from a healthy startup — the application is mid-init when killed.

**Indicator:**

- [Step 2] events table contains `Liveness probe failed` immediately followed by `Killing container` and `Container failed liveness probe, will be restarted`
<!-- match: {"step": 2, "predicate": "contains", "target": "Liveness probe failed"} -->
- [Step 6] `livenessProbe.initialDelaySeconds * livenessProbe.failureThreshold` is less than the application's documented startup time and no `startupProbe` is configured
- [Step 3] previous-container logs show the application mid-initialization (loading config, opening DB pool) with no fatal error before termination

**Mitigation:**

- **Risk:** Temporarily removing the liveness probe means a genuinely deadlocked process will not be restarted; only safe while triaging.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/livenessProbe"}]'
  ```

- **Duration:** Hours, not days. Replace with a correctly sized startup probe as soon as possible.

**Resolution:**

```yaml
# Add a startupProbe that gates liveness/readiness while the application boots.
# failureThreshold * periodSeconds must exceed observed worst-case cold start.
startupProbe:
  httpGet:
    path: /healthz
    port: 8080
  failureThreshold: 30
  periodSeconds: 10           # 30 * 10s = 300s max startup window
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  periodSeconds: 10
  failureThreshold: 3
```

**Impact:** Single deployment; rolling update brings the new probe configuration in pod-by-pod.
**Rollback:** `kubectl rollout undo deployment/<deployment-name> -n <namespace>` restores the previous probe configuration.

**Verification:** After rollout, `kubectl describe pod -l <selector> -n <namespace>` events must show `Started container` without any subsequent `Liveness probe failed`, and `RESTARTS=0` after 30 minutes.

### Cause D: Container image is missing or its tag/digest does not exist

**Statement:** The image reference in the pod spec is misspelled, points to a deleted tag, or is on a registry the node cannot reach, so the kubelet repeatedly fails to start the container.

**Mechanism:** When the kubelet cannot pull the image, the pod enters `ImagePullBackOff` or `ErrImagePull` (not strictly CrashLoopBackOff, but the operator-visible loop is the same). When the image is pulled successfully but the container's entrypoint is missing inside the image (e.g. a `CMD` referencing a binary that was not copied in the final stage of a multi-stage build), the runtime exits immediately with code `127` and `reason=ContainerCannotRun` or `StartError`, and the kubelet enters the CrashLoopBackOff restart cycle. See `k8s-imagepullbackoff.md` for image-pull-specific diagnosis.

**Indicator:**

- [Step 2] events table contains `Failed to pull image` or `ErrImagePull` or `manifest unknown` or `not found`
<!-- match: {"step": 2, "predicate": "contains", "target": "Failed to pull image"} -->
- [Step 4] container status reports `exitCode=127` with `reason=ContainerCannotRun` or `StartError`
- [Step 3] previous-container logs are empty or show `exec: "<binary>": executable file not found in $PATH`

**Mitigation:**

- **Risk:** Pinning to `:latest` masks the underlying issue and breaks reproducibility; only acceptable as a stopgap.
- **Command:**

  ```bash
  kubectl set image deployment/<deployment-name> -n <namespace> \
    <container-name>=<image>:<known-good-tag>
  ```

- **Duration:** Until the correct image / entrypoint is rebuilt.

**Resolution:**

```bash
# 1. Confirm the tag exists in the registry
crane ls <image-repo> | grep <expected-tag>
# 2. Fix the Dockerfile entrypoint / push the missing tag / correct the spec
kubectl set image deployment/<deployment-name> -n <namespace> \
  <container-name>=<image>:<correct-tag>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Verification:** `kubectl get pod -l <selector> -n <namespace> -o jsonpath='{.items[*].status.containerStatuses[*].image}'` shows the corrected image and `RESTARTS=0` after the new rollout completes.

### Cause E: Referenced ConfigMap, Secret, or volume does not exist or is missing a key

**Statement:** The pod spec references a ConfigMap, Secret, or volume that has not been created in the namespace, or a key inside it that is missing, so the kubelet cannot configure the container.

**Mechanism:** Before launching the container, the kubelet resolves every `envFrom`, `valueFrom.configMapKeyRef`, `valueFrom.secretKeyRef`, and volume-mounted ConfigMap/Secret listed in the pod spec. Missing names or keys produce `CreateContainerConfigError` (kubelet-level) and the pod cycles without ever entering `Running`. If a volume mount silently overlays an expected directory (e.g. mounting a ConfigMap over `/etc/myapp` masks the image's default config), the container starts but the application exits because the file it expected is missing or empty — surfacing as application-level CrashLoopBackOff with `exitCode=1`.

**Indicator:**

- [Step 2] events table contains `configmap "<name>" not found`, `secret "<name>" not found`, or `couldn't find key "<key>" in ConfigMap`
<!-- match: {"step": 2, "predicate": "contains", "target": "not found"} -->
- [Step 5] one or more referenced ConfigMap/Secret names from the pod spec are absent from the `kubectl get configmap,secret` output
- [Step 3] previous-container logs (if any) show "config file not found", "missing required environment variable", or empty config values

**Mitigation:**

- **Risk:** Creating a stub ConfigMap/Secret with placeholder values can satisfy the kubelet but will fail at runtime if the application validates the values; flag the stub clearly.
- **Command:**

  ```bash
  kubectl create configmap <name> -n <namespace> --from-literal=<key>=<placeholder>
  # OR for secrets:
  kubectl create secret generic <name> -n <namespace> --from-literal=<key>=<placeholder>
  ```

- **Duration:** Hours, only while the correct config is being prepared.

**Resolution:**

```bash
# 1. Apply the correct ConfigMap/Secret manifest
kubectl apply -f <configmap-or-secret>.yaml
# 2. Trigger a rollout so pods pick up the new resource (ConfigMap changes do not auto-reload existing pods)
kubectl rollout restart deployment/<deployment-name> -n <namespace>
```

**Impact:** Namespace-scoped; rolling restart cycles all pods of the deployment, briefly halving available replicas during the roll.
**Rollback:** `kubectl delete configmap/<name> -n <namespace>` (or `kubectl rollout undo deployment/<deployment-name>`) reverts to the pre-fix state.

**Verification:** After rollout, `kubectl describe pod -l <selector> -n <namespace>` must contain no `CreateContainerConfigError` event, and `RESTARTS=0` after 10 minutes.

### Cause F: Init container exits non-zero, blocking the main container

**Statement:** An init container exits with a non-zero status — typically because a dependency it waits for is unavailable or a migration step fails — so the kubelet never starts the main container and the pod cycles with `Init:CrashLoopBackOff`.

**Mechanism:** Init containers run sequentially before any main container; the kubelet requires each to exit zero before progressing. When an init container exits non-zero, the kubelet records the failure on `initContainerStatuses`, applies the standard restart backoff, and retries the same init step. The pod status reports `Init:CrashLoopBackOff` (the prefix distinguishes it from a main-container CrashLoopBackOff). Common init-step failures: DB migration tool times out against an unreachable database, a wait-for-service script never sees its dependency become ready, or a chown step fails on a read-only volume.

**Indicator:**

- [Step 1] pod status string is `Init:CrashLoopBackOff` or `Init:Error`
<!-- match: {"step": 1, "predicate": "contains", "target": "Init:CrashLoopBackOff"} -->
- [Step 8] one or more entries in `initContainerStatuses` show `exitCode!=0`
- [Step 8] init-container previous-instance logs show the specific failure (connection refused, migration error, permission denied)

**Mitigation:**

- **Risk:** Temporarily disabling an init container can mask data-integrity steps (migrations, schema checks); only safe for wait-for-dependency probes, never for state-mutating init steps.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/initContainers"}]'
  ```

- **Duration:** Minutes-to-hours while triaging the dependency.

**Resolution:**

```bash
# Fix the dependency the init container is waiting on (start the DB, fix the migration, correct the wait script),
# then trigger a rollout to retry the init chain.
kubectl rollout restart deployment/<deployment-name> -n <namespace>
kubectl get pod -l <selector> -n <namespace> -w
```

**Verification:** `kubectl get pod -l <selector> -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.phase}{"  init="}{range .status.initContainerStatuses[*]}{.ready}{","}{end}{"\n"}{end}'` shows all init containers `ready=true` and pod phase `Running` for at least 10 minutes.

### Cause G: Application binds to a port already in use inside the pod

**Statement:** Two containers in the same pod (or the same container restarted before the kernel released its socket) attempt to listen on the same TCP port, so the bind syscall returns `EADDRINUSE` and the application exits.

**Mechanism:** Containers in a pod share the same network namespace, which means they share the same TCP/UDP port space. If a sidecar (proxy, metrics exporter) and the application both try to bind `:8080`, the second to start fails with `address already in use`. The application logs the bind error and exits non-zero; the kubelet restarts it; the same race recurs. The variant is a single container whose previous instance held the port in `TIME_WAIT` — `SO_REUSEADDR` is not set, so the new instance fails until the kernel reaps the socket (60-120s), producing intermittent CrashLoopBackOff that resolves after a few backoff cycles.

**Indicator:**

- [Step 3] previous-container logs contain `bind: address already in use`, `EADDRINUSE`, or `listen tcp :<port>: bind: address already in use`
<!-- match: {"step": 3, "predicate": "contains", "target": "address already in use"} -->
- [Step 2] pod has two or more containers in `spec.containers` whose declared `containerPort` values overlap
- [Symptom] restart loop sometimes resolves on its own after 1-2 minutes (kernel TIME_WAIT expiry) but recurs on every redeploy

**Mitigation:**

- **Risk:** Scaling the deployment to zero and back to one releases all sockets but interrupts service; do only during a maintenance window or for non-tier-1 workloads.
- **Command:**

  ```bash
  kubectl scale deployment/<deployment-name> -n <namespace> --replicas=0
  kubectl scale deployment/<deployment-name> -n <namespace> --replicas=<original>
  ```

- **Duration:** Single-cycle hold (seconds to minutes).

**Resolution:**

```bash
# Either change the sidecar's listen port or enable SO_REUSEADDR in the application,
# then redeploy. Confirm the resulting port plan has no overlaps.
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  ports="}{.ports}{"\n"}{end}'
kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<fixed-tag>
```

**Verification:** After rollout, `kubectl logs <pod-name> -n <namespace> -c <container>` shows the application's "listening on :<port>" log line without errors and `RESTARTS=0` after 15 minutes.

### Cause H: Volume mount obscures or has wrong permissions on a path the app needs

**Statement:** A volume mount overlays a directory the application expects to contain image content, or the volume's filesystem permissions block the container's user from reading/writing required files, so the application exits at startup.

**Mechanism:** Volume mounts cover any path inside the container with the volume's contents. Mounting an `emptyDir`, ConfigMap, or PV at `/var/lib/myapp` replaces whatever the image baked there; if the application expects schema files, default configs, or executable plugins from the image at that path, it cannot find them. Separately, when `securityContext.runAsNonRoot: true` or `runAsUser` is set, the container process may lack permission on the mounted volume (especially `hostPath` or NFS-backed PVs) and fails with `permission denied` on its data directory. Both manifest as application-level CrashLoopBackOff with `exitCode=1`.

**Indicator:**

- [Step 3] previous-container logs contain `permission denied`, `read-only file system`, `no such file or directory` referencing a path declared in `volumeMounts`
<!-- match: {"step": 3, "predicate": "contains", "target": "permission denied"} -->
- [Step 2] `Mounts` block lists a volume at a path that overlaps the image's expected runtime data directory
- [Step 3] previous-container logs show the application starting but exiting immediately after touching its data path

**Mitigation:**

- **Risk:** Setting `fsGroup` or making the volume world-writable widens the security boundary; only acceptable for workloads with non-sensitive data.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=strategic \
    -p='{"spec":{"template":{"spec":{"securityContext":{"fsGroup":1000}}}}}'
  ```

- **Duration:** Permanent once verified, but revisit if the workload's threat model changes.

**Resolution:**

```bash
# Option 1: mount the volume at a non-overlapping subPath so image content at the parent is preserved
# Option 2: set securityContext.fsGroup so the kubelet chowns the volume on first mount
# Option 3: switch to an initContainer that pre-populates the volume from the image, then mount in the main container
kubectl edit deployment <deployment-name> -n <namespace>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Verification:** `kubectl exec <pod-name> -n <namespace> -- ls -la <mount-path>` returns the expected content with appropriate ownership, and `RESTARTS=0` after 15 minutes.

### Cause I: Container image architecture does not match the node's CPU architecture

**Statement:** The image was built for `amd64` (or `arm64`) and the pod was scheduled onto a node of the opposite architecture, so the container runtime cannot execute the entrypoint binary.

**Mechanism:** Container runtimes verify image-manifest architecture against the host before running. When a single-arch image is scheduled to an incompatible node, runtime behavior differs: containerd reports `no match for platform in manifest` at pull time; Docker/Moby may pull a stale cached layer and surface `exec format error` at exec time, exiting `exitCode=1` immediately. On heterogeneous clusters (mixed Graviton/x86 nodes, mixed Apple Silicon dev machines pushing to x86 clusters), pods scheduled to the wrong-arch node CrashLoopBackOff while pods on the matching-arch node run normally — making this a hard-to-spot, partial-fleet issue.

**Indicator:**

- [Step 3] previous-container logs contain `exec format error` or `exec /<binary>: exec format error`
<!-- match: {"step": 3, "predicate": "contains", "target": "exec format error"} -->
- [Step 9] image manifest architectures do not include the node's `nodeInfo.architecture`
- [Symptom] same pod template runs on some nodes but CrashLoopBackOffs on others

**Mitigation:**

- **Risk:** Pinning to one architecture via `nodeSelector` reduces scheduling flexibility and may push pressure onto a smaller node pool.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=strategic \
    -p='{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}}}'
  ```

- **Duration:** Permanent until a multi-arch image is published.

**Resolution:**

```bash
# Build and push a multi-arch manifest list so the right variant runs on each node.
docker buildx build --platform=linux/amd64,linux/arm64 -t <image>:<tag> --push .
kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<tag>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Impact:** Cluster-wide for the deployment; existing pods on matching-arch nodes also restart during the rollout.
**Rollback:** `kubectl rollout undo deployment/<deployment-name> -n <namespace>` restores the prior (single-arch) image.

**Verification:** `kubectl get pod -l <selector> -n <namespace> -o wide` shows pods running on nodes of both architectures with `RESTARTS=0` after 15 minutes.

### Cause J: Pod exits cleanly with code 0 but `restartPolicy: Always` keeps restarting it

**Statement:** The container's entrypoint completes normally and returns exit code 0, but the controller's `restartPolicy: Always` (Deployments, StatefulSets, DaemonSets) treats any termination — even successful — as a failure and restarts the container.

**Mechanism:** `restartPolicy: Always` is the default for long-running workloads and instructs the kubelet to restart a container regardless of exit status. When the container's PID 1 is a short-lived process (a one-shot script, a CLI tool, a wrapper that forks a daemon then exits), it returns 0 within seconds; the kubelet restarts it; the pattern repeats. The pod shows `STATUS: CrashLoopBackOff` because the kubelet's view is "container terminated and is being restarted with backoff," even though the application reports success. Common shapes: shell entrypoint that `exec`s in the background, a binary that prints a help message and exits, a `kubectl apply` job template re-used as a Deployment.

**Indicator:**

- [Step 4] container status reports `exitCode=0` with `reason=Completed`
<!-- match: {"step": 4, "predicate": "contains", "target": "exitCode=0  reason=Completed"} -->
- [Step 3] previous-container logs show the process completing successfully (no errors, no stack traces)
- [Step 2] workload kind is `Deployment`, `StatefulSet`, or `DaemonSet` (which force `restartPolicy: Always`) but the entrypoint is short-lived

**Mitigation:**

- **Risk:** Converting a Deployment to a Job changes its lifecycle semantics (no rolling update, no replica autoscaling); only correct if the workload is genuinely batch.
- **Command:**

  ```bash
  kubectl get deployment <deployment-name> -n <namespace> -o yaml > /tmp/<name>.yaml
  # Edit to kind: Job and apply the new manifest; delete the old Deployment.
  ```

- **Duration:** Permanent — the workload kind should match its lifecycle.

**Resolution:**

```bash
# Option A: Fix the entrypoint so it stays in the foreground (`exec <daemon>` without `&`).
kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<fixed-tag>
# Option B: Convert to a Job if the workload is meant to run once.
kubectl delete deployment <deployment-name> -n <namespace>
kubectl apply -f <job-manifest>.yaml
```

**Verification:** For a corrected Deployment, `kubectl get pod -l <selector> -n <namespace>` shows `STATUS: Running` with `RESTARTS=0` for at least 30 minutes; for a converted Job, `kubectl get job` shows `COMPLETIONS: 1/1`.

### Cause Z: Unidentified

**Statement:** The container is repeatedly terminating and restarting but no indicator from Causes A through J matches the gathered evidence.

**Mechanism:** The kubelet records repeated terminations and applies backoff, producing `STATUS: CrashLoopBackOff`. The captured exit code, reason, events, logs, and resource metrics do not isolate the failure path — typically because the application is silently exiting before producing usable logs, the failure is intermittent across restarts, or the symptom involves a less common cause (CNI initialization failure, admission webhook side-effect, kernel module mismatch, kube-proxy iptables corruption). Further isolation requires richer signals: live container tracing, audit logs around pod creation, node kubelet logs, or comparing against a known-good baseline pod on the same node.

**Indicator:**

- [Default] CrashLoopBackOff confirmed (Step 1, Step 2) but Causes A–J indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Rolling back to a previous known-good revision can mask data-integrity changes if the new revision included migrations; verify the diff before reverting.
- **Command:**

  ```bash
  kubectl rollout undo deployment/<deployment-name> -n <namespace>
  kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector involvedObject.name=<pod-name>
  ```

- **Duration:** Use only as a holding action while engaging the application owner with the gathered diagnostic artefacts.

**Resolution:** Out of runbook scope. Capture the artefacts from Steps 1–10 (pod description, previous-container logs, structured exit code/reason, ConfigMap/Secret inventory, probe config, resource usage, init-container output, image architecture, kernel dmesg) and escalate to the application owner or platform on-call with the failure-mode summary.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Configure a `startupProbe` for every workload with cold-start time longer than 30 seconds. Set `failureThreshold * periodSeconds` to at least 1.5× the observed p99 startup time so liveness/readiness probes do not engage before the application is ready.
- Set both `requests` and `limits` for memory and CPU on every container. Enforce defaults cluster-wide via a `LimitRange` per namespace so workloads cannot be deployed without limits.
- Validate ConfigMap and Secret references before deploying. CI-level kustomize/helm rendering should fail the pipeline when a referenced name is not in the manifest set.
- Publish multi-arch image manifests (`docker buildx build --platform=linux/amd64,linux/arm64`) for any cluster that includes Graviton, Apple Silicon, or mixed-arch node pools.
- Adopt structured termination messages: have the application write a one-line summary to `/dev/termination-log` on fatal exit, and set `terminationMessagePolicy: FallbackToLogsOnError` so the message is captured even when the file is empty.
- Alert on `RESTARTS` increases. A Prometheus rule like `increase(kube_pod_container_status_restarts_total{namespace="<ns>"}[10m]) > 2` catches CrashLoopBackOff before the backoff fully expands.
- Forbid `:latest` and untagged image references via an admission policy (OPA Gatekeeper / Kyverno) so a deleted upstream tag cannot break new pod schedules.
- Run image-architecture validation in CI: compare the published manifest's platform list against the cluster's node-pool architecture mix.
- Use `kubectl rollout status` with `--timeout` in CI/CD so a CrashLoopBackOff is caught at deploy time, not after replicas have already cycled out the healthy ReplicaSet.

## Sources

- [Kubernetes — Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) — Priority 1. Container states (Waiting, Running, Terminated), terminated reasons (Error, OOMKilled, ContainerCannotRun), restartPolicy semantics, CrashLoopBackOff display.
- [Kubernetes — Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) — Priority 1. `kubectl describe pod`, restart count interpretation, event log fields, debugging procedure for crashing pods.
- [Kubernetes — Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/) — Priority 1. `kubectl logs --previous`, ephemeral debug containers, `kubectl debug node`, and copying files in/out of failed containers.
- [Kubernetes — Debug Pods (general)](https://kubernetes.io/docs/tasks/debug/debug-application/) — Priority 1. Overall diagnostic flow for unhealthy pods, where CrashLoopBackOff documentation is rooted.
- [Kubernetes — Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) — Priority 1. Probe configuration parameters (initialDelaySeconds, periodSeconds, failureThreshold), startup-probe pattern for slow-starting apps, examples of aggressive probes causing restart loops.
- [Kubernetes — Determine the Reason for Pod Failure](https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/) — Priority 1. `terminationMessagePath`, `terminationMessagePolicy: FallbackToLogsOnError`, `lastState.terminated` fields (exitCode, reason, signal, message).
- [Kubernetes — ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/) — Priority 1. envFrom / valueFrom.configMapKeyRef / volume references, `CreateContainerConfigError` failure mode, namespace and key-name constraints.
- [AWS — Amazon EKS Troubleshooting](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html) — Priority 1 (vendor). EKS-specific CrashLoopBackOff contributors (aws-auth ConfigMap, IRSA token expiry, CNI not ready), exit-code interpretation on EKS optimized AMIs.
