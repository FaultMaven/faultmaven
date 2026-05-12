---
id: "k8s-coredns-failures"
title: "Kubernetes CoreDNS Resolution Failures"
domain: networking
service: kubernetes
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [coredns, dns, ndots, conntrack, kube-dns, kube-system, networkpolicy]
difficulty: intermediate
---

## Symptom Recognition

- Pods receive `connection refused` or `timeout` errors when contacting other services by name
- `nslookup <service-name>` or `dig` from within a pod returns `SERVFAIL`, `NXDOMAIN`, or times out
- Application logs show `dial tcp: lookup <host> on 10.96.0.10:53: i/o timeout` or `no such host`
- DNS resolution latency spikes to 5–30 seconds per query (UDP retry timeout threshold)
- External hostnames fail to resolve while internal service names succeed, or vice versa
- CoreDNS pods in `kube-system` show `CrashLoopBackOff`, `OOMKilled`, or `0/1 Running`
- CoreDNS logs contain `plugin/loop: Loop ... detected`, `SERVFAIL`, or `failed to list *v1.Service`
- Prometheus metric `coredns_dns_responses_total{rcode="SERVFAIL"}` is non-zero and rising
- `dmesg` on affected nodes shows `nf_conntrack: table full, dropping packet`

## Applicability

- Kubernetes 1.13+ clusters using CoreDNS as the cluster DNS provider (default since 1.13)
- Required access: `kubectl` with permissions to read/exec pods, configmaps, services, endpoints, and logs in `kube-system`; `describe` and `get` on NetworkPolicy cluster-wide
- For conntrack inspection (Step 7): SSH or privileged node access, or a privileged DaemonSet pod
- Tools needed: `kubectl`, `nslookup` or `dig` (available inside test pods), `dmesg`, `sysctl`

## Diagnostic Steps

### Step 1: Check CoreDNS pod status

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide
```

Expected output: two or more pods in `Running` state, `READY 1/1`. Pods in `CrashLoopBackOff`, `OOMKilled`, or `Pending` indicate DNS is degraded or down.

### Step 2: Inspect CoreDNS logs for error patterns

```bash
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=100
```

Expected output: startup info lines with no `[ERROR]` entries. Key patterns to look for:

- `plugin/loop: Loop ... detected for zone "."` — forwarding loop
- `plugin/kubernetes: failed to list *v1.Service` — RBAC error
- `plugin/kubernetes: failed to list *v1.EndpointSlice` — RBAC error
- `plugin/forward: no nameservers found` — upstream resolver unreachable
- `[FATAL] plugin/loop: Loop detected` — CoreDNS will restart repeatedly

### Step 3: Test DNS resolution from a debug pod

```bash
kubectl run dnsutils --image=registry.k8s.io/e2e-test-images/agnhost:2.39 \
  --restart=Never -- sleep 3600
kubectl exec -it dnsutils -- nslookup kubernetes.default
kubectl exec -it dnsutils -- nslookup google.com
```

Expected output for internal:

```text
Server:    10.96.0.10
Address 1: 10.96.0.10 kube-dns.kube-system.svc.cluster.local
Name:      kubernetes.default
Address 1: 10.96.0.1 kubernetes.default.svc.cluster.local
```

If internal succeeds but external fails, the upstream forwarder is misconfigured. If both fail, CoreDNS itself is the problem.

### Step 4: Inspect pod resolv.conf

```bash
kubectl exec -it dnsutils -- cat /etc/resolv.conf
```

Expected output:

```text
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5
```

Verify: `nameserver` matches `kube-dns` Service ClusterIP; `ndots` is `5` (the Kubernetes default). A lower `ndots` value (e.g., `1`) causes short service names to be sent as absolute queries, skipping the cluster search path.

### Step 5: Verify the kube-dns Service and endpoints

```bash
kubectl get svc kube-dns -n kube-system
kubectl get endpointslices -n kube-system -l kubernetes.io/service-name=kube-dns
```

Expected output: Service has a ClusterIP; EndpointSlice lists IPs of running CoreDNS pods. Empty endpoints mean CoreDNS pods are not running or their pod labels do not match the Service selector.

### Step 6: Inspect the CoreDNS Corefile ConfigMap

```bash
kubectl get configmap coredns -n kube-system -o yaml
```

Expected output: a valid Corefile with a `kubernetes cluster.local in-addr.arpa ip6.arpa` block and a `forward` directive. The line `forward . /etc/resolv.conf` is the most common source of forwarding loops when the node resolver points back to `127.0.0.53` (systemd-resolved).

### Step 7: Check conntrack table saturation on affected nodes

```bash
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
dmesg | grep "nf_conntrack: table full"
cat /proc/net/snmp | grep -i "udp "
```

Expected output: `nf_conntrack_count` well below `nf_conntrack_max`. Any `nf_conntrack: table full` in dmesg confirms UDP DNS packets are being silently dropped.

### Step 8: Check NetworkPolicies blocking DNS egress

```bash
kubectl get networkpolicy -A
kubectl get networkpolicy -n <affected-namespace> -o yaml
```

Expected output: either no NetworkPolicies in the affected namespace, or egress policies that explicitly allow UDP and TCP port 53. A restrictive egress policy without a port 53 allowance blocks all DNS resolution.

## Causes

### Cause A: CoreDNS pods not running

**Statement:** CoreDNS pods are absent, in `CrashLoopBackOff`, or evicted, leaving no DNS server available to handle pod queries.

**Mechanism:** CoreDNS runs as a Deployment in `kube-system` and serves all cluster DNS traffic. When all replicas are down, every DNS query from every pod in the cluster times out after the kernel UDP retry cycle (typically 5–30 seconds per attempt). Applications that don't cache DNS experience repeated failures.

**Indicator:**

- [Step 1] pods show status other than `Running` with `READY 1/1`
- [Symptom] cluster-wide DNS failures affecting all namespaces simultaneously

<!-- match: {"step": 1, "predicate": "absent", "target": "Running"} -->

**Mitigation:**

- **Risk:** Brief DNS gap (under 30 seconds) while replacement pods schedule and become ready
- **Command:**

  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  ```

