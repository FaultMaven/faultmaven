---
id: istio-503-upstream
title: "Istio 503 Upstream Connect Error"
domain: networking
service: istio
symptom_class:
  - service_unavailable
  - connection_refused
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - istio
  - envoy
  - "503"
  - service-mesh
  - mtls
  - destination-rule
  - sidecar
  - peer-authentication
  - authorization-policy
  - circuit-breaker
difficulty: intermediate
---

# Istio 503 Upstream Connect Error

## Symptom Recognition

- Clients receive HTTP `503 Service Unavailable` and the response header carries `server: istio-envoy`, confirming the response originated at the Envoy sidecar rather than the application container.
- Envoy access log line for the failed request ends with an `%RESPONSE_FLAGS%` token of `UC`, `UF`, `UH`, `UO`, `URX`, `NR`, `NC`, `DC`, `UAEX`, or `RH`. Common literal log fragments include `"response_code":503,"response_flags":"UC"`, `"response_flags":"UF"`, and `"response_flags":"UH"`.
- Common `response_code_details` strings in Envoy logs include `upstream_reset_before_response_started{connection_failure}`, `upstream_reset_before_response_started{connection_termination}`, `no_healthy_upstream`, `cluster_not_found`, `route_not_found`, `rbac_access_denied_matched_policy`, `delayed_close_timeout`, and `upstream_per_try_timeout`.
- istiod cluster manager events: `cds: add ... cluster` followed by `eds: removed all endpoints from cluster <name>` for the affected destination, or `RDS: route not found` for the affected `virtual_host` in proxy-config dumps.
- 503 burst is bounded to traffic flowing through one specific Service / DestinationRule / VirtualService combination, with the rest of the mesh healthy.
- `istioctl proxy-status` shows one or more workloads with non-`SYNCED` status (`STALE`, `NOT SENT`) for `CDS`, `EDS`, `LDS`, or `RDS` columns.
- For mTLS failures, the destination-side sidecar log contains `transport socket connect timeout` or `TLS error: 268435703:SSL routines:OPENSSL_internal:WRONG_VERSION_NUMBER` correlated with the same request id.
- For AuthorizationPolicy denials, the access log shows `403 - rbac_access_denied_matched_policy[ns[<ns>]-policy[<name>]-rule[<n>]]` instead of 503; some workloads with `action: CUSTOM` (ext-authz) surface as 503 with `UAEX`.

## Applicability

- Istio 1.17+ on Kubernetes 1.25+, using the default sidecar injection model (`istio-proxy` Envoy container in every workload pod). Ambient-mode (`ztunnel`) deployments are out of scope.
- `kubectl` configured for the affected cluster with `get`, `logs`, and `exec` on the workload namespaces and on `istio-system`.
- `istioctl` binary on the operator's host, matching the control plane minor version (`istioctl version` agrees with `istiod` deployed version).
- Read access to `PeerAuthentication`, `DestinationRule`, `VirtualService`, `AuthorizationPolicy`, `Gateway`, and `Sidecar` resources cluster-wide (or in the affected namespaces).
- For proxy config inspection: ability to `kubectl exec` into the affected pod's `istio-proxy` container, or use `istioctl proxy-config` against it.
- For mTLS investigation: ability to read the istiod logs in `istio-system` (`kubectl logs -n istio-system deploy/istiod`).

## Diagnostic Steps

### Step 1: Confirm Envoy is the responder and capture the response flag

```bash
curl -sS -o /dev/null -D - https://<host>/<path> 2>&1 | grep -iE "^(HTTP|server|x-envoy)"
# From the client-side sidecar:
kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=200 | grep -E '"response_code":503|HTTP/[0-9.]+" 503'
# From the destination-side sidecar:
kubectl logs -n <dst-ns> <dst-pod> -c istio-proxy --tail=200 | grep -E '"response_code":503|HTTP/[0-9.]+" 503'
```

Expected output: `HTTP/2 503` (or `HTTP/1.1 503`) with `server: istio-envoy` and an `x-envoy-upstream-service-time` header. Each matching access-log line ends with a `response_flags` token; record the exact flag (`UC`, `UF`, `UH`, `UO`, `URX`, `NR`, `NC`, `DC`, `UAEX`, `RH`) and any `response_code_details` field — these select the Cause below.

### Step 2: Verify the destination sidecar is injected and ready

```bash
kubectl get pod -n <dst-ns> <dst-pod> -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}'
kubectl get pod -n <dst-ns> <dst-pod> -o jsonpath='{.status.containerStatuses[?(@.name=="istio-proxy")].ready}'
kubectl get ns <dst-ns> -L istio-injection,istio.io/rev
kubectl get pod -n <dst-ns> <dst-pod> -o jsonpath='{.metadata.annotations.sidecar\.istio\.io/inject}'
```

Expected output: container list contains both the application container and `istio-proxy`; the `istio-proxy` `ready` field is `true`; the namespace shows `istio-injection=enabled` (or carries an `istio.io/rev=<revision>` label for revisioned injection) and the pod-level `sidecar.istio.io/inject` annotation is unset or `true`. Missing `istio-proxy` container, `ready=false`, or `istio-injection=disabled` indicates the destination is outside the mesh.

### Step 3: Check sidecar config sync with the control plane

```bash
istioctl proxy-status
istioctl proxy-status <dst-pod>.<dst-ns>
```

Expected output: every workload row shows `SYNCED` in the `CDS`, `EDS`, `LDS`, and `RDS` columns. Values of `STALE`, `NOT SENT`, or `SYNCED (no push)` against the affected pod indicate the proxy holds an older config than istiod last computed — routes, clusters, or endpoints for the destination may be missing or wrong on that proxy.

### Step 4: Dump the cluster, endpoint, and route config from the client sidecar

