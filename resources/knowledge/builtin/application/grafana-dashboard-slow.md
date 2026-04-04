---
id: grafana-dashboard-slow
title: "Grafana Dashboard Loading Slowly"
domain: application
service: grafana
symptom_class:
  - latency
severity: medium
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - grafana
  - dashboard
  - observability
  - query-performance
  - prometheus
difficulty: intermediate
---

# Grafana Dashboard Loading Slowly

## Problem Definition

Applies to Grafana OSS and Grafana Enterprise 9.x+ (also Grafana Cloud). Requires Grafana admin or editor role to access Query Inspector and dashboard settings. Access to the underlying data source (Prometheus, InfluxDB, Elasticsearch) is needed for backend-side diagnosis.

Dashboards take more than 5-10 seconds to render or time out entirely. Users observe spinner icons on individual panels, browser tab freezing during rendering, or partial dashboard loads where some panels display "No data" while others complete. The Grafana server logs may show `context deadline exceeded` or `query timeout` errors. The browser developer console may show long-running XHR requests to `/api/ds/query` exceeding 30 seconds. Panels with template variables that expand to hundreds of values load especially slowly. Dashboards that worked previously degrade as the monitored environment grows in cardinality or retention depth.

## Diagnostic Steps

### 1. Use Query Inspector to measure per-panel query time

Identifies which specific panel queries are slow and how much data each query returns.

Open the slow dashboard, click a panel title, select **Inspect > Query**. Review the **Query** tab for the raw query sent and the **Stats** tab for execution time and row count.

**Expected output:** Query execution time under 1 second and fewer than 10,000 data points per panel for smooth rendering. The inspector shows total request time, data source query time, and bytes transferred.

**What this means:** If query time exceeds 2 seconds, the data source is the bottleneck. If request time is much longer than query time, network or Grafana server processing is the issue. If row count exceeds 50,000, the query is returning too much data for browser rendering.

### 2. Check data source response time directly

Determines whether slowness originates in the data source backend or in Grafana's processing and rendering layer.

```bash
# For Prometheus — measure a representative query directly
time curl -s "http://prometheus:9090/api/v1/query?query=up" | jq '.data.result | length'
```

For the actual slow query (copy from Query Inspector):

```bash
time curl -s 'http://prometheus:9090/api/v1/query_range?query=sum(rate(http_requests_total[5m]))by(service)&start=2026-03-25T00:00:00Z&end=2026-03-26T00:00:00Z&step=60s' | jq '.data.result | length'
```

**Expected output:** Response in under 500ms with fewer than 500 result series.

**What this means:** If direct data source queries are fast but Grafana panels are slow, the bottleneck is in Grafana's query processing, transformation pipeline, or browser rendering. If direct queries are also slow, optimize at the data source level first.

### 3. Check Grafana server resource usage

Determines whether the Grafana server itself is resource-constrained.

```bash
ps aux | grep grafana-server | grep -v grep
curl -s http://localhost:3000/metrics | grep -E "grafana_api_response_status_total|grafana_datasource_request_duration_seconds"
```

For Kubernetes deployments:

```bash
kubectl top pod -l app.kubernetes.io/name=grafana -n monitoring
```

**Expected output:** CPU usage below 80%, memory usage within configured limits. `grafana_datasource_request_duration_seconds` p99 under 2 seconds.

**What this means:** High CPU indicates the server is spending excessive time processing query results or rendering. High memory may indicate large cached result sets. An undersized Grafana instance becomes the bottleneck regardless of data source performance when many users access dashboards concurrently.

### 4. Count panels, queries, and template variable cardinality

Determines whether the dashboard design itself is causing excessive parallel query load.

```bash
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "http://localhost:3000/api/dashboards/uid/<UID>" | \
  jq '{
    panels: (.dashboard.panels | length),
    total_queries: ([.dashboard.panels[].targets // [] | length] | add),
    template_vars: [.dashboard.templating.list[] | {name, multi, includeAll, query: .query}]
  }'
```

**Expected output:** Panels under 25, total queries under 30, no template variables with `multi: true` and `includeAll: true` that resolve to more than 50 values.

**What this means:** Each panel fires independent queries on load. Dashboards with 40+ panels create a burst of concurrent requests that saturate the Grafana query worker pool and the data source. Row-repeated panels with a high-cardinality variable multiply the query count by the number of variable values. A variable selecting "All" with 500 values on 10 panels generates 5,000 queries.

