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
version: "2.0.0"
last_updated: "2026-06-25"
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

### Cause A: PeerAuthentication STRICT meets a DestinationRule that disables TLS

**Statement:** The destination enforces `PeerAuthentication.mode: STRICT` while its `DestinationRule.trafficPolicy.tls.mode` is `DISABLE`, so the client sidecar sends plaintext to an mTLS-only listener and the handshake fails before any HTTP request.

**Chain:**
- root: A `DISABLE` DestinationRule overrides the client's mTLS while the destination listener is STRICT-only, producing a TLS mode mismatch.
- s1: The client sidecar opens a plaintext connection to the destination's mTLS-only inbound listener.
- s2: The destination sidecar (or kernel) rejects the unwrapped connection with a TLS handshake error.
- s3: Envoy resets the connection before the upstream response starts and emits `UF` with `upstream_reset_before_response_started{connection_failure}`.
- D: Client receives 503 with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 5] `istioctl x describe pod` reports a STRICT PeerAuthentication paired with a DestinationRule whose `tls.mode` is `DISABLE`
- s3: [Step 1] client sidecar access log contains `"response_flags":"UF"` for the failed request
- s3: [Step 1] `response_code_details` field contains `upstream_reset_before_response_started{connection_failure}`

**Interventions:**
- **remediation** (root): align the DestinationRule with the in-mesh STRICT destination.

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
      tls:
        mode: ISTIO_MUTUAL
  EOF
  ```

  **Verification:** `istioctl x describe pod -n <dst-ns> <dst-pod>` prints `Effective PeerAuthentication: STRICT` plus a matching `ISTIO_MUTUAL` DestinationRule with no warnings; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"UF"'` shows no new entries over a 10-minute window.
- **mitigation** (root): lower the namespace `PeerAuthentication` to `PERMISSIVE` so plaintext is accepted while the DestinationRule is fixed.

  ```bash
  kubectl patch peerauthentication default -n <dst-ns> --type merge \
    -p '{"spec":{"mtls":{"mode":"PERMISSIVE"}}}'
  ```

  **Risk:** PERMISSIVE accepts plaintext from anywhere, eroding tenant isolation; only acceptable in a single-tenant cluster during incident response. **Duration:** Hours — revert to STRICT once the DestinationRule is aligned. **Verification:** new requests return 2xx and no fresh `"response_flags":"UF"` lines appear in the client sidecar log.

### Cause B: Destination workload is missing the sidecar entirely

**Statement:** The destination pod has no `istio-proxy` container, so under namespace STRICT mTLS the client sidecar's mTLS connection has nothing to terminate and the request fails before reaching the application.

**Chain:**
- root: Injection preconditions are unmet (namespace lacks `istio-injection=enabled`/`istio.io/rev`, pod annotated `sidecar.istio.io/inject: "false"`, or `hostNetwork: true`), so the admission webhook skips the sidecar.
- s1: The destination pod runs without an `istio-proxy` container.
- s2: With namespace `PeerAuthentication` STRICT, the in-mesh client's mTLS connection has no Envoy to terminate it and fails.
- s3: Envoy resets before the upstream response starts and emits `UF` against the workload.
- D: Client receives 503 with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] namespace `<dst-ns>` is missing both `istio-injection=enabled` and any `istio.io/rev` label, or the pod annotation `sidecar.istio.io/inject` is `"false"` or `spec.hostNetwork: true`
- s1: [Step 2] container list for `<dst-pod>` does not contain `istio-proxy`
- s3: [Step 1] client sidecar access log contains `"response_flags":"UF"` for the failed request

**Interventions:**
- **remediation** (root): label the namespace, clear any opt-out annotation, and roll the workload so every pod gains a sidecar.

  ```bash
  kubectl label namespace <dst-ns> istio-injection=enabled --overwrite
  kubectl annotate pod -n <dst-ns> -l app=<dst-app> sidecar.istio.io/inject-
  kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>
  kubectl rollout status deployment/<dst-deploy> -n <dst-ns>
  ```

  **Verification:** `kubectl get pod -n <dst-ns> -l app=<dst-app> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].name}{"\n"}{end}'` lists `istio-proxy` for every pod; `istioctl proxy-status` shows the new pods as `SYNCED` across all four xDS columns.