```bash
istioctl proxy-config cluster -n <src-ns> <src-pod> --fqdn <dst-svc>.<dst-ns>.svc.cluster.local
istioctl proxy-config endpoint -n <src-ns> <src-pod> --cluster "outbound|<svc-port>||<dst-svc>.<dst-ns>.svc.cluster.local"
istioctl proxy-config route -n <src-ns> <src-pod> --name <port-or-vhost>
istioctl proxy-config listener -n <src-ns> <src-pod>
```

Expected output: the cluster line lists `outbound|<port>||<fqdn>` with a non-empty `TYPE` (EDS) and a `DestinationRule` reference; the endpoint dump prints one `HEALTHY` row per ready destination pod IP. Empty endpoint output, `cluster not found`, or every endpoint marked `UNHEALTHY` / `FAILED` corresponds directly to the `NC`, `UH`, or `UF` response flag from Step 1.

### Step 5: Verify PeerAuthentication and DestinationRule TLS modes align

```bash
kubectl get peerauthentication -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,MTLS:.spec.mtls.mode,SELECTOR:.spec.selector
kubectl get destinationrule -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,HOST:.spec.host,TLS:.spec.trafficPolicy.tls.mode
istioctl x describe pod -n <dst-ns> <dst-pod>
```

Expected output: every workload either inherits the mesh-wide `STRICT` / `PERMISSIVE` mode from `istio-system` or carries an explicit `PeerAuthentication`; matching `DestinationRule.spec.trafficPolicy.tls.mode` is `ISTIO_MUTUAL` for in-mesh destinations, `SIMPLE` / `MUTUAL` for external HTTPS, or `DISABLE` for explicit plaintext. `istioctl x describe pod` prints `Effective PeerAuthentication: STRICT` and lists matched `DestinationRule`s; any "WARNING: DestinationRule disables mTLS but PeerAuthentication is STRICT" line is the smoking gun for `UF`.

### Step 6: Run istioctl analyze on the affected namespace

```bash
istioctl analyze -n <dst-ns>
istioctl analyze --all-namespaces 2>&1 | head -50
```

Expected output: `No validation issues found when analyzing namespace <dst-ns>`. Errors such as `IST0101 (ReferencedResourceNotFound)`, `IST0118 (PortNameIsNotUnderNamingConvention)`, `IST0108 (UnknownAnnotation)`, or `IST0103 (PodMissingProxy)` point at concrete misconfigurations — port-name violations (Service port not named `http`, `http2`, `grpc`, `tcp`, etc.) prevent protocol detection and cause `NR`/`UC` against the route.

### Step 7: Inspect outlier detection and connection pool state

```bash
kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request GET clusters?format=json \
  | jq '.cluster_statuses[] | select(.name | contains("<dst-svc>")) | {name, host_statuses}'
kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request GET stats \
  | grep -E "<dst-svc>.*(upstream_rq_pending_overflow|upstream_cx_overflow|upstream_rq_retry_overflow|outlier_detection.ejections_active)"
```

Expected output: every `host_statuses` row carries `"health_status":{"eds_health_status":"HEALTHY"}` and no `failed_outlier_check` flag. Non-zero counters for `upstream_rq_pending_overflow`, `upstream_cx_overflow`, `upstream_rq_retry_overflow`, or `outlier_detection.ejections_active` map directly to the `UO`, `URX`, and `UH` response flags.

### Step 8: Confirm the Kubernetes Service and Endpoints exist with ready pods

```bash
kubectl get svc -n <dst-ns> <dst-svc> -o wide
kubectl get endpoints -n <dst-ns> <dst-svc>
kubectl get endpointslices -n <dst-ns> -l kubernetes.io/service-name=<dst-svc>
kubectl get pod -n <dst-ns> -l <selector-from-service> -o wide
```

Expected output: Service selector matches at least one pod's labels; `kubectl get endpoints` lists at least one `<ip>:<port>` pair (not `<none>`); `EndpointSlice` rows show `ready=true` for each address. An empty `Endpoints` object (or `Endpoints: <none>`) means no pod is `Ready` for the Service, which surfaces as `UH` (`no_healthy_upstream`) at the sidecar.

### Step 9: Test direct connectivity from the source sidecar to a destination pod IP

```bash
# Get a destination pod IP from Step 8, then:
kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- curl -sS -o /dev/null -w "%{http_code}\n" \
  http://<dst-pod-ip>:<container-port>/<probe-path>
kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- curl -sS -o /dev/null -w "%{http_code}\n" \
  http://<dst-svc>.<dst-ns>.svc.cluster.local:<svc-port>/<probe-path>
```

Expected output: both calls return the application's normal response code (2xx or 3xx). A 503 from the FQDN call combined with a 2xx from the direct-pod-IP call indicates a Service/Endpoints or DestinationRule issue. A 503 from both indicates an upstream-side failure (sidecar mTLS reject, AuthorizationPolicy deny, container down).

### Step 10: Check AuthorizationPolicy decisions

```bash
kubectl get authorizationpolicy -A -o yaml | grep -E "action:|name:|namespace:|selector:" | head -60
# Inspect RBAC stats on the destination sidecar:
kubectl exec -n <dst-ns> <dst-pod> -c istio-proxy -- pilot-agent request GET stats | grep rbac
# Recent denials in the destination sidecar:
kubectl logs -n <dst-ns> <dst-pod> -c istio-proxy --tail=200 | grep -E "rbac_access_denied|denied by"
```

Expected output: `rbac.allowed` counter is incrementing for the route; `rbac.denied` and `rbac.shadow_denied` are zero. A non-zero `rbac.denied` with matching access-log lines containing `rbac_access_denied_matched_policy[ns[<ns>]-policy[<name>]-rule[<n>]]` shows which DENY (or unmatched ALLOW) rule blocked the request. CUSTOM-action policies surface as 503 with `UAEX` when the external authorizer is unreachable.

