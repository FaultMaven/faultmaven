---
id: "k8s-service-unreachable"
title: "Kubernetes Service Unreachable"
domain: compute
service: kubernetes
symptom_class: [service_unavailable, connection_refused]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Chain:**
- root: Service selector has a key-value pair matching no running pod's labels
- s1: The endpoint controller builds an empty EndpointSlice for the Service
- s2: kube-proxy programs zero backend rules for the ClusterIP
- D: traffic to the Service IP is silently dropped or times out (Symptom)

**Indicators:**
- root: [Step 3] `kubectl get pods -l <key>=<value>` returns zero pods
- s1: [Step 2] `ENDPOINTS` column is empty or shows `<none>`

**Interventions:**
- **remediation** (root): fix the Service selector or add the missing label to the Deployment pod template.

  ```bash
  # Option A: fix the Service selector
  kubectl patch svc <service-name> -n <namespace> \
    -p '{"spec":{"selector":{"app":"<correct-label-value>"}}}'

  # Option B: add the missing label to the Deployment pod template
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"metadata":{"labels":{"app":"<correct-label-value>"}}}}}'
  ```

  **Verification:** re-run Step 2; `kubectl get endpoints <service-name> -n <namespace>` lists one or more pod IPs within 5 seconds of the patch.
- **mitigation** (root): patch the Service selector to immediately match the live pod labels.

  ```bash
  kubectl patch svc <service-name> -n <namespace> \
    -p '{"spec":{"selector":{"<correct-key>":"<correct-value>"}}}'
  ```

  **Risk:** Updating the Service selector is safe and takes effect immediately with no pod restarts. **Duration:** Immediate — endpoint controller reconciles within seconds. **Verification:** Step 2 shows pod IPs in ENDPOINTS.

---

### Cause B: Pods Not Ready Due to Failing Readiness Probe

**Statement:** Backend pods are Running but excluded from Service Endpoints because their readiness probes are continuously failing.

**Chain:**
- root: readiness probe targets a wrong path/port or fires before app init
- s1: the pod's `Ready` condition stays `False`
- s2: the endpoint controller omits the pod from EndpointSlices
- s3: kube-proxy programs no backend rule, so zero endpoints are available
- D: clients cannot reach the Service even though pods are Running (Symptom)

**Indicators:**
- root: [Step 4] pod shows `READY 0/1` and events contain `Readiness probe failed`
- s2: [Step 2] `ENDPOINTS` is empty

**Interventions:**
- **remediation** (root): inspect and correct the readiness probe so it targets the real health endpoint.

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

  **Verification:** re-run Step 4; all pods show `READY 1/1` and Step 2 lists pod IPs.
- **mitigation** (s1): remove the readiness probe to force unready pods into rotation.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","readinessProbe":null}]}}}}'
  ```

  **Risk:** Allows unready pods into rotation; only do this in a degraded production situation where uptime outweighs correctness. **Duration:** Until probe misconfiguration is corrected; revert immediately after traffic is restored. **Verification:** Step 2 lists pod IPs.

---

### Cause C: Port or TargetPort Mismatch

**Statement:** The Service `targetPort` specifies a port number or name that does not match the port the container application actually listens on, causing immediate connection refusals at the pod.

**Chain:**
- root: Service `targetPort` differs from the container's actual listening port
- s1: kube-proxy routes ClusterIP traffic to the wrong port on the pod IP
- s2: the kernel finds no listener on that port and returns a TCP RST
- D: clients see `Connection refused` though the pod IP is reachable (Symptom)

**Indicators:**
- root: [Step 6] direct curl to pod IP on the Service `targetPort` returns `Connection refused`, but `ss -tlnp` shows the container listening on a different port
- s1: [Step 2] EndpointSlices are populated (pod IPs appear)

**Interventions:**
- **remediation** (root): patch `targetPort` to the port the container actually listens on.

  ```bash
  kubectl patch svc <service-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":<correct-port>}]'
  ```

  **Verification:** `kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- curl -sv http://<service-name>.<namespace>.svc.cluster.local:<port>/` returns the expected HTTP status code.
