---
id: "k8s-coredns-failures"
title: "Kubernetes CoreDNS Resolution Failures"
domain: networking
service: kubernetes
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Chain:**
- root: CoreDNS Deployment in `kube-system` has zero healthy replicas (pods absent, `CrashLoopBackOff`, or evicted).
- s1: no DNS server is available to answer any pod's queries cluster-wide.
- s2: every DNS query times out after the kernel UDP retry cycle (5–30 seconds per attempt).
- D: pods get cluster-wide `connection refused`/`timeout` resolving services by name (Symptom).

**Indicators:**
- root: [Step 1] pods show status other than `Running` with `READY 1/1`
  <!-- match: {"step": 1, "predicate": "absent", "target": "Running"} -->
- D: [Symptom] cluster-wide DNS failures affecting all namespaces simultaneously

**Interventions:**
- **remediation** (root): restore healthy replicas by restarting and scaling the Deployment.

  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  kubectl scale deployment/coredns -n kube-system --replicas=3
  ```

  **Verification:** `kubectl get pods -n kube-system -l k8s-app=kube-dns` shows all pods `Running 1/1`; `kubectl exec -it dnsutils -- nslookup kubernetes.default` returns a valid IP.
- **mitigation** (root): roll the Deployment to reschedule pods onto healthy nodes.

  ```bash
  kubectl rollout restart deployment/coredns -n kube-system
  ```

  **Risk:** Brief DNS gap (under 30 seconds) while replacement pods schedule and become ready. **Duration:** 30–60 seconds; revert if new pods also crash (indicates a deeper configuration issue). **Verification:** new pods reach `Running 1/1`; `nslookup kubernetes.default` resolves.

---

### Cause B: Forwarding loop — CoreDNS resolves back to itself

**Statement:** CoreDNS uses `forward . /etc/resolv.conf` on a node whose `/etc/resolv.conf` names a local stub resolver (e.g., `127.0.0.53` from systemd-resolved) that forwards back to the cluster DNS Service, creating an infinite loop.

**Chain:**
- root: Corefile sets `forward . /etc/resolv.conf` while the node `/etc/resolv.conf` names a local stub resolver (`127.0.0.53`/`127.0.0.1`).
- s1: CoreDNS forwards external queries to `127.0.0.53`, which forwards them to the `kube-dns` ClusterIP, routing straight back to CoreDNS.
- s2: the `loop` plugin detects the cycle and terminates the process, triggering `CrashLoopBackOff`.
- D: CoreDNS is repeatedly down, so pod DNS queries fail cluster-wide (Symptom).

**Indicators:**
- root: [Step 6] Corefile contains `forward . /etc/resolv.conf` and node `/etc/resolv.conf` contains `127.0.0.53` or `127.0.0.1`
- s2: [Step 2] log line `plugin/loop: Loop ... detected for zone "."` or `[FATAL] plugin/loop: Loop detected`
  <!-- match: {"step": 2, "predicate": "contains", "target": "Loop"} -->

**Interventions:**
- **remediation** (root): point the forwarder at explicit upstream resolvers and reload.

  ```bash
  kubectl patch configmap coredns -n kube-system --type=merge \
    -p '{"data":{"Corefile":".:53 {\n    errors\n    health {\n       lameduck 5s\n    }\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n       pods insecure\n       fallthrough in-addr.arpa ip6.arpa\n       ttl 30\n    }\n    prometheus :9153\n    forward . 8.8.8.8 8.8.4.4\n    cache 30\n    loop\n    reload\n    loadbalance\n}\n"}}'
  kubectl rollout restart deployment/coredns -n kube-system
  ```

  **Verification:** `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=20` shows no `Loop detected` entries; CoreDNS pods stay `Running` without restarting.
- **mitigation** (root): hand-edit the Corefile to replace the stub forwarder with public resolvers.

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Change: forward . /etc/resolv.conf
  # To:     forward . 8.8.8.8 8.8.4.4
  ```

  **Risk:** Low; replaces a non-functional forwarder with explicit well-known resolvers. **Duration:** Immediate after CoreDNS reloads (the `reload` plugin auto-reloads within 30 seconds). **Verification:** no new `Loop detected` log lines; pods stop restarting.

---

### Cause C: RBAC missing — CoreDNS cannot list Services or EndpointSlices

**Statement:** The `system:coredns` ClusterRole lacks the required verbs (`list`, `watch`) on `services`, `endpoints`, or `endpointslices`, preventing CoreDNS from building its internal service-to-IP mapping.

**Chain:**
- root: the `system:coredns` ClusterRole omits `list`/`watch` on `services`/`endpoints`/`endpointslices`.
- s1: the CoreDNS kubernetes plugin cannot watch the API server and fails to populate its in-memory DNS table.
- s2: CoreDNS returns `SERVFAIL` for all cluster-internal names (external names may still resolve via the upstream forwarder).
- D: pods fail to resolve internal services by name (Symptom).

