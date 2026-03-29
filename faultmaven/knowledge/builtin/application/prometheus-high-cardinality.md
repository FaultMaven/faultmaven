---
id: prometheus-high-cardinality
title: "Prometheus High Cardinality — Diagnosis and Resolution"
domain: application
service: prometheus
symptom_class:
  - oom
  - latency
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - prometheus
  - cardinality
  - memory
  - time-series
  - observability
difficulty: intermediate
---

# Prometheus High Cardinality

## Problem Definition

Applies to Prometheus 2.x+ running on Linux or Kubernetes. Requires access to the Prometheus web UI or API (port 9090), configuration files (`prometheus.yml`, rule files), and `promtool` CLI. Admin access to scrape configuration and application instrumentation code is needed for permanent fixes.

High cardinality occurs when the number of unique time series grows excessively due to labels with unbounded or highly variable values. Each unique combination of metric name and label key-value pairs creates a distinct time series stored in the TSDB head block in memory. A healthy Prometheus instance manages 1-5 million series; above 10 million typically indicates a cardinality problem. Symptoms include Prometheus process OOMKilled or consuming memory far beyond baseline, slow or timing-out PromQL queries, TSDB head block growing unboundedly (`prometheus_tsdb_head_series` trending upward), scrape durations increasing significantly, and log messages containing `out of memory` or `too many samples`. Common culprits are labels containing user IDs, request IDs, IP addresses, UUIDs, or session tokens; Kubernetes pod name or container ID labels on custom metrics; HTTP path labels with unbounded route parameters (e.g., `/users/12345` instead of `/users/{id}`); per-request metrics instead of aggregated counters; and community exporters exposing high-cardinality metrics by default (kube-state-metrics, cAdvisor).

## Diagnostic Steps

### 1. Check current total time series count

Establishes the baseline series count and determines whether it is within a healthy range.

```bash
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' | jq '.data.result[0].value[1]'
```

**Expected output:** A number representing active series in the head block. Below 5 million is typical for a single Prometheus instance.

**What this means:** Above 5 million warrants investigation. Above 10 million is likely causing memory pressure. Compare against historical values to determine growth rate. Prometheus memory is roughly proportional to series count (approximately 1-3KB per series for the head block).

### 2. Identify top metrics by series count using TSDB status API

Pinpoints which metric names are producing the most time series.

```bash
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20' | \
  jq '.data.seriesCountByMetricName[:10]'
```

**Expected output:** Array of `{name, value}` pairs, sorted by series count descending. The top entries are the highest-cardinality metrics.

**What this means:** A single metric with 500K+ series is almost certainly the problem. Common offenders: `container_*` (cAdvisor), `kube_pod_*` (kube-state-metrics), or application-level HTTP metrics with path/user labels. The TSDB status API also returns `labelValueCountByLabelName` and `seriesCountByLabelPair` which identify the specific labels causing the explosion.

### 3. Identify high-cardinality labels on the top metric

Determines which label dimension is creating the cardinality explosion on a specific metric.

```bash
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20' | \
  jq '.data.labelValueCountByLabelName[:10]'
```

For a specific metric, count distinct values per label:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=count(http_requests_total)+by+(path)' | jq '.data.result | length'
curl -s 'http://localhost:9090/api/v1/query?query=count(http_requests_total)+by+(user_id)' | jq '.data.result | length'
```

**Expected output:** The label with the highest distinct value count is the cardinality driver. A `path` label with 50,000 values or a `user_id` label with 100,000 values is the root cause.

**What this means:** Labels with unbounded value spaces (IDs, IPs, paths with parameters) must be removed, aggregated, or normalized. A label with fewer than 100 distinct values is fine; above 1,000 is a warning; above 10,000 is a problem.

### 4. Identify scrape targets contributing the most series

Determines which scrape jobs or instances are introducing the most time series.

```bash
curl -s 'http://localhost:9090/api/v1/query?query=topk(20,+scrape_samples_post_metric_relabeling)' | \
  jq '.data.result[] | {job: .metric.job, instance: .metric.instance, samples: .value[1]}'
```

To find targets with recent series growth:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=topk(10,+increase(scrape_series_added[1h]))' | \
  jq '.data.result[] | {job: .metric.job, instance: .metric.instance, added: .value[1]}'
```

**Expected output:** Targets with the highest sample count or recent series additions. A single target exposing 100K+ samples per scrape is a significant cardinality source.

**What this means:** The target with the highest `scrape_samples_post_metric_relabeling` or fastest `scrape_series_added` growth is the source to investigate. This may be a misbehaving application, an over-configured exporter, or a service discovery rule that matches too many endpoints.

### 5. Check Prometheus memory usage and growth rate

Determines whether memory pressure is imminent and how fast it is growing.