### Step 11: Inspect istiod control-plane logs and push status

```bash
kubectl logs -n istio-system deploy/istiod --tail=200 | grep -E "ADS|push|rejected|<dst-svc>|<dst-ns>"
kubectl exec -n istio-system deploy/istiod -- pilot-discovery request GET /debug/syncz | jq '.[] | select(.proxy | contains("<dst-pod>"))'
```

Expected output: istiod log shows periodic `Push debounce stable[N]` and `XDS: Pushing` events without `rejected` lines for the affected workload. Lines such as `rejected CDS update from <pod>` or `update rejected: <reason>` indicate the proxy refused a config push — typically the cause of stale routes and `NR` flags.

## Causes

### Cause A: mTLS mismatch — PeerAuthentication STRICT but destination has no sidecar or DestinationRule sets `DISABLE`

**Statement:** The client sidecar initiates an Istio mTLS handshake but the destination either has no sidecar or its DestinationRule explicitly disables TLS, so the handshake fails before any HTTP request is sent.

**Mechanism:** When `PeerAuthentication.mode: STRICT` is in effect on the destination workload and the matching `DestinationRule.trafficPolicy.tls.mode` is `ISTIO_MUTUAL`, the client sidecar wraps the connection in mTLS. If the destination has no sidecar (namespace not labeled `istio-injection=enabled`, pod annotated `sidecar.istio.io/inject: "false"`, or `hostNetwork: true`), no Envoy is there to terminate TLS. If the DestinationRule sets `tls.mode: DISABLE` while the destination enforces STRICT, the client sends plaintext to an mTLS-only listener. Either case produces a TLS handshake error logged on the destination sidecar (or the kernel) and a `UF` response flag with `response_code_details: upstream_reset_before_response_started{connection_failure}` on the client.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"UF"` for the failed request
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"UF\""} -->
- [Step 1] `response_code_details` field contains `upstream_reset_before_response_started{connection_failure}`
<!-- match: {"step": 1, "predicate": "contains", "target": "upstream_reset_before_response_started{connection_failure}"} -->
- [Step 2] destination pod's `istio-proxy` container is absent or `ready=false`
- [Step 5] `istioctl x describe pod` reports a STRICT PeerAuthentication paired with a DestinationRule whose `tls.mode` is `DISABLE` (or the destination is non-mesh)

**Mitigation:**

- **Risk:** Lowering the namespace `PeerAuthentication` to `PERMISSIVE` lets traffic through but accepts plaintext from anywhere, eroding tenant isolation; only acceptable in a single-tenant cluster during incident response.
- **Command:**

  ```bash
  kubectl patch peerauthentication default -n <dst-ns> --type merge \
    -p '{"spec":{"mtls":{"mode":"PERMISSIVE"}}}'
  ```

- **Duration:** Hours. Revert to STRICT once the destination sidecar is injected or the DestinationRule is aligned.

**Resolution:**

```bash
# Option 1 — inject a sidecar into the destination workload
kubectl label namespace <dst-ns> istio-injection=enabled --overwrite
kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>

# Option 2 — align the DestinationRule with the in-mesh destination
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: <dst-svc>
  namespace: <dst-ns>
spec:
  host: <dst-svc>.<dst-ns>.svc.cluster.local
  trafficPolicy:
    tls:
      mode: ISTIO_MUTUAL
EOF
```

**Impact:** Option 1 restarts every pod in `<dst-deploy>` so the sidecar can be injected; expect a normal rolling-update blip. Option 2 is a config-only change; istiod pushes the updated cluster to every client sidecar within seconds and existing connections in the old plaintext mode drain on next request.

**Rollback:** Option 1 — `kubectl label namespace <dst-ns> istio-injection-` and re-roll the deployment. Option 2 — `kubectl delete destinationrule <dst-svc> -n <dst-ns>` or `kubectl apply` the previous manifest from git.

**Verification:** `istioctl x describe pod -n <dst-ns> <dst-pod>` prints `Effective PeerAuthentication: STRICT` plus a matching `ISTIO_MUTUAL` DestinationRule with no warnings; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"UF"'` shows no new entries over a 10-minute window.

### Cause B: Destination workload is missing the sidecar entirely

**Statement:** The destination pod has no `istio-proxy` container, so the client sidecar's mTLS connection has nothing to terminate and the request fails before reaching the application.

**Mechanism:** The mutating admission webhook injects the `istio-proxy` container only when the namespace carries `istio-injection=enabled` (or the per-revision label `istio.io/rev=<rev>`), the pod is not annotated `sidecar.istio.io/inject: "false"`, and the pod is not in `kube-system` / `kube-public` or running with `hostNetwork: true`. When any of these conditions fail, the pod runs without a sidecar; if the namespace `PeerAuthentication` is STRICT, every in-mesh client sees `UF` against the workload. When `PeerAuthentication` is PERMISSIVE the request usually succeeds — making this cause dominate after a STRICT-mode migration.

**Indicator:**

- [Step 2] container list for `<dst-pod>` does not contain `istio-proxy`
<!-- match: {"step": 2, "predicate": "absent", "target": "istio-proxy"} -->
- [Step 2] namespace `<dst-ns>` is missing both `istio-injection=enabled` and any `istio.io/rev` label
- [Step 2] pod annotation `sidecar.istio.io/inject` is `"false"` or the pod sets `spec.hostNetwork: true`

**Mitigation:**

- **Risk:** Labelling the namespace forces a sidecar into every existing pod after restart; pods with init containers that race the sidecar may need `holdApplicationUntilProxyStarts: true` in the injection template.
- **Command:**

  ```bash
  kubectl label namespace <dst-ns> istio-injection=enabled --overwrite
  kubectl get pod -n <dst-ns> <dst-pod> -o jsonpath='{.metadata.annotations}'
  ```

