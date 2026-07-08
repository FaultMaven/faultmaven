---
id: k8s-crashloopbackoff
title: "Kubernetes Pod CrashLoopBackOff"
domain: compute
service: kubernetes
symptom_class:
  - crash_loop
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
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

### Step 8: Inspect init-container chain when status shows Init:CrashLoopBackOff

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

**Statement:** The application process exits non-zero during initialization because of an unhandled exception, a configuration parse error, or a failed downstream connection during boot.

**Chain:**
- root: An unhandled exception, config parse error, or failed downstream connection during boot makes PID 1 exit non-zero.
- s1: The container runtime records `reason=Error` with the captured exit code on the pod's container status.
- s2: The kubelet's restart manager sees a terminated container under `restartPolicy: Always` and schedules a restart after the backoff window, incrementing the restart count.
- s3: Because the startup error is deterministic, each restart hits the same code path and exits the same way.
- D: The pod loops indefinitely as CrashLoopBackOff (Symptom Recognition).

**Indicators:**
- root: [Step 3] previous-container logs show a stack trace, parse error, or "fatal" log line just before termination.
- s1: [Step 4] container status reports `exitCode=1` (or any non-zero, non-137 code) with `reason=Error`.
- D: [Symptom] restart count climbs in lockstep with backoff intervals; container never reaches its normal "ready" log line.