- **Duration:** 30–60 seconds; revert if new pods also crash (indicates a deeper configuration issue)

**Resolution:**

```bash
kubectl rollout restart deployment/coredns -n kube-system
kubectl scale deployment/coredns -n kube-system --replicas=3
```

- **Impact:** Cluster-wide; all pods regain DNS after new replicas become `Ready`
- **Rollback:** `kubectl scale deployment/coredns -n kube-system --replicas=2`

**Verification:** `kubectl get pods -n kube-system -l k8s-app=kube-dns` shows all pods `Running 1/1`; `kubectl exec -it dnsutils -- nslookup kubernetes.default` returns a valid IP.

---

### Cause B: Forwarding loop — CoreDNS resolves back to itself

**Statement:** The CoreDNS `forward` plugin is configured to use `forward . /etc/resolv.conf`, and the node's `/etc/resolv.conf` points to a local stub resolver (e.g., `127.0.0.53` from systemd-resolved) that forwards queries back to the cluster DNS Service IP, creating an infinite loop.

**Mechanism:** On nodes running systemd-resolved, `/etc/resolv.conf` typically names `127.0.0.53` as the nameserver. CoreDNS reads this file, forwards external queries to `127.0.0.53`, which then forwards them to the `kube-dns` ClusterIP, which routes back to CoreDNS — completing the loop. CoreDNS detects the loop via its `loop` plugin and terminates, triggering `CrashLoopBackOff`.

**Indicator:**

- [Step 2] log line `plugin/loop: Loop ... detected for zone "."` or `[FATAL] plugin/loop: Loop detected`
- [Step 6] Corefile contains `forward . /etc/resolv.conf` and node `/etc/resolv.conf` contains `127.0.0.53` or `127.0.0.1`

<!-- match: {"step": 2, "predicate": "contains", "target": "Loop"} -->

**Mitigation:**

- **Risk:** Low; replaces a non-functional forwarder with explicit well-known resolvers
- **Command:**

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Change: forward . /etc/resolv.conf
  # To:     forward . 8.8.8.8 8.8.4.4
  ```

- **Duration:** Immediate after CoreDNS reloads (the `reload` plugin auto-reloads within 30 seconds)

**Resolution:**

```bash
kubectl patch configmap coredns -n kube-system --type=merge \
  -p '{"data":{"Corefile":".:53 {\n    errors\n    health {\n       lameduck 5s\n    }\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n       pods insecure\n       fallthrough in-addr.arpa ip6.arpa\n       ttl 30\n    }\n    prometheus :9153\n    forward . 8.8.8.8 8.8.4.4\n    cache 30\n    loop\n    reload\n    loadbalance\n}\n"}}'