- **Duration:** Minutes — applies on next pod creation.

**Resolution:**

```bash
# Permanent fix: label the namespace and roll the workload
kubectl label namespace <dst-ns> istio-injection=enabled --overwrite
kubectl annotate pod -n <dst-ns> -l app=<dst-app> sidecar.istio.io/inject-
kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>
kubectl rollout status deployment/<dst-deploy> -n <dst-ns>
```

**Impact:** Every pod in the targeted deployment recreates and gains a sidecar. Memory footprint per pod increases by roughly 50-150 MiB for `istio-proxy`; ensure node capacity headroom before rolling large fleets.

**Rollback:** `kubectl label namespace <dst-ns> istio-injection-` and `kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>`. New pods come up without the sidecar.

**Verification:** `kubectl get pod -n <dst-ns> -l app=<dst-app> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].name}{"\n"}{end}'` lists `istio-proxy` for every pod; `istioctl proxy-status` shows the new pods as `SYNCED` across all four xDS columns.

### Cause C: No healthy upstream — Service has zero ready Endpoints

**Statement:** The destination Kubernetes Service has no ready pods backing it, so istiod publishes an empty EDS cluster and Envoy responds 503 with `UH` and `response_code_details: no_healthy_upstream`.

**Mechanism:** Istio's EDS feed mirrors Kubernetes `EndpointSlice` `ready` addresses. If every backing pod is Terminating, failing its readinessProbe, evicted, or its selector does not match the Service, the EndpointSlice contains no ready endpoints. istiod pushes an empty endpoint list to all client sidecars for that cluster; Envoy then has zero healthy hosts to load-balance to and short-circuits requests with 503 / `UH`. The same flag appears when outlier detection has ejected the entire pool (see Cause F) or when the `subset:` filter in a DestinationRule matches no labels.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"UH"`
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"UH\""} -->
- [Step 1] `response_code_details` field contains `no_healthy_upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "no_healthy_upstream"} -->
- [Step 4] `istioctl proxy-config endpoint` returns an empty list for the affected cluster
- [Step 8] `kubectl get endpoints -n <dst-ns> <dst-svc>` shows `<none>` or no `addresses:` block

**Mitigation:**

- **Risk:** Scaling up before identifying why pods failed readiness can mask a deeper crash loop and burn capacity; cap the scale step and watch the new pods land.
- **Command:**

  ```bash
  kubectl scale deployment/<dst-deploy> -n <dst-ns> --replicas=<n+1>
  kubectl get pod -n <dst-ns> -l app=<dst-app> -w
  ```

- **Duration:** Minutes, until at least one pod becomes Ready.

**Resolution:**

```bash
# Investigate why the existing pods aren't Ready
kubectl describe pod -n <dst-ns> <dst-pod> | sed -n '/Events:/,$p'
kubectl logs -n <dst-ns> <dst-pod> -c <app-container> --previous --tail=200

# Fix the root cause (readinessProbe path, dependency, image), then ensure replicas match desired:
kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>
kubectl rollout status deployment/<dst-deploy> -n <dst-ns>
```

**Impact:** Restart is namespace-scoped; pods replace one-by-one under the deployment's update strategy. If the cause is a poll-loop dependency (DB unreachable, secret missing), restart alone will not help — fix the dependency first.

**Rollback:** `kubectl rollout undo deployment/<dst-deploy> -n <dst-ns>` to revert to the prior ReplicaSet.

**Verification:** `kubectl get endpoints -n <dst-ns> <dst-svc>` lists at least one `<ip>:<port>` pair; `istioctl proxy-config endpoint -n <src-ns> <src-pod> --cluster "outbound|<port>||<fqdn>"` prints rows with status `HEALTHY`; the access log shows no new `UH` flags over 10 minutes.

### Cause D: No route configured — VirtualService host or port mismatch (`NR`)

**Statement:** Envoy has no route configuration matching the request's `:authority` and port pair, so it returns 503 / `NR` (or 404 for pure RDS misses) because the VirtualService host, Service port, or `meshConfig.outboundTrafficPolicy` does not cover the destination.

**Mechanism:** Routes reach Envoy via RDS, derived from `VirtualService` + `Service` definitions. A `VirtualService.spec.hosts` entry that does not match the request `Host` header, a port number that doesn't exist on the Service, or a Service port whose `name` violates Istio's protocol-detection convention (must start with `http`, `http2`, `grpc`, `tls`, `tcp`, `udp`, `mongo`, `redis`, `mysql`) all leave the request without a matching route. With `REGISTRY_ONLY` outbound policy, calls to unknown FQDNs (typos, external hosts without a `ServiceEntry`) similarly produce `NR`.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"NR"`
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"NR\""} -->
- [Step 1] `response_code_details` field contains `route_not_found` or `cluster_not_found`
<!-- match: {"step": 1, "predicate": "contains", "target": "route_not_found"} -->
- [Step 4] `istioctl proxy-config route` for the affected port returns no `virtual_host` whose domains include `<dst-host>`
- [Step 6] `istioctl analyze` reports `IST0118 (PortNameIsNotUnderNamingConvention)` or `IST0101 (ReferencedResourceNotFound)` against the Service or VirtualService

**Mitigation:**

- **Risk:** Setting `outboundTrafficPolicy.mode: ALLOW_ANY` lets unknown destinations through without route enforcement, but also disables protocol detection and may break observability for external calls.
- **Command:**

  ```bash
  kubectl get configmap -n istio-system istio -o yaml \
    | grep -E "outboundTrafficPolicy|mode:"
  ```

- **Duration:** Minutes — diagnostic only.

**Resolution:**

