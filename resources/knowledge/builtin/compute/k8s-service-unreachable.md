---
id: k8s-service-unreachable
title: "Kubernetes Service Unreachable: Diagnosis and Resolution"
domain: compute
service: kubernetes
symptom_class:
  - service_unavailable
  - connection_refused
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - services
  - networking
  - dns
  - kube-proxy
  - endpoints
difficulty: intermediate
---

# Kubernetes Service Unreachable: Diagnosis and Resolution

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution. Requires `kubectl` access with permissions to get, describe Services, Endpoints, EndpointSlices, pods, and NetworkPolicies. Testing connectivity requires the ability to run ephemeral debug pods. Node-level diagnostics (iptables/IPVS inspection) require SSH access.

A Kubernetes Service becomes unreachable when clients (pods or external traffic) cannot connect to the Service's ClusterIP, NodePort, or LoadBalancer address. The connection may time out, be refused, or return no response. This indicates a breakdown in the chain from DNS resolution through kube-proxy rules to backend pod endpoints.

Kubernetes Services provide stable network endpoints backed by a set of pods selected by label selectors. The connection path involves multiple components: CoreDNS resolves the Service name to a ClusterIP, kube-proxy (or a CNI plugin in proxyless mode) programs iptables/IPVS rules to forward traffic from the ClusterIP to backend pod IPs, and the pod must be listening on the target port. A failure in any component breaks connectivity.

Common root causes include selector mismatch (the Service's label selector does not match any running pod's labels, resulting in zero endpoints), no ready endpoints (backend pods exist but are not ready due to failing readiness probes), port mismatch (the Service `targetPort` does not match the port the application listens on), DNS resolution failure (CoreDNS is down or misconfigured), kube-proxy failure (kube-proxy is not running or has stale iptables/IPVS rules), NetworkPolicy blocking traffic (a policy denies ingress to the target pods or egress from the source), pod not listening (the application has crashed or is bound to localhost only), and namespace mismatch (the client references the Service without the correct namespace qualifier).

Typical presentation from a client pod:

```text
$ curl http://my-service:8080/api
curl: (7) Failed to connect to my-service port 8080: Connection refused
```

Or timeout:

```text
$ curl http://my-service:8080/api
curl: (28) Connection timed out after 30001 milliseconds
```

Or DNS failure:

```text
$ nslookup my-service
** server can't find my-service: NXDOMAIN
```

## Diagnostic Steps

### Step 1: Verify the Service Exists and Is Configured Correctly

**What this checks:** Whether the Service object exists with the correct type, ClusterIP, ports, and selector.

```bash
kubectl get svc <service-name> -n <namespace>
kubectl describe svc <service-name> -n <namespace>
```

**Expected output:** The Service with an assigned ClusterIP (not `None` unless headless), correct Port/TargetPort mapping, and a Selector that should match backend pods.

**What the finding means:** If the Service does not exist, it needs to be created. If ClusterIP is `None`, it is a headless Service (no load balancing via kube-proxy). If the Port or TargetPort is wrong, traffic is routed to the wrong port on the pod.

### Step 2: Check If Endpoints Exist

**What this checks:** Whether the Service has backend pod IPs registered as endpoints.

```bash
# Check endpoints
kubectl get endpoints <service-name> -n <namespace>

# Check EndpointSlices (preferred in Kubernetes 1.21+)
kubectl get endpointslices -l kubernetes.io/service-name=<service-name> -n <namespace>
```

**Expected output:** A list of pod IPs and ports. If the endpoints list is empty (no IPs), there are no matching ready pods.

**What the finding means:** Empty endpoints means either the selector does not match any pods (proceed to Step 3) or all matching pods are not ready (proceed to Step 4). If endpoints exist, the issue is further down the chain (kube-proxy, network, or the pod itself -- skip to Step 5).

### Step 3: Verify Selector Matches Pod Labels

**What this checks:** Whether the Service selector labels match the labels on running pods.

