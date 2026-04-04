---
id: k8s-imagepullbackoff
title: "Kubernetes ImagePullBackOff"
domain: compute
service: kubernetes
symptom_class:
  - image_pull_failure
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - pods
  - image-pull
  - registry
  - containers
difficulty: intermediate
---

# Kubernetes ImagePullBackOff

## Problem Definition

Applies to Kubernetes 1.24+ clusters using containerd or CRI-O as the container runtime. Requires `kubectl` access with permissions to get, describe pods, and manage secrets in the target namespace. Registry connectivity testing requires `crane`, `skopeo`, or `docker` CLI. Node-level diagnostics require SSH access or `kubectl debug node`.

ImagePullBackOff is a pod status indicating that the kubelet cannot pull the container image specified in the pod spec. After each failed pull attempt, the kubelet applies an exponential backoff delay starting at 10 seconds, doubling up to a maximum of 5 minutes, before retrying. The pod remains in a `Waiting` state with reason `ImagePullBackOff` or `ErrImagePull` and cannot start until the image is successfully pulled.

The container runtime attempts to pull the image from the registry specified in the pod spec. When the pull fails repeatedly, the kubelet enters a backoff loop to avoid overwhelming the registry. The pod status alternates between `ErrImagePull` (pull just failed) and `ImagePullBackOff` (waiting for backoff timer).

Common root causes include incorrect image name or tag (typo, nonexistent tag), registry authentication failure (missing or invalid `imagePullSecret`, expired credentials, secret in wrong namespace), private registry without credentials configured, network connectivity issues (firewall, DNS, proxy), image deleted from registry, registry rate limiting (Docker Hub pull limits), image platform mismatch (no manifest for the requested architecture), and registry TLS issues (self-signed or expired certificates).

Typical presentation:

```text
NAME          READY   STATUS             RESTARTS   AGE
my-app-pod    0/1     ImagePullBackOff   0          5m
```

Events from `kubectl describe pod` show:

```text
Warning  Failed     2m    kubelet  Failed to pull image "myregistry.io/myapp:v2.0": rpc error: ...
Warning  Failed     2m    kubelet  Error: ErrImagePull
Normal   BackOff    90s   kubelet  Back-off pulling image "myregistry.io/myapp:v2.0"
Warning  Failed     90s   kubelet  Error: ImagePullBackOff
```

## Diagnostic Steps

### Step 1: Get Pod Status and Events

**What this checks:** The specific error message from the failed image pull attempt, which directly identifies the failure category.

```bash
kubectl get pod <pod-name> -n <namespace> -o wide
kubectl describe pod <pod-name> -n <namespace>
```

**Expected output:** The `Events` section contains a `Failed to pull image` message with the specific error.

**What the finding means:** The error message categorizes the failure:

| Error Message | Likely Cause |
| ------------- | ------------ |
| `manifest unknown` | Image tag does not exist in the registry |
| `unauthorized: authentication required` | Missing or invalid credentials |
| `denied: requested access to the resource is denied` | Insufficient permissions on the registry |
| `dial tcp: lookup registry.io: no such host` | DNS resolution failure |
| `connection refused` or `i/o timeout` | Network connectivity issue |
| `toomanyrequests` | Registry rate limit exceeded |
| `x509: certificate signed by unknown authority` | Untrusted TLS certificate |

### Step 2: Verify the Image Reference

**What this checks:** Whether the image name, repository, and tag are correct and the image exists in the registry.

```bash
# Check the configured image reference
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].image}'

# Verify the image exists in the registry
crane manifest <registry>/<image>:<tag>

# Alternative with docker
docker manifest inspect <registry>/<image>:<tag>

# Alternative with skopeo
skopeo inspect docker://<registry>/<image>:<tag>
```

**Expected output:** The image reference string and a successful manifest response confirming the image exists.

**What the finding means:** If the manifest command fails with "not found" or "manifest unknown", the tag does not exist. Check for typos in the registry hostname, repository path, or tag name. If the registry requires authentication, the manifest check will also fail without credentials.

### Step 3: Check imagePullSecrets Configuration

**What this checks:** Whether the pod has registry credentials configured and whether those credentials are valid.

```bash
# Check if the pod has imagePullSecrets configured
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.imagePullSecrets[*].name}'

# List docker-registry secrets in the namespace
kubectl get secrets -n <namespace> --field-selector type=kubernetes.io/dockerconfigjson

# Verify the secret exists and contains valid credentials
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data.\.dockerconfigjson}' | base64 --decode | jq .
```

**Expected output:** The secret name referenced by the pod, and the decoded credentials showing registry server URL, username, and auth token.

**What the finding means:** If imagePullSecrets is empty and the image is in a private registry, credentials are missing. If the secret exists but contains wrong credentials (wrong registry URL, expired token), the pull will fail with "unauthorized".

### Step 4: Check ServiceAccount Default Pull Secrets

**What this checks:** Whether the pod's ServiceAccount has default imagePullSecrets that apply to all pods using that account.

```bash
# Check which service account the pod uses
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.serviceAccountName}'

# Check if the service account has imagePullSecrets
kubectl get serviceaccount <sa-name> -n <namespace> -o jsonpath='{.imagePullSecrets[*].name}'
```