```bash
curl -s 'http://localhost:9090/api/v1/query?query=process_resident_memory_bytes' | jq '.data.result[0].value[1]'
curl -s 'http://localhost:9090/api/v1/query?query=rate(process_resident_memory_bytes[1h])' | jq '.data.result[0].value[1]'
```

**Expected output:** Memory usage below the container/host limit with headroom. Growth rate near zero at steady state.

**What this means:** Positive growth rate means cardinality is still increasing. If memory is above 80% of the limit with positive growth, OOMKill is imminent. Prometheus memory is approximately: `series_count * 1-3KB` for the head block plus query working memory.

### 6. Identify unused metrics with mimirtool (optional)

Finds metrics that are collected but not used by any dashboard or alert rule, allowing safe removal.

```bash
mimirtool analyze prometheus --address=http://localhost:9090 \
  --grafana-address=http://grafana:3000 --grafana-api-key=$GRAFANA_TOKEN
```

**Expected output:** Report of metrics used in rules/dashboards vs. metrics being scraped. "Unused metrics" section lists candidates for dropping.

**What this means:** Metrics not referenced by any alert rule or Grafana dashboard can likely be dropped via `metric_relabel_configs`. This is the safest way to reduce cardinality since no existing functionality depends on the dropped metrics.

## Mitigation

### Option 1: Drop high-cardinality labels via metric_relabel_configs

**Risk:** Low. Only affects future scrapes; does not delete historical data. Verify the label is not used in alerts or dashboards before dropping.

**Command:**

Add to the relevant scrape job in `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'my-app'
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: 'http_requests_total'
        action: keep
      - regex: 'user_id|request_id|trace_id'
        action: labeldrop
```

```bash
curl -X POST http://localhost:9090/-/reload
```

**Verify:** `curl -s 'http://localhost:9090/api/v1/query?query=count(http_requests_total)' | jq '.data.result[0].value[1]'` shows reduced series count after one scrape interval.

**Duration:** 1-5 minutes. Series reduction visible within 2 scrape intervals. Can reduce cardinality by 50-90%.

### Option 2: Drop entire high-cardinality metrics

**Risk:** Medium. The metric is no longer collected. Verify no active alerts or dashboards depend on it.

**Command:**

```yaml
scrape_configs:
  - job_name: 'my-app'
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: 'my_debug_metric|my_per_request_histogram'
        action: drop
```

```bash
curl -X POST http://localhost:9090/-/reload
```

**Verify:** `curl -s 'http://localhost:9090/api/v1/query?query=my_debug_metric' | jq '.data.result | length'` returns 0 after old series expire (default 2 hours).

**Duration:** Immediate for new scrapes. Old series expire after `--storage.tsdb.min-block-duration` (default 2h).

### Option 3: Set sample_limit per scrape job

**Risk:** Low. Prevents any single target from exceeding the sample limit, protecting the Prometheus instance. Targets exceeding the limit show scrape errors.

**Command:**

```yaml
scrape_configs:
  - job_name: 'my-app'
    sample_limit: 50000
```

```bash
curl -X POST http://localhost:9090/-/reload
```

**Verify:** Check for scrape errors on targets that exceed the limit: `curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.lastError != "") | {job: .labels.job, error: .lastError}'`

**Duration:** Immediate after reload.

### Option 4: Increase Prometheus memory limits (temporary relief)

**Risk:** Low. Buys time but does not fix the root cause. Cardinality will continue growing.

**Command:**

```bash
kubectl patch statefulset prometheus-server -n monitoring --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"12Gi"}]'
```

**Verify:** `kubectl top pod -n monitoring -l app=prometheus` shows the pod running with memory below the new limit.

**Duration:** 1-2 minutes for pod restart.

### Option 5: Create recording rules to pre-aggregate

**Risk:** Low. Creates new lower-cardinality aggregated metrics. Does not affect originals. Dashboards and alerts should be updated to use the recording rules.

**Command:**

```yaml
groups:
  - name: cardinality-reduction
    interval: 1m
    rules:
      - record: http_requests:rate5m_by_method_status
        expr: sum by (method, status_code) (rate(http_requests_total[5m]))
      - record: http_request_duration:p99_by_service
        expr: histogram_quantile(0.99, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))
```

```bash
curl -X POST http://localhost:9090/-/reload
```

**Verify:** `curl -s 'http://localhost:9090/api/v1/query?query=http_requests:rate5m_by_method_status' | jq '.data.result | length'` shows a much smaller series count than the raw metric.

**Duration:** Recording rules populate within one evaluation interval (1 minute).

## Root Cause Resolution

**If** a label contains unbounded values (user IDs, request IDs, IP addresses, UUIDs) → remove the label from the metric at the application instrumentation level. Use `metric_relabel_configs` as an interim measure. The permanent fix is modifying the application code to stop emitting the high-cardinality label.

**If** HTTP path labels contain route parameters (`/users/12345`) → normalize paths in the application's metrics middleware to use route templates (`/users/{id}`). In Express.js, use `req.route.path`; in Go, use the router's pattern; in Spring Boot, use `@Timed` annotations on handler methods.