### 5. Identify expensive queries via Prometheus query log

Surfaces the most expensive queries hitting the Prometheus backend, regardless of which dashboard triggered them.

Enable query logging in Prometheus if not already active:

```yaml
# prometheus.yml
global:
  query_log_file: /var/log/prometheus/query.log
```

Then find the slowest queries:

```bash
cat /var/log/prometheus/query.log | jq -r 'select(.stats.timings.evalTotalTime > 2) | "\(.stats.timings.evalTotalTime)s | \(.query.query)"' | sort -rn | head -20
```

**Expected output:** A ranked list of queries with execution time over 2 seconds. Queries using regex label matchers, `{__name__=~".*"}`, or wide `rate()` windows appear frequently.

**What this means:** These queries are candidates for recording rules or query rewriting. The slowest queries drive the most data source load and should be optimized first.

### 6. Review Grafana query concurrency configuration

Checks whether the Grafana query worker pool is appropriately sized for the dashboard load.

```bash
grep -E "concurrent_render|max_conns_per_host|max_idle_conn" /etc/grafana/grafana.ini
```

**Expected output:** `max_conns_per_host` defaults to 0 (unlimited). `concurrent_render_request_limit` defaults to 5 in OSS.

**What this means:** If the concurrent query limit is too low for the number of panels, queries queue and panels load sequentially instead of in parallel. If too high, the data source may be overwhelmed by concurrent connections.

## Mitigation

### Option 1: Reduce dashboard time range and set Min interval

**Risk:** Low. Users see less historical data at lower resolution but dashboards load immediately.

**Command:** In the dashboard time picker, change from 24h/7d to 1h/6h. Edit each slow panel, open Query Options, set **Max data points** to 1000 and **Min interval** to match scrape interval (e.g., `15s`).

**Verify:** Reload the dashboard. Query Inspector shows data point count dropped below 5,000 per panel. Panels render in under 2 seconds.

**Duration:** Immediate.

### Option 2: Create Prometheus recording rules for expensive queries

**Risk:** Low. Recording rules pre-compute results and reduce query-time computation. Original raw metrics remain available.

**Command:**

```yaml
# /etc/prometheus/rules/dashboard-optimization.yml
groups:
  - name: dashboard-optimization
    interval: 1m
    rules:
      - record: http_requests:rate5m_by_service
        expr: sum by (service, status_code) (rate(http_requests_total[5m]))
      - record: node_cpu:usage_percent
        expr: 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

```bash
curl -X POST http://prometheus:9090/-/reload
```

Update dashboard panels to reference the recording rule metric names.

**Verify:** `curl -s 'http://prometheus:9090/api/v1/query?query=http_requests:rate5m_by_service' | jq '.data.result | length'` returns results. Dashboard panel load time drops significantly.

**Duration:** 5-10 minutes for rule creation and dashboard updates.

### Option 3: Increase Grafana concurrent query workers and connection pool

**Risk:** Medium. Increases parallelism but may overload the data source if it cannot handle additional concurrent connections.

**Command:**

```ini
# /etc/grafana/grafana.ini
[dataproxy]
max_conns_per_host = 25
max_idle_conns = 25
idle_conn_timeout_seconds = 90