kubectl rollout restart deployment/coredns -n kube-system
```

- **Impact:** All external DNS queries now go directly to Google DNS; internal cluster names unaffected
- **Rollback:** Revert to `forward . /etc/resolv.conf` only on nodes not running systemd-resolved

**Verification:** `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=20` shows no `Loop detected` entries; CoreDNS pods stay `Running` without restarting.

---

### Cause C: RBAC missing — CoreDNS cannot list Services or EndpointSlices

**Statement:** The `system:coredns` ClusterRole lacks the required verbs (`list`, `watch`) on `services`, `endpoints`, or `endpointslices`, preventing CoreDNS from building its internal service-to-IP mapping.

**Mechanism:** CoreDNS uses the Kubernetes plugin to watch the API server for Service and Endpoint changes and build an in-memory DNS table. Without `list` and `watch` on these resources, CoreDNS cannot populate the table and returns `SERVFAIL` for all cluster-internal names. External queries may still succeed if the upstream forwarder is reachable.

**Indicator:**

- [Step 2] log line `plugin/kubernetes: failed to list *v1.Service` or `plugin/kubernetes: failed to list *v1.EndpointSlice`
- [Step 3] internal `nslookup kubernetes.default` returns `SERVFAIL`; external `nslookup google.com` succeeds

<!-- match: {"step": 2, "predicate": "contains", "target": "failed to list"} -->

**Mitigation:**

- **Risk:** Low; read-only permissions addition does not expand write access
- **Command:**

  ```bash
  kubectl describe clusterrole system:coredns
  ```

- **Duration:** Inspect only; apply resolution immediately

**Resolution:**

```bash
kubectl edit clusterrole system:coredns
# Ensure the rules include:
#   - apiGroups: [""]
#     resources: ["endpoints", "services", "pods", "namespaces"]
#     verbs: ["list", "watch"]
#   - apiGroups: ["discovery.k8s.io"]
#     resources: ["endpointslices"]
#     verbs: ["list", "watch"]
```

- **Impact:** Cluster-wide; CoreDNS regains the ability to resolve internal names without restart
- **Rollback:** Remove the added rules (no service disruption beyond re-breaking internal DNS)

**Verification:** `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=20` shows no `failed to list` errors; `kubectl exec -it dnsutils -- nslookup kubernetes.default` resolves successfully.

---

### Cause D: ndots misconfiguration — short service names bypass cluster search path

**Statement:** A pod's `dnsConfig` sets `ndots` below 5, causing short internal service names (e.g., `my-service`) to be sent as absolute queries rather than expanded through the cluster search path, producing `NXDOMAIN` for valid internal services.

**Mechanism:** The `ndots` option in `/etc/resolv.conf` controls how many dots a query must contain before it is sent as an absolute name. The Kubernetes default of `ndots:5` ensures that `my-service` (0 dots) is first tried with each search domain appended (`my-service.default.svc.cluster.local`). A lower value (e.g., `ndots:1`) causes `my-service` to be tried as an absolute name first and fail with `NXDOMAIN` before the search domains are tried.

**Indicator:**

- [Step 4] `/etc/resolv.conf` shows `options ndots:` value less than `5`
- [Step 3] `nslookup my-service` fails but `nslookup my-service.default.svc.cluster.local` succeeds
- [Symptom] only certain pods fail DNS resolution while others in the same cluster succeed

<!-- match: {"step": 4, "predicate": "absent", "target": "ndots:5"} -->

**Mitigation:**

- **Risk:** Low; restoring the cluster default `ndots` value
- **Command:**

  ```bash
  kubectl patch deployment <name> -n <namespace> --type=merge \
    -p '{"spec":{"template":{"spec":{"dnsConfig":{"options":[{"name":"ndots","value":"5"}]}}}}}'
  ```

- **Duration:** Takes effect after pods are rescheduled (rolling restart)

**Resolution:**

```yaml
# In pod/deployment spec:
spec:
  dnsConfig:
    options:
      - name: ndots
        value: "5"