```bash
# Fix the VirtualService host list and port number
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: <dst-svc>
  namespace: <dst-ns>
spec:
  hosts:
    - <dst-svc>.<dst-ns>.svc.cluster.local
    - <dst-svc>           # short-name match within the namespace
  http:
    - route:
        - destination:
            host: <dst-svc>.<dst-ns>.svc.cluster.local
            port:
              number: <svc-port>
EOF

# Or rename the Service port so protocol detection picks the right filter chain
kubectl patch svc -n <dst-ns> <dst-svc> --type=json \
  -p='[{"op":"replace","path":"/spec/ports/0/name","value":"http"}]'
```

**Impact:** istiod pushes the new RDS / CDS within seconds; existing connections continue on previous routes until they end. Renaming a port may briefly cause `NC` (cluster_not_found) until the push lands across all sidecars.

**Rollback:** `kubectl apply -f` the prior VirtualService manifest from git, or `kubectl patch svc ... value: "<original-name>"`.

**Verification:** `istioctl proxy-config route -n <src-ns> <src-pod>` shows a `virtual_host` whose `domains` list contains the request host; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"NR"'` returns no new lines.

### Cause E: Connection-pool overflow or pending-request overflow (`UO`)

**Statement:** The destination cluster's `connectionPool` limits in the DestinationRule are saturated, so Envoy short-circuits new requests with 503 and the `UO` flag (upstream overflow / circuit breaking).

**Mechanism:** Envoy enforces `connectionPool.tcp.maxConnections`, `connectionPool.http.http1MaxPendingRequests`, `connectionPool.http.http2MaxRequests`, and `connectionPool.http.maxRequestsPerConnection` per host per worker thread. When any limit is hit, the corresponding `_overflow` counter increments and the request is rejected before reaching the upstream. The default `2^32-1` ceilings are effectively unlimited, but operators frequently set conservative values for tenant isolation; under traffic bursts the limit becomes the bottleneck and clients see 503/`UO`.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"UO"`
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"UO\""} -->
- [Step 7] `pilot-agent request GET stats | grep <dst-svc>` shows a non-zero counter for `upstream_rq_pending_overflow`, `upstream_cx_overflow`, or `upstream_rq_active`
<!-- match: {"step": 7, "predicate": "contains", "target": "upstream_rq_pending_overflow"} -->
- [Step 7] traffic spike coincides with the 503 burst and the upstream pods are otherwise healthy

**Mitigation:**

- **Risk:** Raising connection-pool limits shifts the bottleneck to the upstream's own resource limits — a sudden 10x in concurrent requests can overload application threads or DB pools downstream.
- **Command:**

  ```bash
  kubectl patch destinationrule <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"trafficPolicy":{"connectionPool":{
    "tcp":{"maxConnections":1024},
    "http":{"http1MaxPendingRequests":1024,"http2MaxRequests":1024,"maxRequestsPerConnection":0}
  }}}}'
  ```

- **Duration:** Hours, while the upstream is scaled or sized for the new load.

**Resolution:**

```bash
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: <dst-svc>
  namespace: <dst-ns>
spec:
  host: <dst-svc>.<dst-ns>.svc.cluster.local
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 1024
      http:
        http1MaxPendingRequests: 1024
        http2MaxRequests: 1024
        maxRequestsPerConnection: 0
        idleTimeout: 30s
EOF
```

**Impact:** Push is delivered through CDS within seconds and affects every client of `<dst-svc>` cluster-wide. Each client sidecar will hold more concurrent TCP/HTTP sockets — verify the upstream's `--max-connections` (or equivalent) and Pod resource limits before raising the cap.

**Rollback:** `kubectl apply` the prior DestinationRule from git, or `kubectl delete destinationrule <dst-svc> -n <dst-ns>` to fall back to mesh defaults.

**Verification:** After the change, `pilot-agent request GET stats | grep -E "<dst-svc>.*(upstream_rq_pending_overflow|upstream_cx_overflow)"` shows the counter stops incrementing; 503 / `UO` is no longer present in the client sidecar log over 15 minutes of steady traffic.

### Cause F: Outlier detection ejected every endpoint, leaving the pool empty (`UH`)

**Statement:** Envoy's outlier-detection logic ejected every host in the cluster after consecutive 5xx or gateway errors, so the pool is empty and new requests fail with 503 / `UH` even though pods are still Running.

**Mechanism:** `DestinationRule.spec.trafficPolicy.outlierDetection` ejects a host from the load-balancing pool when it returns `consecutive5xxErrors` (default 5) or `consecutiveGatewayErrors` (502/503/504) errors in a row. Ejected hosts are removed for `baseEjectionTime` (default 30s), with exponential backoff for repeated ejections. `maxEjectionPercent` caps the share of hosts that can be ejected simultaneously (default 10%), but operators commonly set it to 100% for tighter isolation — a brief upstream incident then ejects every host at once and the pool empties, mirroring Cause C's surface symptom.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"UH"`
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"UH\""} -->
- [Step 7] `pilot-agent request GET stats | grep outlier` shows `outlier_detection.ejections_active` greater than zero for the destination cluster
<!-- match: {"step": 7, "predicate": "contains", "target": "outlier_detection.ejections_active"} -->
- [Step 8] `kubectl get endpoints` shows backing pods still listed as ready (distinguishes from Cause C)

**Mitigation:**

- **Risk:** Disabling outlier detection removes protection against a genuinely bad host serving errors to every caller; only acceptable while debugging the over-aggressive ejection.
- **Command:**

  ```bash
  kubectl patch destinationrule <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"trafficPolicy":{"outlierDetection":{"maxEjectionPercent":50}}}}'
  ```

- **Duration:** Hours, until thresholds are tuned to the real error budget.

**Resolution:**

