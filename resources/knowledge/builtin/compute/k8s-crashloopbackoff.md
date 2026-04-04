---
id: k8s-crashloopbackoff
title: "Kubernetes CrashLoopBackOff"
domain: compute
service: kubernetes
symptom_class:
  - crash_loop
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pods
  - crashloop
  - containers
  - restart
difficulty: intermediate
---

# Kubernetes CrashLoopBackOff

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any managed or self-hosted distribution. Requires `kubectl` access with permissions to get, describe, and log pods in the target namespace. The `metrics-server` add-on is needed for `kubectl top` commands. Node-level debugging requires SSH access or `kubectl debug node`.

CrashLoopBackOff is a pod status indicating that a container is repeatedly crashing and being restarted by the kubelet. After each crash the kubelet applies an exponential backoff delay between restart attempts: 10s, 20s, 40s, 80s, 160s, capped at 300s. The backoff timer resets after the container runs successfully for 10 minutes. The pod alternates between `CrashLoopBackOff` (waiting for backoff timer) and `Error` (crash just occurred).

This occurs when all of the following are true:

1. A container terminates with a non-zero exit code or is killed by the kernel.
2. The pod's `restartPolicy` is `Always` (default for Deployments) or `OnFailure` (default for Jobs).
3. The kubelet continuously restarts the container but it keeps failing.

Common root causes include application errors (unhandled exceptions, missing entrypoint), missing configuration dependencies (environment variables, ConfigMaps, Secrets), resource limit violations (OOMKilled with exit code 137), aggressive liveness probes killing the container before startup completes, image issues (wrong tag, architecture mismatch), volume mount failures that overlay required directories, and permission errors on the entrypoint binary.

Typical presentation:

```
NAME          READY   STATUS             RESTARTS      AGE
my-app-pod    0/1     CrashLoopBackOff   14 (2m ago)   10m
```

The restart count climbs steadily and the `RESTARTS` column includes the time since the last restart attempt in parentheses.

## Diagnostic Steps

### Step 1: Get Pod Status and Events

**What this checks:** The current pod state, restart count, node assignment, and recent Kubernetes events that indicate why the container is failing.

```bash
kubectl get pod <pod-name> -n <namespace> -o wide
kubectl describe pod <pod-name> -n <namespace>
```

**Expected output:** The `describe` output contains a `State` / `Last State` section showing the container termination reason (e.g., `Error`, `OOMKilled`, `Completed`) and exit code. The `Events` section at the bottom shows scheduling, image pull, mount, and restart events.

**What the finding means:** If `Last State` shows `Reason: OOMKilled`, proceed to Step 5 (resource limits). If events show `Liveness probe failed`, proceed to Step 7. If events show `Failed to pull image`, this is an ImagePullBackOff issue rather than CrashLoopBackOff. If events show `Back-off restarting failed container`, the pod is in the backoff cycle.

### Step 2: Read Container Logs

**What this checks:** The application's stdout/stderr output from the previous crashed container instance, which typically contains the error that caused the crash.

```bash
# Previous crashed container (critical for CrashLoopBackOff)
kubectl logs <pod-name> -n <namespace> --previous

# Current container attempt (may be empty if crash is instant)
kubectl logs <pod-name> -n <namespace>

# For multi-container pods, specify the crashing container
kubectl logs <pod-name> -n <namespace> -c <container-name> --previous
```

**Expected output:** Application-level error messages, stack traces, or configuration errors. If logs are empty, the crash occurs before the application writes any output (binary not found, permission denied, segfault).

**What the finding means:** Stack traces or error messages point directly to the application-level root cause. Empty logs suggest a container-level issue (missing binary, permission error, exec format error) rather than an application logic error. Proceed to Step 3 to check the exit code.

### Step 3: Check Exit Code