```

- **Impact:** Per-pod/deployment; use fully qualified names (trailing dot) if a lower `ndots` is required for external lookup performance
- **Rollback:** Remove the `dnsConfig.options` override to revert to the cluster default

**Verification:** `kubectl exec -it <pod> -- cat /etc/resolv.conf` shows `ndots:5`; `nslookup my-service` resolves without needing the FQDN.

---

### Cause E: Conntrack table exhaustion causing UDP DNS packet drops

**Statement:** The node's conntrack table is full, causing the kernel to silently drop incoming UDP DNS packets, producing intermittent 5–30 second DNS timeouts with no error logged at the CoreDNS layer.

**Mechanism:** Every UDP DNS query creates a conntrack entry that persists for 30 seconds by default. Clusters with high DNS query rates or many short-lived connections can exhaust `nf_conntrack_max`. When the table is full, new UDP packets are dropped silently — CoreDNS never receives the query, and the pod's resolver retries after a 5-second timeout, producing characteristic latency spikes.

**Indicator:**

- [Step 7] `nf_conntrack_count` at or near `nf_conntrack_max`
- [Step 7] `dmesg` contains `nf_conntrack: table full, dropping packet`
- [Symptom] intermittent DNS timeouts with no corresponding errors in CoreDNS logs

<!-- match: {"step": 7, "predicate": "contains", "target": "nf_conntrack: table full"} -->

**Mitigation:**

- **Risk:** Increases kernel memory for connection tracking; safe to double the default value
- **Command:**

  ```bash
  sudo sysctl -w net.netfilter.nf_conntrack_max=262144
  ```

- **Duration:** Immediate; make persistent via sysctl configuration file

**Resolution:**

```bash
echo "net.netfilter.nf_conntrack_max=262144" | \
  sudo tee /etc/sysctl.d/99-conntrack.conf
sudo sysctl --system
```

- **Impact:** Node-level; must be applied to all affected nodes; long-term fix is NodeLocal DNSCache which eliminates conntrack entries for DNS entirely
- **Rollback:** `sudo sysctl -w net.netfilter.nf_conntrack_max=<original-value>`

**Verification:** `cat /proc/sys/net/netfilter/nf_conntrack_count` stays well below `nf_conntrack_max`; `dmesg | grep "nf_conntrack"` shows no new `table full` entries; DNS latency returns to sub-millisecond.

---

### Cause F: NetworkPolicy blocking DNS egress on port 53

**Statement:** A NetworkPolicy in the affected namespace restricts egress traffic and does not include an explicit allow rule for UDP and TCP port 53, blocking all DNS queries from pods in that namespace.

**Mechanism:** A NetworkPolicy with `policyTypes: [Egress]` and no matching egress rule blocks all outbound traffic including DNS. CoreDNS runs in `kube-system` on UDP/TCP port 53. Pods that cannot reach the `kube-dns` ClusterIP on port 53 receive no DNS responses and experience connection timeouts when trying to reach services by name.

**Indicator:**

- [Step 8] NetworkPolicy with `policyTypes: [Egress]` exists in the affected namespace and has no `port: 53` allowance
- [Step 3] DNS resolution fails from an affected pod but succeeds from a pod in a different namespace without NetworkPolicies

<!-- match: {"step": 8, "predicate": "contains", "target": "Egress"} -->

**Mitigation:**

- **Risk:** Low; adding a targeted port 53 egress rule does not open other traffic
- **Command:**

  ```bash
  kubectl apply -f - <<EOF
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
      - ports:
          - protocol: UDP
            port: 53
          - protocol: TCP
            port: 53
  EOF
  ```

- **Duration:** Immediate after applying

**Resolution:** Same as Mitigation.

- **Impact:** Namespace-scoped; does not affect other namespaces or ingress traffic
- **Rollback:** `kubectl delete networkpolicy allow-dns-egress -n <affected-namespace>`

**Verification:** `kubectl exec -it <pod-in-namespace> -- nslookup kubernetes.default` resolves successfully; no DNS timeouts in application logs.

---

### Cause G: Upstream DNS forwarder unreachable

**Statement:** The upstream resolver configured in the CoreDNS `forward` plugin is unreachable from the cluster nodes, causing all external hostname queries to return `SERVFAIL` while internal cluster-name resolution continues to work.

**Mechanism:** CoreDNS forwards queries for names outside `cluster.local` to the upstream resolver(s) configured in the `forward` directive. If those resolvers are unreachable — due to firewall rules, incorrect IP configuration, or VPC routing gaps — CoreDNS returns `SERVFAIL` for all external names. Internal Kubernetes service resolution (handled by the `kubernetes` plugin) is unaffected.

**Indicator:**

- [Step 3] `nslookup google.com` returns `SERVFAIL` or times out; `nslookup kubernetes.default` succeeds
- [Step 2] log line `plugin/forward: no nameservers found` or upstream timeout errors
- [Step 6] `forward` directive specifies IPs that are not reachable from the node network

<!-- match: {"step": 3, "predicate": "contains", "target": "SERVFAIL"} -->

**Mitigation:**

- **Risk:** Low; switching to public resolvers restores external DNS immediately
- **Command:**

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Set: forward . 8.8.8.8 8.8.4.4
  ```

- **Duration:** 30 seconds for CoreDNS `reload` plugin to pick up the change; or restart pods immediately

**Resolution:**

```bash
kubectl rollout restart deployment/coredns -n kube-system
```

