---
id: "k8s-networkpolicy-blocking"
title: "Kubernetes NetworkPolicy Unexpectedly Blocking Pod Traffic"
domain: security
service: kubernetes
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [networkpolicy, default-deny, egress, ingress, cni]
difficulty: advanced
---

## Symptom Recognition

- Pod-to-pod or pod-to-service connections hang then fail: `wget: download timed out`
- `nc -zv <svc-ip> <port>` reports `Connection timed out` (no response) rather than `Connection refused`
- Application logs show `dial tcp <ip>:<port>: i/o timeout` or `context deadline exceeded`
- Traffic that worked yesterday breaks immediately after a `NetworkPolicy` is applied to the namespace
- `kubectl get endpoints <svc>` lists healthy backend IPs, yet clients still cannot reach them
- DNS resolves correctly (`nslookup` returns the ClusterIP) but the TCP connection never completes

## Applicability

- Kubernetes 1.21+ clusters using a CNI that enforces `networking.k8s.io/v1` NetworkPolicy (Calico, Cilium, Antrea, Weave, etc.)
- Required access: `get`/`list`/`describe` on `networkpolicies`, `pods`, `services`, `endpoints`, `namespaces`; `exec` into a debug or application pod
- Tools: `kubectl`, plus `nc`/`wget`/`nslookup` available inside a pod (e.g. `busybox`, `nicolaka/netshoot`)
- Does NOT apply when the CNI has no NetworkPolicy controller — in that case policies are silently inert (see Cause D)

## Diagnostic Steps

### Step 1: Reproduce the failure and classify it as a timeout (not a refusal)

```bash
kubectl run npdebug --rm -ti --restart=Never \
  --image=registry.k8s.io/busybox:1.27.2 -n <client-ns> -- \
  nc -zv <dest-svc-or-pod-ip> <port>
```

Expected output: a timeout (`nc: <ip> (<ip>:<port>): Operation timed out`) indicates traffic is dropped (NetworkPolicy / firewall behavior). A `Connection refused` instead points away from NetworkPolicy toward a dead listener or wrong port.

### Step 2: List NetworkPolicies in BOTH the source and destination namespaces

```bash
kubectl get networkpolicy -n <dest-ns> -o wide
kubectl get networkpolicy -n <client-ns> -o wide
```

Expected output: a table of policy names with a `POD-SELECTOR` column, e.g. `access-nginx   app=nginx`. An empty selector renders as `<none>`/`{}` and selects every pod in that namespace.

### Step 3: Describe each candidate policy to read its policyTypes, ingress, and egress rules

```bash
kubectl describe networkpolicy <policy-name> -n <dest-ns>
```

Expected output: shows `PolicyTypes: Ingress, Egress`, the `Allowing ingress traffic` / `Allowing egress traffic` blocks, and the `from`/`to` selectors. A policyType present with **no matching allow rule** for your peer means that direction is default-deny.

### Step 4: Compare the destination pod's labels against ingress `from` selectors

```bash
kubectl get pod <dest-pod> -n <dest-ns> --show-labels
kubectl get pod <client-pod> -n <client-ns> --show-labels
kubectl get namespace <client-ns> --show-labels
```

Expected output: the label sets of the destination pod, the client pod, and the client namespace, so they can be matched against the `podSelector`/`namespaceSelector` values printed in Step 3.

### Step 5: Confirm the CNI actually enforces NetworkPolicy

```bash
kubectl get pods -n kube-system -o wide | grep -Ei 'calico|cilium|antrea|weave|kube-router'
```

Expected output: at least one running CNI policy-enforcement pod per node. No matching pod means NetworkPolicy resources exist but have no controller implementing them.

## Causes

### Cause A: Destination has an ingress policy that does not select the client as a source
**Statement:** A NetworkPolicy with `Ingress` in `policyTypes` selects the destination pod but its `from` rules do not match the client pod's labels or namespace, so once selected the pod is ingress-isolated and the client's traffic is dropped (additive-allow: only explicitly listed sources are permitted).
**Chain:**
- root: ingress policy selects the destination pod but omits the client from its `from` allow-list
- s1: destination pod becomes ingress-isolated (implicit default-deny for unlisted sources)
- s2: inbound TCP from the client is silently dropped at the destination's CNI
- D: client connection times out (Symptom Recognition)
**Indicators:**
- root: [Step 3] `describe` shows `PolicyTypes: Ingress` with `from` selectors that do not include the client's pod/namespace labels
  <!-- match: {"step": 3, "predicate": "contains", "target": "PolicyTypes: Ingress"} -->
- s1: [Step 4] client pod/namespace labels do not match the policy's `podSelector`/`namespaceSelector`
- s2: [Step 1] `nc -zv` returns `Operation timed out` (dropped, not refused)
  <!-- match: {"step": 1, "predicate": "contains", "target": "timed out"} -->
