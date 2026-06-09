---
id: "grafana-dashboard-slow"
title: "Grafana Dashboard Loading Slowly"
domain: application
service: grafana
symptom_class: [latency]
severity: medium
scope: "global"
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: "draft"
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

**Mechanism:** Grafana streams the full query result from the data source to the browser; each data-point tuple is allocated in JavaScript heap. When result cardinality or point count exceeds browser rendering thresholds (roughly 50 000 points per panel), the main thread blocks during chart re-draw, causing the tab to freeze. The data source itself may also be slow because high-cardinality queries force a full scan of TSDB head blocks or index structures.

**Indicator:**

- [Step 1] Query Inspector Stats tab shows `"Rows"` exceeding 10 000 or execution time exceeding 2 000 ms
- [Step 2] Direct data-source timing exceeds 2 seconds

<!-- match: {"step": 1, "predicate": "threshold", "target": "executionTime_ms", "op": ">", "value": 2000} -->
<!-- match: {"step": 1, "predicate": "threshold", "target": "row_count", "op": ">", "value": 10000} -->

**Mitigation:**

- **Risk:** Narrowing the query reduces historical visibility; ensure oncall engineers are aware of changed panel scope.
- **Command:**

  ```bash
  # Add label selector to restrict cardinality — example for Prometheus
  # Change: sum(rate(http_requests_total[5m])) by (service)
  # To:     sum(rate(http_requests_total{env="production"}[5m])) by (service)
  # Also set Max data points = 1000 in panel Query Options via UI
  ```

- **Duration:** Immediate; revert by removing the label filter.

**Resolution:**

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

- **Impact:** Recording rules reduce per-query CPU on every scrape. Config is cluster-wide; a bad expr will produce zero data until corrected.

- **Rollback:** Remove the rule block and reload Prometheus; update panel query back to original expression.

**Verification:** Query Inspector execution time drops below 1 000 ms; `"Rows"` count drops below 5 000. Browser tab no longer freezes during panel render.

---

### Cause B: Template Variable Resolves to High-Cardinality "All" Selection

**Statement:** A multi-value template variable with `includeAll = true` expands to hundreds of values, multiplying the query count by the number of variable values on each dashboard load.

**Mechanism:** Grafana evaluates template variables before firing panel queries. When "All" is selected on a variable that resolves to N values and a repeated row or panel uses that variable, Grafana fires N independent queries per panel. A dashboard with 10 panels and a variable resolving to 200 services generates 2 000 concurrent data-source requests, exhausting the query worker pool and stalling all other user requests.

**Indicator:**

- [Step 4] `multi_all_vars` array contains variables where resolved value count exceeds 50
- [Step 1] Panel load time scales linearly with the number of selected variable values

<!-- match: {"step": 4, "predicate": "threshold", "target": "multi_all_var_count", "op": ">", "value": 50} -->

**Mitigation:**

- **Risk:** Restricting the default selection requires users to manually choose the full set; communicate the change in a team channel.
- **Command:**

  ```bash
  # Via dashboard JSON: set includeAll=false and set current.value to a specific subset
  curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
    "http://localhost:3000/api/dashboards/uid/<UID>" \
    | jq '.dashboard.templating.list[] |= if .name == "<VAR_NAME>" then .includeAll = false | .current.value = ["svc-a","svc-b"] else . end' \
    > /tmp/patched.json
  # Then POST the patched dashboard JSON back via /api/dashboards/db
  ```

- **Duration:** Immediate; revert by re-enabling `includeAll` in dashboard JSON.

**Resolution:** Replace high-cardinality multi-select variable with a recording rule that pre-aggregates across the dimension, removing the need to select individual values.

- **Impact:** All users of the dashboard lose the per-value drill-down; provide a linked detail dashboard for per-service investigation.

- **Rollback:** Re-import the original dashboard JSON from version history (`Dashboard Settings > Versions`).

**Verification:** Dashboard load time is constant regardless of how many variable values are logically available. Step 4 `multi_all_vars` shows resolved count under 20.

