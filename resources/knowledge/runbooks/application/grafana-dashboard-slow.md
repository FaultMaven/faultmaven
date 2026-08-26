---
id: "grafana-dashboard-slow"
title: "Grafana Dashboard Loading Slowly"
domain: application
service: grafana
symptom_class: [latency]
severity: medium
scope: "global"
version: "2.0.1"
last_updated: "2026-08-26"
verified_by: "kb-researcher"
status: draft
tags: [grafana, dashboard, observability, query-performance, prometheus, rendering]
difficulty: intermediate
---

## Symptom Recognition

Dashboards take more than 5–10 seconds to render or time out entirely. Individual panels display spinner icons indefinitely or show "No data" while other panels complete. Browser developer tools (Network tab) show XHR requests to `/api/ds/query` exceeding 30 seconds. Grafana server logs contain `context deadline exceeded`, `query timeout`, or `i/o timeout` errors. Template variable dropdowns with "All" selected trigger cascading slow loads. Auto-refresh causes the browser tab to become unresponsive. Panels that previously loaded in under 2 seconds now take 10–30 seconds after the monitored environment grew in cardinality or retention depth.

## Applicability

Applies to Grafana OSS and Grafana Enterprise 9.x–11.x and Grafana Cloud. Requires Grafana admin or editor role to access Query Inspector and dashboard JSON. Direct API or shell access to the underlying data source (Prometheus, InfluxDB, Loki, Elasticsearch, or SQL database) is needed for backend-side steps. For Kubernetes-deployed Grafana, `kubectl` access to the `monitoring` namespace is required.

## Diagnostic Steps

### Step 1:

Open the slow dashboard, click a panel title, and select **Inspect > Query**. Record query execution time and data-point count from the Stats tab.

```bash
# Alternatively via API — replace UID and panel ID
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "http://localhost:3000/api/ds/query" \
  -H "Content-Type: application/json" \
  -d '{"queries":[{"datasource":{"uid":"<DS_UID>"},"expr":"<QUERY>","range":true}],"from":"now-1h","to":"now"}'
```

Expected output: `"executionTime"` field in milliseconds; `frames[].schema.meta.stats` shows `"Rows"` count. Values under 1000 ms and under 10 000 rows indicate a healthy panel query.

### Step 2:

Time a representative slow query directly against the data source to isolate whether slowness is in Grafana or the backend.

```bash
# Prometheus
time curl -s \
  'http://prometheus:9090/api/v1/query_range?query=sum(rate(http_requests_total[5m]))by(service)&start=now-1h&end=now&step=60s' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['result']), 'series')"
```

Expected output: Response under 500 ms with fewer than 500 result series. Response over 2 seconds confirms a data-source bottleneck independent of Grafana.

### Step 3:

Measure Grafana server resource utilization.

```bash
# Bare-metal or VM
ps aux | grep grafana-server | grep -v grep | awk '{print "CPU:",$3,"% MEM:",$4,"%"}'
curl -s http://localhost:3000/metrics \
  | grep -E 'grafana_datasource_request_duration_seconds_bucket|grafana_api_response_status_total'

# Kubernetes
kubectl top pod -l app.kubernetes.io/name=grafana -n monitoring
```

Expected output: CPU below 80 % and memory within configured limits. `grafana_datasource_request_duration_seconds` p99 bucket label `le="2"` should carry the majority of requests.

### Step 4:

Count panels, total queries, and template variable cardinality via the dashboard API.

```bash
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "http://localhost:3000/api/dashboards/uid/<DASHBOARD_UID>" \
  | jq '{
      panel_count: (.dashboard.panels | length),
      total_queries: ([.dashboard.panels[].targets // [] | length] | add // 0),
      multi_all_vars: [.dashboard.templating.list[]
        | select(.multi == true and .includeAll == true)
        | {name, query: .query}]
    }'
```

Expected output: `panel_count` under 25, `total_queries` under 30, `multi_all_vars` empty or each resolving to fewer than 50 values.

### Step 5:

Identify expensive queries via the Prometheus query log (requires `query_log_file` enabled).

```bash
# Enable in prometheus.yml if absent:
# global:
#   query_log_file: /var/log/prometheus/query.log

cat /var/log/prometheus/query.log \
  | jq -r 'select(.stats.timings.evalTotalTime > 2)
      | "\(.stats.timings.evalTotalTime | tostring | .[0:5])s  \(.query.query)"' \
  | sort -rn | head -20
```