- D: [Symptom] `wget: download timed out`
**Interventions:**
- **remediation** (root): add an ingress rule whose `from` matches the client. Label the client and allow that label.

  ```bash
  kubectl label pod <client-pod> -n <client-ns> access=true --overwrite
  kubectl -n <dest-ns> patch networkpolicy <policy-name> --type=json -p \
    '[{"op":"add","path":"/spec/ingress/-","value":{"from":[{"podSelector":{"matchLabels":{"access":"true"}}}]}}]'
  ```

  **Verification:** re-run Step 1; `nc -zv` should now report `open`. Equivalently `wget --spider --timeout=1 <svc>` prints `remote file exists`.
- **mitigation** (s1): temporarily relax the policy to allow all pods in the client namespace while the precise rule is reviewed.

  ```bash
  kubectl -n <dest-ns> patch networkpolicy <policy-name> --type=json -p \
    '[{"op":"add","path":"/spec/ingress/-","value":{"from":[{"namespaceSelector":{}}]}}]'
  ```

  **Risk:** opens the destination to every namespace, widening the attack surface. **Duration:** until the scoped `from` rule lands (hours, not days). **Verification:** re-run Step 1 and confirm `open`, then revert the broad rule.

### Cause B: Client has an egress policy that does not permit the destination
**Statement:** A NetworkPolicy with `Egress` in `policyTypes` selects the client pod but its `to` rules omit the destination (or its DNS port 53), so the client is egress-isolated and its outbound packets never leave — even if the destination's ingress allows everything.
**Chain:**
- root: egress policy selects the client pod but its `to` allow-list excludes the destination
- s1: client pod becomes egress-isolated for unlisted destinations
- s2: outbound TCP (and possibly UDP/53 DNS) from the client is dropped before reaching the destination
- D: client connection times out (Symptom Recognition)
**Indicators:**
- root: [Step 3] `describe` of the client-namespace policy shows `PolicyTypes: Egress` whose `to` does not cover the destination CIDR/selector
  <!-- match: {"step": 3, "predicate": "contains", "target": "PolicyTypes: Egress"} -->
- s1: [Step 2] a policy in the CLIENT namespace selects the client pod
- s2: [Step 1] dropped outbound connection shows `Operation timed out`
- D: [Symptom] application log `dial tcp <ip>:<port>: i/o timeout`
**Interventions:**
- **remediation** (root): add an egress rule covering the destination AND DNS (UDP/TCP 53), since blocked DNS alone manifests as a timeout.

  ```bash
  kubectl -n <client-ns> patch networkpolicy <policy-name> --type=json -p \
    '[{"op":"add","path":"/spec/egress/-","value":{"to":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"<dest-ns>"}}}],"ports":[{"protocol":"TCP","port":<port>}]}},
      {"op":"add","path":"/spec/egress/-","value":{"to":[{"namespaceSelector":{}}],"ports":[{"protocol":"UDP","port":53},{"protocol":"TCP","port":53}]}}]'
  ```

  **Verification:** from the client pod, `nslookup <dest-svc>` resolves and Step 1 `nc -zv` reports `open`.
- **mitigation** (s2): exec into the client and confirm DNS is the actual chokepoint before broad changes.

  ```bash
  kubectl exec -ti <client-pod> -n <client-ns> -- nslookup <dest-svc>.<dest-ns>.svc.cluster.local
  ```

  **Risk:** none (read-only probe); does not fix traffic. **Duration:** n/a. **Verification:** if `nslookup` itself times out, the egress policy is blocking port 53 — prioritize the DNS rule above.

### Cause C: Selector OR/AND confusion makes the allow rule match nothing
**Statement:** The ingress/egress rule pairs `namespaceSelector` and `podSelector` as separate `from`/`to` list entries (OR) when an AND was intended (or vice-versa), so the combined selector never matches the real peer and the additive-allow rule contributes zero allowed sources.
**Chain:**
- root: the policy's `from`/`to` uses two list items (OR semantics) where the peer needs both namespace AND pod labels in one item (AND semantics)
- s1: the intended allow rule matches no peer, so it adds nothing to the union of allowed connections
- s2: the selected pod stays effectively default-deny for the real peer and drops its traffic
- D: connection times out (Symptom Recognition)
**Indicators:**
- root: [Step 3] `describe` shows `from`/`to` with `namespaceSelector` and `podSelector` as two separate bullet entries instead of one combined entry
  <!-- match: {"step": 3, "predicate": "contains", "target": "NamespaceSelector"} -->
- s1: [Step 4] no pod simultaneously satisfies both selectors as the author intended
- D: [Symptom] `wget: download timed out`
**Interventions:**
- **remediation** (root): rewrite the rule so namespace AND pod selectors live in the SAME `from` entry (no leading `-` before `podSelector`).

  ```yaml
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: <client-ns>
      podSelector:
        matchLabels:
          app: <client-app>
  ```

  **Verification:** `kubectl apply -f policy.yaml` then re-run Step 1; `nc -zv` reports `open`. Re-run Step 3 and confirm the two selectors appear under one `from` entry.