- **mitigation** (root): label the namespace now so injection applies on the next pod creation without a forced roll.

  ```bash
  kubectl label namespace <dst-ns> istio-injection=enabled --overwrite
  kubectl get pod -n <dst-ns> <dst-pod> -o jsonpath='{.metadata.annotations}'
  ```

  **Risk:** Pods with init containers that race the sidecar may need `holdApplicationUntilProxyStarts: true` in the injection template. **Duration:** Minutes — applies on next pod creation. **Verification:** newly created pods list `istio-proxy` in their container set.

### Cause C: No healthy upstream — Service has zero ready Endpoints

**Statement:** The destination Kubernetes Service has no ready pods backing it, so istiod publishes an empty EDS cluster and Envoy responds 503 with `UH` and `no_healthy_upstream`.

**Chain:**
- root: Every backing pod is Terminating, failing its readinessProbe, evicted, or unmatched by the Service selector, so the EndpointSlice has no ready addresses.
- s1: istiod's EDS feed mirrors the empty ready set and pushes an empty endpoint list for the cluster to all client sidecars.
- s2: Envoy has zero healthy hosts to load-balance to and short-circuits requests with `no_healthy_upstream`.
- D: Client receives 503 / `UH` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] `kubectl get endpoints -n <dst-ns> <dst-svc>` shows `<none>` or no `addresses:` block
- s1: [Step 4] `istioctl proxy-config endpoint` returns an empty list for the affected cluster
- s2: [Step 1] client sidecar access log contains `"response_flags":"UH"`
- s2: [Step 1] `response_code_details` field contains `no_healthy_upstream`

**Interventions:**
- **remediation** (root): find why pods are not Ready, fix the root (readinessProbe path, dependency, image), then roll the deployment.

  ```bash
  kubectl describe pod -n <dst-ns> <dst-pod> | sed -n '/Events:/,$p'
  kubectl logs -n <dst-ns> <dst-pod> -c <app-container> --previous --tail=200
  kubectl rollout restart deployment/<dst-deploy> -n <dst-ns>
  kubectl rollout status deployment/<dst-deploy> -n <dst-ns>
  ```

  **Verification:** `kubectl get endpoints -n <dst-ns> <dst-svc>` lists at least one `<ip>:<port>` pair; `istioctl proxy-config endpoint -n <src-ns> <src-pod> --cluster "outbound|<port>||<fqdn>"` prints rows with status `HEALTHY`; the access log shows no new `UH` flags over 10 minutes.
- **mitigation** (root): scale up by one replica to recover at least one ready pod while the readiness cause is investigated.

  ```bash
  kubectl scale deployment/<dst-deploy> -n <dst-ns> --replicas=<n+1>
  kubectl get pod -n <dst-ns> -l app=<dst-app> -w
  ```

  **Risk:** Scaling before identifying why pods failed readiness can mask a deeper crash loop and burn capacity; cap the scale step and watch the new pods land. **Duration:** Minutes, until at least one pod becomes Ready. **Verification:** `kubectl get endpoints` lists at least one address and 503/`UH` stops appearing.

### Cause D: No route configured — VirtualService host or Service port mismatch (`NR`)

**Statement:** Envoy has no route matching the request's `:authority` and port because the VirtualService host, Service port number, or port `name` (protocol-detection convention) does not cover the destination, so it returns 503 / `NR`.

**Chain:**
- root: A VirtualService host that does not match the request `Host`, a non-existent Service port, or a Service port `name` violating Istio's protocol convention leaves the destination uncovered.
- s1: RDS, derived from VirtualService + Service, computes no `virtual_host`/route matching the request authority and port for the client proxy.
- s2: Envoy finds no matching route and short-circuits with `route_not_found` (or `cluster_not_found`).
- D: Client receives 503 / `NR` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 6] `istioctl analyze` reports `IST0118 (PortNameIsNotUnderNamingConvention)` or `IST0101 (ReferencedResourceNotFound)` against the Service or VirtualService
- s1: [Step 4] `istioctl proxy-config route` for the affected port returns no `virtual_host` whose domains include `<dst-host>`
- s2: [Step 1] client sidecar access log contains `"response_flags":"NR"`
- s2: [Step 1] `response_code_details` field contains `route_not_found` or `cluster_not_found`

**Interventions:**
- **remediation** (root): correct the VirtualService host list and port, or rename the Service port to a recognised protocol prefix.

  ```bash
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
  kubectl patch svc -n <dst-ns> <dst-svc> --type=json \
    -p='[{"op":"replace","path":"/spec/ports/0/name","value":"http"}]'
  ```

  **Verification:** `istioctl proxy-config route -n <src-ns> <src-pod>` shows a `virtual_host` whose `domains` list contains the request host; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"NR"'` returns no new lines.