**Expected output:** The service account name and any imagePullSecrets attached to it.

**What the finding means:** If the service account has no imagePullSecrets and the pod spec also has none, the pull uses anonymous credentials. For private registries, either the pod or its service account must reference a valid pull secret.

### Step 5: Test Registry Connectivity from the Node

**What this checks:** Whether the node can reach the registry over the network, including DNS resolution and HTTPS connectivity.

```bash
# Find which node the pod is scheduled on
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}'

# Run a debug pod on the same node to test connectivity
kubectl run -it --rm registry-test --image=alpine:latest --restart=Never \
  --overrides='{"spec":{"nodeName":"<node-name>"}}' -- sh

# From inside the debug pod:
nslookup <registry-hostname>
wget -O /dev/null https://<registry-hostname>/v2/ 2>&1
```

**Expected output:** Successful DNS resolution returning an IP address, and a successful HTTPS connection (HTTP 200 or 401 for auth-required registries).

**What the finding means:** If DNS fails, the registry hostname cannot be resolved from the node. If the connection times out or is refused, a firewall or network policy is blocking access. A 401 response is expected for private registries and confirms network connectivity is working.

### Step 6: Check Node-Level Container Runtime Logs

**What this checks:** The container runtime's own error logs for the failed pull attempt, which may contain more detail than the Kubernetes events.

```bash
# SSH to the node and check containerd logs
journalctl -u containerd -n 50 --no-pager | grep -i "pull\|error\|auth"

# Or for CRI-O
journalctl -u crio -n 50 --no-pager

# Try pulling the image manually on the node
crictl pull <registry>/<image>:<tag>
```

**Expected output:** Detailed error messages from the container runtime about why the pull failed.

**What the finding means:** Runtime logs often provide more specific error details than Kubernetes events, including TLS handshake failures, proxy errors, or credential decoding issues. A successful `crictl pull` bypasses Kubernetes credential handling and tests direct node-to-registry connectivity.

### Step 7: Check for Rate Limiting

**What this checks:** Whether the registry is rate-limiting pull requests, particularly relevant for Docker Hub.

```bash
# For Docker Hub, check your rate limit status
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" "https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest" -I 2>&1 | grep -i ratelimit
```

**Expected output:** Headers showing `ratelimit-limit` and `ratelimit-remaining`.

**What the finding means:** Docker Hub allows 100 pulls per 6 hours for anonymous users and 200 for authenticated users. If `ratelimit-remaining` is 0, pulls will fail with `toomanyrequests` until the window resets.

## Mitigation

### Option 1: Fix or Create imagePullSecret

Use when the error message indicates authentication failure.

- **Risk:** Low. Creates or updates credentials without affecting running workloads.
- **Command:**
  ```bash
  kubectl create secret docker-registry regcred \
    --docker-server=<registry-server> \
    --docker-username=<username> \
    --docker-password=<password> \
    -n <namespace>

  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
  The new pod should transition from `ContainerCreating` to `Running`.
- **Duration:** 1 to 3 minutes for the rolling update to complete.

### Option 2: Use a Known Working Image Tag

Use when the specified tag does not exist or is broken.

- **Risk:** Low to Medium. Rolling back to a previous tag may revert application features or fixes.
- **Command:**
  ```bash
  kubectl set image deployment/<deployment-name> \
    <container-name>=<registry>/<image>:<known-good-tag> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  kubectl get pods -n <namespace> -l app=<app-label>
  ```
  All pods should reach `Running` 1/1 status.
- **Duration:** 1 to 5 minutes depending on image size and pull speed.

### Option 3: Switch to a Mirrored Registry

Use when the primary registry is unreachable or rate-limited.

- **Risk:** Medium. Ensure the mirror contains a trusted, identical image. Verify image digests match before deploying.
- **Command:**
  ```bash
  kubectl set image deployment/<deployment-name> \
    <container-name>=<mirror-registry>/<image>:<tag> -n <namespace>
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
  Pods should pull successfully from the alternate registry.
- **Duration:** 1 to 5 minutes.

### Option 4: Pre-pull the Image on the Node

Use as a last resort when registry access is blocked and you have node access.

- **Risk:** Medium. Bypasses normal image distribution. The image must be pre-pulled on every node where the pod could be scheduled.
- **Command:**
  ```bash
  # SSH to the node and pull manually with credentials
  crictl pull <registry>/<image>:<tag>

  # Set imagePullPolicy to IfNotPresent so kubelet uses the local image
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]'
  ```
- **Verify:**
  ```bash
  kubectl get pods -n <namespace> -l app=<app-label> -w
  ```
- **Duration:** 5 to 15 minutes depending on image size and number of nodes.

## Root Cause Resolution

**If** the image name or tag contains a typo or the tag does not exist **then** correct the image reference in the deployment manifest:

```bash
kubectl set image deployment/<deployment-name> \
  <container-name>=<registry>/<image>:<correct-tag> -n <namespace>
```

**If** events show `unauthorized` or `authentication required` **then** create the pull secret and attach it to either the pod spec or the default service account:

```bash
# Create the secret
kubectl create secret docker-registry regcred \
  --docker-server=<registry-server> \
  --docker-username=<username> \
  --docker-password=<password> \
  -n <namespace>

# Option A: Add to deployment
kubectl patch deployment <deployment-name> -n <namespace> \
  -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'

# Option B: Add to default service account (applies to all pods in namespace)
kubectl patch serviceaccount default -n <namespace> \
  -p '{"imagePullSecrets":[{"name":"regcred"}]}'
```

**If** credentials existed but stopped working **then** the token or password has expired. Recreate the secret with fresh credentials:

```bash
kubectl delete secret regcred -n <namespace>
kubectl create secret docker-registry regcred \
  --docker-server=<registry-server> \
  --docker-username=<username> \
  --docker-password=<new-password> \
  -n <namespace>
kubectl rollout restart deployment/<deployment-name> -n <namespace>
```

**If** events show `no such host`, `connection refused`, or `i/o timeout` **then** the node cannot reach the registry. Diagnose the network path:

```bash
kubectl run -it --rm dns-test --image=alpine --restart=Never -- nslookup <registry-hostname>
kubectl run -it --rm net-test --image=alpine --restart=Never -- wget -O /dev/null https://<registry-hostname>/v2/
```

Fix the underlying network issue (firewall rules, proxy configuration, DNS records) or configure an image mirror within the reachable network.

**If** events show `toomanyrequests` **then** the registry's pull rate limit is exceeded. Authenticate pulls to get higher limits, or deploy a pull-through cache:

```bash
kubectl create secret docker-registry dockerhub-creds \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<username> \
  --docker-password=<token> \
  -n <namespace>
```

For long-term resolution, deploy a pull-through cache registry (Harbor, Docker Registry mirror) within your cluster network.

**If** events show `x509: certificate signed by unknown authority` **then** the container runtime does not trust the registry's TLS certificate. Add the CA certificate to each node's trusted store:

```bash
# For containerd: add CA cert and restart
sudo mkdir -p /etc/containerd/certs.d/<registry-hostname>
sudo cp ca.crt /etc/containerd/certs.d/<registry-hostname>/ca.crt
sudo systemctl restart containerd
```

## Verification

After applying a fix, confirm the image pull succeeds and the pod starts.

```bash
# 1. Confirm pod transitions to Running
kubectl get pod -n <namespace> -l app=<app-label> -w
# Pod should go through ContainerCreating to Running with READY 1/1
```

```bash
# 2. Check events are clean
kubectl describe pod <pod-name> -n <namespace> | tail -20
# Should see: Normal Pulled "Successfully pulled image"
# No Warning events related to image pulling
```

```bash
# 3. Verify image identity
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].imageID}'
# Confirm the pulled image digest matches the expected image
```

```bash
# 4. Validate application health
kubectl exec -it <test-pod> -n <namespace> -- curl -s http://<service-name>:<port>/healthz
# Application should respond normally
```

## Prevention

**Use specific image tags, not latest.** Avoid the `latest` tag in production. Pin images to immutable tags or digests to prevent unexpected changes and make failures reproducible:

```yaml
image: myregistry.io/myapp:v1.2.3@sha256:abc123...
```

**Automate registry credential rotation.** Use tools like External Secrets Operator or Sealed Secrets to manage registry credentials with automated rotation before tokens expire:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: regcred
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault
    kind: ClusterSecretStore
  target:
    name: regcred
    template:
      type: kubernetes.io/dockerconfigjson
```

**Deploy a pull-through cache registry.** Deploy a registry mirror (Harbor, Docker Distribution) within your cluster network to avoid external registry outages and rate limits. Configure containerd to use the mirror:

```toml
# /etc/containerd/config.toml
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
  endpoint = ["https://mirror.internal:5000"]
```

**Attach imagePullSecrets to default ServiceAccount.** Reduce the risk of forgetting `imagePullSecrets` on individual pods by attaching them to the namespace's default service account:

```bash
kubectl patch serviceaccount default -n <namespace> \
  -p '{"imagePullSecrets":[{"name":"regcred"}]}'
```

**Monitor for ImagePullBackOff events.** Set up Prometheus alerts:

```yaml
- alert: PodImagePullBackOff
  expr: kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"} > 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} has ImagePullBackOff"
```

**Validate image existence in CI/CD.** Add a pipeline step that verifies the image exists in the registry before deploying:

```bash
crane manifest <registry>/<image>:<tag> > /dev/null 2>&1 || { echo "Image not found"; exit 1; }
```

## Sources

- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) -- Official pod debugging guide covering ImagePullBackOff diagnosis
- [Kubernetes: Images](https://kubernetes.io/docs/concepts/containers/images/) -- Image pull policies, private registry authentication, and imagePullSecrets
- [Kubernetes: Pull an Image from a Private Registry](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/) -- Step-by-step guide for configuring registry credentials
- [Kubernetes: Image Pull Policy](https://kubernetes.io/blog/2025/05/12/kubernetes-v1-33-ensure-secret-pulled-images-alpha/) -- Security improvements for image pull credential handling in Kubernetes v1.33+
