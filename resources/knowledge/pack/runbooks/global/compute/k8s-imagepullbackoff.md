---
id: k8s-imagepullbackoff
title: "Kubernetes ImagePullBackOff"
domain: compute
service: kubernetes
symptom_class:
  - image_pull_failure
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - image-pull
  - registry
  - containerd
  - ecr
  - dockerhub
difficulty: intermediate
---

# Kubernetes ImagePullBackOff

## Symptom Recognition

- `kubectl get pods` shows `STATUS: ImagePullBackOff` or `STATUS: ErrImagePull` with `READY 0/1` and `RESTARTS 0`. The status alternates between `ErrImagePull` (pull just failed) and `ImagePullBackOff` (waiting for retry timer).
- `kubectl describe pod` Events section contains lines such as `Failed to pull image "<ref>": rpc error: ...`, `Error: ErrImagePull`, `Back-off pulling image "<ref>"`, and `Error: ImagePullBackOff`.
- Specific runtime error strings observed in events or container-runtime logs identify the failure family: `manifest unknown`, `unauthorized: authentication required`, `denied: requested access to the resource is denied`, `no basic auth credentials`, `dial tcp: lookup <host>: no such host`, `connection refused`, `i/o timeout`, `toomanyrequests` (HTTP 429), `x509: certificate signed by unknown authority`, `no match for platform in manifest`, `ErrImageNeverPull`.
- Container state in `kubectl get pod <name> -o jsonpath` shows `waiting.reason=ImagePullBackOff` or `waiting.reason=ErrImagePull` with `waiting.message` carrying the underlying error.
- Prometheus `kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"} > 0` fires for the affected pod.

## Applicability

- Kubernetes 1.24 or newer on any distribution (vanilla, EKS, GKE, AKS, OpenShift, Rancher) using containerd or CRI-O as the container runtime.
- Requires `kubectl` access with `get`, `list`, `describe` verbs on `pods` and `serviceaccounts`, and `get` on `secrets` in the target namespace.
- For network/registry probing from inside the cluster: ability to create short-lived debug pods on the same node (`kubectl run --rm` or `kubectl debug node/`).
- Image inspection from a workstation requires one of: `crane` (go-containerregistry), `skopeo`, or `docker` CLI configured for the target registry.
- ECR-specific diagnostics require AWS CLI v2 and IAM permissions for `ecr:GetAuthorizationToken` and `ecr:BatchGetImage` against the target registry.

## Diagnostic Steps

### Step 1: Capture the pod's waiting reason and exact pull error message

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"  reason="}{.state.waiting.reason}{"  msg="}{.state.waiting.message}{"\n"}{end}'
kubectl describe pod <pod-name> -n <namespace> | sed -n '/Events:/,$p'
```

Expected output: a line per container with `reason=ImagePullBackOff` or `reason=ErrImagePull` and a `msg=` field carrying the runtime error verbatim, plus the `Events` block listing `Failed to pull image "<ref>": <error>` warnings from `kubelet`.

### Step 2: Read the image reference and pull policy from the pod spec

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*]}{.name}{"  image="}{.image}{"  imagePullPolicy="}{.imagePullPolicy}{"\n"}{end}'
```

Expected output: one line per container with the full image reference (`<registry>/<repo>:<tag>` or `<registry>/<repo>@sha256:<digest>`) and the resolved pull policy (`Always`, `IfNotPresent`, or `Never`).

### Step 3: Resolve the image manifest against the registry from a workstation

```bash
crane manifest <registry>/<repo>:<tag>
# Or, if crane is unavailable:
skopeo inspect --raw docker://<registry>/<repo>:<tag>
# Or:
docker manifest inspect <registry>/<repo>:<tag>
```

Expected output: a JSON manifest or manifest-list body. Non-zero exit with `MANIFEST_UNKNOWN`, `manifest unknown`, or `not found` means the tag does not exist in the repository. Authentication errors here mirror what the kubelet sees.

### Step 4: List per-architecture manifests for the image

```bash
crane manifest <registry>/<repo>:<tag> | jq -r '.manifests[]? | "\(.platform.os)/\(.platform.architecture)  \(.digest)"'
```

Expected output: one row per supported platform (e.g., `linux/amd64`, `linux/arm64`). Empty output means the image is a single-platform manifest and only matches one node architecture.