Expected output: Ranked list of queries over 2 seconds. PromQL with `{__name__=~".*"}`, high-cardinality label matchers, or very wide range windows (`[24h]`) appear most frequently.

### Step 6:

Check Grafana data-proxy and connection-pool configuration.

```bash
grep -E 'max_conns_per_host|max_idle_conn|idle_conn_timeout|timeout|concurrent_render' \
  /etc/grafana/grafana.ini 2>/dev/null \
  || kubectl exec -n monitoring deploy/grafana -- \
     grep -E 'max_conns_per_host|max_idle_conn|timeout|concurrent_render' \
     /etc/grafana/grafana.ini
```

Expected output: `[dataproxy]` section visible. Default values: `max_conns_per_host = 0` (unlimited), `timeout = 30`, `concurrent_render_request_limit = 0`.

### Step 7:

Check whether Grafana's backend database is SQLite and whether it is under write contention.

```bash
grep -E '^type\s*=|^path\s*=|^host\s*=|^name\s*=' /etc/grafana/grafana.ini \
  | grep -A4 '\[database\]' || \
  sqlite3 /var/lib/grafana/grafana.db "PRAGMA journal_mode; PRAGMA wal_checkpoint;"
```

Expected output: `type = sqlite3` indicates the embedded database is in use. Under concurrent users, SQLite serialises writes and creates queueing delays for dashboard-save and session operations.

## Causes

### Cause A: Data Source Query Returns Excessive Series or Data Points

**Statement:** A PromQL or other data-source query returns hundreds of series or millions of data points, saturating network transfer and browser rendering capacity.

**Chain:**
- root: a high-cardinality query forces a full scan of TSDB head blocks or index structures and returns hundreds of series or millions of data points
- s1: Grafana streams the full result to the browser, allocating each data-point tuple in the JavaScript heap
- s2: result point count exceeds the browser rendering threshold (~50 000 points per panel), blocking the main thread during chart re-draw
- D: the browser tab freezes and the panel renders slowly or times out (see Symptom Recognition)

**Indicators:**
- root: [Step 2] direct data-source timing for the panel query exceeds 2 seconds
- s2: [Step 1] Query Inspector Stats tab shows `"Rows"` exceeding 10 000 or execution time exceeding 2 000 ms

**Interventions:**
- **remediation** (root): create a Prometheus recording rule for the expensive aggregation so per-query CPU drops on every scrape. The recording rule is cluster-wide; a bad expr produces zero data until corrected (roll back by removing the rule block, reloading Prometheus, and restoring the original panel query).

  ```bash
  # Create a Prometheus recording rule for the expensive aggregation
  cat >> /etc/prometheus/rules/dashboard-opt.yml <<'EOF'
  groups:
    - name: dashboard-opt
      interval: 60s
      rules:
        - record: http_requests:rate5m_by_service
          expr: sum by (service) (rate(http_requests_total{env="production"}[5m]))
  EOF
  curl -X POST http://prometheus:9090/-/reload
  # Update dashboard panel query to reference recording-rule metric name
  ```

  **Verification:** Query Inspector execution time drops below 1 000 ms and `"Rows"` count drops below 5 000; the browser tab no longer freezes during panel render.
- **mitigation** (s2): narrow the query with a label selector and cap returned points to keep the result under the browser rendering threshold.

  ```bash
  # Add label selector to restrict cardinality — example for Prometheus
  # Change: sum(rate(http_requests_total[5m])) by (service)
  # To:     sum(rate(http_requests_total{env="production"}[5m])) by (service)
  # Also set Max data points = 1000 in panel Query Options via UI
  ```

  **Risk:** Narrowing the query reduces historical visibility; ensure oncall engineers are aware of changed panel scope. **Duration:** Immediate; revert by removing the label filter. **Verification:** Query Inspector `"Rows"` count drops and the panel renders without the tab freezing.

---

### Cause B: Template Variable Resolves to High-Cardinality "All" Selection

**Statement:** A multi-value template variable with `includeAll = true` expands to hundreds of values, multiplying the query count by the number of variable values on each dashboard load.

**Chain:**
- root: a multi-value template variable with `includeAll = true` resolves to N (hundreds of) values when "All" is selected
- s1: a repeated row or panel using that variable fires N independent queries per panel, generating thousands of concurrent data-source requests
- s2: the burst exhausts the query worker pool, stalling all other user requests
- D: the dashboard renders slowly or times out (see Symptom Recognition)