```bash
# Get the Service selector
kubectl get svc <service-name> -n <namespace> -o jsonpath='{.spec.selector}' | jq .

# List pods that match the selector
kubectl get pods -n <namespace> -l <key>=<value>

# Compare with pod labels
kubectl get pods -n <namespace> --show-labels
```

**Expected output:** The selector key-value pairs and pods that match them.

**What the finding means:** If no pods match the selector, either the selector is wrong (typo, outdated label) or the pods have incorrect labels (common after a deployment refactor). The fix is to align the selector with the pod labels or vice versa.

### Step 4: Check Pod Readiness

**What this checks:** Whether backend pods exist but are failing their readiness probes, which excludes them from Service endpoints.

```bash
# Check pod status and readiness
kubectl get pods -n <namespace> -l <selector-key>=<selector-value>

# Check readiness probe details
kubectl describe pod <pod-name> -n <namespace> | grep -A 10 "Readiness:"

# Check if readiness probe is failing
kubectl events --for pod/<pod-name> -n <namespace> | grep -i readiness
```

**Expected output:** Pod status showing `Running` but `READY 0/1`, and events indicating readiness probe failures.

**What the finding means:** Pods must pass their readiness probe to be added to Service endpoints. If a readiness probe is failing, the pod is excluded from traffic. Fix the readiness probe configuration or the application's health endpoint.

### Step 5: Test DNS Resolution

**What this checks:** Whether CoreDNS can resolve the Service name to the ClusterIP.

```bash
# Run a DNS test pod
kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- nslookup <service-name>.<namespace>.svc.cluster.local

# Check from inside an existing pod
kubectl exec <pod-name> -n <namespace> -- cat /etc/resolv.conf
kubectl exec <pod-name> -n <namespace> -- nslookup <service-name>
```

**Expected output:** The DNS query should return the Service ClusterIP address.

**What the finding means:** If DNS returns `NXDOMAIN`, either the Service does not exist, the namespace qualifier is wrong, or CoreDNS is not functioning. If DNS returns the correct IP but the connection still fails, the issue is in kube-proxy or the pod itself.

If DNS fails, check CoreDNS:

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50
```

### Step 6: Test Direct Pod Connectivity

**What this checks:** Whether the backend pod is reachable directly by IP, bypassing the Service and kube-proxy.

```bash
# Get pod IPs from endpoints
kubectl get endpoints <service-name> -n <namespace> -o jsonpath='{.subsets[*].addresses[*].ip}'

# Test direct connection to a pod IP
kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- curl -s -o /dev/null -w "%{http_code}" http://<pod-ip>:<target-port>/
```

**Expected output:** An HTTP response code (e.g., 200) confirming the pod is reachable directly.

**What the finding means:** If direct pod connectivity works but the Service ClusterIP does not, the issue is in kube-proxy or iptables rules. If direct pod connectivity also fails, the issue is in the pod itself (application not listening, wrong port, or NetworkPolicy blocking traffic).

### Step 7: Check kube-proxy

**What this checks:** Whether kube-proxy is running and has programmed the correct forwarding rules for the Service.

```bash
# Check kube-proxy pods
kubectl get pods -n kube-system -l k8s-app=kube-proxy

# Check kube-proxy logs
kubectl logs -n kube-system -l k8s-app=kube-proxy --tail=50

# On a node, check iptables rules for the Service ClusterIP
sudo iptables -t nat -L KUBE-SERVICES -n | grep <cluster-ip>

# For IPVS mode
sudo ipvsadm -ln | grep <cluster-ip>
```

**Expected output:** kube-proxy pods in Running state, and iptables/IPVS rules mapping the ClusterIP to backend pod IPs.

**What the finding means:** If kube-proxy is not running, no Service routing works on that node. If kube-proxy is running but rules are missing or stale, a restart will regenerate them. In IPVS mode, check that virtual servers and real servers are correctly configured.

### Step 8: Check Network Policies

**What this checks:** Whether a NetworkPolicy is blocking traffic between the client and the backend pods.

```bash
# List NetworkPolicies in the namespace
kubectl get networkpolicy -n <namespace>