### Step 5: Identify which imagePullSecrets the pod actually resolves

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.imagePullSecrets[*].name}{"\n"}'
SA=$(kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.serviceAccountName}')
kubectl get serviceaccount "$SA" -n <namespace> -o jsonpath='{.imagePullSecrets[*].name}{"\n"}'
```

Expected output: two whitespace-separated lists — pull secrets declared on the pod itself, then those inherited from its ServiceAccount. Both empty means the kubelet pulls anonymously from the registry.

### Step 6: Decode the referenced docker-registry secret

```bash
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq '.auths | keys, (to_entries[] | {host: .key, user: (.value.username // (.value.auth | @base64d | split(":")[0]))})'
```

Expected output: the list of registry hostnames the secret holds credentials for, plus the username for each. A `jq: error` from this command means the secret is malformed; an empty hostname list means the secret is `type: Opaque` rather than `type: kubernetes.io/dockerconfigjson`.

### Step 7: Test registry DNS and HTTPS reachability from a pod on the same node

```bash
NODE=$(kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}')
kubectl run net-probe-$$ -n <namespace> --rm -i --tty --restart=Never \
  --overrides="{\"spec\":{\"nodeName\":\"$NODE\"}}" \
  --image=alpine:3.20 -- sh -c '
    apk add --no-cache curl bind-tools >/dev/null
    nslookup <registry-host>
    curl -sSv -o /dev/null -w "%{http_code}\n" "https://<registry-host>/v2/"
  '
```

Expected output: a resolved A/AAAA record for the registry host, followed by an HTTP status code. `200` indicates an open public registry; `401` confirms TCP+TLS reachability for a private registry (the kubelet's credentials handle the auth). `000`, `i/o timeout`, or `Could not resolve host` localize the failure to DNS or network egress on that specific node.

### Step 8: Pull the image directly with the container runtime on the node

```bash
kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c "crictl pull <registry>/<repo>:<tag>"
```

Expected output: `Image is up to date for ...` on success. A failure here reproduces the kubelet's error without Kubernetes credential plumbing, isolating the cause to the registry/network rather than to secret resolution. ECR returns `no basic auth credentials` when the runtime has no ECR helper configured.

### Step 9: Read the container runtime's own pull logs on the node

```bash
kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c "journalctl -u containerd -n 200 --no-pager | grep -iE 'pull|auth|x509|denied|429|manifest' | tail -40"
```

Expected output: containerd or CRI-O log lines with the full pull URL, the HTTP status from the registry, and any TLS or auth error. These are richer than the truncated messages surfaced via kubelet events and frequently disambiguate `denied` (RBAC on registry) from `unauthorized` (no/bad credentials).

### Step 10: Check Docker Hub rate-limit headroom (only when the registry is Docker Hub)

```bash
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:ratelimitpreview/test:pull" | jq -r .token)
curl -sI -H "Authorization: Bearer $TOKEN" "https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest" | grep -iE 'ratelimit-(limit|remaining)'
```

Expected output: two header lines — `ratelimit-limit: <N>;w=21600` (limit per 6-hour window) and `ratelimit-remaining: <N>;w=21600`. `ratelimit-remaining: 0` means subsequent anonymous pulls return HTTP 429 with body `toomanyrequests`. Anonymous limit is 100 per 6 hours per IPv4/IPv6 /64; authenticated personal accounts get 200.

## Causes

### Cause A: Image tag or repository does not exist in the registry

**Statement:** The image reference in the pod spec points to a tag or repository the registry cannot resolve, so the manifest request returns `manifest unknown` and the kubelet enters ImagePullBackOff.

**Chain:**
- root: the pod spec references a registry host, repository path, or tag that does not exist (typo, or a tag deleted/garbage-collected upstream).
- s1: the runtime issues `GET /v2/<repo>/manifests/<tag>`; the registry returns HTTP 404 with body `{"errors":[{"code":"MANIFEST_UNKNOWN"}]}`.
- s2: the runtime propagates `manifest unknown` to the kubelet, which emits a `Failed to pull image` warning and increments the backoff; retries take the same path because the cause is structural.
- D: the pod remains in ImagePullBackOff indefinitely (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error message contains `manifest unknown` or `MANIFEST_UNKNOWN`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "manifest unknown"} -->
- root: [Step 3] `crane manifest`/`skopeo inspect` from a workstation also fails not-found against the same reference.
- root: [Symptom] the reference uses a humanly-mutable tag like `v1.2.4` or `latest` rather than an immutable digest.

**Interventions:**
- **mitigation** (root): roll the deployment back to a previously-validated tag.

  ```bash
  kubectl set image deployment/<deployment-name> -n <namespace> \
    <container-name>=<registry>/<repo>:<known-good-tag>
  ```

  **Risk:** Pointing to a different tag changes what runs in production; pick a previously-validated tag, never substitute `latest`. **Duration:** Until the correct tag is published; safe to leave permanently if the rolled-back tag is the desired version. **Verification:** `kubectl get pod -n <namespace> -l app=<label>` shows pods `Running 1/1`.
- **remediation** (root): verify the intended tag exists, then update the manifest source-of-truth and roll out.

  ```bash
  crane manifest <registry>/<repo>:<correct-tag>
  kubectl set image deployment/<deployment-name> -n <namespace> \
    <container-name>=<registry>/<repo>:<correct-tag>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** Re-run Step 1; `kubectl describe pod` Events end with `Normal Pulled  Successfully pulled image "<correct-ref>"` with no further `Failed to pull image` warnings for 5 minutes.

