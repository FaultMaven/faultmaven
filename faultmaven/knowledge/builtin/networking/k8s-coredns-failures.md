---
id: k8s-coredns-failures
title: "Kubernetes CoreDNS Resolution Failures — Diagnosis and Resolution"
domain: networking
service: kubernetes
symptom_class:
  - connection_refused
  - timeout
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - kubernetes
  - coredns
  - dns
  - resolution
  - ndots
  - kube-system
  - udp
difficulty: intermediate
---

## Problem Definition

This runbook covers CoreDNS resolution failures in Kubernetes clusters. It applies to Kubernetes 1.21+ clusters using CoreDNS as the cluster DNS provider (the default since Kubernetes 1.13). Diagnosis requires `kubectl` access with permissions to read pods, services, configmaps, and logs in the `kube-system` namespace, plus the ability to exec into pods for DNS testing. For conntrack-related issues, SSH or node-level access is needed to inspect kernel parameters.

CoreDNS resolution failures occur when pods cannot resolve internal service names (e.g., `my-svc.my-namespace.svc.cluster.local`) or external hostnames (e.g., `api.example.com`). Because nearly all inter-service communication in Kubernetes relies on DNS, a CoreDNS failure cascades into widespread application errors: connection timeouts, HTTP 503 responses, and failed health checks across the cluster. The failure manifests as DNS lookup timeouts (5-second delays per attempt), `NXDOMAIN` for valid service names, or `SERVFAIL` responses.

CoreDNS runs as a Deployment in the `kube-system` namespace and serves DNS queries on UDP/TCP port 53 via the `kube-dns` ClusterIP Service. Pods are configured by the kubelet to use this Service IP as their nameserver (visible in `/etc/resolv.conf`).

Common causes include:

- **CoreDNS pods not running** — the Deployment is scaled to zero, pods are in CrashLoopBackOff, or they were evicted due to node resource pressure.
- **CoreDNS misconfiguration** — a bad Corefile (the CoreDNS ConfigMap) causes the process to fail on startup or return SERVFAIL for valid queries.
- **RBAC permission errors** — CoreDNS lacks ClusterRole permissions to list Services, Endpoints, or EndpointSlices, causing SERVFAIL responses.
- **ndots misconfiguration** — pods with a low `ndots` value send unqualified names as absolute queries, bypassing the cluster search path and failing to resolve internal services.
- **UDP packet drops** — conntrack table exhaustion on nodes causes UDP DNS packets to be silently dropped, resulting in intermittent timeouts.
- **Network policy blocking port 53** — a NetworkPolicy prevents pods from reaching CoreDNS on UDP/TCP port 53.
- **Upstream DNS failure** — CoreDNS forwards external queries to an upstream resolver (configured via `forward . /etc/resolv.conf` or explicit IPs) that is unreachable or misconfigured.
- **DNS search domain limits** — pods with custom `dnsConfig` exceed the maximum number of search domains (6) or nameservers (3), causing the kubelet to truncate the configuration.
- **Loop detection** — CoreDNS detects a forwarding loop (e.g., node `/etc/resolv.conf` points back to CoreDNS ClusterIP) and shuts down with a loop plugin error.

## Diagnostic Steps

### Step 1: Check CoreDNS pod status

Checks whether CoreDNS pods are running and healthy. If pods are down, no DNS queries can be served.

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide
```

Expected output: two or more pods in `Running` state with `READY 1/1`. If pods show `CrashLoopBackOff`, `OOMKilled`, or `Pending`, DNS is degraded or down. Check the reason with:

```bash
kubectl describe pods -n kube-system -l k8s-app=kube-dns
```

Look for events such as OOMKilled (increase memory limits), Liveness probe failure (Corefile error or resource starvation), or image pull errors (registry connectivity).

### Step 2: Check CoreDNS logs for errors

Checks CoreDNS controller logs for specific error messages that identify the failure category.

```bash
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=100
```

Key error patterns and what they mean:

| Log Message | Root Cause |
|---|---|
| `plugin/loop: Loop ... detected for zone "."` | CoreDNS forward target resolves back to itself (loop) |
| `SERVFAIL` in query logs | CoreDNS cannot resolve the query (check upstream or RBAC) |
| `plugin/kubernetes: failed to list *v1.Service` | RBAC permissions missing for CoreDNS ServiceAccount |
| `plugin/kubernetes: failed to list *v1.EndpointSlice` | RBAC permissions missing for EndpointSlice access |
| `plugin/forward: no nameservers found` | Upstream resolver in Corefile is unreachable |
| `[FATAL] plugin/loop: Loop detected` | Fatal loop — CoreDNS will exit and restart |

If the logs show `Loop detected`, the immediate fix is changing the `forward` target (see Root Cause Resolution). If RBAC errors appear, the ClusterRole needs updating.

### Step 3: Test DNS resolution from a debug pod

Deploys a diagnostic pod to test DNS resolution directly, isolating whether the problem is CoreDNS or a specific application's configuration.

```bash
kubectl run dnsutils --image=registry.k8s.io/e2e-test-images/agnhost:2.39 --restart=Never -- sleep 3600
kubectl exec -it dnsutils -- nslookup kubernetes.default
```

Expected successful output:

```
Server:    10.96.0.10
Address 1: 10.96.0.10 kube-dns.kube-system.svc.cluster.local