```bash
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: <dst-svc>
  namespace: <dst-ns>
spec:
  host: <dst-svc>.<dst-ns>.svc.cluster.local
  trafficPolicy:
    outlierDetection:
      consecutive5xxErrors: 10
      consecutiveGatewayErrors: 10
      interval: 30s
      baseEjectionTime: 30s
      maxEjectionPercent: 50
EOF
```

**Impact:** Pushed through CDS; affects every client sidecar for `<dst-svc>`. Raising thresholds keeps a marginally-bad host in the pool longer, accepting a slightly higher error rate to clients in exchange for keeping the pool non-empty under partial outages.

**Rollback:** Revert the DestinationRule via `kubectl apply` of the prior manifest, or `kubectl delete destinationrule <dst-svc> -n <dst-ns>`.

**Verification:** `pilot-agent request GET stats | grep "<dst-svc>.*outlier_detection.ejections_active"` returns 0 over 10 minutes of steady traffic; no new `"response_flags":"UH"` entries appear on the client sidecar.

### Cause G: Upstream request retry budget exhausted (`URX`)

**Statement:** The configured retry policy attempted the request the maximum number of times across the upstream pool and every attempt failed, so Envoy gives up and returns 503 with the `URX` flag.

**Mechanism:** VirtualService `retries.attempts`, `retries.perTryTimeout`, and `retries.retryOn` control how many times Envoy re-tries a failing request against alternate endpoints. If `retries.retryOn: 5xx,connect-failure,refused-stream` is in effect and every retried endpoint also fails — typical when the underlying issue affects the whole pool, such as a bad deploy, a downstream dependency outage, or a transient mTLS rotation — Envoy increments `upstream_rq_retry_overflow` (when the per-route budget caps further retries) and returns 503 / `URX` to the client. Unlike `UH`, the endpoints are not ejected; they just keep failing.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"URX"`
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"URX\""} -->
- [Step 7] `pilot-agent request GET stats` shows `upstream_rq_retry_overflow` or `upstream_rq_retry_limit_exceeded` incrementing for the cluster
<!-- match: {"step": 7, "predicate": "contains", "target": "upstream_rq_retry_overflow"} -->
- [Step 4] endpoints are listed as `HEALTHY` (the retry exhaustion is downstream of pool health)

**Mitigation:**

- **Risk:** Raising retry attempts amplifies load on a struggling pool; combine with `perTryTimeout` to bound total wait, otherwise client p99 grows linearly with the retry count.
- **Command:**

  ```bash
  kubectl patch virtualservice <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"http":[{"retries":{"attempts":3,"perTryTimeout":"5s","retryOn":"5xx,connect-failure,refused-stream"}}]}}'
  ```

- **Duration:** Hours, until the upstream failure mode is fixed.

**Resolution:**

```bash
# Fix the underlying failure first — retries should not be the durable answer.
# 1) Check recent deploys
kubectl rollout history deployment/<dst-deploy> -n <dst-ns>
kubectl rollout undo deployment/<dst-deploy> -n <dst-ns>

# 2) Then tune retry policy to a sensible budget
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: <dst-svc>
  namespace: <dst-ns>
spec:
  hosts:
    - <dst-svc>.<dst-ns>.svc.cluster.local
  http:
    - route:
        - destination:
            host: <dst-svc>.<dst-ns>.svc.cluster.local
      retries:
        attempts: 3
        perTryTimeout: 5s
        retryOn: 5xx,connect-failure,refused-stream,reset
EOF
```

**Impact:** Rolling back the deployment is the durable fix; the retry config change pushes through RDS and affects every client of the VirtualService. Higher attempts can multiply load on an already-degraded pool — never raise without also fixing the root error.

**Rollback:** `kubectl apply` the prior VirtualService and re-roll the deployment with `kubectl rollout undo deployment/<dst-deploy> -n <dst-ns>`.

**Verification:** `pilot-agent request GET stats | grep upstream_rq_retry_overflow` stops incrementing; client access log shows no new `URX` flags; the rolled-back deployment's pod-level error rate (`upstream_rq_xx{response_code_class=5xx}`) drops to pre-deploy baseline.

### Cause H: AuthorizationPolicy DENY or external-authz failure

**Statement:** An Istio AuthorizationPolicy denied the request at the destination sidecar, returning 403 for native DENY rules and 503 with the `UAEX` flag when a CUSTOM-action external authorizer is unreachable.

**Mechanism:** AuthorizationPolicy ALLOW/DENY rules are compiled into the destination sidecar's RBAC filter. DENY matches return HTTP 403 with `rbac_access_denied_matched_policy[ns[<ns>]-policy[<name>]-rule[<n>]]` in the access log; if no ALLOW rule matches in a namespace that has any ALLOW policies, the implicit deny also returns 403. CUSTOM-action policies (commonly used to call an external OPA, Authzed, or OAuth introspection endpoint) require the external service to respond within the configured timeout — when it doesn't, Envoy fails-closed with 503 / `UAEX` (`Request was denied by external authorization service`).

**Indicator:**

- [Step 1] destination sidecar access log contains `rbac_access_denied_matched_policy` for the request (status 403, not 503)
<!-- match: {"step": 1, "predicate": "contains", "target": "rbac_access_denied_matched_policy"} -->
- [Step 1] client sidecar access log contains `"response_flags":"UAEX"` and status 503 (CUSTOM ext-authz path)
<!-- match: {"step": 1, "predicate": "contains", "target": "\"response_flags\":\"UAEX\""} -->
- [Step 10] `kubectl exec ... pilot-agent request GET stats | grep rbac` shows `rbac.denied` incrementing on the destination sidecar

**Mitigation:**

- **Risk:** Temporarily switching a CUSTOM policy to `action: ALLOW` (or deleting the policy) opens the route to every caller; only acceptable during an incident with explicit change-control sign-off.
- **Command:**

  ```bash
  kubectl patch authorizationpolicy <policy-name> -n <dst-ns> --type=json \
    -p='[{"op":"replace","path":"/spec/action","value":"ALLOW"}]'
  ```

- **Duration:** Minutes. Restore the original `action` as soon as the policy mismatch or ext-authz outage is fixed.

**Resolution:**

```bash
# Native DENY 403: identify the offending policy from the access log line
#   rbac_access_denied_matched_policy[ns[foo]-policy[bar]-rule[2]]
kubectl get authorizationpolicy -n <dst-ns> <policy-name> -o yaml
# Adjust the rule (principal, source, paths, methods) to include the intended caller, then re-apply.