### Cause B: Missing or unattached imagePullSecret for a private registry

**Statement:** The pod and its ServiceAccount declare no imagePullSecret, so the kubelet pulls anonymously and the private registry rejects the manifest request with `unauthorized: authentication required`.

**Chain:**
- root: no pull credential covers the target registry host — `pod.spec.imagePullSecrets`, the ServiceAccount's `imagePullSecrets`, kubelet credential providers, and node-local `~/.docker/config.json` are all empty for that host.
- s1: the runtime sends the manifest request with no `Authorization` header.
- s2: the registry replies HTTP 401 with `WWW-Authenticate: Bearer realm=...` and body `errors: [{code: UNAUTHORIZED, message: "authentication required"}]`.
- s3: the runtime surfaces `unauthorized: authentication required` to the kubelet, which records the event and enters backoff.
- D: the pod stays in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s3: [Step 1] pull error contains `unauthorized: authentication required` or `unauthorized` with no other qualifier.
  <!-- match: {"step": 1, "predicate": "contains", "target": "unauthorized: authentication required"} -->
- root: [Step 5] both the pod's `spec.imagePullSecrets` and its ServiceAccount's `imagePullSecrets` are empty.
- s2: [Step 7] HTTP probe to `/v2/` returns `401`, proving network reach and an auth-gated registry.

**Interventions:**
- **mitigation** (root): create a docker-registry secret and patch it onto the deployment template.

  ```bash
  kubectl create secret docker-registry regcred \
    --docker-server=<registry-host> \
    --docker-username=<user> \
    --docker-password=<token> \
    -n <namespace>
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}}}'
  ```

  **Risk:** A wrong `--docker-server` silently fails to match the image's host and the kubelet falls back to anonymous; use the exact hostname from the image reference (e.g., `ghcr.io`, `123456789012.dkr.ecr.us-east-1.amazonaws.com`, `https://index.docker.io/v1/` for Docker Hub). **Duration:** Permanent once the secret is owned by the namespace and the template references it. **Verification:** Re-run Step 1; the pod reaches `Running 1/1`.
- **remediation** (root): attach the pull secret to the namespace's `default` ServiceAccount so every pod inherits it.

  ```bash
  kubectl create secret docker-registry regcred \
    --docker-server=<registry-host> \
    --docker-username=<user> --docker-password=<token> \
    -n <namespace>
  kubectl patch serviceaccount default -n <namespace> \
    -p '{"imagePullSecrets":[{"name":"regcred"}]}'
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl get pod -n <namespace> -l app=<label>` reaches `Running 1/1` within one rollout window; `kubectl describe pod` shows `Normal Pulled Successfully pulled image`; `kubectl get sa default -n <namespace> -o jsonpath='{.imagePullSecrets[*].name}'` returns `regcred`.

### Cause C: imagePullSecret credentials are revoked or expired

**Statement:** A pull secret exists and is attached to the pod, but the embedded credential has been rotated, revoked, or has expired, so the registry returns `unauthorized` despite the kubelet sending an Authorization header.

**Chain:**
- root: the static credential captured into `.dockerconfigjson` at secret-creation time has been rotated in the IdP or the bearer token has expired; Kubernetes never refreshes it.
- s1: the next manifest request authenticates with the stale credential and fails.
- s2: the registry returns HTTP 401 with the `unauthorized` body, often with `token has expired` or `invalid_token` in the runtime log.
- s3: backoff begins; restarts do not help because every retry reuses the same stale secret.
- D: the pod stays in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `unauthorized` or `token has expired` or `invalid_token`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "unauthorized"} -->
- root: [Step 5] pod or ServiceAccount references a pull secret AND [Step 6] the secret decodes cleanly to a non-empty username for the target registry host.
- s3: [Step 8] a manual `crictl pull` on the node fails the same way until fresh credentials are written.

**Interventions:**
- **mitigation** (root): delete and recreate the secret with a fresh credential.

  ```bash
  kubectl delete secret regcred -n <namespace>
  kubectl create secret docker-registry regcred \
    --docker-server=<registry-host> \
    --docker-username=<user> --docker-password=<new-token> \
    -n <namespace>
  ```

  **Risk:** Recreating mid-rollout can race — new pods get the fresh secret, old pods keep retrying the stale one until evicted. **Duration:** Until the next credential rotation; for bounded-lifetime tokens, schedule renewal before expiry. **Verification:** Re-run Step 8; `crictl pull` succeeds with fresh credentials.