Name:      kubernetes.default
Address 1: 10.96.0.1 kubernetes.default.svc.cluster.local
```

If internal resolution fails, test external:

```bash
kubectl exec -it dnsutils -- nslookup google.com
```

If external fails but internal works, the upstream forwarder is the problem. If both fail, CoreDNS itself is the problem. If the debug pod resolves fine but the application pod does not, check the application pod's `dnsPolicy` and `dnsConfig`.

### Step 4: Inspect pod resolv.conf

Checks the DNS configuration injected into the pod by the kubelet to verify it points to the correct nameserver and has the expected search domains.

```bash
kubectl exec -it dnsutils -- cat /etc/resolv.conf
```

Expected output:

```
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5
```

Verify: `nameserver` matches the `kube-dns` Service ClusterIP; `search` includes `<namespace>.svc.cluster.local svc.cluster.local cluster.local`; `ndots:5` is present (the Kubernetes default). If `ndots` is lower than 5, short service names like `my-service` are queried as absolute names first, bypassing the search path and failing to resolve cluster-internal services.

### Step 5: Verify the kube-dns Service and endpoints

Checks that the `kube-dns` Service exists and has endpoints pointing to running CoreDNS pods.

```bash
# Check the Service exists and has a ClusterIP
kubectl get svc kube-dns -n kube-system

# Check endpoints are populated
kubectl get endpointslices -n kube-system -l kubernetes.io/service-name=kube-dns
```

Expected output: the Service has a ClusterIP (e.g., `10.96.0.10`) and the EndpointSlice lists the IPs of running CoreDNS pods. If endpoints are empty, CoreDNS pods are not running or their labels do not match the Service selector.

### Step 6: Check CoreDNS ConfigMap (Corefile)

Checks the Corefile configuration for syntax errors or misconfigured plugins that prevent CoreDNS from functioning.

```bash
kubectl get configmap coredns -n kube-system -o yaml
```

Expected output: a valid Corefile containing a `kubernetes cluster.local in-addr.arpa ip6.arpa` block for cluster DNS, a `forward . /etc/resolv.conf` or explicit upstream IPs for external resolution, an `errors` plugin for error logging, and no syntax errors. A malformed Corefile prevents CoreDNS from starting. The `forward . /etc/resolv.conf` line is the most common source of loop issues when the node's resolv.conf points back to the cluster DNS.

### Step 7: Check for conntrack table exhaustion (UDP drops)

Checks whether the node's conntrack table is full, causing UDP DNS packets to be silently dropped. This produces intermittent DNS timeouts that are difficult to diagnose at the application layer.

```bash
# Check conntrack table usage (run on affected node)
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max

# Check for dropped packets due to conntrack full
dmesg | grep "nf_conntrack: table full"

# Check UDP packet drop stats
cat /proc/net/snmp | grep Udp
```

Expected output: `nf_conntrack_count` well below `nf_conntrack_max`. If the count is near the max, UDP DNS packets are being silently dropped. The `dmesg` output confirms drops with `nf_conntrack: table full` messages. The `InErrors` column in the UDP stats shows dropped packets.

### Step 8: Check NetworkPolicies blocking DNS

Checks whether a NetworkPolicy in the affected namespace blocks egress to CoreDNS on port 53.

```bash
# List all NetworkPolicies
kubectl get networkpolicy -A