# Describe policies to check ingress/egress rules
kubectl describe networkpolicy -n <namespace>
```

**Expected output:** NetworkPolicy rules showing which traffic is allowed or denied.

**What the finding means:** If a NetworkPolicy exists with an ingress rule, only traffic matching the `from` selector is allowed. Pods not matching the allowed sources are blocked. If no NetworkPolicy exists in the namespace, traffic is unrestricted by default.

## Mitigation

### Option 1: Fix Selector Mismatch

Use when endpoints are empty because the Service selector does not match pod labels.

- **Risk:** Low. Updating the selector only changes which pods receive traffic.
- **Command:**
  ```bash
  kubectl patch svc <service-name> -n <namespace> \
    -p '{"spec":{"selector":{"app":"<correct-label>"}}}'
  ```
- **Verify:**
  ```bash
  kubectl get endpoints <service-name> -n <namespace>
  ```
  Endpoints should now list pod IPs.
- **Duration:** Immediate (seconds).

### Option 2: Fix Port Configuration

Use when the Service targetPort does not match the port the pod listens on.

- **Risk:** Low. Only changes the traffic routing target.
- **Command:**
  ```bash
  kubectl patch svc <service-name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":<correct-port>}]'
  ```
- **Verify:**
  ```bash
  kubectl run -it --rm test --image=curlimages/curl --restart=Never -- \
    curl -s http://<service-name>.<namespace>.svc.cluster.local:<port>/
  ```
- **Duration:** Immediate (seconds).

### Option 3: Restart kube-proxy

Use when kube-proxy has stale rules or is in a bad state.

- **Risk:** Low to Medium. There may be a brief disruption to Service routing cluster-wide during the rolling restart.
- **Command:**
  ```bash
  kubectl rollout restart daemonset/kube-proxy -n kube-system
  ```
- **Verify:**
  ```bash
  kubectl get pods -n kube-system -l k8s-app=kube-proxy -w
  kubectl run -it --rm test --image=curlimages/curl --restart=Never -- \
    curl -s http://<service-name>.<namespace>.svc.cluster.local:<port>/
  ```
- **Duration:** 1 to 5 minutes for the DaemonSet rollout.

### Option 4: Restart CoreDNS

Use when DNS resolution is failing cluster-wide.

- **Risk:** Medium. DNS is briefly unavailable during restart, affecting all pod DNS lookups cluster-wide.
- **Command:**
  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  ```
- **Verify:**
  ```bash
  kubectl get pods -n kube-system -l k8s-app=kube-dns -w
  kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- \
    nslookup <service-name>.<namespace>.svc.cluster.local
  ```
- **Duration:** 1 to 3 minutes.

## Root Cause Resolution

**If** the Service selector labels do not match any pod labels **then** update either the Service selector or the pod labels to align:

```bash
# Option A: Update the Service selector
kubectl patch svc <service-name> -n <namespace> \
  -p '{"spec":{"selector":{"app":"<correct-label>"}}}'

# Option B: Update the Deployment template labels
kubectl patch deployment <deployment-name> -n <namespace> \
  -p '{"spec":{"template":{"metadata":{"labels":{"app":"<correct-label>"}}}}}'
```

**If** pods exist but are not ready and events show readiness probe failures **then** fix the readiness probe:

```bash
# Check what the readiness probe expects
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].readinessProbe}' | jq .

# Test the health endpoint manually
kubectl exec <pod-name> -n <namespace> -- curl -s localhost:<probe-port><probe-path>
```

Fix the application's health endpoint or adjust the probe path, port, `initialDelaySeconds`, or `failureThreshold`.

**If** the Service `targetPort` does not match the container's listening port **then** verify which port the application listens on:

```bash
kubectl exec <pod-name> -n <namespace> -- ss -tlnp
# Or
kubectl exec <pod-name> -n <namespace> -- netstat -tlnp
```

Ensure the `targetPort` in the Service matches the `containerPort` in the pod spec and the port the application actually binds to.