- **remediation** (root): refresh the static secret, then drive an immediate rollout so all replicas pick up new credentials.

  ```bash
  kubectl delete secret regcred -n <namespace>
  kubectl create secret docker-registry regcred \
    --docker-server=<registry-host> \
    --docker-username=<user> --docker-password=<new-token> \
    -n <namespace>
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** After rollout, `kubectl get pod -n <namespace> -l app=<label>` shows all pods `Running 1/1`; `kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -i 'Failed to pull'` produces no entries for the past 10 minutes.

### Cause D: ECR authorization token expired or ECR credential helper absent

**Statement:** The node pulls from Amazon ECR but the ECR authorization token has expired (default 12-hour lifetime) or the node's container runtime lacks an ECR credential helper, so the runtime sends no credentials and ECR replies `no basic auth credentials`.

**Chain:**
- root: the kubelet ECR credential provider (`ecr-credential-provider`) is missing/misconfigured, or the node IAM role lacks `ecr:GetAuthorizationToken` and `ecr:BatchGetImage` (region mismatch produces the same effect).
- s1: ECR requires a 12-hour bearer token from `aws ecr get-authorization-token`, but none is minted, so the runtime falls through to anonymous auth.
- s2: ECR responds HTTP 401 with body `code: DENIED, message: "Your authorization token has expired. Reauthenticate and try again."`.
- s3: containerd surfaces this as `no basic auth credentials` and the kubelet enters backoff.
- D: the pod stays in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s3: [Step 1] pull error contains `no basic auth credentials` or `Your authorization token has expired`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "no basic auth credentials"} -->
- root: [Step 2] image reference matches `<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>`.
- s2: [Step 9] containerd journal shows `failed to fetch oauth token` or `401 Unauthorized` against the ECR host.

**Interventions:**
- **mitigation** (root): mint a 12-hour token manually and write it into a static secret.

  ```bash
  aws ecr get-login-password --region <region> | kubectl create secret docker-registry ecr-creds \
    --docker-server=<account>.dkr.ecr.<region>.amazonaws.com \
    --docker-username=AWS --docker-password-stdin -n <namespace> \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"ecr-creds"}]}}}}'
  ```

  **Risk:** Works for one 12-hour window only; the workload breaks again on the next expiry unless automated. **Duration:** ≤12 hours; use only while a durable credential-provider fix is in flight. **Verification:** Re-run Step 1; the pod pulls and reaches `Running 1/1` for the token's lifetime.
- **remediation** (root): use the kubelet credential provider — no Kubernetes secret, tokens refresh automatically.

  ```bash
  # 1) Attach the managed read-only policy to the node IAM role (instance profile or IRSA):
  aws iam attach-role-policy --role-name <node-role> \
    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
  # 2) Verify the credential provider config exists on every node:
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host \
    cat /etc/eks/image-credential-provider/config.json
  # 3) Roll the deployment so pods pick up a fresh pull attempt:
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl debug node/<node-name> -it --image=busybox -- chroot /host crictl pull <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>` succeeds; `kubectl get pod -n <namespace> -l app=<label>` reaches `Running 1/1`; no `Failed to pull image` events for one hour.

### Cause E: Node cannot resolve the registry hostname (DNS failure)

**Statement:** DNS resolution for the registry hostname fails on the node, so the runtime cannot open a TCP connection and reports `dial tcp: lookup <host>: no such host`.

**Chain:**
- root: the node's DNS path is broken — an unhealthy upstream resolver, a stale registry CNAME, or a NetworkPolicy/security-group rule blocking outbound DNS (UDP/TCP 53) from the node subnet.
- s1: image pulls run in the node's host network namespace and use `/etc/resolv.conf`, so the registry-host lookup fails and Go's `net` package returns `no such host`.
- s2: the runtime wraps it as `Failed to pull image "<ref>": ... dial tcp: lookup <host>: no such host`.
- D: only pods on the affected node(s) show the symptom (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `no such host` or `dial tcp: lookup`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "no such host"} -->
- s1: [Step 7] `nslookup <registry-host>` from a pod on the same node fails (no answer, or `SERVFAIL`).
- root: [Step 8] manual `crictl pull` on the affected node fails identically; nodes in different subnets succeed.

**Interventions:**
- **mitigation** (root): cordon and drain the broken node so new pods land on healthy nodes.

  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
  ```

  **Risk:** Hard-coding a registry IP in node `/etc/hosts` bypasses DNS but breaks the moment the registry rotates IPs; cordon/drain is the safer stopgap but removes capacity. **Duration:** Until the underlying DNS path is restored. **Verification:** Re-run Step 7 on a remaining node; pods reschedule and reach `Running 1/1`.