# CUSTOM ext-authz 503 / UAEX: verify the ext-authz Service is reachable and healthy
kubectl get svc -A | grep -i authz
kubectl logs -n <authz-ns> deploy/<authz-deploy> --tail=200
# Verify the meshConfig.extensionProviders entry matches the policy provider.name
kubectl get configmap -n istio-system istio -o yaml | grep -A 10 extensionProviders
```

**Impact:** Policy changes propagate via RDS to every destination sidecar within seconds. Modifying an ALLOW principal list is namespace-scoped; restoring an ext-authz Service unblocks every CUSTOM policy that references it.

**Rollback:** `kubectl apply` the prior AuthorizationPolicy from git, or recreate the ext-authz Service and pod with the prior manifest if the outage was caused by accidental deletion.

**Verification:** `pilot-agent request GET stats | grep rbac.denied` no longer increments on the destination sidecar; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"UAEX"'` returns nothing for 10 minutes; the previously denied principal receives 2xx for a representative request.

### Cause I: Sidecar config out of sync with istiod (`NC`, `NR`)

**Statement:** istiod has not pushed the latest xDS configuration to one or more sidecars, so the affected proxies hold a stale cluster or route table and return 503 with `NC` (`cluster_not_found`) or `NR` against newly created destinations.

**Mechanism:** istiod debounces and pushes xDS updates to sidecars over a long-lived gRPC stream. When istiod is under load, when the proxy is on an isolated node with degraded networking, or when a CRD update was rejected by the proxy (schema mismatch, exceeded `pilot-agent` ACK), the proxy's `CDS`/`EDS`/`LDS`/`RDS` lags. `istioctl proxy-status` reports `STALE` or `NOT SENT` for the affected dimension. Requests to newly created Services have no matching cluster (`NC`) or no matching route (`NR`) on the stale proxy, even though every other proxy in the mesh serves them correctly.

**Indicator:**

- [Step 1] client sidecar access log contains `"response_flags":"NC"` or `"response_code_details":"cluster_not_found"`
<!-- match: {"step": 1, "predicate": "contains", "target": "cluster_not_found"} -->
- [Step 3] `istioctl proxy-status` shows `STALE` or `NOT SENT` against the affected pod for one or more of CDS/EDS/LDS/RDS
- [Step 11] istiod log contains `rejected ... update from <pod>` or `update rejected` referencing the source workload

**Mitigation:**

- **Risk:** Deleting the affected pod forces a fresh xDS connection to istiod; if the underlying cause is an istiod overload, the new connection adds to the load.
- **Command:**

  ```bash
  kubectl delete pod -n <src-ns> <src-pod>
  ```

- **Duration:** Minutes — applies until the workload's pods are replaced.

**Resolution:**

```bash
# 1) Confirm istiod is healthy and not memory-constrained
kubectl top pod -n istio-system -l app=istiod
kubectl logs -n istio-system deploy/istiod --tail=200 | grep -E "OOM|debounce|push|rejected"

# 2) Scale istiod if push debounce times are growing
kubectl scale deployment/istiod -n istio-system --replicas=3
kubectl rollout status deployment/istiod -n istio-system

# 3) If a specific proxy keeps rejecting updates, restart that proxy
kubectl delete pod -n <src-ns> <src-pod>
```

**Impact:** Scaling istiod is cluster-wide for the control plane and adds load to API server watches; expect a brief blip during rollout. Deleting a single client pod only restarts that workload.

**Rollback:** `kubectl scale deployment/istiod -n istio-system --replicas=<original>` to revert to the prior istiod fleet size.

**Verification:** `istioctl proxy-status` reports `SYNCED` for every pod across all four xDS columns; client sidecar access logs show no new `NC` or `NR` flags for routes to recently-created destinations.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirmed an Envoy-sourced 503 but did not match any of the indicators for Causes A through I.

**Mechanism:** A 503 was observed with `server: istio-envoy` and the access log carries a response flag, but the gathered evidence does not isolate the failure path. The response flag may be uncommon (`RH` for downstream HTTP/1.1 reset before headers, `OM` for overload-manager-driven drops, `DF` for DNS-resolution failure on a `ServiceEntry`), the failure may correlate with infrastructure (node loss, CNI plugin restart) outside Istio's view, or the symptom may be intermittent enough that no single Indicator fires.

**Indicator:**

- [Default] 503 is confirmed (Step 1) but Causes A–I indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Bumping Envoy log level to `debug` is read-only but verbose; on a busy proxy it can fill stdout buffers within minutes — scope to the affected component (`http`, `connection`, `router`, `rbac`) instead of `all`.
- **Command:**

  ```bash
  kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request POST 'logging?http=debug&connection=debug&router=debug'
  # Capture cluster / endpoint / listener / route dumps:
  istioctl proxy-config all -n <src-ns> <src-pod> > /tmp/istio-proxy-${src-pod}.dump
  # Capture istiod state:
  kubectl logs -n istio-system deploy/istiod --tail=2000 > /tmp/istiod.log
  # Snapshot Envoy config dump:
  kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request GET config_dump > /tmp/envoy-${src-pod}.json
  ```