**What this checks:** The numeric exit code returned by the crashed container, which categorizes the failure.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.exitCode}'
```

**Expected output:** A numeric exit code.

| Exit Code | Meaning |
|-----------|---------|
| 0 | Success (unexpected for a long-running service -- process exited cleanly) |
| 1 | Generic application error |
| 2 | Shell misuse or missing command argument |
| 126 | Command not executable (permission denied on binary) |
| 127 | Command not found (missing binary in image) |
| 137 | SIGKILL (OOMKilled by kernel or forced pod deletion) |
| 139 | SIGSEGV (segmentation fault in application) |
| 143 | SIGTERM (graceful shutdown requested but process still exited) |

**What the finding means:** Exit code 137 strongly indicates OOMKilled -- confirm with the `describe` output showing `Reason: OOMKilled`. Exit code 127 means the container entrypoint binary does not exist in the image. Exit code 126 means the binary exists but is not executable. Exit code 1 means the application encountered a runtime error visible in logs.

### Step 4: Check Termination Message

**What this checks:** An optional termination message written by the application to `/dev/termination-log`, providing application-specific failure context.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.message}'
```

**Expected output:** An application-defined error string, or empty if the application does not write to the termination log.

**What the finding means:** A non-empty message provides the application's own explanation for the crash. If empty, rely on container logs (Step 2) and exit code (Step 3). Setting `terminationMessagePolicy: FallbackToLogsOnError` in the pod spec will automatically populate this with the last few log lines when the container exits with a non-zero code.

### Step 5: Check Resource Usage and Limits

**What this checks:** Whether the container has memory/CPU limits configured and whether actual usage is approaching or exceeding those limits.

```bash
# Check configured limits
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"\t requests.memory="}{.resources.requests.memory}{"\t limits.memory="}{.resources.limits.memory}{"\n"}{end}'

# Check actual usage (requires metrics-server)
kubectl top pod <pod-name> -n <namespace> --containers
```

**Expected output:** The first command shows configured requests and limits. The second shows actual CPU and memory consumption per container.

**What the finding means:** If exit code is 137 and `describe` shows `Reason: OOMKilled`, the container exceeded its memory limit. If actual usage is near the limit, the limit is too low for the workload. If no limits are set and the node is under memory pressure, the pod may be killed by the node-level OOM killer.

### Step 6: Verify Configuration Dependencies

**What this checks:** Whether required environment variables, Secrets, and ConfigMaps exist and are accessible to the pod.

```bash
# Check environment variables injected into the pod
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'

# Check environment variables from ConfigMap/Secret refs
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[0].envFrom[*]}{.configMapRef.name}{.secretRef.name}{"\n"}{end}'

# Verify referenced secret exists
kubectl get secret <secret-name> -n <namespace>

# Verify referenced configmap exists
kubectl get configmap <configmap-name> -n <namespace>
```

**Expected output:** Environment variable names and values, and confirmation that referenced Secrets and ConfigMaps exist in the namespace.

**What the finding means:** If a Secret or ConfigMap does not exist, the pod fails to start with an event like `Error: configmap "my-config" not found`. If environment variables have wrong values (empty database URL, invalid API key), the application crashes at startup with an error visible in logs.

### Step 7: Check Liveness and Startup Probes

**What this checks:** Whether liveness or startup probes are misconfigured and killing the container before the application finishes initialization.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].livenessProbe}'
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].startupProbe}'
```

**Expected output:** JSON objects describing the probe configuration, including `initialDelaySeconds`, `periodSeconds`, `failureThreshold`, and the check mechanism (httpGet, exec, tcpSocket).

**What the finding means:** If `describe` events show `Liveness probe failed` or `Startup probe failed`, the probe is killing the container. A low `initialDelaySeconds` with no startup probe is a common cause for slow-starting applications (JVM warmup, database migrations at startup, large cache preloading).

### Step 8: Validate the Container Image

**What this checks:** Whether the container image tag is correct, the image exists, and the entrypoint is valid.

```bash
# Check which image is configured
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].image}'

# Check image pull status in events
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 "Events:"

