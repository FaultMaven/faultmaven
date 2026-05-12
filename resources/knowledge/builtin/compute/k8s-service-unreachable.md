---
id: "k8s-service-unreachable"
title: "Kubernetes Service Unreachable"
domain: compute
service: kubernetes
symptom_class: [service_unavailable, connection_refused]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [kubernetes, services, networking, dns, kube-proxy, endpoints, endpointslices]
difficulty: intermediate
---

## Symptom Recognition

Clients receive connection errors when contacting a Kubernetes Service by ClusterIP, NodePort, or DNS name. Common presentations from within a cluster pod:

```text
curl: (7) Failed to connect to my-service port 8080: Connection refused
curl: (28) Connection timed out after 30001 milliseconds
** server can't find my-service.default.svc.cluster.local: NXDOMAIN
wget: server returned error: HTTP/1.1 503 Service Unavailable
```

External clients via LoadBalancer or NodePort see `ERR_CONNECTION_REFUSED` or `504 Gateway Timeout`. Kubernetes events may show `Readiness probe failed` on backend pods. `kubectl get endpoints <service>` shows an empty `ENDPOINTS` column. Prometheus alert `KubernetesServiceEndpointsEmpty` fires.

## Applicability

Applies to Kubernetes 1.21+ (EndpointSlices default) on any distribution (self-managed, EKS, GKE, AKS). Requires `kubectl` with `get`/`describe` permissions on Services, Endpoints, EndpointSlices, Pods, and NetworkPolicies. Running ephemeral debug pods requires `create pod` permission. Node-level iptables/IPVS inspection requires SSH or `kubectl debug node` access. Kube-proxy DaemonSet restart requires `patch` on DaemonSets in `kube-system`.

## Diagnostic Steps

### Step 1: Confirm the Service exists and inspect its configuration

```bash
kubectl get svc <service-name> -n <namespace>
kubectl get svc <service-name> -n <namespace> -o yaml
```

Expected output: A Service with a non-`None` `clusterIP`, the correct `port`/`targetPort` mapping, and a populated `spec.selector`. A headless Service shows `clusterIP: None` — it has no VIP and kube-proxy does not program rules for it.

### Step 2: Check whether Endpoints or EndpointSlices are populated

```bash
kubectl get endpoints <service-name> -n <namespace>
kubectl get endpointslices -n <namespace> -l kubernetes.io/service-name=<service-name>
```

Expected output: One or more pod IPs listed under `ENDPOINTS`. An empty field (e.g., `<none>`) means no ready pods match the selector.

### Step 3: Compare the Service selector against pod labels

```bash
kubectl get svc <service-name> -n <namespace> -o jsonpath='{.spec.selector}'
kubectl get pods -n <namespace> --show-labels
kubectl get pods -n <namespace> -l <key>=<value>
```

Expected output: The selector key-value pairs printed, and matching pods listed by the label query. Zero results from the label query confirm a selector mismatch.

### Step 4: Inspect pod readiness status and readiness probe events

```bash
kubectl get pods -n <namespace> -l <selector-key>=<selector-value>
kubectl describe pod <pod-name> -n <namespace> | grep -A 10 "Readiness:"
kubectl get events -n <namespace> --field-selector involvedObject.name=<pod-name> | grep -i readiness
```

Expected output: Pods in `Running` status with `READY 1/1`. A `0/1` READY column with events containing `Readiness probe failed` indicates the pod is excluded from endpoints until the probe passes.

### Step 5: Test DNS resolution for the Service name

```bash
kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- \
  nslookup <service-name>.<namespace>.svc.cluster.local
kubectl exec -n <namespace> <pod-name> -- cat /etc/resolv.conf
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50
```

Expected output: `nslookup` resolves the FQDN to the Service `clusterIP`. `/etc/resolv.conf` should contain `nameserver <dns-service-ip>` and `search <namespace>.svc.cluster.local svc.cluster.local cluster.local`. CoreDNS pods should be `Running`.

### Step 6: Test direct pod IP connectivity bypassing the Service

```bash
kubectl get endpoints <service-name> -n <namespace> \
  -o jsonpath='{.subsets[*].addresses[*].ip}'
kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- \
  curl -sv http://<pod-ip>:<target-port>/
kubectl exec -n <namespace> <pod-name> -- ss -tlnp
```