- **defensive_fix** (root): for unknown external FQDNs, relaxing `outboundTrafficPolicy` to `ALLOW_ANY` lets calls through while the missing route/ServiceEntry is added.

  ```bash
  kubectl get configmap -n istio-system istio -o yaml \
    | grep -E "outboundTrafficPolicy|mode:"
  ```

  **Verification:** with the route or ServiceEntry in place, calls to the destination resolve and `NR` no longer appears; restore `REGISTRY_ONLY` once routing is correct.

### Cause E: Connection-pool overflow / circuit breaking (`UO`)

**Statement:** The destination cluster's `connectionPool` limits in the DestinationRule are saturated, so Envoy short-circuits new requests with 503 and the `UO` flag (upstream overflow / circuit breaking).

**Chain:**
- root: Conservative `connectionPool` limits (`tcp.maxConnections`, `http1MaxPendingRequests`, `http2MaxRequests`, `maxRequestsPerConnection`) are sized below peak concurrency.
- s1: A traffic burst drives concurrent connections/requests past a configured limit per host per worker thread.
- s2: Envoy increments the matching `_overflow` counter and rejects the request before it reaches the upstream.
- D: Client receives 503 / `UO` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 7] traffic spike coincides with the 503 burst and the upstream pods are otherwise healthy
- s2: [Step 1] client sidecar access log contains `"response_flags":"UO"`
- s2: [Step 7] `pilot-agent request GET stats | grep <dst-svc>` shows a non-zero `upstream_rq_pending_overflow` or `upstream_cx_overflow` counter

**Interventions:**
- **remediation** (root): raise the `connectionPool` limits to match sized peak concurrency after confirming upstream headroom.

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

  **Verification:** `pilot-agent request GET stats | grep -E "<dst-svc>.*(upstream_rq_pending_overflow|upstream_cx_overflow)"` shows the counter stops incrementing; 503 / `UO` is absent from the client sidecar log over 15 minutes of steady traffic.
- **mitigation** (root): apply the higher pool limits as a quick patch while the upstream is scaled or resized.

  ```bash
  kubectl patch destinationrule <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"trafficPolicy":{"connectionPool":{
    "tcp":{"maxConnections":1024},
    "http":{"http1MaxPendingRequests":1024,"http2MaxRequests":1024,"maxRequestsPerConnection":0}
  }}}}'
  ```

  **Risk:** Raising limits shifts the bottleneck to the upstream's own resource limits — a sudden 10x in concurrent requests can overload application threads or DB pools downstream. **Duration:** Hours, while the upstream is scaled or sized for the new load. **Verification:** overflow counters stop incrementing and `UO` clears.

### Cause F: Outlier detection ejected every endpoint, emptying the pool (`UH`)

**Statement:** Envoy's outlier-detection logic ejected every host in the cluster after consecutive 5xx or gateway errors, so the pool is empty and new requests fail with 503 / `UH` even though pods are still Running.

**Chain:**
- root: `outlierDetection` is configured with `maxEjectionPercent: 100` (or near it), removing the cap that normally keeps part of the pool in service.
- s1: A brief upstream incident produces `consecutive5xxErrors`/`consecutiveGatewayErrors` on every host.
- s2: Outlier detection ejects all hosts at once for `baseEjectionTime`, leaving the load-balancing pool empty.
- s3: Envoy has no host to route to and returns `UH`, mirroring Cause C's surface symptom while pods stay Ready.
- D: Client receives 503 / `UH` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] `kubectl get endpoints` shows backing pods still listed as ready (distinguishes from Cause C)
- s2: [Step 7] `pilot-agent request GET stats | grep outlier` shows `outlier_detection.ejections_active` greater than zero for the destination cluster
- s3: [Step 1] client sidecar access log contains `"response_flags":"UH"`

**Interventions:**
- **remediation** (root): tune outlier-detection thresholds and cap `maxEjectionPercent` so brief blips cannot empty the pool.

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

  **Verification:** `pilot-agent request GET stats | grep "<dst-svc>.*outlier_detection.ejections_active"` returns 0 over 10 minutes of steady traffic; no new `"response_flags":"UH"` entries appear on the client sidecar.