**Indicators:**
- root: [Step 4] `multi_all_vars` array contains a variable whose resolved value count exceeds 50
- s1: [Step 1] panel load time scales linearly with the number of selected variable values

**Interventions:**
- **remediation** (root): replace the high-cardinality multi-select variable with a recording rule that pre-aggregates across the dimension, removing the need to select individual values. Users lose per-value drill-down; provide a linked detail dashboard for per-service investigation (roll back by re-importing the original dashboard JSON from `Dashboard Settings > Versions`).

  ```bash
  # Define a recording rule that pre-aggregates across the variable dimension,
  # then point the panel at the pre-aggregated metric instead of the variable.
  # See Cause A for recording-rule syntax and `curl -X POST .../-/reload`.
  ```

  **Verification:** Dashboard load time is constant regardless of how many variable values are logically available; Step 4 `multi_all_vars` shows resolved count under 20.
- **mitigation** (root): disable `includeAll` and pin the default selection to a specific subset.

  ```bash
  # Via dashboard JSON: set includeAll=false and set current.value to a specific subset
  curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
    "http://localhost:3000/api/dashboards/uid/<UID>" \
    | jq '.dashboard.templating.list[] |= if .name == "<VAR_NAME>" then .includeAll = false | .current.value = ["svc-a","svc-b"] else . end' \
    > /tmp/patched.json
  # Then POST the patched dashboard JSON back via /api/dashboards/db
  ```

  **Risk:** Restricting the default selection requires users to manually choose the full set; communicate the change in a team channel. **Duration:** Immediate; revert by re-enabling `includeAll` in dashboard JSON. **Verification:** Step 4 `multi_all_vars` shows resolved count under 20 and dashboard load time is constant.

---

### Cause C: Dashboard Has Too Many Panels Firing Concurrent Queries

**Statement:** A dashboard with 30 or more panels generates a burst of concurrent data-source requests that saturates both the Grafana query worker pool and the data source's connection limit.

**Chain:**
- root: a dashboard with 30 or more panels fires all visible panel queries in parallel on load, each occupying a connection slot on the Grafana and data-source sides
- s1: the burst exceeds the data source's `max_connections` or Prometheus's `--query.max-concurrency`, so queries queue waiting for a free slot
- D: panels load sequentially despite intended parallelism and the dashboard renders slowly (see Symptom Recognition)

**Indicators:**
- root: [Step 4] `panel_count` exceeds 25 or `total_queries` exceeds 30
- s1: [Step 3] `grafana_datasource_request_duration_seconds` p99 is high while direct data-source queries are fast (Step 2)

**Interventions:**
- **remediation** (root): export the dashboard and split it into two or more focused dashboards of 10–15 panels each, then add Grafana dashboard links (`Dashboard Settings > Links`) for navigation. Users must navigate between dashboards for full coverage; consider a summary "overview" dashboard with links to detail views (roll back by re-importing the original dashboard JSON from version history).

  ```bash
  # Export the dashboard, split panels into focused dashboards of 10-15 panels,
  # and re-import each via /api/dashboards/db; add cross-links under Settings > Links.
  ```

  **Verification:** Each resulting dashboard loads in under 3 seconds; Step 4 shows `panel_count` below 20 per dashboard.
- **mitigation** (s1): collapse non-critical rows so Grafana only queries panels in expanded (visible) rows, cutting the concurrent burst.

  ```bash
  # Collapse secondary metric rows using Grafana row grouping
  # In dashboard JSON, set row.collapsed=true for non-critical sections
  # Grafana only queries panels in expanded (visible) rows
  curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
    "http://localhost:3000/api/dashboards/uid/<UID>" \
    | jq '.dashboard.panels[] |= if .type == "row" and .title != "Key Metrics" then .collapsed = true else . end' \
    > /tmp/collapsed.json
  ```

  **Risk:** Splitting/collapsing changes the at-a-glance view; use Grafana dashboard links to preserve navigation. **Duration:** Immediate; rows can be expanded on demand without triggering a full reload. **Verification:** Step 3 `grafana_datasource_request_duration_seconds` p99 drops and panels load without queueing.

---

### Cause D: Grafana Data-Proxy Timeout Too Short for Legitimate Queries

**Statement:** The Grafana data-proxy `timeout` is shorter than the time a legitimately slow data-source query needs to complete, causing premature cancellation and "No data" or timeout errors in panels.