Expected output: An HTTP response (e.g., `200 OK`) when hitting the pod IP directly. `ss -tlnp` lists the port the container is actually bound on. If the pod IP is unreachable or `ss` shows no matching listener, the application is at fault, not kube-proxy.

### Step 7: Verify kube-proxy is running and has programmed forwarding rules

```bash
kubectl get pods -n kube-system -l k8s-app=kube-proxy
kubectl logs -n kube-system -l k8s-app=kube-proxy --tail=50
# On the affected node (requires SSH or kubectl debug node):
sudo iptables -t nat -L KUBE-SERVICES -n | grep <cluster-ip>
sudo ipvsadm -L -n | grep <cluster-ip>
```

Expected output: All kube-proxy pods in `Running` state. `iptables` output shows a `KUBE-SVC-*` chain entry for the Service ClusterIP. `ipvsadm` shows a virtual server entry for the ClusterIP with real server entries for each pod IP.

### Step 8: Check for NetworkPolicies blocking ingress to backend pods

```bash
kubectl get networkpolicy -n <namespace>
kubectl describe networkpolicy -n <namespace>
```

Expected output: If any NetworkPolicy selects the backend pods with `policyTypes: [Ingress]`, only traffic from `from` selectors is allowed. An ingress policy with no matching `from` rule for the client pod blocks all other traffic.

## Causes

### Cause A: Selector Mismatch

**Statement:** The Service `spec.selector` contains a key-value pair that does not exactly match the labels on any running pod, so the controller creates no Endpoints.

**Mechanism:** The Service controller watches pods and builds EndpointSlices from pods whose labels satisfy every key-value pair in the selector. A single typo, renamed label, or missing label key produces an empty EndpointSlice, causing kube-proxy to program zero backend rules for the ClusterIP. All traffic to the Service IP is silently dropped or times out.

**Indicator:**

- [Step 2] `ENDPOINTS` column is empty or shows `<none>`
- [Step 3] `kubectl get pods -l <key>=<value>` returns zero pods

<!-- match: {"step": 2, "predicate": "contains", "target": "<none>"} -->

**Mitigation:**

- **Risk:** Updating the Service selector is safe and takes effect immediately with no pod restarts.
- **Command:**

  ```bash
  kubectl patch svc <service-name> -n <namespace> \
    -p '{"spec":{"selector":{"<correct-key>":"<correct-value>"}}}'
  ```

- **Duration:** Immediate — endpoint controller reconciles within seconds.

**Resolution:**

```bash
# Option A: fix the Service selector
kubectl patch svc <service-name> -n <namespace> \
  -p '{"spec":{"selector":{"app":"<correct-label-value>"}}}'

# Option B: add the missing label to the Deployment pod template
kubectl patch deployment <deployment-name> -n <namespace> \
  -p '{"spec":{"template":{"metadata":{"labels":{"app":"<correct-label-value>"}}}}}'
```

- **Impact:** Selector patch is cluster-wide for this Service; Deployment label patch triggers a rolling restart of pods.

- **Rollback:** `kubectl patch svc <service-name> -n <namespace> -p '{"spec":{"selector":{"app":"<old-value>"}}}'`

**Verification:** `kubectl get endpoints <service-name> -n <namespace>` — ENDPOINTS column lists one or more pod IPs within 5 seconds of the patch.

---

### Cause B: Pods Not Ready Due to Failing Readiness Probe

**Statement:** Backend pods are Running but excluded from Service Endpoints because their readiness probes are continuously failing.

**Mechanism:** The endpoint controller only adds a pod to EndpointSlices when all containers report `Ready=True`. A readiness probe that targets the wrong path, wrong port, or fires before the application has initialized keeps the pod's `Ready` condition `False`. kube-proxy therefore programs no backend rule for that pod, so zero endpoints are available even though pods exist and are Running.

**Indicator:**

- [Step 2] `ENDPOINTS` is empty
- [Step 4] Pod shows `READY 0/1` and events contain `Readiness probe failed`

<!-- match: {"step": 4, "predicate": "contains", "target": "Readiness probe failed"} -->

**Mitigation:**

