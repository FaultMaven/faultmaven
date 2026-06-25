---
id: "envoy-503-no-healthy-upstream"
title: "Envoy returns 503 'no healthy upstream' for a cluster"
domain: networking
service: envoy
symptom_class: [service_unavailable, connection_refused]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [no-healthy-upstream, http-503, response-flag-uh, outlier-detection, circuit-breaking, eds]
difficulty: advanced
---

## Symptom Recognition

- HTTP response body: `no healthy upstream` with status `503 Service Unavailable`.
- Access-log response flag `UH` (no healthy upstream host in the cluster).
- Access-log response flag `UF` (upstream connection failure) or `UO` (upstream overflow / circuit breaking) on the same route.
- Admin `/clusters` shows host `health_flags` containing `/failed_active_hc`, `/failed_eds_health`, or `/failed_outlier_check`.
- Stat `cluster.<name>.membership_healthy` is `0` while `cluster.<name>.membership_total` is `> 0`.
- In Istio meshes the same body/flag appears for sidecar-proxied traffic; admin port is `15000` instead of the default `9901`.

## Applicability

- Envoy 1.14+ (stat and admin paths below are stable across these releases); applies equally to Istio/istio-proxy data-plane sidecars.
- Required access: shell on the Envoy/sidecar pod or host, or network reach to the admin interface (`localhost:9901`, Istio `localhost:15000`).
- Tools: `curl`, `jq`, and (for Istio) `kubectl` / `istioctl`.
- The admin interface is privileged — only reach it over localhost or a trusted network.

## Diagnostic Steps

### Step 1: Confirm the cluster has zero healthy endpoints

```bash
curl -s http://localhost:9901/clusters | grep -E '::(health_flags|hostname|weight)::' | head -40
curl -s "http://localhost:9901/stats?filter=membership" | grep -E 'membership_(healthy|total|degraded)'
```

Expected output: per-host `::health_flags::healthy` for a working cluster; `membership_healthy` equals `membership_total`. A failing cluster shows `membership_healthy: 0` and non-`healthy` `health_flags`.

### Step 2: Read the per-host health-flag reason

```bash
curl -s http://localhost:9901/clusters?format=json \
  | jq -r '.cluster_statuses[] | .name as $n | (.host_statuses // [])[] | "\($n) \(.address.socket_address.address) \(.health_status)"'
```

Expected output: lines such as `<cluster> 10.0.0.5 {"eds_health_status":"HEALTHY"}`; failures show `failed_active_health_check: true`, `failed_outlier_check: true`, or `eds_health_status:"UNHEALTHY"`.

### Step 3: Inspect endpoint discovery (EDS) membership

```bash
curl -s "http://localhost:9901/config_dump?include_eds" \
  | jq '.configs[] | select(.["@type"] | test("EndpointsConfigDump")) | .dynamic_endpoint_configs[]?.endpoint_config.endpoints[]?.lb_endpoints | length'
curl -s "http://localhost:9901/stats?filter=cluster" | grep -E 'update_(success|failure|empty|no_rebuild)'
```

Expected output: a positive endpoint count per locality. `0` (or a missing cluster) plus rising `cluster.<name>.update_empty` / `update_failure` indicates EDS delivered no/empty endpoints.

### Step 4: Inspect outlier-detection ejections

```bash
curl -s "http://localhost:9901/stats?filter=outlier_detection" \
  | grep -E 'ejections_(active|enforced_total|detected_consecutive_5xx)'
```

Expected output: `cluster.<name>.outlier_detection.ejections_active: 0` when nothing is ejected. A value equal to `membership_total` means outlier detection ejected every host.

### Step 5: Inspect connection-pool circuit breakers

```bash
curl -s "http://localhost:9901/stats?filter=cluster" \
  | grep -E 'upstream_(cx_overflow|rq_pending_overflow|rq_active_overflow|cx_pool_overflow)|circuit_breakers'
```

Expected output: overflow counters flat (`0`) and `circuit_breakers.<priority>.cx_open: 0` / `rq_pending_open: 0` / `rq_open: 0` when healthy. Rising overflow counters and `*_open: 1` indicate the breaker is tripped.