---

### Cause C: Dashboard Has Too Many Panels Firing Concurrent Queries

**Statement:** A dashboard with 30 or more panels generates a burst of concurrent data-source requests that saturates both the Grafana query worker pool and the data source's connection limit.

**Mechanism:** Grafana fires all visible panel queries in parallel on dashboard load. Each query occupies a connection slot on both the Grafana side (HTTP keep-alive pool) and the data source side. When the burst exceeds the data source's `max_connections` or Prometheus's `--query.max-concurrency`, queries queue and panels wait for a free slot, producing sequential-looking load times even though Grafana intended parallelism.

**Indicator:**

- [Step 4] `panel_count` exceeds 25 or `total_queries` exceeds 30
- [Step 3] `grafana_datasource_request_duration_seconds` p99 is high while data-source direct queries are fast (Step 2)

<!-- match: {"step": 4, "predicate": "threshold", "target": "panel_count", "op": ">", "value": 25} -->

**Mitigation:**

- **Risk:** Splitting dashboards breaks existing bookmarks; use Grafana dashboard links to preserve navigation.
- **Command:**

  ```bash
  # Collapse secondary metric rows using Grafana row grouping
  # In dashboard JSON, set row.collapsed=true for non-critical sections
  # Grafana only queries panels in expanded (visible) rows
  curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
    "http://localhost:3000/api/dashboards/uid/<UID>" \
    | jq '.dashboard.panels[] |= if .type == "row" and .title != "Key Metrics" then .collapsed = true else . end' \
    > /tmp/collapsed.json
  ```

- **Duration:** Immediate; rows can be expanded on demand without triggering a full reload.

**Resolution:** Export the dashboard and split into two or more focused dashboards of 10–15 panels each. Add Grafana dashboard links (`Dashboard Settings > Links`) to enable navigation between related views.

- **Impact:** Users must navigate between dashboards for full coverage; consider a summary "overview" dashboard with links to detail views.

- **Rollback:** Re-import the original dashboard JSON from the Grafana version history.

**Verification:** Each resulting dashboard loads in under 3 seconds. Step 4 shows `panel_count` below 20 per dashboard.

---

### Cause D: Grafana Data-Proxy Timeout Too Short for Legitimate Queries

**Statement:** The Grafana data-proxy `timeout` setting is shorter than the time required by the data source to evaluate a legitimate query, causing premature cancellation and "No data" or timeout errors in panels.

**Mechanism:** Grafana's data-proxy layer wraps each outbound data-source HTTP request in a context with a configurable deadline (default 30 seconds). When a data-source query is inherently slow (e.g., a Loki log aggregation over 7 days or a complex SQL join), the context deadline fires before the query completes. Grafana returns an empty or error result to the panel while the data source continues processing the abandoned query, wasting resources.

**Indicator:**

- [Step 1] Query Inspector shows a request error or `"context deadline exceeded"` in the response rather than a data result
- [Symptom] Grafana logs contain `context deadline exceeded` or `request canceled` paired with `/api/ds/query` path

<!-- match: {"step": 1, "predicate": "contains", "target": "context deadline exceeded"} -->

**Mitigation:**

- **Risk:** Increasing timeout allows slow queries to consume Grafana worker threads longer; monitor concurrent-request depth.
- **Command:**

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

- **Duration:** Temporary — apply until the underlying slow query is optimized or a recording rule is in place.

**Resolution:** Same as Mitigation.

**Verification:** Step 1 Query Inspector no longer returns `context deadline exceeded`. Panels complete within the new timeout. Simultaneously optimize the query via recording rules (see Cause A) to reduce response time below the original 30-second limit.

---

### Cause E: Grafana Server Under-Resourced (CPU or Memory Saturation)

**Statement:** The Grafana server process is CPU- or memory-saturated, causing query response-processing and result-serialization to queue behind other concurrent requests.