- **Risk:** Temporarily removing the readiness probe allows unready pods into rotation; only do this in a degraded production situation where uptime outweighs correctness.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","readinessProbe":null}]}}}}'
  ```

- **Duration:** Until probe misconfiguration is corrected; revert immediately after traffic is restored.

**Resolution:**

```bash
# Inspect the current probe and test the health endpoint manually
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{.spec.containers[0].readinessProbe}' | jq .
kubectl exec <pod-name> -n <namespace> -- \
  curl -sv localhost:<probe-port><probe-path>

# Apply corrected probe via Deployment patch
kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/healthz"}]'
```

**Verification:** `kubectl get pods -n <namespace> -l <selector>` — all pods show `READY 1/1` and `kubectl get endpoints <service-name> -n <namespace>` lists pod IPs.

---

### Cause C: Port or TargetPort Mismatch

**Statement:** The Service `targetPort` field specifies a port number or name that does not match the port the container application actually listens on, causing immediate connection refusals at the pod.

**Mechanism:** kube-proxy correctly programs rules routing ClusterIP traffic to pod IPs, but routes it to the wrong port on the pod. The kernel delivers the packet to that port; because nothing is listening, the kernel returns a TCP RST, which manifests as `Connection refused` in the client. The pod IP is reachable but the port is wrong.

**Indicator:**

- [Step 2] EndpointSlices are populated (pod IPs appear)
- [Step 6] Direct curl to pod IP on the Service `targetPort` returns `Connection refused`, but `ss -tlnp` shows the container listening on a different port

<!-- match: {"step": 6, "predicate": "contains", "target": "Connection refused"} -->

**Mitigation:**

- **Risk:** Changing `targetPort` takes effect for new connections immediately; in-flight connections are not affected.
- **Command:**

  ```bash
  kubectl patch svc <service-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":<correct-port>}]'
  ```

- **Duration:** Immediate.

**Resolution:** **Same as Mitigation.**

- **Rollback:** Re-patch `targetPort` back to the original value.

**Verification:** `kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- curl -sv http://<service-name>.<namespace>.svc.cluster.local:<port>/` returns the expected HTTP status code.

---

### Cause D: DNS Resolution Failure (CoreDNS Down or Misconfigured)

**Statement:** CoreDNS pods are unavailable or misconfigured, preventing pods from resolving Service names to ClusterIP addresses.

**Mechanism:** Every pod's `/etc/resolv.conf` points to the CoreDNS ClusterIP as its nameserver. When a pod dials a Service by short name (e.g., `my-service`), the kernel appends the search domains from `resolv.conf` (e.g., `default.svc.cluster.local`) and queries CoreDNS. If CoreDNS pods are crashlooping, pending, or the Corefile contains a syntax error, DNS queries return `NXDOMAIN` or time out, and the application cannot resolve the Service IP at all.

**Indicator:**

- [Step 5] `nslookup` from a debug pod returns `NXDOMAIN` or `server can't find`
- [Step 5] `kubectl get pods -n kube-system -l k8s-app=kube-dns` shows `CrashLoopBackOff` or `0/1 Running`

<!-- match: {"step": 5, "predicate": "contains", "target": "NXDOMAIN"} -->

**Mitigation:**

- **Risk:** Medium — rolling restart of CoreDNS causes brief DNS unavailability cluster-wide (typically under 10 seconds with 2+ replicas).
- **Command:**

  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  ```

- **Duration:** 1–3 minutes until rollout completes.

**Resolution:**

```bash
# Inspect Corefile for syntax errors
kubectl get configmap coredns -n kube-system -o yaml

# If configmap is corrupt, restore the default Corefile
kubectl edit configmap coredns -n kube-system