### Cause D: CNI does not enforce NetworkPolicy (or it does, but kube-proxy/flat networking bypasses it inconsistently)
**Statement:** The cluster's network plugin lacks a running NetworkPolicy controller (or a flat/host-network path bypasses CNI enforcement), so policies are silently inert and the observed block originates from a different layer rather than the NetworkPolicy itself.
**Chain:**
- root: no CNI policy-enforcement agent reconciles NetworkPolicy objects on the nodes carrying the traffic
- s1: applied NetworkPolicy resources exist but have no effect ("Creating a NetworkPolicy resource without a controller that implements it will have no effect")
- s2: the real drop happens at an unmanaged layer (node iptables, cloud security group, or hostNetwork pod) that the policy cannot describe
- D: connection times out while NetworkPolicy edits change nothing (Symptom Recognition)
**Indicators:**
- root: [Step 5] no `calico`/`cilium`/`antrea`/`weave` enforcement pod is running in `kube-system`
  <!-- match: {"step": 5, "predicate": "absent", "target": "Running"} -->
- s1: [Step 1] connectivity is unchanged after adding/deleting policies (still `Operation timed out`)
- D: [Symptom] `nc -zv` reports `Operation timed out` regardless of policy edits
**Interventions:**
- **remediation** (root): install or repair a NetworkPolicy-capable CNI; until then, treat the drop as a node/cloud-firewall issue.

  ```bash
  kubectl get pods -n kube-system -l k8s-app=cilium -o wide
  kubectl -n kube-system rollout status ds/cilium
  ```

  **Verification:** after the enforcement DaemonSet is Ready on every node, re-apply a known default-deny + allow pair and confirm Step 1 toggles between `timed out` and `open` as expected.
- **mitigation** (s2): bypass the policy layer to localize the real drop — test the destination's node firewall directly from a `hostNetwork` debug pod.

  ```bash
  kubectl run npnode --rm -ti --restart=Never --overrides='{"spec":{"hostNetwork":true}}' \
    --image=nicolaka/netshoot -- nc -zv <dest-pod-ip> <port>
  ```

  **Risk:** `hostNetwork` pod runs on the node's network namespace — restrict to a debug window. **Duration:** single diagnostic session. **Verification:** if hostNetwork reaches the pod but in-cluster pods cannot, the block is below CNI (node/SG), not in NetworkPolicy.

### Cause Z: Unidentified
**Statement:** Traffic is blocked but no NetworkPolicy ingress/egress mismatch, selector error, or CNI enforcement gap from Causes A–D matches the collected evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the networking SME with the bundle attached.

  ```bash
  NS_DEST=<dest-ns>; NS_CLIENT=<client-ns>
  kubectl get networkpolicy -A -o yaml > np-snapshot.yaml
  kubectl get pods -n "$NS_DEST" --show-labels -o wide >> np-snapshot.yaml
  kubectl get pods -n "$NS_CLIENT" --show-labels -o wide >> np-snapshot.yaml
  kubectl get ns --show-labels >> np-snapshot.yaml
  kubectl get endpoints,svc -n "$NS_DEST" -o wide >> np-snapshot.yaml
  kubectl get pods -n kube-system -o wide >> np-snapshot.yaml
  ```

  **Risk:** none (read-only collection). **Duration:** n/a. **Verification:** confirm `np-snapshot.yaml` contains policies, labels, endpoints, and CNI pod state before handing off.

## Prevention

- Adopt an explicit default-deny baseline per namespace, then add scoped allows, so missing rules fail closed predictably:

  ```yaml
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata:
    name: default-deny-all
    namespace: <ns>
  spec:
    podSelector: {}
    policyTypes:
    - Ingress
    - Egress
  ```

- Always allow DNS egress (UDP/TCP 53 to kube-system) in every egress policy to avoid timeouts that masquerade as connectivity loss.
- Always set `policyTypes` explicitly; relying on the default (`Egress` only set when egress rules exist) hides intent during review.
- Label namespaces and pods with stable, documented keys (e.g. `kubernetes.io/metadata.name`) so selectors are auditable.
- Validate policies in staging with the `wget --spider --timeout=1`/`nc -zv` toggle test before promoting; alert on a rise in TCP connection timeouts to selected pods.
- Confirm the CNI's NetworkPolicy controller is healthy in cluster monitoring so inert-policy situations (Cause D) surface early.

## Sources

- [Network policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/) — NetworkPolicy semantics: additive (non-conflicting) allow rules, ingress/egress isolation once a pod is selected, podSelector vs namespaceSelector scoping, empty-selector `{}` behavior, default-deny YAML, policyTypes defaulting, and the CNI-must-implement requirement.
- [Debug service](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) — connectivity debugging commands (`kubectl exec ... nc -zv`, `wget -qO-`, `nslookup`, `kubectl get endpoints/pods -o wide`) and the explicit note to review NetworkPolicy ingress rules affecting target pods.
- [Declare network policy](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/) — `kubectl get networkpolicy` output format, the `wget --spider --timeout=1` blocked-vs-allowed test (`wget: download timed out` vs `remote file exists`), and labeling a client pod (`--labels="access=true"`) to satisfy a policy.