# Check if any policy in the affected namespace restricts egress to port 53
kubectl get networkpolicy -n <affected-namespace> -o yaml | grep -A 20 "egress"
```

Expected output: either no NetworkPolicies in the namespace, or egress policies that explicitly allow UDP and TCP port 53. A restrictive egress NetworkPolicy that does not include a port 53 allowance blocks all DNS resolution for pods in that namespace.

## Mitigation

### Option 1: Restart CoreDNS pods

- **Risk**: Low. Brief DNS resolution gap (typically under 10 seconds) while new pods start. Cached DNS entries in applications may cover the gap.
- **Command**:

```bash
kubectl rollout restart deployment/coredns -n kube-system
```

- **Verify**:

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl exec -it dnsutils -- nslookup kubernetes.default
```

- **Duration**: 10-30 seconds for pods to restart and begin serving queries.

### Option 2: Scale up CoreDNS replicas

- **Risk**: Low. Adds capacity without disrupting existing pods. May consume additional node resources.
- **Command**:

```bash
kubectl scale deployment/coredns -n kube-system --replicas=4
```

- **Verify**:

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
```

- **Duration**: 10-20 seconds for new pods to become ready.

### Option 3: Increase conntrack table size

- **Risk**: Low. Increases kernel memory usage for connection tracking. Appropriate when conntrack table is full.
- **Command**:

```bash
# On affected nodes
sudo sysctl -w net.netfilter.nf_conntrack_max=262144
```

- **Verify**:

```bash
cat /proc/sys/net/netfilter/nf_conntrack_max
dmesg | grep "nf_conntrack: table full"
```

- **Duration**: Immediate. Make persistent by adding to `/etc/sysctl.d/99-conntrack.conf`.

### Option 4: Bypass cluster DNS with host DNS for emergency resolution

- **Risk**: Medium. Only for emergency situations where CoreDNS is completely down and a specific pod needs DNS using the node's resolver. This bypasses cluster DNS entirely — internal service names will not resolve.
- **Command**:

```bash
kubectl patch deployment <name> -n <namespace> -p '{"spec":{"template":{"spec":{"dnsPolicy":"Default"}}}}'
```

- **Verify**:

```bash
kubectl exec -it <new-pod> -- nslookup google.com
```

- **Duration**: Immediate after pod restart. Revert once CoreDNS is restored.

## Root Cause Resolution

**If** CoreDNS pods are in CrashLoopBackOff with `plugin/loop: Loop detected` → the node's `/etc/resolv.conf` points to a local DNS cache (e.g., `127.0.0.53` from systemd-resolved) that forwards to the cluster DNS, creating a loop. Fix by configuring the `forward` plugin to use explicit upstream DNS servers instead of `/etc/resolv.conf`.

```bash
kubectl edit configmap coredns -n kube-system
# Change: forward . /etc/resolv.conf
# To:     forward . 8.8.8.8 8.8.4.4
```

**If** CoreDNS logs show `failed to list *v1.Service` or `failed to list *v1.EndpointSlice` → the CoreDNS ClusterRole is missing required permissions. Verify and fix the RBAC configuration.

```bash
kubectl describe clusterrole system:coredns
# Ensure these resources are listed with [list watch] verbs:
#   endpoints, services, pods, namespaces, endpointslices
kubectl edit clusterrole system:coredns
```

**If** pods cannot resolve internal services but external resolution works, and `ndots` is set below 5 → a low `ndots` value (e.g., `ndots:1`) causes short names like `my-service` to be queried as absolute names first, skipping the search path. Restore the Kubernetes default of `ndots:5` in the pod spec or use fully qualified domain names ending with a trailing dot.

```yaml
# In the pod spec, fix dnsConfig:
spec:
  dnsConfig:
    options:
      - name: ndots
        value: "5"
```

**If** conntrack table is full and DNS packets are being dropped → increase the conntrack table size persistently and consider deploying NodeLocal DNSCache to reduce the number of conntrack entries for DNS.

```bash
echo "net.netfilter.nf_conntrack_max=262144" | sudo tee /etc/sysctl.d/99-conntrack.conf
sudo sysctl --system
```

**If** a NetworkPolicy is blocking DNS egress → add an egress rule allowing UDP and TCP port 53 to the `kube-system` namespace (or to the CoreDNS pod CIDR).

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: <affected-namespace>
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - to: []
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

**If** CoreDNS Corefile has a syntax error preventing startup → fix the ConfigMap and restart CoreDNS. Validate the Corefile syntax by checking CoreDNS logs after the edit.

```bash
kubectl edit configmap coredns -n kube-system
kubectl rollout restart deployment/coredns -n kube-system
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=20
```

**If** upstream DNS resolver is unreachable (external queries fail) → verify the upstream DNS servers specified in the `forward` plugin are reachable from the nodes. Replace with known-good resolvers if needed.

```bash
# Test from a node
nslookup google.com 8.8.8.8