# After fixing, restart CoreDNS
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system
```

**Verification:** `kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- nslookup <service-name>.<namespace>.svc.cluster.local` returns the Service ClusterIP.

---

### Cause E: kube-proxy Not Running or Has Stale/Missing Rules

**Statement:** kube-proxy DaemonSet pods are absent or crashlooping on one or more nodes, so no iptables/IPVS forwarding rules exist for the Service ClusterIP on those nodes.

**Mechanism:** kube-proxy watches Services and EndpointSlices and translates them into per-node iptables or IPVS rules. When kube-proxy crashes or is absent, traffic to the ClusterIP on that node has no forwarding rule and is dropped by the kernel. Pods scheduled on affected nodes cannot reach any Service by ClusterIP, while pods on healthy nodes are unaffected — this produces intermittent connectivity depending on which node the client pod is running on.

**Indicator:**

- [Step 6] Direct pod-IP connectivity works
- [Step 7] kube-proxy pods show `CrashLoopBackOff` or fewer pods than nodes, or `iptables -t nat -L KUBE-SERVICES` returns no entry for the ClusterIP

<!-- match: {"step": 7, "predicate": "absent", "target": "<cluster-ip>"} -->

**Mitigation:**

- **Risk:** Low to medium — a DaemonSet rolling restart briefly interrupts Service routing on each node in sequence; existing TCP connections may reset.
- **Command:**

  ```bash
  kubectl rollout restart daemonset/kube-proxy -n kube-system
  ```

- **Duration:** 2–5 minutes for full DaemonSet rollout.

**Resolution:**

```bash
# Restart kube-proxy and monitor rollout
kubectl rollout restart daemonset/kube-proxy -n kube-system
kubectl rollout status daemonset/kube-proxy -n kube-system

# Verify rules are reprogrammed on a node
sudo iptables -t nat -L KUBE-SERVICES -n | grep <cluster-ip>
# IPVS mode:
sudo ipvsadm -L -n | grep <cluster-ip>
```

**Verification:** `kubectl get pods -n kube-system -l k8s-app=kube-proxy` — all pods `Running 1/1`. Service connectivity test from a pod on the previously affected node returns expected HTTP response.

---

### Cause F: NetworkPolicy Blocking Ingress to Backend Pods

**Statement:** A NetworkPolicy selects the backend pods with an `Ingress` rule that does not include an allow entry for the client pod or namespace, silently dropping traffic at the network layer.

**Mechanism:** By default Kubernetes allows all pod-to-pod traffic. Once any NetworkPolicy selects a pod with `policyTypes: [Ingress]`, only traffic explicitly permitted by a `from` rule is delivered — all other ingress is blocked at the CNI layer before the pod's kernel even sees the packet. The connection attempt hangs (timeout rather than refused) because no TCP RST is sent.

**Indicator:**

- [Step 6] Direct pod-IP curl times out (not connection refused)
- [Step 8] A NetworkPolicy exists that selects the backend pods and has no `from` rule covering the client pod's namespace or labels

<!-- match: {"step": 8, "predicate": "contains", "target": "Ingress"} -->

**Mitigation:**

- **Risk:** Adding an allow rule to a NetworkPolicy opens a traffic path; review with the security team before applying in production.
- **Command:**

  ```bash
  kubectl apply -f - <<'EOF'
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: allow-client-to-backend
    namespace: <namespace>
  spec:
    podSelector:
      matchLabels:
        app: <backend-label>
    policyTypes:
    - Ingress
    ingress:
    - from:
      - podSelector:
          matchLabels:
            app: <client-label>
      ports:
      - protocol: TCP
        port: <target-port>
  EOF
  ```

- **Duration:** Effective immediately after apply.

**Resolution:** **Same as Mitigation.** For namespace-level access, replace `podSelector` with `namespaceSelector`.

**Verification:** Connectivity test from the client pod to `<service-name>.<namespace>.svc.cluster.local:<port>` succeeds within 5 seconds of policy apply.

---

### Cause G: Application Not Listening or Bound to Loopback Only

**Statement:** The container application is not listening on the declared `targetPort`, or is bound to `127.0.0.1` only, so all external connections are refused even though the pod is Running and Ready.

**Mechanism:** Endpoints are populated and kube-proxy rules are correct, so traffic reaches the pod IP on the target port. If the application has not started, has crashed silently, or binds to `localhost` (`127.0.0.1`) instead of `0.0.0.0`, the OS kernel has no listener on the pod's network interface and returns TCP RST immediately. This is indistinguishable from a port mismatch without inspecting the live socket table inside the container.

**Indicator:**

- [Step 2] EndpointSlices are populated
- [Step 6] Direct curl to pod IP returns `Connection refused` AND `ss -tlnp` inside the pod shows no listener on `targetPort`, or shows listener bound to `127.0.0.1`

<!-- match: {"step": 6, "predicate": "absent", "target": "0.0.0.0:<target-port>"} -->

**Mitigation:**

- **Risk:** Restarting the pod may cause brief downtime for connections to that replica.
- **Command:**

  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```

- **Duration:** Immediate; deployment controller recreates the pod.

**Resolution:**