### Step 6: Check active health-check outcomes

```bash
curl -s "http://localhost:9901/stats?filter=health_check" \
  | grep -E 'health_check\.(attempt|success|failure|healthy)'
```

Expected output: `health_check.healthy` equals `membership_total` and `health_check.failure` is flat. Rising `health_check.failure` with `health_check.healthy: 0` confirms active health checks are failing.

## Causes

### Cause A: Active health checks fail for every endpoint
**Statement:** The upstream endpoints are reachable on the data path but fail Envoy's configured active health check (wrong HC path/port, app readiness endpoint returning non-2xx, or mTLS on the HC port), so Envoy marks all hosts unhealthy.
**Chain:**
- root: active health check probes return non-success for every endpoint
- s1: each host gets the `/failed_active_hc` health flag and `health_check.healthy` falls to 0
- s2: `membership_healthy` for the cluster reaches 0
- D: load balancer finds no host and returns 503 `no healthy upstream`
**Indicators:**
- root: [Step 6] `health_check.failure` is climbing while `health_check.success` is flat
  <!-- match: {"step": 6, "predicate": "contains", "target": "health_check.failure"} -->
- s1: [Step 2] host `health_status` shows `failed_active_health_check: true`
  <!-- match: {"step": 2, "predicate": "contains", "target": "failed_active_health_check"} -->
- s2: [Step 1] `membership_healthy: 0` with `membership_total` greater than 0
  <!-- match: {"step": 1, "predicate": "contains", "target": "membership_healthy: 0"} -->