**Interventions:**
- **remediation** (root): Fix the application bug or configuration in source, build a new image, and roll forward in place; rolling-update keeps prior replicas serving until new pods become ready.

  ```bash
  # 1. Pull the failing log line locally for analysis
  kubectl logs <pod-name> -n <namespace> --previous --tail=500 > /tmp/<pod>.log
  # 2. Fix the application bug or configuration issue in source, build a new image, and roll out
  kubectl set image deployment/<deployment-name> -n <namespace> <container-name>=<image>:<fixed-tag>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl get pod -l <selector> -n <namespace>` shows `STATUS: Running` and `RESTARTS: 0` for at least 10 minutes (the kubelet's backoff reset threshold).
- **mitigation** (root): Roll the deployment back to the previous known-good ReplicaSet.

  ```bash
  kubectl rollout undo deployment/<deployment-name> -n <namespace>
  ```

  **Risk:** Rolling back to a known-good image discards any new feature work since that release; verify the rollback target is acceptable before issuing. **Duration:** Permanent until the failing change is corrected and re-rolled out. **Verification:** rolled-back pods show `RESTARTS: 0` and reach their "ready" log line.

### Cause B: Container OOMKilled because memory usage exceeds the configured limit

**Statement:** The container's working set exceeds `resources.limits.memory`, so the cgroup OOM killer sends SIGKILL to the main process every time it reaches the limit.

**Chain:**
- root: The container's working set exceeds `resources.limits.memory`.
- s1: The kubelet wrote `limits.memory` into the cgroup `memory.max` (v2) / `memory.limit_in_bytes` (v1); crossing it triggers the kernel OOM killer.
- s2: The kernel selects the highest-`oom_score` process — typically the application — and delivers SIGKILL.
- s3: The container exits with code `137` (`128 + 9`) and `reason=OOMKilled`; the structural ceiling makes every restart re-enter the same allocation pattern.
- D: The pod loops as CrashLoopBackOff (Symptom Recognition). See `k8s-oomkilled.md` for full OOM-specific diagnosis and tuning.

**Indicators:**
- root: [Step 7] `kubectl top` shows the container's memory at or above its configured `limits.memory`.
- s2: [Step 10] node `dmesg` contains `Memory cgroup out of memory: Killed process ...` naming the container's main process.
- s3: [Step 4] container status reports `exitCode=137` and `reason=OOMKilled`.

**Interventions:**
- **remediation** (root): Right-size `limits.memory` from observed peak over 7 days plus 25-30% headroom; for managed runtimes (JVM/V8) also set container-aware heap flags.

  ```bash
  # Right-size from observed peak over 7 days plus 25-30% headroom; for managed runtimes (JVM/V8) also set container-aware heap flags. See runbook k8s-oomkilled for memory-leak vs sidecar vs tmpfs differentiation.
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<peak_bytes_times_1.25> --requests=memory=<peak_bytes>
  ```

  **Verification:** `kubectl top pod -l <selector> -n <namespace>` working-set stabilizes at least 20% below the new limit for 30 minutes and `RESTARTS` remains 0.
- **mitigation** (root): Raise `limits.memory` as a stopgap to stop the immediate OOM kills.

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --limits=memory=<new-limit> --requests=memory=<new-request>
  ```

  **Risk:** More node capacity is consumed and can starve other pods; if the cause is a leak, this only delays the next kill. **Duration:** Safe to leave permanently if sized from observed peak working-set; revisit if traffic patterns change. **Verification:** working-set stays below the new limit and `RESTARTS` stops climbing.

### Cause C: Liveness probe kills the container before it finishes starting

**Statement:** The liveness probe's `initialDelaySeconds` plus `failureThreshold * periodSeconds` is shorter than the application's cold-start time, so the kubelet kills the container before it serves its first healthy response.

**Chain:**
- root: The liveness probe budget (`initialDelaySeconds + failureThreshold * periodSeconds`) is shorter than the application's cold-start time, with no `startupProbe` configured.
- s1: The kubelet starts probing after `initialDelaySeconds` and each failed probe increments a failure counter while the app is still mid-init.
- s2: On reaching `failureThreshold` the kubelet sends SIGTERM, waits `terminationGracePeriodSeconds`, then SIGKILL, logging `Liveness probe failed` and `Container failed liveness probe, will be restarted`.
- s3: The container restarts and hits the same probe window again.
- D: The pod loops as CrashLoopBackOff, indistinguishable in logs from a healthy startup (Symptom Recognition).

**Indicators:**
- root: [Step 6] `livenessProbe.initialDelaySeconds * livenessProbe.failureThreshold` is less than the application's documented startup time and no `startupProbe` is configured.
- s1: [Step 3] previous-container logs show the application mid-initialization (loading config, opening DB pool) with no fatal error before termination.
- s2: [Step 2] events table contains `Liveness probe failed` immediately followed by `Killing container` and `Container failed liveness probe, will be restarted`.

**Interventions:**
- **defensive_fix** (root): Add a `startupProbe` that gates liveness/readiness while the application boots; its `failureThreshold * periodSeconds` must exceed worst-case cold start.

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

  **Verification:** `kubectl describe pod -l <selector> -n <namespace>` events show `Started container` without any subsequent `Liveness probe failed`, and `RESTARTS=0` after 30 minutes.
- **mitigation** (s2): Temporarily remove the liveness probe to stop the kubelet killing the still-starting container.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/livenessProbe"}]'
  ```

  **Risk:** A genuinely deadlocked process will not be restarted; only safe while triaging. **Duration:** Hours, not days. Replace with a correctly sized startup probe as soon as possible. **Verification:** the container reaches its "ready" log line and `RESTARTS` stops climbing.

### Cause D: Container image is missing or its tag/digest does not exist

**Statement:** The image reference in the pod spec is misspelled, points to a deleted tag, or is on a registry the node cannot reach, so the kubelet repeatedly fails to start the container.

**Chain:**
- root: The image reference is misspelled, points to a deleted tag, or is on an unreachable registry — or its entrypoint binary is missing inside the image.
- s1: When the image cannot be pulled the pod enters `ImagePullBackOff`/`ErrImagePull`; when pulled but the entrypoint is missing the runtime exits code `127` with `reason=ContainerCannotRun` or `StartError`.
- s2: The kubelet enters the restart cycle, retrying the same failing pull or exec each time.
- D: The pod loops as CrashLoopBackOff (operator-visible loop is the same; Symptom Recognition). See `k8s-imagepullbackoff.md` for image-pull-specific diagnosis.

**Indicators:**
- root: [Step 3] previous-container logs are empty or show `exec: "<binary>": executable file not found in $PATH`.
- s1: [Step 4] container status reports `exitCode=127` with `reason=ContainerCannotRun` or `StartError`.
- s1: [Step 2] events table contains `Failed to pull image` or `ErrImagePull` or `manifest unknown` or `not found`.

**Interventions:**
- **remediation** (root): Confirm the tag exists, fix the Dockerfile entrypoint / push the missing tag / correct the spec, then roll out the correct image.

  ```bash
  # 1. Confirm the tag exists in the registry
  crane ls <image-repo> | grep <expected-tag>
  # 2. Fix the Dockerfile entrypoint / push the missing tag / correct the spec
  kubectl set image deployment/<deployment-name> -n <namespace> \
    <container-name>=<image>:<correct-tag>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl get pod -l <selector> -n <namespace> -o jsonpath='{.items[*].status.containerStatuses[*].image}'` shows the corrected image and `RESTARTS=0` after the new rollout completes.
- **mitigation** (root): Pin to a known-good tag as a stopgap to get the pod running again.

  ```bash
  kubectl set image deployment/<deployment-name> -n <namespace> \
    <container-name>=<image>:<known-good-tag>
  ```

  **Risk:** Pinning to `:latest` masks the underlying issue and breaks reproducibility; only acceptable as a stopgap. **Duration:** Until the correct image / entrypoint is rebuilt. **Verification:** pod reaches `Running` on the known-good tag with `RESTARTS=0`.

### Cause E: Referenced ConfigMap, Secret, or volume does not exist or is missing a key

**Statement:** The pod spec references a ConfigMap, Secret, or volume that has not been created in the namespace, or a key inside it that is missing, so the kubelet cannot configure the container.

**Chain:**
- root: The pod spec references a ConfigMap, Secret, volume, or key that does not exist in the namespace.
- s1: The kubelet resolves every `envFrom`/`valueFrom`/volume reference before launch and a missing name or key produces `CreateContainerConfigError`, cycling the pod without ever reaching `Running`.
- s2: Alternatively a volume mount silently overlays an expected directory (e.g. ConfigMap over `/etc/myapp`), so the container starts but the app exits because its file is missing or empty.
- D: The pod loops as CrashLoopBackOff (kubelet-level config error, or application-level `exitCode=1`; Symptom Recognition).

**Indicators:**
- root: [Step 5] one or more referenced ConfigMap/Secret names from the pod spec are absent from the `kubectl get configmap,secret` output.
- s1: [Step 2] events table contains `configmap "<name>" not found`, `secret "<name>" not found`, or `couldn't find key "<key>" in ConfigMap`.
- s2: [Step 3] previous-container logs (if any) show "config file not found", "missing required environment variable", or empty config values.

**Interventions:**
- **remediation** (root): Apply the correct ConfigMap/Secret manifest, then trigger a rollout so pods pick up the new resource (ConfigMap changes do not auto-reload existing pods).

  ```bash
  # 1. Apply the correct ConfigMap/Secret manifest
  kubectl apply -f <configmap-or-secret>.yaml
  # 2. Trigger a rollout so pods pick up the new resource (ConfigMap changes do not auto-reload existing pods)
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl describe pod -l <selector> -n <namespace>` contains no `CreateContainerConfigError` event, and `RESTARTS=0` after 10 minutes.
- **mitigation** (s1): Create a stub ConfigMap/Secret with placeholder values to satisfy the kubelet while the correct config is prepared.

  ```bash
  kubectl create configmap <name> -n <namespace> --from-literal=<key>=<placeholder>
  # OR for secrets:
  kubectl create secret generic <name> -n <namespace> --from-literal=<key>=<placeholder>
  ```

  **Risk:** A stub can satisfy the kubelet but fail at runtime if the application validates the values; flag the stub clearly. **Duration:** Hours, only while the correct config is being prepared. **Verification:** the `CreateContainerConfigError` event clears and the container starts.

### Cause F: Init container exits non-zero, blocking the main container

**Statement:** An init container exits non-zero — typically because a dependency it waits for is unavailable or a migration step fails — so the kubelet never starts the main container and the pod cycles with `Init:CrashLoopBackOff`.

**Chain:**
- root: An init container exits non-zero — e.g. a DB migration times out against an unreachable database, a wait-for-service script never sees its dependency, or a chown step fails on a read-only volume.
- s1: Init containers run sequentially and the kubelet requires each to exit zero before progressing, so the main container never starts.
- s2: The kubelet records the failure on `initContainerStatuses`, applies the standard restart backoff, and retries the same init step.
- D: The pod cycles as `Init:CrashLoopBackOff` (prefix distinguishes it from a main-container loop; Symptom Recognition).

**Indicators:**
- root: [Step 8] one or more entries in `initContainerStatuses` show `exitCode!=0`.
- root: [Step 8] init-container previous-instance logs show the specific failure (connection refused, migration error, permission denied).
- D: [Step 1] pod status string is `Init:CrashLoopBackOff` or `Init:Error`.

**Interventions:**
- **remediation** (root): Fix the dependency the init container waits on (start the DB, fix the migration, correct the wait script), then trigger a rollout to retry the init chain.

  ```bash
  # Fix the dependency the init container is waiting on (start the DB, fix the migration, correct the wait script),
  # then trigger a rollout to retry the init chain.
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  kubectl get pod -l <selector> -n <namespace> -w
  ```

  **Verification:** `kubectl get pod -l <selector> -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.phase}{"  init="}{range .status.initContainerStatuses[*]}{.ready}{","}{end}{"\n"}{end}'` shows all init containers `ready=true` and pod phase `Running` for at least 10 minutes.
- **mitigation** (s1): Temporarily remove the init container to let the main container start while triaging the dependency.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/initContainers"}]'
  ```

  **Risk:** Disabling an init container can mask data-integrity steps (migrations, schema checks); only safe for wait-for-dependency probes, never for state-mutating init steps. **Duration:** Minutes-to-hours while triaging the dependency. **Verification:** the main container starts and reaches its "ready" state.

### Cause G: Application binds to a port already in use inside the pod

**Statement:** Two containers in the same pod (or the same container restarted before the kernel released its socket) attempt to listen on the same TCP port, so the bind syscall returns `EADDRINUSE` and the application exits.

**Chain:**
- root: A sidecar and the application (or a restarted instance still holding a `TIME_WAIT` socket without `SO_REUSEADDR`) both target the same TCP port in the pod's shared network namespace.
- s1: The second listener's bind syscall returns `EADDRINUSE`; the application logs the bind error and exits non-zero.
- s2: The kubelet restarts the container and the same port race recurs (or resolves once the kernel reaps the `TIME_WAIT` socket after 60-120s).
- D: The pod loops as CrashLoopBackOff, sometimes self-resolving then recurring on redeploy (Symptom Recognition).

**Indicators:**
- root: [Step 2] pod has two or more containers in `spec.containers` whose declared `containerPort` values overlap.
- s1: [Step 3] previous-container logs contain `bind: address already in use`, `EADDRINUSE`, or `listen tcp :<port>: bind: address already in use`.
- D: [Symptom] restart loop sometimes resolves on its own after 1-2 minutes (kernel TIME_WAIT expiry) but recurs on every redeploy.

**Interventions:**
- **remediation** (root): Change the sidecar's listen port or enable `SO_REUSEADDR` in the application, confirm the port plan has no overlaps, then redeploy.

  ```bash
  # Either change the sidecar's listen port or enable SO_REUSEADDR in the application,
  # then redeploy. Confirm the resulting port plan has no overlaps.
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  ports="}{.ports}{"\n"}{end}'
  kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<fixed-tag>
  ```

  **Verification:** `kubectl logs <pod-name> -n <namespace> -c <container>` shows the application's "listening on :<port>" log line without errors and `RESTARTS=0` after 15 minutes.
- **mitigation** (s2): Scale the deployment to zero and back to release all held sockets.

  ```bash
  kubectl scale deployment/<deployment-name> -n <namespace> --replicas=0
  kubectl scale deployment/<deployment-name> -n <namespace> --replicas=<original>
  ```

  **Risk:** Scaling to zero releases all sockets but interrupts service; do only during a maintenance window or for non-tier-1 workloads. **Duration:** Single-cycle hold (seconds to minutes). **Verification:** the application binds its port cleanly and `RESTARTS` stops climbing.

### Cause H: Volume mount obscures or has wrong permissions on a path the app needs

**Statement:** A volume mount overlays a directory the application expects to contain image content, or the volume's filesystem permissions block the container's user from reading/writing required files, so the application exits at startup.

**Chain:**
- root: A volume mount overlays a path the image baked content into, or its filesystem permissions block the container's user (e.g. `runAsNonRoot`/`runAsUser` on a `hostPath`/NFS-backed PV).
- s1: The application cannot find its schema files / default configs / plugins, or hits `permission denied` on its data directory.
- s2: The application exits at startup with `exitCode=1`.
- s3: The kubelet restarts it and the same mount/permission condition recurs.
- D: The pod loops as application-level CrashLoopBackOff (Symptom Recognition).

**Indicators:**
- root: [Step 2] `Mounts` block lists a volume at a path that overlaps the image's expected runtime data directory.
- s1: [Step 3] previous-container logs contain `permission denied`, `read-only file system`, or `no such file or directory` referencing a path declared in `volumeMounts`.
- s2: [Step 3] previous-container logs show the application starting but exiting immediately after touching its data path.

**Interventions:**
- **remediation** (root): Mount at a non-overlapping `subPath`, set `securityContext.fsGroup` so the kubelet chowns the volume, or pre-populate it via an init container.

  ```bash
  # Option 1: mount the volume at a non-overlapping subPath so image content at the parent is preserved
  # Option 2: set securityContext.fsGroup so the kubelet chowns the volume on first mount
  # Option 3: switch to an initContainer that pre-populates the volume from the image, then mount in the main container
  kubectl edit deployment <deployment-name> -n <namespace>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl exec <pod-name> -n <namespace> -- ls -la <mount-path>` returns the expected content with appropriate ownership, and `RESTARTS=0` after 15 minutes.
- **mitigation** (s1): Set `fsGroup` so the kubelet chowns the volume, unblocking the permission failure.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=strategic \
    -p='{"spec":{"template":{"spec":{"securityContext":{"fsGroup":1000}}}}}'
  ```

  **Risk:** Setting `fsGroup` or making the volume world-writable widens the security boundary; only acceptable for workloads with non-sensitive data. **Duration:** Permanent once verified, but revisit if the workload's threat model changes. **Verification:** the application reads/writes its data path without `permission denied` and `RESTARTS=0`.

### Cause I: Container image architecture does not match the node's CPU architecture

**Statement:** The image was built for `amd64` (or `arm64`) and the pod was scheduled onto a node of the opposite architecture, so the container runtime cannot execute the entrypoint binary.

**Chain:**
- root: A single-arch image is scheduled onto a node of the opposite CPU architecture on a heterogeneous cluster.
- s1: The runtime cannot execute the entrypoint — containerd reports `no match for platform in manifest` at pull time; Docker/Moby surfaces `exec format error` at exec time, exiting `exitCode=1` immediately.
- s2: The kubelet restarts the container on the same wrong-arch node and the exec fails again.
- D: The pod CrashLoopBackOffs on wrong-arch nodes while pods on matching-arch nodes run normally (partial-fleet; Symptom Recognition).

**Indicators:**
- root: [Step 9] image manifest architectures do not include the node's `nodeInfo.architecture`.
- s1: [Step 3] previous-container logs contain `exec format error` or `exec /<binary>: exec format error`.
- D: [Symptom] same pod template runs on some nodes but CrashLoopBackOffs on others.

**Interventions:**
- **remediation** (root): Build and push a multi-arch manifest list so the right variant runs on each node, then roll out.

  ```bash
  # Build and push a multi-arch manifest list so the right variant runs on each node.
  docker buildx build --platform=linux/amd64,linux/arm64 -t <image>:<tag> --push .
  kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<tag>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl get pod -l <selector> -n <namespace> -o wide` shows pods running on nodes of both architectures with `RESTARTS=0` after 15 minutes.
- **mitigation** (root): Pin the workload to the image's architecture via `nodeSelector` so it only schedules onto compatible nodes.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=strategic \
    -p='{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}}}'
  ```

  **Risk:** Pinning to one architecture reduces scheduling flexibility and may push pressure onto a smaller node pool. **Duration:** Permanent until a multi-arch image is published. **Verification:** all pods schedule onto matching-arch nodes and run with `RESTARTS=0`.

### Cause J: Pod exits cleanly with code 0 but restartPolicy Always keeps restarting it

**Statement:** The container's entrypoint completes normally and returns exit code 0, but the controller's `restartPolicy: Always` treats any termination — even successful — as a failure and restarts the container.

**Chain:**
- root: A short-lived entrypoint (one-shot script, CLI tool, or wrapper that forks a daemon then exits) is run under a controller that forces `restartPolicy: Always`.
- s1: The container's PID 1 returns exit code 0 within seconds of starting.
- s2: `restartPolicy: Always` instructs the kubelet to restart the container regardless of exit status, and the pattern repeats.
- D: The pod shows CrashLoopBackOff ("container terminated and is being restarted with backoff") even though the application reports success (Symptom Recognition).

**Indicators:**
- root: [Step 2] workload kind is `Deployment`, `StatefulSet`, or `DaemonSet` (which force `restartPolicy: Always`) but the entrypoint is short-lived.
- s1: [Step 3] previous-container logs show the process completing successfully (no errors, no stack traces).
- s1: [Step 4] container status reports `exitCode=0` with `reason=Completed`.

**Interventions:**
- **remediation** (root): Fix the entrypoint so it stays in the foreground (`exec <daemon>` without `&`), or convert the workload to a Job if it is meant to run once.

  ```bash
  # Option A: Fix the entrypoint so it stays in the foreground (`exec <daemon>` without `&`).
  kubectl set image deployment/<deployment-name> -n <namespace> <container>=<image>:<fixed-tag>
  # Option B: Convert to a Job if the workload is meant to run once.
  kubectl delete deployment <deployment-name> -n <namespace>
  kubectl apply -f <job-manifest>.yaml
  ```

  **Verification:** for a corrected Deployment, `kubectl get pod -l <selector> -n <namespace>` shows `STATUS: Running` with `RESTARTS=0` for at least 30 minutes; for a converted Job, `kubectl get job` shows `COMPLETIONS: 1/1`.
- **mitigation** (root): Capture the manifest and convert it to a `kind: Job` if the workload is genuinely batch.

  ```bash
  kubectl get deployment <deployment-name> -n <namespace> -o yaml > /tmp/<name>.yaml
  # Edit to kind: Job and apply the new manifest; delete the old Deployment.
  ```

  **Risk:** Converting a Deployment to a Job changes its lifecycle semantics (no rolling update, no replica autoscaling); only correct if the workload is genuinely batch. **Duration:** Permanent — the workload kind should match its lifecycle. **Verification:** `kubectl get job` shows `COMPLETIONS: 1/1` with no restart loop.

### Cause Z: Unidentified

**Statement:** The container is repeatedly terminating and restarting but no indicator from Causes A through J matches the gathered evidence.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot (Steps 1–10) and escalate to the application owner / platform on-call with the failure-mode summary. Optionally roll back to a known-good revision as a holding action.

  ```bash
  kubectl rollout undo deployment/<deployment-name> -n <namespace>
  kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector involvedObject.name=<pod-name>
  ```

  **Risk:** Rolling back to a previous revision can mask data-integrity changes if the new revision included migrations; verify the diff before reverting. **Duration:** Use only as a holding action while engaging the application owner with the gathered diagnostic artefacts. **Verification:** hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

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