**Mechanism:** Grafana performs post-query processing in-process: JSON deserialization of data-source responses, transformation pipeline execution (join, filter, calculate), and server-side rendering for PNG exports. Under concurrent user load, a single undersized Grafana pod becomes the bottleneck even when data sources respond quickly. Memory pressure causes GC pauses that stall all in-flight request goroutines.

**Indicator:**

- [Step 3] `kubectl top pod` shows CPU throttling (`cpu` at or above request limit) or memory near the resource limit
- [Step 2] Direct data-source queries are fast but Step 1 panel load times are slow

<!-- match: {"step": 3, "predicate": "threshold", "target": "cpu_pct", "op": ">", "value": 80} -->

**Mitigation:**

- **Risk:** Increasing replicas requires a shared PostgreSQL/MySQL database backend; SQLite does not support multiple writers.
- **Command:**

  ```bash
  # Kubernetes: increase CPU/memory limits and optionally scale replicas
  kubectl set resources deployment/grafana -n monitoring \
    --requests='cpu=500m,memory=512Mi' \
    --limits='cpu=2000m,memory=2Gi'
  # Verify database is PostgreSQL before scaling replicas:
  kubectl exec -n monitoring deploy/grafana -- \
    grep -A5 '\[database\]' /etc/grafana/grafana.ini | grep type
  ```

- **Duration:** Immediate after pod restart; monitor for 15 minutes to confirm CPU drops below 70 %.

**Resolution:** Scale Grafana horizontally (multiple replicas behind a load balancer) after migrating the backend database from SQLite to PostgreSQL or MySQL. Configure `GF_DATABASE_TYPE`, `GF_DATABASE_HOST`, and `GF_DATABASE_NAME` environment variables.

- **Impact:** Horizontal scaling requires shared session storage; sessions are database-backed by default. Test login flows after migration.

- **Rollback:** Scale deployment back to 1 replica; if DB migration occurred, restore from backup taken before migration.

**Verification:** Step 3 `kubectl top pod` shows CPU below 70 % during peak load. Step 1 panel query times drop proportionally with reduced server-side processing load.

---

### Cause F: Grafana Backend Database Is SQLite Under Concurrent Load

**Statement:** Grafana's embedded SQLite database serialises writes and creates lock contention when multiple users access dashboards simultaneously.

**Mechanism:** SQLite uses a file-level write lock. When multiple Grafana users trigger concurrent dashboard saves, annotation writes, or session refreshes, write operations queue behind the single active writer. On read-heavy workloads, WAL mode mitigates this, but heavy annotation or alert-state write bursts still cause the Grafana UI API to return slowly for all users, including panel data requests that share the same Gorilla mux thread pool.

**Indicator:**

- [Step 7] `grep type` in `[database]` section returns `sqlite3`
- [Symptom] Slowness is correlated with number of concurrent active Grafana users rather than with data-source query complexity

<!-- match: {"step": 7, "predicate": "contains", "target": "sqlite3"} -->

**Mitigation:**

- **Risk:** None for read-only WAL enablement; full migration risks data loss if backup is not taken first.
- **Command:**

  ```bash
  # Enable WAL mode for short-term relief (read concurrency improvement)
  sudo systemctl stop grafana-server
  sqlite3 /var/lib/grafana/grafana.db "PRAGMA journal_mode=WAL;"
  sudo systemctl start grafana-server
  ```

- **Duration:** WAL mode persists across restarts; provides relief until PostgreSQL migration is complete.

**Resolution:**

```bash
# Migrate to PostgreSQL — backup first
sqlite3 /var/lib/grafana/grafana.db .dump > /tmp/grafana-backup-$(date +%F).sql
# Configure GF_DATABASE_TYPE=postgres, GF_DATABASE_HOST, GF_DATABASE_USER,
# GF_DATABASE_PASSWORD, GF_DATABASE_NAME in Grafana's environment
# Grafana auto-runs schema migrations on first startup against the new DB
```

- **Impact:** Requires Grafana downtime during migration. All dashboard versions, users, and annotations are migrated via Grafana's built-in migration tooling.