- **Impact:** All external DNS queries route via the new forwarder; verify with security team that public resolver egress is allowed
- **Rollback:** Restore the original `forward` targets in the Corefile

**Verification:** `kubectl exec -it dnsutils -- nslookup google.com` resolves successfully; no `forward` errors in CoreDNS logs.

---

### Cause Z: Unidentified DNS resolution failure

**Statement:** [Default] DNS resolution failures are present but no specific cause has been confirmed through the diagnostic steps above.

**Mechanism:** DNS failures in Kubernetes can arise from combinations of causes, cloud-provider-specific network overlays, CNI plugin bugs, or kernel-level packet filtering issues that are not covered by this runbook.

**Indicator:**

- [Default] All other causes in this runbook have been ruled out
- [Symptom] DNS failures are intermittent, environment-specific, or reproduce only under load

**Mitigation:**

- **Risk:** Low; the steps below are additive diagnostics and do not change cluster state
- **Command:**

  ```bash
  # Enable CoreDNS query logging for detailed per-query tracing
  kubectl edit configmap coredns -n kube-system
  # Add 'log' plugin to the Corefile block, then restart
  kubectl rollout restart deployment/coredns -n kube-system
  kubectl logs -n kube-system -l k8s-app=kube-dns --follow
  ```

- **Duration:** Run query logging for up to 30 minutes; disable afterward to avoid log volume

**Resolution:** Out of runbook scope — escalate to cluster networking team with: CoreDNS logs (with `log` plugin enabled), `/etc/resolv.conf` from affected pods, output of `kubectl get events -n kube-system`, and conntrack stats from affected nodes.

**Verification:** DNS resolution consistently succeeds from all namespaces after escalation and fix; no recurring `SERVFAIL` in CoreDNS metrics.

## Prevention

1. **Deploy NodeLocal DNSCache** — Run a CoreDNS caching agent as a DaemonSet on each node. It eliminates conntrack entries for DNS, reduces query latency, and provides local resilience when central CoreDNS pods are temporarily unavailable. Stable since Kubernetes 1.18. See [NodeLocal DNSCache docs](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/).

2. **Use explicit upstream IPs in the Corefile** — Replace `forward . /etc/resolv.conf` with `forward . 8.8.8.8 8.8.4.4` on any node running systemd-resolved to prevent forwarding loops.

3. **Alert on CoreDNS error metrics** — Set Prometheus alerts on `rate(coredns_dns_responses_total{rcode="SERVFAIL"}[5m]) > 0`, `coredns_panics_total > 0`, and `kube_pod_container_status_restarts_total` for CoreDNS pods.

4. **Alert on conntrack table utilization** — Alert when `nf_conntrack_count / nf_conntrack_max > 0.75` to give headroom before drops begin.

5. **Include DNS egress in NetworkPolicy templates** — Any namespace NetworkPolicy template with egress rules must include a UDP/TCP port 53 allowance as a standard clause. Enforce via policy admission webhooks (e.g., OPA/Gatekeeper).

6. **Avoid lowering ndots without FQDNs** — Reducing `ndots` below 5 increases DNS lookup speed for external names but breaks short internal service names. If a lower `ndots` is required, ensure all internal service references use fully qualified domain names (e.g., `my-svc.my-namespace.svc.cluster.local`).

7. **Enable CoreDNS autoscaling** — Deploy the cluster-proportional autoscaler (`dns-autoscaler`) so CoreDNS replicas scale with node and core count as the cluster grows.

8. **Set appropriate resource requests for CoreDNS** — Ensure CoreDNS has CPU/memory requests that prevent eviction under node pressure, and avoid memory limits low enough to trigger OOMKill under query spikes.

9. **Test DNS in deployment smoke tests** — Include `nslookup <service>` checks in post-deployment health checks to catch Corefile misconfigurations or RBAC regressions before they impact production traffic.

## Sources

- [Kubernetes: Debugging DNS Resolution](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/) — Priority 1. Official diagnostic guide: test pod setup, resolv.conf inspection, endpoint verification, RBAC checks, query log enablement.
- [Kubernetes: DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) — Priority 1. Reference for ndots behavior, dnsPolicy options, dnsConfig, search domain construction, and record formats.
- [Kubernetes: NodeLocal DNSCache](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/) — Priority 1. Deployment steps, conntrack elimination mechanism, IPVS vs iptables configuration, memory and cache tuning.
- [Kubernetes: Using CoreDNS for Service Discovery](https://kubernetes.io/docs/tasks/administer-cluster/coredns/) — Priority 1. Corefile schema, plugin list, migration from kube-dns, ConfigMap management.