**If** Kubernetes metadata labels (pod name, container ID, UID) appear on custom application metrics → configure the Prometheus client library to exclude these labels, or use `metric_relabel_configs` to strip them at scrape time. Pod-level granularity is rarely needed for application metrics.

**If** a community exporter (kube-state-metrics, cAdvisor, node-exporter) produces too many series → configure the exporter to reduce output. For kube-state-metrics: use `--metric-labels-allowlist` and `--resources` flags. For cAdvisor: filter with `metric_relabel_configs` to keep only needed `container_*` metrics.

**If** metrics are collected but never used in dashboards or alerts → use `mimirtool analyze` to identify unused metrics and drop them via `metric_relabel_configs` with `action: drop`.

**If** the cardinality growth is from target discovery matching too many endpoints → tighten service discovery selectors (`kubernetes_sd_configs` with namespace, label, or annotation filters) or add `relabel_configs` to drop unwanted targets before scraping.

## Verification

1. **Series count is decreasing or stable:**

```bash
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' | jq '.data.result[0].value[1]'
```

Compare against the value from diagnosis. Expect a decrease within 2 hours as stale series are garbage-collected from the head block.

2. **Memory usage stabilized:**

```bash
curl -s 'http://localhost:9090/api/v1/query?query=rate(process_resident_memory_bytes[1h])' | jq '.data.result[0].value[1]'
```

Growth rate should be near zero or negative.

3. **Query latency improved:**

```bash
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_engine_query_duration_seconds{quantile="0.99"}' | jq '.data.result[0].value[1]'
```

p99 query duration should return to baseline (under 2 seconds for typical queries).

4. **No OOMKill events:**

```bash
kubectl get events -n monitoring --field-selector reason=OOMKilling --sort-by='.lastTimestamp' | tail -5
```

No new OOMKill events for the Prometheus pod after the fix.

## Prevention

- **Enforce cardinality budgets with alerts.** Alert when `prometheus_tsdb_head_series > 5000000` or `increase(scrape_series_added[1h]) > 100000` to detect growth early before it causes OOM.
- **Set `sample_limit` on every scrape job.** Use `sample_limit: 50000` (or appropriate value) to prevent any single target from exploding cardinality. Targets exceeding the limit fail the scrape with a clear error.
- **Review metric labels in code review.** Treat new metric labels as a mandatory review checkpoint. Reject labels with unbounded value spaces (user IDs, timestamps, UUIDs, request IDs). Use the rule: if the label can have more than 100 distinct values, it needs justification.
- **Use recording rules proactively.** Pre-aggregate high-dimensional metrics into lower-cardinality recording rules. Point dashboards and alerts at the aggregated versions. Drop the raw metrics once recording rules are stable.
- **Normalize HTTP paths at the instrumentation layer.** Use route templates (`/users/{id}`) instead of actual URL paths. Configure middleware to group paths before they reach the metrics library.
- **Audit third-party exporters before production deployment.** Review kube-state-metrics, cAdvisor, and community exporters for high-cardinality defaults. Configure `--metric-labels-allowlist`, `--resources`, or `metric_relabel_configs` to limit output.
- **Use `mimirtool analyze` quarterly** to identify collected-but-unused metrics and remove them.
- **Monitor TSDB health metrics** on a dedicated self-monitoring dashboard: `prometheus_tsdb_head_series`, `prometheus_tsdb_head_chunks`, `scrape_series_added`, `scrape_samples_post_metric_relabeling`, and `process_resident_memory_bytes`.

## Sources

- [Prometheus — Instrumentation Best Practices](https://prometheus.io/docs/practices/instrumentation/) — Official guidance on metric design and label cardinality
- [Prometheus — TSDB Status API](https://prometheus.io/docs/prometheus/latest/querying/api/#tsdb-stats) — `seriesCountByMetricName`, `labelValueCountByLabelName`, and `seriesCountByLabelPair` endpoints
- [Prometheus — metric_relabel_configs](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#metric_relabel_configs) — Relabeling rules to drop or modify labels at scrape time
- [Prometheus — Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Pre-aggregation to reduce query-time cardinality
- [Finding and Reducing High Cardinality in Prometheus](https://kaidalov.com/posts/2025/09/prometheus-optimization/) — TSDB status API usage, PromQL diagnostic queries, and relabeling strategies
- [Grafana Labs — Managing High Cardinality in Prometheus and Kubernetes](https://grafana.com/blog/how-to-manage-high-cardinality-metrics-in-prometheus-and-kubernetes/) — kube-state-metrics and cAdvisor optimization, mimirtool usage
- [How Cloudflare Runs Prometheus at Scale](https://blog.cloudflare.com/how-cloudflare-runs-prometheus-at-scale/) — Production-scale cardinality management and sharding strategies