- **Rollback:** Restore from SQLite dump and set `GF_DATABASE_TYPE=sqlite3`.

**Verification:** Step 7 `grep type` returns `postgres` or `mysql`. Dashboard API response times remain stable as concurrent user count scales. `sqlite3 PRAGMA wal_checkpoint` is no longer applicable.

---

### Cause G: Grafana Query Worker Pool Exhausted by Concurrent Panel Requests

**Statement:** The Grafana data-proxy connection pool limit is set too low, causing panel queries to queue rather than execute in parallel.

**Mechanism:** Grafana's `[dataproxy]` section controls `max_conns_per_host` (connections to a single data source host) and `max_idle_conns` (idle connection pool size). If `max_conns_per_host` is set to a low value (e.g., 5) and a dashboard fires 30 panel queries concurrently, 25 queries wait for a free connection slot. The result is sequential panel loading that mimics slow queries but is actually queue latency.

**Indicator:**

- [Step 6] `max_conns_per_host` is set to a non-zero value less than 20
- [Step 4] `total_queries` exceeds `max_conns_per_host` value

<!-- match: {"step": 6, "predicate": "contains", "target": "max_conns_per_host"} -->

**Mitigation:**

- **Risk:** Increasing connection limit may overload the data source if it cannot handle additional concurrent connections; monitor data-source CPU after change.
- **Command:**

  ```bash
  sudo tee -a /etc/grafana/grafana.ini <<'EOF'
  [dataproxy]
  max_conns_per_host = 25
  max_idle_conns = 25
  idle_conn_timeout_seconds = 90
  EOF
  sudo systemctl restart grafana-server
  ```

- **Duration:** Immediate after restart.

**Resolution:** Same as Mitigation.

**Verification:** Panels that previously loaded sequentially now load in parallel. Network tab in browser dev tools shows `/api/ds/query` requests overlapping rather than starting one after another. Step 3 Grafana metrics show reduced p99 latency.

---

### Cause Z: Unidentified

**Statement:** Dashboard load latency cannot be attributed to a specific diagnosable cause from the steps above.

**Mechanism:** [Default]

**Indicator:**

- [Default] None of the above Causes match the observed diagnostic findings

**Mitigation:**

- **Risk:** Escalating without a clear cause may delay resolution; collect all diagnostic artifacts first.
- **Command:**

  ```bash
  # Collect Grafana server logs covering the slow period
  journalctl -u grafana-server --since "1 hour ago" > /tmp/grafana-recent.log
  # Export slow panel query via Query Inspector JSON (click Download button in UI)
  # Capture browser HAR file: DevTools > Network > Export HAR
  grep -E 'error|warn|timeout|deadline' /tmp/grafana-recent.log | tail -50
  ```

- **Duration:** Diagnostic only; no configuration change applied.

**Resolution:** Out of runbook scope. Escalate to Grafana support (Enterprise) or open a GitHub issue with collected HAR file, Grafana server logs, and Query Inspector JSON export.

**Verification:** N/A — escalation initiated with full diagnostic artifacts attached.

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
- [Grafana — Troubleshoot Queries](https://grafana.com/docs/grafana/latest/panels-visualizations/query-transform-data/troubleshoot-queries/) — Query Inspector usage, data-point limits, and transformation debugging; priority 1
- [Grafana — Configure Grafana](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/) — `[dataproxy]`, `[rendering]`, and `[database]` section reference with default values; priority 1
- [Prometheus — Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Pre-aggregation configuration for reducing query-time computation; priority 1
- [Prometheus — Query Logging](https://prometheus.io/docs/guides/query-log/) — Identifying expensive queries at the data-source level via `query_log_file`; priority 1
- [Grafana Labs Blog — Tips for Optimizing Query Performance](https://grafana.com/blog/grafana-dashboards-tips-for-optimizing-query-performance/) — PromQL optimization, recording rules, shared query results, and panel consolidation; priority 2