- **remediation** (root): diagnose the broken DNS path on the node, then fix at the right layer.

  ```bash
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c '
    cat /etc/resolv.conf
    nslookup <registry-host>
    nslookup <registry-host> 8.8.8.8
  '
  # If only the node-local resolver fails: restart node-local-dns or kube-dns.
  # If upstream VPC DNS fails: check VPC DHCP options set, Route53 Resolver health, security-group/NACL egress on UDP 53.
  kubectl uncordon <node-name>
  ```

  **Verification:** From the previously affected node, `nslookup <registry-host>` returns an A/AAAA record in under 1s; `crictl pull <registry>/<repo>:<tag>` succeeds; new pods reach `Running 1/1` within one minute.

### Cause F: Registry network egress blocked by firewall, security group, or NetworkPolicy

**Statement:** DNS resolves correctly but TCP/443 from the node to the registry is blocked, so the runtime reports `connection refused` or `i/o timeout` on the manifest fetch.

**Chain:**
- root: a network-path rule drops or rejects node→registry traffic — a misconfigured VPC route, security group, NACL, host-matching NetworkPolicy, or HTTP proxy.
- s1: a dropped SYN to the registry produces `i/o timeout`; a reachable host that closes the port returns RST, producing `connection refused`.
- s2: the runtime reports `Failed to pull image "<ref>": ... dial tcp <ip>:443: i/o timeout` or `connection refused`.
- D: pods on nodes in a different subnet/AZ may pull fine; affected-path pods stay in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `i/o timeout` or `connection refused` against the registry host.
  <!-- match: {"step": 1, "predicate": "contains", "target": "i/o timeout"} -->
- s1: [Step 7] DNS resolves but `curl https://<registry-host>/v2/` from a pod on the same node hangs or returns `000`.
- root: [Step 8] `crictl pull` on the node times out identically; the same image pulls fine from a workstation outside the cluster network.

**Interventions:**
- **mitigation** (root): temporarily open egress on TCP/443 from the node security group.

  ```bash
  # AWS example — temporarily allow egress on TCP/443 from the node security group:
  aws ec2 authorize-security-group-egress --group-id <node-sg> \
    --protocol tcp --port 443 --cidr 0.0.0.0/0
  ```

  **Risk:** `0.0.0.0/0:443` restores pulls but expands the node's attack surface; scope to the registry CIDR / FQDN if the firewall supports it. **Duration:** Until a narrowly scoped allow rule is in place; revert the broad rule the same day. **Verification:** Re-run Step 7; `/v2/` returns 200 or 401 in under 2s.
- **remediation** (root): allow egress to the registry's actual range (or proxy), and configure containerd's proxy if required.

  ```bash
  # 1) Allow egress to the registry's IP range or the corporate proxy (for ECR, the regional S3 prefix list).
  aws ec2 authorize-security-group-egress --group-id <node-sg> \
    --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=pl-xxxxxxxx}]
  # 2) If a corporate HTTP proxy is required, configure it in containerd's drop-in:
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c '
    mkdir -p /etc/systemd/system/containerd.service.d
    printf "[Service]\nEnvironment=\"HTTPS_PROXY=http://proxy.corp:3128\"\nEnvironment=\"NO_PROXY=10.0.0.0/8,.svc,.cluster.local\"\n" > /etc/systemd/system/containerd.service.d/http-proxy.conf
    systemctl daemon-reload && systemctl restart containerd
  '
  ```

  **Verification:** `curl -sSI https://<registry-host>/v2/` from a pod on the affected node returns HTTP 200 or 401 in under 2s; `crictl pull <registry>/<repo>:<tag>` completes; new pods reach `Running 1/1`.

### Cause G: Docker Hub anonymous pull rate limit exhausted

**Statement:** The node's egress IP has consumed its Docker Hub pull quota for the 6-hour window, so the registry returns HTTP 429 with body `toomanyrequests`.

**Chain:**
- root: the node's egress IP (often shared NAT) has exceeded Docker Hub's 100 anonymous pulls per 6-hour window per IPv4/IPv6 /64.
- s1: manifest and blob requests return HTTP 429 with a `Retry-After` header and body `code: TOOMANYREQUESTS`.
- s2: the runtime surfaces `pull access denied ... toomanyrequests: You have reached your pull rate limit`.
- s3: backoff alone does not help — the next 6-hour window must elapse, or the kubelet must authenticate.
- D: the pod stays in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `toomanyrequests` or `pull rate limit`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "toomanyrequests"} -->
- s1: [Step 10] `ratelimit-remaining: 0;w=21600` for the anonymous bearer token.
  <!-- match: {"step": 10, "predicate": "contains", "target": "ratelimit-remaining: 0"} -->