- **Duration:** Minutes. Reset log level to `warning` (`pilot-agent request POST 'logging?http=warning&connection=warning&router=warning'`) once the capture is complete.

**Resolution:** Out of runbook scope. Package the captured `istio-proxy-*.dump`, `envoy-*.json`, `istiod.log`, the application logs for the same window, the affected `PeerAuthentication` / `DestinationRule` / `VirtualService` / `AuthorizationPolicy` manifests, and the access-log slice; escalate to the mesh owner or platform on-call with the affected route, the response flag, and the timestamp window.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Enforce mesh-wide `PeerAuthentication.mode: STRICT` only after every workload namespace is labelled `istio-injection=enabled` (or carries the matching `istio.io/rev` label) — pair the migration with the namespace label sweep, never lead with the policy change.
- Default every in-mesh `DestinationRule` to `trafficPolicy.tls.mode: ISTIO_MUTUAL`; treat `DISABLE` and `SIMPLE` as exceptional and gate them behind code review.
- Name every Service port with an Istio-recognised prefix (`http`, `http2`, `grpc`, `tls`, `tcp`, `mongo`, `redis`, `mysql`) so protocol detection picks the correct filter chain — add `istioctl analyze` to CI so port-name violations fail builds.
- Set conservative but realistic `connectionPool` (`tcp.maxConnections`, `http.http1MaxPendingRequests`, `http.http2MaxRequests`) values per workload sized from observed peak concurrency, and alert on `envoy_cluster_upstream_rq_pending_overflow > 0` per cluster.
- Configure `outlierDetection.maxEjectionPercent <= 50` and `consecutive5xxErrors >= 5` so brief upstream blips do not eject the entire pool. Page on `envoy_cluster_outlier_detection_ejections_active > 0` sustained for 1 minute.
- Run `istioctl analyze --all-namespaces` in a scheduled CI job (or as a pre-merge check) and alert on any IST01xx violation.
- Keep istiod horizontally scaled (`replicas >= 2`) with `topologySpreadConstraints` across zones; alert on `pilot_xds_pushes_total` push-time p99 > 1s and on `pilot_xds_push_context_errors_total > 0`.
- Page on the mesh SLI `sum(rate(envoy_cluster_upstream_rq{response_code_class="5xx"}[5m])) / sum(rate(envoy_cluster_upstream_rq[5m])) > 0.01` per service, and on `envoy_cluster_upstream_cx_connect_fail` per cluster.
- For CUSTOM-action AuthorizationPolicies, run health checks on the external authorizer behind an `httpGet` readinessProbe and alert on `envoy_ext_authz_denied + envoy_ext_authz_error`.
- Bake the `holdApplicationUntilProxyStarts: true` annotation (or `meshConfig.defaultConfig.holdApplicationUntilProxyStarts: true`) into the injection template so application containers never start before the sidecar is ready — this eliminates the startup-race `UC` burst.

## Sources

- [Istio — Common Problems: Network Issues](https://istio.io/latest/docs/ops/common-problems/network-issues/) — Priority 1. mTLS-misconfiguration as the dominant cause of 503 / UF / UC, double-TLS and gateway-VirtualService mismatch failure modes, headless-Service 503 scenario, response-flag mapping for UF / NR / UO.
- [Istio — Diagnostic Tools](https://istio.io/latest/docs/ops/diagnostic-tools/) — Priority 1. `istioctl proxy-config` for cluster / endpoint / listener / route dumps, `istioctl proxy-status` sync state, `istioctl analyze` validation, `istioctl x describe pod` effective-policy view, `istioctl authz check`.
- [Istio — Common Problems: Sidecar Injection](https://istio.io/latest/docs/ops/common-problems/injection/) — Priority 1. Conditions that prevent automatic injection (`hostNetwork`, missing `istio-injection=enabled`, `sidecar.istio.io/inject: "false"`, restricted namespaces), webhook configuration validation, consequences of missing sidecars (no mTLS, no traffic policies).
- [Istio — mTLS Migration Task](https://istio.io/latest/docs/tasks/security/authentication/mtls-migration/) — Priority 1. `PeerAuthentication.mode` semantics for STRICT / PERMISSIVE / DISABLE, namespace-vs-mesh-scoped enforcement, exit-code 56 (TLS reset) symptom when uninjected workloads hit STRICT-mode services.
- [Istio — DestinationRule Reference](https://istio.io/latest/docs/reference/config/networking/destination-rule/) — Priority 1. TLS modes (`ISTIO_MUTUAL`, `MUTUAL`, `SIMPLE`, `DISABLE`), connectionPool fields (`tcp.maxConnections`, `http1MaxPendingRequests`, `http2MaxRequests`, `maxRequestsPerConnection`), outlierDetection fields (`consecutive5xxErrors`, `consecutiveGatewayErrors`, `baseEjectionTime`, `maxEjectionPercent`).
- [Envoy — Substitution Formatter / Response Flags](https://www.envoyproxy.io/docs/envoy/latest/configuration/advanced/substitution_formatter) — Priority 1. Verbatim definitions for UH, UF, UO, NR, URX, NC, DC, LH, UT, LR, UR, UC, DI, FI, RL, UAEX, RLSE, IH, SI, DPE, UPE, OM, DF, DO — the canonical mapping from response_flag to root-cause category.
- [Istio — AuthorizationPolicy Deny Action](https://istio.io/latest/docs/tasks/security/authorization/authz-deny/) — Priority 2. HTTP 403 on DENY match, `rbac_access_denied_matched_policy[ns[]-policy[]-rule[]]` access-log format, DENY priority over ALLOW, CUSTOM-action ext-authz failure surface (`UAEX`).