**Indicators:**
- root: [Step 2] log line `plugin/kubernetes: failed to list *v1.Service` or `plugin/kubernetes: failed to list *v1.EndpointSlice`
  <!-- match: {"step": 2, "predicate": "contains", "target": "failed to list"} -->
- s2: [Step 3] internal `nslookup kubernetes.default` returns `SERVFAIL`; external `nslookup google.com` succeeds

**Interventions:**
- **remediation** (root): add the missing read-only verbs to the ClusterRole.

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

  **Verification:** `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=20` shows no `failed to list` errors; `kubectl exec -it dnsutils -- nslookup kubernetes.default` resolves successfully.
- **mitigation** (root): confirm the missing permissions by inspecting the current ClusterRole before applying the fix.

  ```bash
  kubectl describe clusterrole system:coredns
  ```

  **Risk:** Low; read-only inspection does not change cluster state. **Duration:** Inspect only; apply the remediation immediately. **Verification:** the rules list confirms `list`/`watch` are absent on the affected resources.

---

### Cause D: ndots misconfiguration — short service names bypass cluster search path

**Statement:** A pod's `dnsConfig` sets `ndots` below 5, causing short internal service names to be sent as absolute queries rather than expanded through the cluster search path, producing `NXDOMAIN` for valid internal services.

**Chain:**
- root: a pod's `dnsConfig` sets `ndots` below the Kubernetes default of `5`.
- s1: a short name like `my-service` (0 dots) is tried as an absolute query before the cluster search domains are appended.
- s2: the absolute lookup fails with `NXDOMAIN` before `my-service.default.svc.cluster.local` is ever attempted.
- D: only the affected pods fail to resolve valid internal services by short name (Symptom).

**Indicators:**
- root: [Step 4] `/etc/resolv.conf` shows `options ndots:` value less than `5`
  <!-- match: {"step": 4, "predicate": "absent", "target": "ndots:5"} -->
- s2: [Step 3] `nslookup my-service` fails but `nslookup my-service.default.svc.cluster.local` succeeds
- D: [Symptom] only certain pods fail DNS resolution while others in the same cluster succeed

**Interventions:**
- **remediation** (root): restore `ndots:5` in the pod/deployment `dnsConfig`.

  ```yaml
  # In pod/deployment spec:
  spec:
    dnsConfig:
      options:
        - name: ndots
          value: "5"
  ```

  **Verification:** `kubectl exec -it <pod> -- cat /etc/resolv.conf` shows `ndots:5`; `nslookup my-service` resolves without needing the FQDN.
- **mitigation** (root): patch the running Deployment to set `ndots:5` and trigger a rolling restart.

  ```bash
  kubectl patch deployment <name> -n <namespace> --type=merge \
    -p '{"spec":{"template":{"spec":{"dnsConfig":{"options":[{"name":"ndots","value":"5"}]}}}}}'
  ```

  **Risk:** Low; restoring the cluster default `ndots` value. **Duration:** Takes effect after pods are rescheduled (rolling restart). **Verification:** new pods show `ndots:5` and resolve short service names.

---

### Cause E: Conntrack table exhaustion causing UDP DNS packet drops

**Statement:** The node's conntrack table is full, causing the kernel to silently drop incoming UDP DNS packets, producing intermittent 5–30 second DNS timeouts with no error logged at the CoreDNS layer.

**Chain:**
- root: the node's `nf_conntrack` table reaches `nf_conntrack_max` (high DNS query rate or many short-lived connections, each entry persisting ~30s).
- s1: the kernel silently drops new UDP packets, so DNS queries never reach CoreDNS and nothing is logged at the CoreDNS layer.
- s2: the pod resolver retries after its 5-second UDP timeout, producing characteristic 5–30s latency spikes.
- D: pods see intermittent DNS timeouts with no corresponding CoreDNS errors (Symptom).

**Indicators:**
- root: [Step 7] `nf_conntrack_count` at or near `nf_conntrack_max`
- s1: [Step 7] `dmesg` contains `nf_conntrack: table full, dropping packet`
  <!-- match: {"step": 7, "predicate": "contains", "target": "nf_conntrack: table full"} -->
- D: [Symptom] intermittent DNS timeouts with no corresponding errors in CoreDNS logs

**Interventions:**
- **remediation** (root): raise `nf_conntrack_max` persistently across reboots on all affected nodes.

  ```bash
  echo "net.netfilter.nf_conntrack_max=262144" | \
    sudo tee /etc/sysctl.d/99-conntrack.conf
  sudo sysctl --system
  ```

  **Verification:** `cat /proc/sys/net/netfilter/nf_conntrack_count` stays well below `nf_conntrack_max`; `dmesg | grep "nf_conntrack"` shows no new `table full` entries; DNS latency returns to sub-millisecond.
- **defensive_fix** (s1): deploy NodeLocal DNSCache to eliminate conntrack entries for DNS entirely (long-term hardening of the drop path).

  ```bash
  # Deploy NodeLocal DNSCache as a DaemonSet per the upstream manifest.
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/kubernetes/master/cluster/addons/dns/nodelocaldns/nodelocaldns.yaml
  ```

  **Verification:** the `node-local-dns` DaemonSet is `Ready` on every node; conntrack usage for UDP/53 drops; DNS latency stays sub-millisecond under load.