- root: [Symptom] image reference uses `docker.io/` or no registry prefix at all (defaulting to Docker Hub).

**Interventions:**
- **mitigation** (root): authenticate to Docker Hub to raise the limit to 200/6h.

  ```bash
  kubectl create secret docker-registry dockerhub-creds \
    --docker-server=https://index.docker.io/v1/ \
    --docker-username=<user> --docker-password=<pat> \
    -n <namespace>
  kubectl patch serviceaccount default -n <namespace> \
    -p '{"imagePullSecrets":[{"name":"dockerhub-creds"}]}'
  ```

  **Risk:** 200/6h scales poorly above a handful of nodes; for production fleets a paid tier or pull-through cache is the only durable answer. **Duration:** Until pull volume grows beyond 200/6h per node. **Verification:** Re-run Step 10; `ratelimit-limit` reflects the authenticated tier and pods reach `Running 1/1`.
- **remediation** (root): run a pull-through cache so every node sees the registry as locally hosted.

  ```bash
  # Example: containerd hosts.toml mirror for docker.io pointing to an internal Harbor.
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c '
    mkdir -p /etc/containerd/certs.d/docker.io
    cat > /etc/containerd/certs.d/docker.io/hosts.toml <<EOF
  server = "https://registry-1.docker.io"
  [host."https://mirror.internal:5000"]
    capabilities = ["pull", "resolve"]
  EOF
    systemctl restart containerd
  '
  ```

  **Verification:** `crictl pull docker.io/library/nginx:stable` on a node logs the mirror URL (`pulling from mirror.internal:5000`) in the containerd journal; Docker Hub `ratelimit-remaining` stops decrementing for cluster pulls; new pods using Docker Hub images reach `Running 1/1`. Deploy the mirror redundantly — a mirror failure blocks all new pod starts cluster-wide.

### Cause H: Registry TLS certificate not trusted by the container runtime

**Statement:** The registry presents a TLS certificate signed by a CA the node does not trust (self-signed, internal CA, or expired), so the runtime aborts the HTTPS handshake with `x509: certificate signed by unknown authority`.

**Chain:**
- root: the registry's certificate chain leads to a CA not in the node's system bundle (`/etc/ssl/certs/ca-certificates.crt` on Debian/Ubuntu, `/etc/pki/tls/certs/ca-bundle.crt` on RHEL), or the leaf cert has expired.
- s1: containerd/CRI-O's Go `crypto/tls` aborts the handshake before any manifest request is sent.
- s2: the runtime emits `failed to do request: ... x509: certificate signed by unknown authority` (or `x509: certificate has expired or is not yet valid`).
- D: the pod stays in ImagePullBackOff (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `x509: certificate signed by unknown authority` or `x509: certificate has expired`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "x509: certificate signed by unknown authority"} -->
- s1: [Step 7] `curl https://<registry-host>/v2/` from a pod returns `unable to get local issuer certificate`.
- s1: [Step 9] containerd journal shows the TLS error at the connection layer, before any auth attempt.

**Interventions:**
- **mitigation** (root): configure `skip_verify` in containerd's `hosts.toml` for the registry.

  ```bash
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c '
    mkdir -p /etc/containerd/certs.d/<registry-host>
    cat > /etc/containerd/certs.d/<registry-host>/hosts.toml <<EOF
  server = "https://<registry-host>"
  [host."https://<registry-host>"]
    skip_verify = true
  EOF
    systemctl restart containerd
  '
  ```

  **Risk:** `skip_verify = true` accepts any certificate and exposes the cluster to TLS MitM; acceptable for a non-production cluster while CA distribution rolls out, never for production. **Duration:** Hours, not days. **Verification:** Re-run Step 8; `crictl pull` succeeds on that node.
- **remediation** (root): install the registry CA into the node trust store.

  ```bash
  # On Debian/Ubuntu-based nodes:
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c '
    cp /tmp/registry-ca.crt /usr/local/share/ca-certificates/registry-ca.crt
    update-ca-certificates
    systemctl restart containerd
  '
  # Or scope the trust to containerd only via hosts.toml + ca_file:
  # [host."https://<registry-host>"]
  #   ca = "/etc/containerd/certs.d/<registry-host>/ca.crt"
  ```

  **Verification:** `openssl s_client -connect <registry-host>:443 -CAfile /etc/ssl/certs/ca-certificates.crt < /dev/null 2>&1 | grep "Verify return code: 0"` succeeds; `crictl pull <registry>/<repo>:<tag>` succeeds; pull events show `Normal Pulled`. Run via DaemonSet or node-bootstrap automation to cover every node.

### Cause I: Image manifest has no entry for the node's architecture or OS