```bash
# Confirm the bind address inside the container
kubectl exec <pod-name> -n <namespace> -- ss -tlnp

# If application is misconfigured to bind localhost, fix the application config
# or override via environment variable (application-specific):
kubectl set env deployment/<deployment-name> -n <namespace> \
  LISTEN_ADDR=0.0.0.0

# Force rolling restart to apply
kubectl rollout restart deployment/<deployment-name> -n <namespace>
```

**Verification:** `kubectl exec <pod-name> -n <namespace> -- ss -tlnp | grep <target-port>` shows a listener bound to `0.0.0.0` or `*` on the correct port. End-to-end Service curl succeeds.

---

### Cause Z: Unidentified

**Statement:** The Service is unreachable but none of the diagnostic steps reveal a clear cause such as selector mismatch, endpoint failure, DNS failure, kube-proxy failure, NetworkPolicy block, or application bind error.

**Mechanism:** Edge cases — including CNI plugin bugs, kernel netfilter table corruption, IPv6/dual-stack misconfiguration, or intermittent infrastructure faults — can cause service unreachability that does not manifest in standard diagnostic outputs. Escalation to cluster administrators and CNI vendor support is required.

**Indicator:**

- [Default] All diagnostic steps (1–8) show nominally correct configuration: endpoints are populated, DNS resolves correctly, kube-proxy rules exist, no blocking NetworkPolicy, and the application listens on the correct address, yet connectivity fails.

**Mitigation:**

- **Risk:** Low — packet capture and CNI plugin diagnostics are read-only.
- **Command:**

  ```bash
  # Capture traffic on the pod's veth interface on the node
  NODE=$(kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}')
  kubectl debug node/$NODE -it --image=nicolaka/netshoot -- \
    tcpdump -i any host <pod-ip> and port <target-port> -nn -c 100
  ```

- **Duration:** Capture can run for up to 10 minutes for intermittent issues.

**Resolution:** Out of runbook scope. Escalate to cluster administrator with packet capture output, `kubectl cluster-info dump`, and CNI plugin logs from the affected node.

**Verification:** After escalation, re-run the full diagnostic sequence (Steps 1–8) and confirm end-to-end Service connectivity from a debug pod.

## Prevention

Use consistent labeling standards across all Deployments and Services. Apply the recommended Kubernetes common labels so selectors are predictable and discoverable:

```yaml
metadata:
  labels:
    app.kubernetes.io/name: my-app
    app.kubernetes.io/component: backend
    app.kubernetes.io/version: "1.2.0"
```

Configure readiness probes on every pod behind a Service. The probe must target the same path and port that handles real traffic:

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
```

Use named ports in both Pod specs and Service specs to eliminate numeric port mismatch:

```yaml
# Pod spec
ports:
  - name: http
    containerPort: 8080
# Service spec
ports:
  - name: http
    port: 80
    targetPort: http
```

Alert on zero-endpoint Services to catch selector mismatches before they reach production:

```yaml
- alert: KubernetesServiceEndpointsEmpty
  expr: kube_endpoint_address_available{endpoint!="kubernetes"} == 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Service {{ $labels.namespace }}/{{ $labels.endpoint }} has no ready endpoints"
```

Add a smoke-test step in CI/CD pipelines that waits for pods to be ready and verifies Service connectivity before marking a deployment complete:

```bash
kubectl wait --for=condition=ready pod -l app=<label> -n <namespace> --timeout=120s
kubectl run -it --rm smoke --image=curlimages/curl --restart=Never -- \
  curl -sf http://<service-name>.<namespace>.svc.cluster.local:<port>/healthz
```

Document all NetworkPolicies and audit them on each deployment change. Default-deny policies applied without corresponding allow rules are the leading cause of unexpected Service blockages after migrations.

## Sources

- [Kubernetes: Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) — Primary diagnostic workflow for service connectivity failures; commands and expected outputs used in Steps 1–9
- [Kubernetes: Service Concepts](https://kubernetes.io/docs/concepts/services-networking/service/) — Port chain mechanics (port/targetPort/containerPort), selector-to-endpoint mapping, headless services, EndpointSlices
- [Kubernetes: Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/) — kube-proxy iptables/IPVS rule programming, `KUBE-SERVICES` chain inspection, stale rule detection, IPVS vs iptables diagnostic differences