- **mitigation** (root): raise `nf_conntrack_max` immediately at runtime.

  ```bash
  sudo sysctl -w net.netfilter.nf_conntrack_max=262144
  ```

  **Risk:** Increases kernel memory for connection tracking; safe to double the default value. **Duration:** Immediate; make persistent via sysctl configuration file. **Verification:** `nf_conntrack_count` stays below the new max; no new `table full` in dmesg.

---

### Cause F: NetworkPolicy blocking DNS egress on port 53

**Statement:** A NetworkPolicy in the affected namespace restricts egress traffic and does not include an explicit allow rule for UDP and TCP port 53, blocking all DNS queries from pods in that namespace.

**Chain:**
- root: an egress NetworkPolicy (`policyTypes: [Egress]`) in the affected namespace has no allow rule for UDP/TCP port 53.
- s1: pods in that namespace cannot reach the `kube-dns` ClusterIP on port 53, so no DNS responses arrive.
- D: pods in the affected namespace time out resolving services by name (Symptom).

**Indicators:**
- root: [Step 8] NetworkPolicy with `policyTypes: [Egress]` exists in the affected namespace and has no `port: 53` allowance
  <!-- match: {"step": 8, "predicate": "contains", "target": "Egress"} -->
- s1: [Step 3] DNS resolution fails from an affected pod but succeeds from a pod in a different namespace without NetworkPolicies

**Interventions:**
- **remediation** (root): apply an egress NetworkPolicy that explicitly allows UDP/TCP port 53.

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

  **Verification:** `kubectl exec -it <pod-in-namespace> -- nslookup kubernetes.default` resolves successfully; no DNS timeouts in application logs.
- **mitigation** (root): apply the same targeted port-53 egress allowance as an immediate unblock.

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

  **Risk:** Low; adding a targeted port 53 egress rule does not open other traffic. **Duration:** Immediate after applying. **Verification:** pods in the namespace resolve names; remove with `kubectl delete networkpolicy allow-dns-egress -n <affected-namespace>` if it must be rolled back.

---

### Cause G: Upstream DNS forwarder unreachable

**Statement:** The upstream resolver configured in the CoreDNS `forward` plugin is unreachable from the cluster nodes, causing all external hostname queries to return `SERVFAIL` while internal cluster-name resolution continues to work.

**Chain:**
- root: the resolver(s) in the CoreDNS `forward` directive are unreachable from the nodes (firewall, wrong IP, or VPC routing gap).
- s1: CoreDNS cannot reach an upstream for names outside `cluster.local` and returns `SERVFAIL` for all external names.
- D: pods fail to resolve external hostnames while internal service names keep working (Symptom).

**Indicators:**
- root: [Step 6] `forward` directive specifies IPs that are not reachable from the node network
- s1: [Step 3] `nslookup google.com` returns `SERVFAIL` or times out; `nslookup kubernetes.default` succeeds
  <!-- match: {"step": 3, "predicate": "contains", "target": "SERVFAIL"} -->
- s1: [Step 2] log line `plugin/forward: no nameservers found` or upstream timeout errors

**Interventions:**
- **remediation** (root): set the `forward` directive to reachable public resolvers and reload CoreDNS.

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Set: forward . 8.8.8.8 8.8.4.4
  kubectl rollout restart deployment/coredns -n kube-system
  ```

  **Verification:** `kubectl exec -it dnsutils -- nslookup google.com` resolves successfully; no `forward` errors in CoreDNS logs.
- **mitigation** (root): switch to public resolvers and restart pods to pick up the change immediately.

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Set: forward . 8.8.8.8 8.8.4.4
  kubectl rollout restart deployment/coredns -n kube-system
  ```

  **Risk:** Low; switching to public resolvers restores external DNS immediately — verify with the security team that public resolver egress is allowed. **Duration:** 30 seconds for the CoreDNS `reload` plugin to pick up the change, or restart pods immediately. **Verification:** external `nslookup google.com` resolves; restore original `forward` targets if rollback is needed.

---

### Cause Z: Unidentified

**Statement:** DNS resolution failures are present but no specific cause above has been confirmed through the diagnostic steps.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (CoreDNS logs with the `log` plugin enabled, affected-pod `/etc/resolv.conf`, `kubectl get events -n kube-system`, and node conntrack stats) and escalate to the cluster networking SME.

  ```bash
  kubectl edit configmap coredns -n kube-system
  # Add 'log' plugin to the Corefile block, then restart
  kubectl rollout restart deployment/coredns -n kube-system
  kubectl logs -n kube-system -l k8s-app=kube-dns --follow
  ```

  **Risk:** Low; query logging is additive and does not change cluster state, but high log volume if left on. **Duration:** Run query logging for up to 30 minutes; disable afterward to avoid log volume. **Verification:** DNS resolution consistently succeeds from all namespaces after the SME applies a fix; no recurring `SERVFAIL` in CoreDNS metrics.

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