**Chain:**
- root: the Grafana data-proxy `timeout` (default 30 s) is set shorter than the time an inherently slow data-source query (e.g. a 7-day Loki aggregation or a complex SQL join) needs to evaluate
- s1: the data-proxy context deadline fires before the query completes, cancelling the outbound request while the data source keeps processing the abandoned query
- D: the panel returns an empty or `context deadline exceeded` error and renders as "No data" or a timeout (see Symptom Recognition)

**Indicators:**
- root: [Step 1] Query Inspector shows a request error or `"context deadline exceeded"` in the response rather than a data result
- s1: [Symptom] Grafana logs contain `context deadline exceeded` or `request canceled` paired with the `/api/ds/query` path

**Interventions:**
- **remediation** (root): raise the data-proxy `timeout` past the legitimate query duration so the request is no longer cancelled, and simultaneously optimize the query via a recording rule (see Cause A) to bring response time back below the original 30-second limit.

  ```bash
  # /etc/grafana/grafana.ini
  # [dataproxy]
  # timeout = 90
  # dial_timeout = 10
  # keep_alive_seconds = 30
  sudo sed -i 's/^;timeout = .*/timeout = 90/' /etc/grafana/grafana.ini \
    || echo "timeout = 90" | sudo tee -a /etc/grafana/grafana.ini
  sudo systemctl restart grafana-server
  ```

  **Verification:** Step 1 Query Inspector no longer returns `context deadline exceeded` and panels complete within the new timeout.
- **mitigation** (s1): apply the same `timeout` bump as a stopgap until the underlying slow query is optimized or a recording rule is in place.

  ```bash
  # Temporarily widen the data-proxy deadline (revert once the query is optimized)
  sudo sed -i 's/^;timeout = .*/timeout = 90/' /etc/grafana/grafana.ini \
    || echo "timeout = 90" | sudo tee -a /etc/grafana/grafana.ini
  sudo systemctl restart grafana-server
  ```

  **Risk:** Increasing the timeout lets slow queries consume Grafana worker threads longer; monitor concurrent-request depth. **Duration:** Temporary — apply until the underlying slow query is optimized or a recording rule is in place. **Verification:** Step 1 Query Inspector no longer returns `context deadline exceeded` and panels complete within the new timeout.

---

### Cause E: Grafana Server Under-Resourced (CPU or Memory Saturation)

**Statement:** The Grafana server process is CPU- or memory-saturated, causing query response-processing and result-serialization to queue behind other concurrent requests.

**Chain:**
- root: the Grafana server process is CPU- or memory-saturated (undersized pod under concurrent user load)
- s1: in-process post-query work (JSON deserialization, transformation pipeline, server-side rendering) and GC pauses stall in-flight request goroutines
- D: panel response-processing and serialization queue, so panels render slowly even when data sources respond quickly (see Symptom Recognition)

**Indicators:**
- root: [Step 3] `kubectl top pod` shows CPU throttling (`cpu` at or above request limit) or memory near the resource limit
- s1: [Step 2] direct data-source queries are fast but Step 1 panel load times are slow

**Interventions:**
- **remediation** (root): scale Grafana horizontally (multiple replicas behind a load balancer) after migrating the backend database from SQLite to PostgreSQL or MySQL, configuring `GF_DATABASE_TYPE`, `GF_DATABASE_HOST`, and `GF_DATABASE_NAME`. Horizontal scaling requires shared (database-backed) session storage; test login flows after migration (roll back by scaling to 1 replica and, if DB migration occurred, restoring from the pre-migration backup).

  ```bash
  # After migrating the backend DB to PostgreSQL/MySQL, scale replicas
  kubectl scale deployment/grafana -n monitoring --replicas=3
  # Confirm the backend is NOT sqlite before scaling (SQLite has no multi-writer):
  kubectl exec -n monitoring deploy/grafana -- \
    grep -A5 '\[database\]' /etc/grafana/grafana.ini | grep type
  ```

  **Verification:** Step 3 `kubectl top pod` shows CPU below 70 % during peak load; Step 1 panel query times drop proportionally with reduced server-side processing load.