# Test the entrypoint locally (if Docker is available)
docker run --rm -it <image> sh
```

**Expected output:** The image reference (registry/name:tag) and confirmation that the image was pulled successfully.

**What the finding means:** If the image pulls successfully but the container exits with code 127, the entrypoint binary is missing from the image (common with multi-stage builds that forget to copy the binary). Architecture mismatch (amd64 image on arm64 node) produces `exec format error` in logs.

## Mitigation

### Option 1: Restart the Pod

Useful when the crash is caused by a transient condition such as a temporarily unavailable dependency.

- **Risk:** Low. Deleting the pod triggers a new scheduling cycle; the controller creates a replacement. Running workloads on other pods are not affected.
- **Command:**
  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl get pod -n <namespace> -l app=<app-label> -w
  ```
  The new pod should reach `Running` 1/1 status without restarting.
- **Duration:** 30 seconds to 2 minutes depending on image pull and startup time.

### Option 2: Roll Back to Last Known Good Deployment

Useful when the crash started after a deployment or configuration change.

- **Risk:** Medium. Reverts to the previous ReplicaSet, which may have its own issues or lack recent features. Verify the previous revision was healthy before rolling back.
- **Command:**
  ```bash
  kubectl rollout history deployment/<deployment-name> -n <namespace>
  kubectl rollout undo deployment/<deployment-name> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  kubectl get pods -n <namespace> -l app=<app-label>
  ```
  All pods should reach `Running` 1/1 status with zero recent restarts.
- **Duration:** 1 to 5 minutes depending on rolling update strategy and readiness probes.

### Option 3: Temporarily Increase Resource Limits

Useful when the crash is caused by OOMKilled and you need to restore service while investigating memory usage.

- **Risk:** Medium. Over-provisioning reduces cluster capacity for other workloads and may mask a memory leak that should be fixed.
- **Command:**
  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"1Gi"}]'
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  kubectl top pod -n <namespace> -l app=<app-label>
  ```
  The new pod should start without OOMKilled and memory usage should stabilize below the new limit.
- **Duration:** 1 to 3 minutes after rolling update completes.

### Option 4: Override the Container Command for Debugging

Useful when you need to keep the container alive to inspect its filesystem, environment, and configuration.

- **Risk:** Low in non-production. The debug pod does not run the application and cannot serve traffic.
- **Command:**
  ```bash
  kubectl debug <pod-name> -n <namespace> --copy-to=debug-pod --container=<container-name> -- sleep 3600
  kubectl exec -it debug-pod -n <namespace> -- sh
  ```
- **Verify:**
  ```bash
  kubectl get pod debug-pod -n <namespace>
  ```
  The debug pod should be in `Running` state.
- **Duration:** Debug pod stays alive for 1 hour (or the configured sleep duration).

## Root Cause Resolution

**If** logs show an unhandled exception or stack trace at startup **then** fix the application code and deploy a corrected image:

```bash
docker build -t <registry>/<image>:<new-tag> .
docker push <registry>/<image>:<new-tag>
kubectl set image deployment/<deployment-name> <container-name>=<registry>/<image>:<new-tag> -n <namespace>
```

**If** exit code is 127 (command not found) **then** the entrypoint binary is missing from the image. Verify the Dockerfile `CMD` or `ENTRYPOINT` instruction, ensure the binary is included in the final build stage (common issue with multi-stage builds), and rebuild the image.

**If** exit code is 126 (permission denied) **then** the entrypoint binary is not executable. Add `RUN chmod +x /app/entrypoint.sh` to the Dockerfile and rebuild.

**If** logs show "environment variable not set" or "config file not found" **then** the required ConfigMap or Secret is missing or misconfigured:

```bash
kubectl get configmap <configmap-name> -n <namespace> -o yaml
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data}' | jq 'keys'
kubectl create configmap <configmap-name> --from-file=config.yaml=./config.yaml -n <namespace>
kubectl rollout restart deployment/<deployment-name> -n <namespace>
```

**If** a volume mount path overlays a directory the application expects to exist **then** adjust the `mountPath` or use `subPath` to mount a single file instead of replacing the entire directory:

```yaml
volumeMounts:
  - name: config-volume
    mountPath: /app/config/app.yaml
    subPath: app.yaml