**If** DNS resolution fails for Service names **then** check CoreDNS health:

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide
kubectl get configmap coredns -n kube-system -o yaml
kubectl get endpoints kube-dns -n kube-system
```

If CoreDNS pods are crashlooping, check logs for configuration errors and fix the Corefile.

**If** direct pod access works but Service ClusterIP does not **then** kube-proxy rules may be stale:

```bash
# On the affected node, flush and regenerate rules
sudo iptables -t nat -F KUBE-SERVICES
kubectl rollout restart daemonset/kube-proxy -n kube-system
```

**If** a NetworkPolicy blocks ingress to the target pods **then** update the policy to allow traffic from the source:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-frontend
  namespace: <namespace>
spec:
  podSelector:
    matchLabels:
      app: <backend-app>
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: <frontend-app>
    ports:
    - protocol: TCP
      port: <target-port>
```

## Verification

After applying a fix, confirm end-to-end Service connectivity.

```bash
# 1. Verify endpoints are populated
kubectl get endpoints <service-name> -n <namespace>
# Output should list one or more pod IPs with the correct port
```

```bash
# 2. Test DNS resolution
kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- \
  nslookup <service-name>.<namespace>.svc.cluster.local
# DNS query should return the Service ClusterIP
```

```bash
# 3. Test Service connectivity
kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- \
  curl -s -o /dev/null -w "%{http_code}\n" http://<service-name>.<namespace>.svc.cluster.local:<port>/
# Should return expected HTTP status code (e.g., 200)
```

```bash
# 4. Verify from multiple source pods on different nodes
kubectl run -it --rm test-cross-node --image=curlimages/curl --restart=Never \
  -n <other-namespace> -- curl -s http://<service-name>.<namespace>.svc.cluster.local:<port>/
```

## Prevention

**Use consistent labeling standards.** Adopt a consistent labeling convention across all deployments and services using recommended Kubernetes labels:

```yaml
metadata:
  labels:
    app.kubernetes.io/name: my-app
    app.kubernetes.io/version: "1.0.0"
    app.kubernetes.io/component: frontend
```

Use these same labels in Service selectors to avoid mismatches.

**Configure readiness probes for all pods behind Services.** Every pod behind a Service should have a readiness probe that accurately reflects whether the pod can handle traffic:

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
```

**Monitor Service endpoints.** Alert when a Service has zero endpoints:

```yaml
- alert: KubernetesServiceEndpointsEmpty
  expr: kube_endpoint_address_available{endpoint!="kubernetes"} == 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Service {{ $labels.namespace }}/{{ $labels.endpoint }} has no ready endpoints"
```

**Use port names instead of numbers.** Define named ports in both pods and Services to reduce port mismatch errors:

```yaml
# In the pod spec
ports:
  - name: http
    containerPort: 8080

# In the Service spec
ports:
  - name: http
    port: 80
    targetPort: http  # References the named port
```

**Test Service connectivity in CI/CD.** Add integration tests that verify Service connectivity after deployment:

```bash
kubectl wait --for=condition=ready pod -l app=<app-label> -n <namespace> --timeout=120s
kubectl run -it --rm smoke-test --image=curlimages/curl --restart=Never -- \
  curl -sf http://<service-name>.<namespace>.svc.cluster.local:<port>/healthz
```

**Document NetworkPolicies.** Maintain documentation of all NetworkPolicies and their intended traffic flow. Regularly audit policies to ensure they do not inadvertently block legitimate traffic.

## Sources

- [Kubernetes: Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) -- Comprehensive guide for debugging Service connectivity issues
- [Kubernetes: Service Concepts](https://kubernetes.io/docs/concepts/services-networking/service/) -- How Services, selectors, endpoints, and kube-proxy work
- [Kubernetes: DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) -- DNS resolution behavior for Services
- [Kubernetes: Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/) -- iptables and IPVS proxy modes, kube-proxy behavior
- [Kubernetes: kube-proxy Subtleties](https://kubernetes.io/blog/2019/03/29/kube-proxy-subtleties-debugging-an-intermittent-connection-reset/) -- Debugging intermittent connection resets caused by kube-proxy