- **mitigation** (root): raise CPU/memory limits (and optionally replicas) to relieve saturation on the existing deployment.

  ```bash
  # Kubernetes: increase CPU/memory limits and optionally scale replicas
  kubectl set resources deployment/grafana -n monitoring \
    --requests='cpu=500m,memory=512Mi' \
    --limits='cpu=2000m,memory=2Gi'
  # Verify database is PostgreSQL before scaling replicas:
  kubectl exec -n monitoring deploy/grafana -- \
    grep -A5 '\[database\]' /etc/grafana/grafana.ini | grep type
  ```

  **Risk:** Increasing replicas requires a shared PostgreSQL/MySQL backend; SQLite does not support multiple writers. **Duration:** Immediate after pod restart; monitor for 15 minutes to confirm CPU drops below 70 %. **Verification:** Step 3 `kubectl top pod` shows CPU below 70 % during peak load.

---

### Cause F: Grafana Backend Database Is SQLite Under Concurrent Load

**Statement:** Grafana's embedded SQLite database serialises writes and creates lock contention when multiple users access dashboards simultaneously.

**Chain:**
- root: Grafana uses the embedded SQLite backend, which holds a single file-level write lock
- s1: concurrent dashboard saves, annotation writes, or session refreshes queue behind the single active writer
- s2: the Grafana UI API (sharing the Gorilla mux thread pool with panel data requests) returns slowly for all users
- D: panels and dashboards render slowly, correlated with concurrent user count (see Symptom Recognition)

**Indicators:**
- root: [Step 7] `grep type` in the `[database]` section returns `sqlite3`
- s1: [Symptom] slowness correlates with the number of concurrent active Grafana users rather than with data-source query complexity

**Interventions:**
- **remediation** (root): migrate the backend from SQLite to PostgreSQL (back up first; Grafana auto-runs schema migrations on first startup against the new DB). Requires Grafana downtime; all dashboard versions, users, and annotations migrate via Grafana's built-in tooling (roll back by restoring the SQLite dump and setting `GF_DATABASE_TYPE=sqlite3`).

  ```bash
  # Migrate to PostgreSQL — backup first
  sqlite3 /var/lib/grafana/grafana.db .dump > /tmp/grafana-backup-$(date +%F).sql
  # Configure GF_DATABASE_TYPE=postgres, GF_DATABASE_HOST, GF_DATABASE_USER,
  # GF_DATABASE_PASSWORD, GF_DATABASE_NAME in Grafana's environment
  # Grafana auto-runs schema migrations on first startup against the new DB
  ```

  **Verification:** Step 7 `grep type` returns `postgres` or `mysql`; dashboard API response times remain stable as concurrent user count scales (`sqlite3 PRAGMA wal_checkpoint` is no longer applicable).
- **mitigation** (s1): enable WAL journal mode for short-term read-concurrency relief while the PostgreSQL migration is prepared.

  ```bash
  # Enable WAL mode for short-term relief (read concurrency improvement)
  sudo systemctl stop grafana-server
  sqlite3 /var/lib/grafana/grafana.db "PRAGMA journal_mode=WAL;"
  sudo systemctl start grafana-server
  ```

  **Risk:** None for read-only WAL enablement; a full migration risks data loss if a backup is not taken first. **Duration:** WAL mode persists across restarts; provides relief until the PostgreSQL migration is complete. **Verification:** Dashboard API response times improve under concurrent reads while `grep type` still returns `sqlite3`.

---

### Cause G: Grafana Data-Proxy Connection Pool Limit Set Too Low

**Statement:** The Grafana data-proxy connection pool limit (`max_conns_per_host`) is set too low, causing panel queries to queue rather than execute in parallel.

**Chain:**
- root: `[dataproxy] max_conns_per_host` is set to a low non-zero value (e.g. 5)
- s1: when a dashboard fires more panel queries than the limit (e.g. 30 against a limit of 5), the excess queries wait for a free connection slot
- D: panels load sequentially as queue latency, mimicking slow queries (see Symptom Recognition)

**Indicators:**
- root: [Step 6] `max_conns_per_host` is set to a non-zero value less than 20
- s1: [Step 4] `total_queries` exceeds the configured `max_conns_per_host` value

**Interventions:**
- **defensive_fix** (root): raise `max_conns_per_host` (and the idle-pool settings) so the data-proxy can fan out panel queries in parallel.

  ```bash
  sudo tee -a /etc/grafana/grafana.ini <<'EOF'
  [dataproxy]
  max_conns_per_host = 25
  max_idle_conns = 25
  idle_conn_timeout_seconds = 90
  EOF
  sudo systemctl restart grafana-server
  ```

  **Verification:** Panels that previously loaded sequentially now load in parallel; the browser Network tab shows `/api/ds/query` requests overlapping rather than starting one after another, and Step 3 Grafana metrics show reduced p99 latency.