- D: [Symptom] response body `no healthy upstream` with flag `UH`
**Interventions:**
- **remediation** (root): correct the health-check definition so it targets the endpoint the app actually serves (right `path`, `port_value`, and `expected_statuses`); for Istio set `appProtocol`/readiness correctly so the probe matches the app.

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' http://<endpoint-ip>:<hc-port><hc-path>
  # then patch the cluster's health_checks (CDS) or Istio DestinationRule to match the URL that returns 200
  ```

  **Verification:** re-run Step 6 — `health_check.success` increments and Step 1 shows `membership_healthy` rising to `membership_total`.
- **mitigation** (s1): temporarily disable active health checking on the cluster so traffic flows to endpoints that are actually serving while the probe is fixed.

  ```bash
  # remove/comment the health_checks block on the cluster (CDS) or set Istio outlier/HC off, then push config
  curl -s http://localhost:9901/clusters | grep -c '::health_flags::healthy'
  ```

  **Risk:** dead endpoints will now receive traffic and surface as 5xx to clients. **Duration:** until the health-check definition is fixed (minutes to hours). **Verification:** Step 1 shows `membership_healthy` greater than 0 and 503s stop.

### Cause B: EDS delivers an empty (or no) endpoint set for the cluster
**Statement:** The control plane (xDS/EDS — e.g. Istiod, or a custom management server) sends the cluster zero endpoints (deleted Service/Endpoints, label-selector mismatch, or a failing EDS subscription), so the load balancer has nothing to choose.
**Chain:**
- root: EDS pushes an empty `ClusterLoadAssignment` (or the subscription never resolves)
- s1: the cluster's endpoint list and `membership_total` drop to 0 and `update_empty` increments
- D: load balancer has no host and returns 503 `no healthy upstream`
**Indicators:**
- root: [Step 3] `lb_endpoints` length is 0 and `cluster.<name>.update_empty` is incrementing
  <!-- match: {"step": 3, "predicate": "contains", "target": "update_empty"} -->
- s1: [Step 1] `membership_total: 0` for the affected cluster
  <!-- match: {"step": 1, "predicate": "contains", "target": "membership_total: 0"} -->
- D: [Symptom] response body `no healthy upstream` with flag `UH`
**Interventions:**
- **remediation** (root): restore the endpoint source so EDS has members to advertise — fix the Kubernetes Service selector / `EndpointSlice`, scale the backing workload above 0, or repair the xDS subscription on the management server.

  ```bash
  kubectl get endpointslices -l kubernetes.io/service-name=<svc> -o wide
  kubectl get pods -l <selector> -o wide   # confirm Ready pods exist and labels match the Service
  ```

  **Verification:** re-run Step 3 — `lb_endpoints` length is positive and Step 1 shows `membership_total` greater than 0.
- **defensive_fix** (s1): add `ignore_health_on_host_removal` plus a small `outlier_detection`/HC grace so a transient empty EDS push does not instantly blackhole, and (Istio) verify `PILOT_ENABLE_*` and proxy `pilot` xDS connectivity.

  ```bash
  curl -s "http://localhost:9901/stats?filter=cluster" | grep -E 'update_(failure|rejected)'
  ```

  **Verification:** Step 3 `update_failure`/`update_rejected` stay flat after a control-plane redeploy and `membership_total` does not drop to 0.

### Cause C: Outlier detection ejected every endpoint
**Statement:** Endpoints returned enough consecutive 5xx / gateway failures that outlier detection ejected all of them at once (aggressive `consecutive_5xx`/`max_ejection_percent`, or an upstream-wide fault), leaving no host eligible.
**Chain:**
- root: each endpoint trips the outlier-detection threshold and is ejected
- s1: `outlier_detection.ejections_active` rises to equal `membership_total` and hosts carry `/failed_outlier_check`
- s2: the cluster's healthy host count reaches 0
- D: load balancer finds no host and returns 503 `no healthy upstream`
**Indicators:**
- root: [Step 4] `outlier_detection.ejections_enforced_total` / `ejections_detected_consecutive_5xx` climbing
  <!-- match: {"step": 4, "predicate": "contains", "target": "ejections_enforced_total"} -->
- s1: [Step 2] host `health_status` shows `failed_outlier_check: true`
  <!-- match: {"step": 2, "predicate": "contains", "target": "failed_outlier_check"} -->
- s2: [Step 1] `membership_healthy: 0` while `membership_total` is unchanged
  <!-- match: {"step": 1, "predicate": "contains", "target": "membership_healthy: 0"} -->
- D: [Symptom] response body `no healthy upstream` with flag `UH`
**Interventions:**
- **remediation** (root): fix the upstream failures driving ejection (the actual 5xx source), then make ejection survivable by capping `max_ejection_percent` below 100 so a panic wave cannot eject the whole cluster.

  ```yaml
  # DestinationRule / outlier_detection
  outlierDetection:
    consecutive5xxErrors: 5
    interval: 10s
    baseEjectionTime: 30s
    maxEjectionPercent: 50
  ```

  **Verification:** re-run Step 4 — `ejections_active` stays below `membership_total`; Step 1 keeps `membership_healthy` greater than 0.
- **mitigation** (s1): temporarily disable outlier detection on the cluster to immediately re-admit ejected hosts while the upstream 5xx cause is fixed.

  ```bash
  # set outlier_detection (DestinationRule) to consecutive5xxErrors: 0 / enforcing 0, push config
  curl -s "http://localhost:9901/stats?filter=outlier_detection" | grep ejections_active
  ```

  **Risk:** flapping/bad endpoints are reused and can re-emit 5xx to clients. **Duration:** until the upstream failure is resolved (minutes). **Verification:** `ejections_active: 0` and Step 1 shows `membership_healthy` greater than 0.

### Cause D: Connection-pool circuit breaker is wedged open
**Statement:** Request volume to the cluster exceeds the configured connection-pool / pending-request limits (`max_connections`, `max_pending_requests`, `max_requests` set too low for load), so the circuit breaker overflows and rejects requests before a host is selected.
**Chain:**
- root: in-flight connections/requests exceed the cluster's circuit-breaker thresholds
- s1: the breaker opens (`cx_open`/`rq_pending_open`/`rq_open` = 1) and overflow counters increment
- D: requests are rejected with 503 and flag `UO` (overflow), surfacing alongside `no healthy upstream`
**Indicators:**
- root: [Step 5] `upstream_rq_pending_overflow` / `upstream_cx_overflow` counters climbing
  <!-- match: {"step": 5, "predicate": "contains", "target": "upstream_rq_pending_overflow"} -->
- s1: [Step 5] a `circuit_breakers.<priority>.*_open` gauge reads `1`
  <!-- match: {"step": 5, "predicate": "contains", "target": "rq_pending_open: 1"} -->
- D: [Symptom] 503 with access-log response flag `UO`
**Interventions:**
- **remediation** (root): raise the circuit-breaker limits to fit real concurrency (and/or scale the upstream) so the pool is not the bottleneck.

  ```yaml
  # DestinationRule / circuit_breakers thresholds
  connectionPool:
    tcp: { maxConnections: 1024 }
    http: { http1MaxPendingRequests: 1024, maxRequestsPerConnection: 0 }
  ```

  **Verification:** re-run Step 5 — overflow counters stop climbing and all `*_open` gauges read `0` under load.
- **mitigation** (s1): shed/queue load at the caller (lower client concurrency or add a retry budget with backoff) to keep in-flight requests under the breaker limit while limits are retuned.

  ```bash
  curl -s "http://localhost:9901/stats?filter=cluster" | grep -E '_open|overflow'
  ```

  **Risk:** capping client concurrency increases caller-side latency/queueing. **Duration:** until breaker limits are raised or the upstream scaled. **Verification:** Step 5 shows `*_open: 0` and overflow counters flat.

### Cause Z: Unidentified
**Statement:** The 503 `no healthy upstream` does not match any cause above (e.g. the cluster is missing entirely from config, listener/route misconfiguration points at a nonexistent cluster, or a data-plane bug).
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full data-plane diagnostic snapshot and escalate to the networking/service-mesh SME.

  ```bash
  ts=$(date +%s)
  for p in clusters "config_dump?include_eds" listeners server_info "stats?filter=cluster"; do
    curl -s "http://localhost:9901/${p}" > "/tmp/envoy_${ts}_$(echo "$p" | tr '/?=&' '_').json"
  done
  # Istio: also grab istioctl proxy-config
  istioctl proxy-config cluster <pod>.<ns> -o json > "/tmp/envoy_${ts}_istio_clusters.json" 2>/dev/null
  ```

  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** snapshot files exist and are attached to the escalation ticket.

## Prevention

- Set `outlier_detection.max_ejection_percent` below 100 (e.g. 50) so a 5xx wave cannot eject an entire cluster.
- Alert on `cluster.<name>.membership_healthy == 0 while membership_total > 0` for any production cluster.
- Alert on rising `cluster.<name>.outlier_detection.ejections_active`, `upstream_rq_pending_overflow`, and `upstream_cx_overflow`.
- Validate health-check definitions in CI: the configured HC path/port must return a 2xx from a real endpoint before rollout.
- Configure a non-zero load-balancer panic threshold so Envoy still serves traffic when only a minority of hosts are healthy.
- Size `connectionPool`/circuit-breaker limits from observed peak concurrency, not defaults; load-test before raising traffic.
- Monitor EDS health via `update_failure`/`update_rejected`/`update_empty` and the proxy's xDS connection to the control plane.

## Sources

- [Admin](https://www.envoyproxy.io/docs/envoy/latest/operations/admin) — admin interface: `/clusters`, `/stats`, `/config_dump`, `/server_info`, `/listeners`, `/ready`, default port 9901, format/filter query params.
- [Cluster stats](https://www.envoyproxy.io/docs/envoy/latest/configuration/upstream/cluster_manager/cluster_stats) — exact stat names: `membership_healthy/total`, `health_check.*`, `outlier_detection.ejections_*`, `upstream_*_overflow`, `circuit_breakers.<priority>.*_open`, `update_empty/failure`.
- [Usage](https://www.envoyproxy.io/docs/envoy/latest/configuration/observability/access_log/usage) — response flags `UH` (no healthy upstream), `UF` (upstream connection failure), `UO` (upstream overflow / circuit breaking).
- [Panic threshold](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/panic_threshold) — "503 - no healthy upstream" behavior when all hosts are unhealthy and panic threshold handling.
- [Outlier](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier) — outlier-detection ejection semantics (`consecutive_5xx`, `max_ejection_percent`).
- [Circuit breaking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking) — connection-pool circuit breaking and overflow behavior.