- **mitigation** (root): apply the same `targetPort` patch as a fast interception; new connections route correctly immediately.

  ```bash
  kubectl patch svc <service-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":<correct-port>}]'
  ```

  **Risk:** Takes effect for new connections immediately; in-flight connections are not affected. Re-patch `targetPort` back to the original value to roll back. **Duration:** Immediate. **Verification:** Step 6 curl to the Service returns the expected status.

---

### Cause D: DNS Resolution Failure (CoreDNS Down or Misconfigured)

**Statement:** CoreDNS pods are unavailable or misconfigured, preventing pods from resolving Service names to ClusterIP addresses.

**Chain:**
- root: CoreDNS pods are crashlooping/pending or the Corefile has a syntax error
- s1: DNS queries for the Service FQDN return `NXDOMAIN` or time out
- s2: the application cannot resolve the Service name to its ClusterIP
- D: clients fail to connect to the Service by name (Symptom)

**Indicators:**
- root: [Step 5] `kubectl get pods -n kube-system -l k8s-app=kube-dns` shows `CrashLoopBackOff` or `0/1 Running`
- s1: [Step 5] `nslookup` from a debug pod returns `NXDOMAIN` or `server can't find`

**Interventions:**
- **remediation** (root): repair the Corefile and restart CoreDNS.

  ```bash
  # Inspect Corefile for syntax errors
  kubectl get configmap coredns -n kube-system -o yaml

  # If configmap is corrupt, restore the default Corefile
  kubectl edit configmap coredns -n kube-system

  # After fixing, restart CoreDNS
  kubectl rollout restart deployment/coredns -n kube-system
  kubectl rollout status deployment/coredns -n kube-system
  ```

  **Verification:** re-run Step 5; `nslookup <service-name>.<namespace>.svc.cluster.local` returns the Service ClusterIP.