- **mitigation** (s1): apply the same connection-limit bump as an immediate stopgap, monitoring data-source load after the change.

  ```bash
  # Quick relief: widen the data-proxy pool, then watch data-source CPU
  sudo tee -a /etc/grafana/grafana.ini <<'EOF'
  [dataproxy]
  max_conns_per_host = 25
  max_idle_conns = 25
  idle_conn_timeout_seconds = 90
  EOF
  sudo systemctl restart grafana-server
  ```

  **Risk:** Increasing the connection limit may overload the data source if it cannot handle additional concurrent connections; monitor data-source CPU after the change. **Duration:** Immediate after restart. **Verification:** Step 4 `total_queries` is now within the new `max_conns_per_host` and panels load in parallel.

---

### Cause Z: Unidentified

**Statement:** Dashboard load latency cannot be attributed to a specific diagnosable cause from the steps above.

**Indicators:**
- [Default] None of the above Causes match the observed diagnostic findings

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (Grafana server logs for the slow period, the slow-panel Query Inspector JSON export, and a browser HAR file) and escalate to the SME. Escalate to Grafana support (Enterprise) or open a GitHub issue with the collected HAR file, Grafana server logs, and Query Inspector JSON export.

  ```bash
  # Collect Grafana server logs covering the slow period
  journalctl -u grafana-server --since "1 hour ago" > /tmp/grafana-recent.log
  # Export slow panel query via Query Inspector JSON (click Download button in UI)
  # Capture browser HAR file: DevTools > Network > Export HAR
  grep -E 'error|warn|timeout|deadline' /tmp/grafana-recent.log | tail -50
  ```

  **Risk:** Escalating without a clear cause may delay resolution; collect all diagnostic artifacts first. **Duration:** Diagnostic only; no configuration change applied. **Verification:** Escalation initiated with full diagnostic artifacts (HAR file, server logs, Query Inspector JSON) attached.

## Prevention

- Set a dashboard performance budget of 3 seconds maximum load time; review dashboards quarterly using Query Inspector.
- Use Prometheus recording rules by default for any aggregation queried across more than 1 000 time series or referenced by more than one dashboard.
- Keep dashboards under 20 panels; use collapsed row grouping for secondary metrics. Grafana queries only panels in expanded rows.
- Set `Max data points = 1000` and `Min interval` (matching the scrape interval, e.g., `15s`) on every panel to prevent unbounded data-point returns.
- Set auto-refresh intervals proportional to time range: 10 s for a 5-minute window, 1 minute for a 1-hour window, 5 minutes or more for a 24-hour-plus window. Never use 5-second refresh on panels with expensive queries.
- Disable `includeAll` on high-cardinality template variables; default to a specific selection of 10–20 values and provide a linked detail dashboard for full enumeration.
- Monitor Grafana's own metrics: alert when `grafana_datasource_request_duration_seconds` p95 exceeds 3 seconds or `grafana_api_response_status_total{code="5xx"}` is non-zero.
- Use PostgreSQL or MySQL as the Grafana backend database in any deployment with more than two concurrent editors.
- Enable Grafana query result caching (Enterprise feature) or place a caching reverse proxy (e.g., Nginx proxy_cache) in front of Prometheus for dashboards with identical repeated queries from multiple users.

## Sources

- [Grafana — Troubleshooting](https://grafana.com/docs/grafana/latest/troubleshooting/) — Official dashboard performance troubleshooting overview; priority 1
- [Grafana — Troubleshoot Queries](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/query-transform-data/troubleshoot-queries/) — Query Inspector usage, data-point limits, and transformation debugging; priority 1
- [Grafana — Configure Grafana](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/) — `[dataproxy]`, `[rendering]`, and `[database]` section reference with default values; priority 1
- [Prometheus — Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Pre-aggregation configuration for reducing query-time computation; priority 1
- [Prometheus — Query Logging](https://prometheus.io/docs/guides/query-log/) — Identifying expensive queries at the data-source level via `query_log_file`; priority 1
- [Grafana Labs Blog — Tips for Optimizing Query Performance](https://grafana.com/blog/grafana-dashboards-tips-for-optimizing-query-performance/) — PromQL optimization, recording rules, shared query results, and panel consolidation; priority 2