- **mitigation** (root): lower `maxEjectionPercent` immediately to keep part of the pool in service while thresholds are tuned.

  ```bash
  kubectl patch destinationrule <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"trafficPolicy":{"outlierDetection":{"maxEjectionPercent":50}}}}'
  ```

  **Risk:** Loosening ejection keeps a genuinely bad host serving errors to some callers; only acceptable while debugging the over-aggressive ejection. **Duration:** Hours, until thresholds are tuned to the real error budget. **Verification:** `ejections_active` drops below the host count and `UH` clears.

### Cause G: Upstream request retry budget exhausted (`URX`)

**Statement:** The retry policy attempted the request the maximum number of times across the upstream pool and every attempt failed, so Envoy gives up and returns 503 with the `URX` flag while endpoints stay HEALTHY.

**Chain:**
- root: An upstream-wide failure (bad deploy, downstream dependency outage, transient mTLS rotation) makes every endpoint return retryable errors.
- s1: With `retries.retryOn: 5xx,connect-failure,refused-stream`, Envoy re-tries the request against alternate endpoints, all of which also fail.
- s2: The per-route retry budget caps further attempts; Envoy increments `upstream_rq_retry_overflow` and gives up.
- D: Client receives 503 / `URX` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] endpoints are listed as `HEALTHY` (the retry exhaustion is downstream of pool health)
- s1: [Step 1] client sidecar access log contains `"response_flags":"URX"`
- s2: [Step 7] `pilot-agent request GET stats` shows `upstream_rq_retry_overflow` or `upstream_rq_retry_limit_exceeded` incrementing for the cluster

**Interventions:**
- **remediation** (root): fix the underlying upstream failure first — roll back a bad deploy — then set a sane retry budget.

  ```bash
  kubectl rollout history deployment/<dst-deploy> -n <dst-ns>
  kubectl rollout undo deployment/<dst-deploy> -n <dst-ns>
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

  **Verification:** `pilot-agent request GET stats | grep upstream_rq_retry_overflow` stops incrementing; client access log shows no new `URX` flags; the rolled-back deployment's pod-level 5xx rate drops to pre-deploy baseline.
- **mitigation** (s1): widen the retry policy to absorb transient failures while the root upstream issue is fixed.

  ```bash
  kubectl patch virtualservice <dst-svc> -n <dst-ns> --type merge -p '
  {"spec":{"http":[{"retries":{"attempts":3,"perTryTimeout":"5s","retryOn":"5xx,connect-failure,refused-stream"}}]}}'
  ```

  **Risk:** More attempts amplify load on a struggling pool; bound total wait with `perTryTimeout` or client p99 grows linearly with the retry count. **Duration:** Hours, until the upstream failure mode is fixed. **Verification:** retry-overflow counter stabilises and transient `URX` bursts subside.

### Cause H: AuthorizationPolicy DENY or external-authz failure

**Statement:** An Istio AuthorizationPolicy blocked the request at the destination sidecar — native DENY (or unmatched ALLOW) returns 403, while a CUSTOM-action external authorizer that is unreachable fails closed with 503 / `UAEX`.

**Chain:**
- root: A DENY rule matches the caller, no ALLOW rule matches in an ALLOW-bearing namespace, or a CUSTOM-action external authorizer (OPA/Authzed/OAuth introspection) is unreachable.
- s1: The destination sidecar's RBAC filter evaluates the request against the compiled policy and blocks it.
- s2: Native DENY/implicit-deny returns 403 with `rbac_access_denied_matched_policy`; an unreachable CUSTOM authorizer fails closed with `UAEX`.
- D: Caller receives 403 (DENY) or 503 / `UAEX` (ext-authz) from the destination sidecar (points at Symptom Recognition).

**Indicators:**
- root: [Step 10] `kubectl exec ... pilot-agent request GET stats | grep rbac` shows `rbac.denied` incrementing on the destination sidecar
- s2: [Step 1] destination sidecar access log contains `rbac_access_denied_matched_policy` for the request (status 403)
- s2: [Step 1] client sidecar access log contains `"response_flags":"UAEX"` and status 503 (CUSTOM ext-authz path)

**Interventions:**
- **remediation** (root): for native DENY 403, identify the offending policy from the access-log line and adjust its rule to admit the intended caller; for ext-authz `UAEX`, restore the external authorizer Service.

  ```bash
  kubectl get authorizationpolicy -n <dst-ns> <policy-name> -o yaml
  kubectl get svc -A | grep -i authz
  kubectl logs -n <authz-ns> deploy/<authz-deploy> --tail=200
  kubectl get configmap -n istio-system istio -o yaml | grep -A 10 extensionProviders
  ```

  **Verification:** `pilot-agent request GET stats | grep rbac.denied` no longer increments; `kubectl logs -n <src-ns> <src-pod> -c istio-proxy --tail=100 | grep '"response_flags":"UAEX"'` returns nothing for 10 minutes; the previously denied principal receives 2xx.
- **mitigation** (root): temporarily switch the CUSTOM policy to `action: ALLOW` (or delete it) to unblock the route during the incident.

  ```bash
  kubectl patch authorizationpolicy <policy-name> -n <dst-ns> --type=json \
    -p='[{"op":"replace","path":"/spec/action","value":"ALLOW"}]'
  ```

  **Risk:** Switching to ALLOW opens the route to every caller; only acceptable during an incident with explicit change-control sign-off. **Duration:** Minutes — restore the original `action` as soon as the mismatch or ext-authz outage is fixed. **Verification:** the intended caller receives 2xx and `rbac.denied`/`UAEX` stop appearing.

### Cause I: Sidecar config out of sync with istiod (`NC`, `NR`)

**Statement:** istiod has not pushed the latest xDS to one or more sidecars, so the affected proxies hold a stale cluster or route table and return 503 with `NC` (`cluster_not_found`) or `NR` for newly created destinations.

**Chain:**
- root: istiod is under load, a proxy is on a degraded-network node, or a CRD update was rejected by the proxy (schema mismatch / failed ACK), stalling the xDS push.
- s1: The affected proxy's `CDS`/`EDS`/`LDS`/`RDS` lags istiod and shows `STALE` or `NOT SENT`.
- s2: Requests to newly created Services find no matching cluster (`NC`) or route (`NR`) on that stale proxy, though other proxies serve them.
- D: Client receives 503 / `NC` or `NR` with `server: istio-envoy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 11] istiod log contains `rejected ... update from <pod>` or `update rejected` referencing the source workload
- s1: [Step 3] `istioctl proxy-status` shows `STALE` or `NOT SENT` against the affected pod for one or more of CDS/EDS/LDS/RDS
- s2: [Step 1] client sidecar access log contains `cluster_not_found` (`NC`)