# Update Corefile forward targets
kubectl edit configmap coredns -n kube-system
# Ensure: forward . 8.8.8.8 8.8.4.4 { prefer_udp }
```

## Verification

After applying a fix, verify DNS resolution is fully restored:

1. Confirm CoreDNS pods are running and ready:

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
```

2. Test internal service resolution:

```bash
kubectl exec -it dnsutils -- nslookup kubernetes.default
kubectl exec -it dnsutils -- nslookup kube-dns.kube-system.svc.cluster.local
```

3. Test external name resolution:

```bash
kubectl exec -it dnsutils -- nslookup google.com
```

4. Test cross-namespace resolution:

```bash
kubectl exec -it dnsutils -- nslookup kube-dns.kube-system
```

5. Verify no SERVFAIL or error entries in CoreDNS logs:

```bash
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50 | grep -i "error\|servfail\|fail"
```

6. Check CoreDNS metrics for error rates (if Prometheus is deployed):

```bash
kubectl exec -n kube-system $(kubectl get pods -n kube-system -l k8s-app=kube-dns -o name | head -1) -- wget -qO- http://localhost:9153/metrics | grep coredns_dns_responses_total
```

7. Monitor affected application pods for recovery:

```bash
kubectl get pods -n <affected-namespace> | grep -v Running
```

## Prevention

1. **Deploy NodeLocal DNSCache** — Run a DNS caching agent on every node to reduce latency, eliminate conntrack entries for DNS, and provide resilience if CoreDNS pods are temporarily unavailable. See the Kubernetes NodeLocal DNSCache documentation.

2. **Enable CoreDNS autoscaling** — Deploy the `dns-autoscaler` addon (proportional autoscaler) so CoreDNS replicas scale with cluster size. This prevents DNS capacity from becoming a bottleneck as the cluster grows.

3. **Monitor CoreDNS metrics** — CoreDNS exposes Prometheus metrics on port 9153. Alert on `coredns_dns_responses_total{rcode="SERVFAIL"}` rate increases, `coredns_panics_total`, and pod restart counts.

4. **Set resource requests and limits for CoreDNS** — Ensure CoreDNS pods have appropriate memory and CPU requests so they are not evicted under node pressure. Avoid setting memory limits too low, which can cause OOMKill under query spikes.

5. **Avoid lowering ndots without understanding the impact** — Reducing `ndots` below 5 improves external DNS resolution latency but breaks unqualified internal service name resolution. If you must lower `ndots`, ensure all internal references use fully qualified domain names.

6. **Use explicit upstream DNS in the Corefile** — Avoid `forward . /etc/resolv.conf` on nodes running systemd-resolved, as the local stub resolver (`127.0.0.53`) can create forwarding loops. Use explicit IPs like `forward . 8.8.8.8 8.8.4.4`.

7. **Ensure NetworkPolicies allow DNS egress** — Any namespace with restrictive egress NetworkPolicies must include an explicit rule allowing UDP and TCP port 53 traffic. Make this a standard part of your NetworkPolicy templates.

8. **Monitor conntrack table utilization** — Alert when `nf_conntrack_count` exceeds 75% of `nf_conntrack_max`. High DNS query rates combined with a full conntrack table cause silent packet drops that are difficult to diagnose.

9. **Test DNS resolution in CI/CD** — Include DNS resolution checks in your deployment pipeline smoke tests to catch misconfigurations (e.g., broken Corefile, missing RBAC) before they affect production traffic.

10. **Keep CoreDNS up to date** — Track the CoreDNS version recommended for your Kubernetes version. Upgrades include bug fixes, security patches, and performance improvements.

## Sources

- [Kubernetes: Debugging DNS Resolution](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/) — Official guide for diagnosing DNS problems in Kubernetes clusters, including test pod setup and CoreDNS log analysis.
- [Kubernetes: Using CoreDNS for Service Discovery](https://kubernetes.io/docs/tasks/administer-cluster/coredns/) — Official documentation on CoreDNS configuration, Corefile management, and migration from kube-dns.
- [Kubernetes: DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) — Reference for DNS record formats, dnsPolicy options, dnsConfig, ndots behavior, and search domain construction.
- [Kubernetes: Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) — Official guide covering DNS resolution testing, resolv.conf inspection, and endpoint verification.
- [Kubernetes: NodeLocal DNSCache](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/) — Documentation on deploying node-level DNS caching to improve DNS reliability and reduce conntrack pressure.