```

**If** `describe` shows `Reason: OOMKilled` with exit code 137 **then** the container exceeded its memory limit. Right-size the limit based on observed usage plus 20-30% headroom:

```bash
kubectl top pod <pod-name> -n <namespace> --containers
kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"768Mi"},
       {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"512Mi"}]'
```

**If** events show `Liveness probe failed` before the application finishes starting **then** add a startup probe to protect slow-starting applications:

```yaml
startupProbe:
  httpGet:
    path: /healthz
    port: 8080
  failureThreshold: 30
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 0
  periodSeconds: 10
  failureThreshold: 3
```

**If** the pod runs on a different CPU architecture than the image was built for **then** build a multi-architecture image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/<image>:<tag> --push .
```

**If** init containers are in CrashLoopBackOff **then** the main container never starts. Debug the init container separately:

```bash
kubectl logs <pod-name> -n <namespace> -c <init-container-name>
kubectl describe pod <pod-name> -n <namespace> | grep -A 20 "Init Containers:"
```

## Verification

After applying a fix, confirm the pod is stable:

```bash
# 1. Confirm pod reaches Running state
kubectl get pod -n <namespace> -l app=<app-label> -w
# Pod should show Running with READY 1/1 and RESTARTS at 0 or stable
```

```bash
# 2. Verify restart count is not increasing (wait 5-10 minutes)
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].restartCount}'
```

```bash
# 3. Check for warning events
kubectl events --for pod/<pod-name> -n <namespace> --watch
# No new Warning events should appear
```

```bash
# 4. Validate application health via service endpoints
kubectl get endpoints <service-name> -n <namespace>
kubectl exec -it <test-pod> -n <namespace> -- curl -s http://<service-name>:<port>/healthz
# The pod IP should appear in endpoints and the health check should succeed
```

## Prevention

**Set resource requests and limits for all containers.** Use LimitRange to enforce namespace-level defaults so that no pod runs without resource boundaries:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: <namespace>
spec:
  limits:
  - default:
      memory: "512Mi"
      cpu: "500m"
    defaultRequest:
      memory: "256Mi"
      cpu: "250m"
    type: Container
```

**Use startup probes for slow-starting applications.** Separate startup probes from liveness probes to avoid killing containers that are still initializing. This is especially important for JVM-based applications, services that run database migrations on startup, or applications with large caches to warm.

**Implement graceful shutdown.** Handle `SIGTERM` in your application to shut down cleanly within `terminationGracePeriodSeconds` (default 30s). This prevents data corruption and allows in-flight requests to complete.

**Pin image tags.** Avoid `:latest` tags in production. Use immutable image digests or semantic version tags to prevent unexpected image changes from causing crashes:

```yaml
image: myregistry/myapp:v1.2.3@sha256:abc123...
```

**Write termination messages.** Set `terminationMessagePolicy: FallbackToLogsOnError` so that `kubectl describe` shows the last log lines as the termination message when the application crashes without writing to `/dev/termination-log`.

**Monitor restart counts with alerts.** Create a Prometheus alert that fires when a pod restarts too frequently:

```yaml
- alert: PodCrashLooping
  expr: rate(kube_pod_container_status_restarts_total[10m]) * 60 * 10 > 3
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} is crash looping"
```

**Validate configurations before deployment.** Use `kubectl diff` and dry-run to catch manifest errors before they reach the cluster:

```bash
kubectl diff -f deployment.yaml
kubectl apply --dry-run=server -f deployment.yaml
```

## Sources

- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) -- Official pod debugging guide covering Pending, Waiting, and Crashing pods
- [Kubernetes: Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) -- Container states, restart policies, backoff behavior, and CrashLoopBackOff mechanics
- [Kubernetes: Determine Reason for Pod Failure](https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/) -- Exit codes and termination messages
- [Kubernetes: Manage Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) -- Resource requests, limits, QoS classes, and OOMKilled behavior
- [Kubernetes: Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/) -- kubectl debug, ephemeral containers, and node-level debugging
- [Kubernetes: Debug Init Containers](https://kubernetes.io/docs/tasks/debug/debug-application/debug-init-containers/) -- Diagnosing init container failures that block main container startup