**Interventions:**
- **remediation** (root): confirm istiod health, scale it if push debounce times grow, and restart any proxy that keeps rejecting updates.

  ```bash
  kubectl top pod -n istio-system -l app=istiod
  kubectl logs -n istio-system deploy/istiod --tail=200 | grep -E "OOM|debounce|push|rejected"
  kubectl scale deployment/istiod -n istio-system --replicas=3
  kubectl rollout status deployment/istiod -n istio-system
  kubectl delete pod -n <src-ns> <src-pod>
  ```

  **Verification:** `istioctl proxy-status` reports `SYNCED` for every pod across all four xDS columns; client sidecar access logs show no new `NC` or `NR` flags for routes to recently-created destinations.
- **mitigation** (s1): delete the affected pod to force a fresh xDS connection while istiod capacity is addressed.

  ```bash
  kubectl delete pod -n <src-ns> <src-pod>
  ```

  **Risk:** If the underlying cause is istiod overload, the new connection adds to the load. **Duration:** Minutes — applies until the workload's pods are replaced. **Verification:** the replacement pod reports `SYNCED` and `NC`/`NR` clear for that workload.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirmed an Envoy-sourced 503 but the gathered evidence did not match the indicators for Causes A through I.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the mesh owner / SME.

  ```bash
  kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request POST 'logging?http=debug&connection=debug&router=debug'
  istioctl proxy-config all -n <src-ns> <src-pod> > /tmp/istio-proxy-${src-pod}.dump
  kubectl logs -n istio-system deploy/istiod --tail=2000 > /tmp/istiod.log
  kubectl exec -n <src-ns> <src-pod> -c istio-proxy -- pilot-agent request GET config_dump > /tmp/envoy-${src-pod}.json
  ```

  **Risk:** Bumping Envoy log level to `debug` is read-only but verbose; on a busy proxy it can fill stdout buffers within minutes — scope to the affected component (`http`, `connection`, `router`, `rbac`) rather than `all`. **Duration:** Minutes — reset log level to `warning` once the capture is complete. **Verification:** the captured dumps, istiod log, and access-log slice are attached to an incident ticket and a follow-up owner is assigned by the receiving engineer.

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