[rendering]
concurrent_render_request_limit = 10
```

```bash
sudo systemctl restart grafana-server
```

**Verify:** Dashboard panels load in parallel instead of sequentially. Monitor data source CPU and memory to confirm it handles the additional connections.

**Duration:** 2 minutes including restart.

### Option 4: Split large dashboards into focused views

**Risk:** Low. Organizational change only; no data loss or configuration risk.

**Command:** Export the current dashboard via API or UI (Settings > JSON Model > Copy). Create new dashboards scoped to specific services or layers with 10-15 panels each. Use Grafana dashboard links to navigate between related views.

**Verify:** Each resulting dashboard loads in under 3 seconds. Panel count per dashboard stays below 20.

**Duration:** 30-60 minutes depending on dashboard complexity.

## Root Cause Resolution

**If** Query Inspector shows query execution time over 2 seconds for PromQL queries → optimize the query. Replace `rate(metric[24h])` with `rate(metric[5m])`, add label selectors to narrow the series set, avoid `{__name__=~".*"}` patterns, and use `topk()` or `bottomk()` to limit returned series. Create Prometheus recording rules for any aggregation queried repeatedly.

**If** template variables expand to hundreds of values with "All" selected → add a default selection that limits to 10-20 values, disable `includeAll` on high-cardinality variables, or replace the variable with a recording rule that pre-aggregates across the dimension.

**If** the dashboard has 30+ panels → split into multiple focused dashboards linked via dashboard links or drill-down URLs. Use row folding to defer loading of panels in collapsed rows (Grafana only queries visible panels).

**If** Grafana server CPU is consistently above 80% → scale horizontally by deploying multiple Grafana instances behind a load balancer with a shared PostgreSQL database. Enable query result caching via Grafana Enterprise caching or an external caching proxy.

**If** data source queries are slow regardless of Grafana configuration → the bottleneck is in the data source. For Prometheus: check for high cardinality (`prometheus_tsdb_head_series`), add recording rules, increase `--query.timeout`. For Elasticsearch: optimize index patterns, increase search thread pool. For SQL databases: add indexes on time and filter columns.

**If** browser freezes during rendering while queries complete quickly → the panel is rendering too many data points client-side. Set **Max data points** to 1000, switch from line graphs to heatmaps or bar charts for high-density data, and reduce the number of series per panel using aggregation.

**If** Grafana database is SQLite under concurrent user load → migrate to PostgreSQL or MySQL. SQLite is single-writer and creates contention when multiple users access dashboards simultaneously.

## Verification

1. **Measure dashboard load time:** Open the dashboard with browser developer tools Network tab. Total load time for all `/api/ds/query` requests should be under 3 seconds.

2. **Confirm per-panel query times via Query Inspector:** Each panel's query execution time should be under 1 second. Data point counts should be under 10,000 per panel.

3. **Monitor Grafana server metrics over 24 hours:**

```bash
curl -s http://localhost:3000/metrics | grep grafana_datasource_request_duration_seconds
```

p95 request duration should remain under 2 seconds during peak usage.

4. **Validate no timeout errors in Grafana logs:**

```bash
grep -i "timeout\|deadline exceeded\|context canceled" /var/log/grafana/grafana.log | tail -20
```

No new timeout entries should appear after the fix.

## Prevention

- **Set dashboard performance budgets.** Establish a target load time (under 3 seconds) and review dashboards quarterly against this budget.
- **Use Prometheus recording rules by default** for any metric queried on more than one dashboard or any aggregation involving more than 1,000 time series.
- **Keep dashboards under 20 panels.** Use row grouping with collapsed rows for secondary metrics. Link to detail dashboards for drill-down.
- **Configure Max data points (1000) and Min interval** on every panel to prevent queries from returning more data than the visualization can meaningfully display.
- **Set auto-refresh intervals appropriate to the time range:** 10s for real-time (5m window), 1m for operational (1h window), 5m+ for historical (24h+ window). Never use 5s refresh on dashboards with expensive queries.
- **Monitor Grafana's own metrics** (`grafana_datasource_request_duration_seconds`, `grafana_api_response_status_total`) and alert when p95 query time exceeds 3 seconds.
- **Enable query result caching** in Grafana Enterprise or use a caching reverse proxy in front of the data source to reduce repeated identical queries from concurrent users.
- **Avoid high-cardinality template variables.** Limit multi-select variables to under 50 values and default to a specific selection rather than "All."

## Sources

- [Grafana — Troubleshoot Dashboards](https://grafana.com/docs/grafana/latest/dashboards/troubleshoot-dashboards/) — Official dashboard performance troubleshooting including Query Inspector usage and rendering optimization
- [Grafana — Troubleshoot Queries](https://grafana.com/docs/grafana/latest/panels-visualizations/query-transform-data/troubleshoot-queries/) — Query-level diagnosis including data point limits and transformation debugging
- [Grafana Labs — Tips for Optimizing Query Performance](https://grafana.com/blog/grafana-dashboards-tips-for-optimizing-query-performance/) — PromQL optimization, recording rules, shared query results, and panel consolidation
- [Prometheus — Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Pre-aggregation configuration for reducing query-time computation
- [Prometheus — Query Logging](https://prometheus.io/docs/guides/query-log/) — Identifying expensive queries at the data source level
- [Grafana Configuration Reference](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/) — Data proxy, concurrent query workers, and database backend settings