**Statement:** The image is a single-architecture build (or a manifest list missing the node's platform), and the runtime rejects the pull with `no match for platform in manifest`.

**Chain:**
- root: the image is single-arch (built without `--platform`) or its manifest list lacks the scheduled node's `platform.os`/`platform.architecture` (e.g., a `linux/arm64`-only image on `linux/amd64` workers).
- s1: when the runtime selects a platform from the manifest list, no entry matches the node.
- s2: containerd emits `no match for platform in manifest <digest>: not found`.
- D: on heterogeneous clusters the symptom appears only on mismatched nodes (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `no match for platform in manifest` or `no matching manifest for linux/`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "no match for platform in manifest"} -->
- root: [Step 4] manifest list does not include the node's `kubectl get node <node-name> -o jsonpath='{.status.nodeInfo.architecture}'` value.
- root: [Symptom] cluster mixes `arm64` and `amd64` nodes; pull works on one architecture and fails on the other.

**Interventions:**
- **mitigation** (s1): pin the deployment to the architecture the image supports.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}}}'
  ```

  **Risk:** A `nodeSelector` works around the missing manifest but reduces scheduling flexibility and capacity headroom. **Duration:** Until a multi-arch image is published. **Verification:** Re-run Step 1 on a matching-arch node; pods reach `Running 1/1`.
- **remediation** (root): rebuild the image as a multi-arch manifest list with buildx.

  ```bash
  docker buildx create --use --name multiarch || true
  docker buildx build --platform linux/amd64,linux/arm64 \
    -t <registry>/<repo>:<tag> --push .
  crane manifest <registry>/<repo>:<tag> | jq '.manifests[].platform'
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `crane manifest <registry>/<repo>:<tag>` shows both `linux/amd64` and `linux/arm64` entries; pods scheduled to both architectures reach `Running 1/1`; no further `no match for platform` events.

### Cause J: imagePullPolicy is Never or IfNotPresent but the image is not on the node

**Statement:** The pod's `imagePullPolicy` is `Never` (or `IfNotPresent` with no local image) and the target image has not been pre-pulled to the node, so the kubelet skips the pull and surfaces `ErrImageNeverPull`.

**Chain:**
- root: the pod is configured `imagePullPolicy: Never` (or `IfNotPresent` with a never-pulled digest) and the image is not baked onto the node — common in air-gapped clusters where an operator forgot to update the node AMI.
- s1: the kubelet only asks the runtime whether the image is on disk; `crictl images` does not contain it.
- s2: the kubelet records `Failed to inspect image "<ref>": ErrImageNeverPull` and the container waits with reason `ErrImageNeverPull`.
- D: the pod stays `Pending`/`Waiting` indefinitely — `imagePullPolicy` semantics are non-recoverable from inside the cluster (see Symptom Recognition).

**Indicators:**
- s2: [Step 1] pull error contains `ErrImageNeverPull` or `Container image "..." is not present with pull policy of Never`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "ErrImageNeverPull"} -->
- root: [Step 2] `imagePullPolicy` is `Never` or `IfNotPresent`.
- s1: [Step 8] `crictl images | grep <repo>` on the node returns no rows for the requested tag/digest.

**Interventions:**
- **mitigation** (s1): pre-pull the image onto the target node.

  ```bash
  kubectl debug node/<node-name> -it --image=busybox -- chroot /host sh -c "crictl pull <registry>/<repo>:<tag>"
  ```

  **Risk:** Pre-pulling one node forces the pod to be re-scheduled there — fragile against drains, autoscaler scale-out, and node replacement. **Duration:** Until the image is baked into the node AMI or distributed via DaemonSet. **Verification:** Re-run Step 8 on that node; `crictl images` lists the tag and the pod transitions to `Running`.
- **remediation** (root): switch the pull policy back to `Always` so the kubelet handles distribution.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Always"}]'
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].imagePullPolicy}'` reports `Always`; new pods transition `ContainerCreating → Running` and Events show `Normal Pulling` followed by `Normal Pulled`.

### Cause Z: Unidentified

**Statement:** Diagnostic steps did not converge on a known image-pull failure mode; the pull error string does not match the indicators of Causes A–J.

**Chain:**
- root: the true root cause is outside the documented failure modes — e.g., a registry-side outage with non-standard error codes, an authenticating proxy injecting unexpected responses, a kubelet bug specific to the cluster version, or a recent CRI/CNI upgrade destabilizing the pull path.
- s1: the gathered evidence (image reference, pull policy, secret resolution, network probe, runtime journal, rate-limit headers, manifest platforms) does not isolate the cause.
- D: the kubelet recorded ImagePullBackOff but no Cause A–J indicator matched (see Symptom Recognition).