- **mitigation** (root): roll-restart CoreDNS to clear crashlooping pods.

  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  ```

  **Risk:** Medium — rolling restart of CoreDNS causes brief DNS unavailability cluster-wide (typically under 10 seconds with 2+ replicas). **Duration:** 1–3 minutes until rollout completes. **Verification:** Step 5 nslookup resolves the FQDN.

---

### Cause E: kube-proxy Not Running or Has Stale/Missing Rules

**Statement:** kube-proxy DaemonSet pods are absent or crashlooping on one or more nodes, so no iptables/IPVS forwarding rules exist for the Service ClusterIP on those nodes.

**Chain:**
- root: kube-proxy is absent or crashlooping on one or more nodes
- s1: no iptables/IPVS forwarding rule is programmed for the ClusterIP on those nodes
- s2: the kernel drops ClusterIP traffic from pods scheduled on affected nodes
- D: connectivity is intermittent depending on the client pod's node (Symptom)

**Indicators:**
- root: [Step 7] kube-proxy pods show `CrashLoopBackOff` or fewer pods than nodes, or `iptables -t nat -L KUBE-SERVICES` returns no entry for the ClusterIP
- s2: [Step 6] direct pod-IP connectivity works

**Interventions:**
- **remediation** (root): restart kube-proxy and confirm rules are reprogrammed on the node.

  ```bash
  # Restart kube-proxy and monitor rollout
  kubectl rollout restart daemonset/kube-proxy -n kube-system
  kubectl rollout status daemonset/kube-proxy -n kube-system

  # Verify rules are reprogrammed on a node
  sudo iptables -t nat -L KUBE-SERVICES -n | grep <cluster-ip>
  # IPVS mode:
  sudo ipvsadm -L -n | grep <cluster-ip>
  ```

  **Verification:** re-run Step 7; all kube-proxy pods are `Running 1/1` and connectivity from a pod on the affected node returns the expected HTTP response.
- **mitigation** (root): roll-restart the kube-proxy DaemonSet to reprogram rules.

  ```bash
  kubectl rollout restart daemonset/kube-proxy -n kube-system
  ```

  **Risk:** Low to medium — a DaemonSet rolling restart briefly interrupts Service routing on each node in sequence; existing TCP connections may reset. **Duration:** 2–5 minutes for full DaemonSet rollout. **Verification:** Step 7 shows all kube-proxy pods Running.

---

### Cause F: NetworkPolicy Blocking Ingress to Backend Pods

**Statement:** A NetworkPolicy selects the backend pods with an `Ingress` rule that lacks an allow entry for the client pod or namespace, silently dropping traffic at the network layer.

**Chain:**
- root: a NetworkPolicy selects the backend pods with `policyTypes: [Ingress]` and no `from` rule for the client
- s1: the CNI layer blocks all non-permitted ingress before the pod's kernel sees the packet
- s2: the connection attempt hangs with no TCP RST sent
- D: clients time out connecting to the Service (Symptom)

**Indicators:**
- root: [Step 8] a NetworkPolicy exists that selects the backend pods and has no `from` rule covering the client pod's namespace or labels
- s2: [Step 6] direct pod-IP curl times out (not connection refused)

**Interventions:**
- **remediation** (root): add an allow rule for the client pod (or namespace) to the NetworkPolicy.

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

  **Verification:** connectivity test from the client pod to `<service-name>.<namespace>.svc.cluster.local:<port>` succeeds within 5 seconds of policy apply. For namespace-level access, replace `podSelector` with `namespaceSelector`.
- **mitigation** (root): apply the same allow rule to immediately open the blocked path.

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

  **Risk:** Adding an allow rule opens a traffic path; review with the security team before applying in production. **Duration:** Effective immediately after apply. **Verification:** Step 6 connectivity to the Service succeeds.

---

### Cause G: Application Not Listening or Bound to Loopback Only

**Statement:** The container application is not listening on the declared `targetPort`, or is bound to `127.0.0.1` only, so all external connections are refused even though the pod is Running and Ready.

**Chain:**
- root: the app is not started/crashed, or binds `127.0.0.1` instead of `0.0.0.0`
- s1: the kernel has no listener on the pod's network interface for that port
- s2: traffic reaching the pod IP on the target port gets an immediate TCP RST
- D: clients see `Connection refused` despite populated endpoints (Symptom)

**Indicators:**
- root: [Step 6] `ss -tlnp` inside the pod shows no listener on `targetPort`, or a listener bound to `127.0.0.1`
- s2: [Step 2] EndpointSlices are populated

**Interventions:**
- **remediation** (root): fix the application bind address to `0.0.0.0` and roll the deployment.

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

  **Verification:** `kubectl exec <pod-name> -n <namespace> -- ss -tlnp | grep <target-port>` shows a listener bound to `0.0.0.0` or `*`; end-to-end Service curl succeeds.
- **mitigation** (s1): delete the affected pod so the controller recreates it (clears a silently-crashed listener).

  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```

  **Risk:** Restarting the pod may cause brief downtime for connections to that replica. **Duration:** Immediate; deployment controller recreates the pod. **Verification:** Step 6 direct curl to the pod IP returns an HTTP response.

---

### Cause Z: Unidentified

**Statement:** The Service is unreachable but none of the diagnostic steps reveal a clear cause such as selector mismatch, endpoint failure, DNS failure, kube-proxy failure, NetworkPolicy block, or application bind error.

**Chain:**
- root: an edge-case fault (CNI bug, netfilter corruption, IPv6/dual-stack, intermittent infra) not surfaced by standard diagnostics
- D: the Service is unreachable despite nominally correct configuration (Symptom)

**Indicators:**
- [Default] All diagnostic steps (1–8) show nominally correct configuration: endpoints are populated, DNS resolves correctly, kube-proxy rules exist, no blocking NetworkPolicy, and the application listens on the correct address, yet connectivity fails.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the cluster administrator / SME.

  ```bash
  # Capture traffic on the pod's veth interface on the node
  NODE=$(kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.nodeName}')
  kubectl debug node/$NODE -it --image=nicolaka/netshoot -- \
    tcpdump -i any host <pod-ip> and port <target-port> -nn -c 100
  ```

  **Risk:** Low — packet capture and CNI plugin diagnostics are read-only. **Duration:** Capture can run for up to 10 minutes for intermittent issues. **Verification:** after escalation with packet capture, `kubectl cluster-info dump`, and CNI logs, re-run the full diagnostic sequence (Steps 1–8) and confirm end-to-end Service connectivity from a debug pod.

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