**Indicators:**
- root: [Default] ImagePullBackOff is confirmed (Step 1) but Causes A–J indicators do not match the pull error message and gathered evidence.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, attempt a holding restart, and escalate to the SME.

  ```bash
  kubectl rollout restart deployment/<deployment-name> -n <namespace>
  kubectl get events -n <namespace> --sort-by='.lastTimestamp' --field-selector reason=Failed -o wide
  ```

  **Risk:** Restarting buys time only if the failure was transient; a persistent registry/network problem re-enters backoff within minutes. **Duration:** Use only as a holding action while gathering richer artefacts (registry logs, packet capture, kubelet debug logs). **Verification:** Artefacts from Steps 1, 3, 7, 8, 9 are captured and attached to an incident ticket; hand-off is acknowledged by the receiving engineer and the registry owner / platform on-call, with a follow-up owner assigned.

## Prevention

- Pin images by immutable digest, not by tag: `image: <registry>/<repo>@sha256:<digest>`. Tag-only references break silently when a tag is moved or deleted upstream; digests make pulls reproducible.
- Set `imagePullPolicy: Always` on production deployments using mutable tags, and `imagePullPolicy: IfNotPresent` only when paired with a digest reference.
- Attach pull secrets to the namespace's `default` ServiceAccount so every pod inherits them automatically; reserve pod-level `imagePullSecrets` for special cases. Audit with `kubectl get sa -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"  "}{.imagePullSecrets[*].name}{"\n"}{end}'`.
- For ECR, use the kubelet credential provider with an IAM role that carries `AmazonEC2ContainerRegistryReadOnly` (or a tighter custom policy). Never bake static `aws ecr get-login-password` tokens into Kubernetes secrets — they expire after 12 hours and require automation to refresh.
- Run an in-cluster pull-through cache (Harbor, Distribution, Zot) for Docker Hub and any public registry to insulate the cluster from rate limits, registry outages, and external network failures. Configure containerd `hosts.toml` mirrors per registry host.
- Validate image existence in CI before deployment: `crane manifest <registry>/<repo>:<tag> > /dev/null || exit 1`. For multi-arch fleets, also assert `linux/amd64` AND `linux/arm64` entries exist in the manifest list.
- Rotate registry credentials with External Secrets Operator or Sealed Secrets backed by Vault/AWS Secrets Manager so token expiry triggers a controlled refresh rather than an outage.
- Alert on the failure surface, not the symptom: page on `kube_pod_container_status_waiting_reason{reason=~"ImagePullBackOff|ErrImagePull"} > 0` for `for: 5m`, scoped to production namespaces.
- Distribute trusted-registry CA certificates via the node-bootstrap process (cloud-init, AMI build, or node-bootstrap DaemonSet) so internal-CA registries work the first time a new node joins.
- Enforce a registry allow-list with admission control (Kyverno, OPA Gatekeeper, or ImagePolicyWebhook) so pods cannot reference unknown registries that the cluster's network egress doesn't permit.

## Sources

- [Kubernetes — Images](https://kubernetes.io/docs/concepts/containers/images/) — Priority 1. ImagePullPolicy defaults (Always/IfNotPresent/Never), ImagePullBackOff exponential backoff, imagePullSecrets, kubelet credential provider, multi-arch manifest lists, pre-pulled images and `ErrImageNeverPull`.
- [Kubernetes — Pull an Image from a Private Registry](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/) — Priority 1. `kubectl create secret docker-registry` exact syntax, `.dockerconfigjson` structure, attaching pull secrets to Pod and ServiceAccount, decoding existing secrets.
- [Kubernetes — Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) — Priority 1. `kubectl describe pod` Events for ImagePullBackOff diagnosis, typical `Failed to pull image` / `BackOff` / `ErrImagePull` / `ImagePullBackOff` event sequence.
- [AWS ECR — Troubleshoot Docker commands and issues](https://docs.aws.amazon.com/AmazonECR/latest/userguide/common-errors-docker.html) — Priority 1. `no basic auth credentials` causes, 12-hour authorization-token expiry from `GetAuthorizationToken`, regional registry endpoint pattern `<account>.dkr.ecr.<region>.amazonaws.com`, region-mismatched-token failure mode.
- [AWS EKS — Troubleshoot problems with Amazon EKS clusters and nodes](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html) — Priority 1. Node networking prerequisites for image pulls (public-IP or NAT route), `Not authorized for images` Windows-AMI variant, node-role IAM requirements for ECR.
- [Docker — Docker Hub usage and limits](https://docs.docker.com/docker-hub/usage/) — Priority 1. Anonymous 100/6h per IPv4 or IPv6 /64, authenticated personal 200/6h, paid tier unlimited, 429 Too Many Requests response with `toomanyrequests` body, authentication for higher limits.
