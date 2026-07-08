---
id: prometheus-high-cardinality
title: "Prometheus High Cardinality"
domain: application
service: prometheus
symptom_class:
  - oom
  - latency
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - prometheus
  - cardinality
  - tsdb
  - memory
  - observability
difficulty: intermediate
---

# Prometheus High Cardinality

## Symptom Recognition

- Prometheus process is OOMKilled or restarted by the supervisor; container exit code is 137 on Kubernetes deployments.
- `prometheus_tsdb_head_series` trends upward without plateau, often crossing 5M (warning) or 10M (likely problem) for a single instance.
- PromQL queries time out or return `query processing would load too many samples into memory`.
- Scrape durations grow: `scrape_duration_seconds` for affected jobs exceeds 80% of the scrape interval.
- `scrape_samples_post_metric_relabeling` for one job jumps by 10x within minutes, or `scrape_series_added` rate stays high for hours.
- Prometheus logs contain `out of memory`, `samples in batch dropped`, or `sample limit exceeded` for specific scrape targets.
- Dashboards backed by Prometheus return empty panels or `error executing query: too many samples`; alert evaluation lags (`prometheus_rule_evaluation_duration_seconds` rising).
- New deployment or label change immediately precedes the spike (correlation in `scrape_series_added` with a `job` or `instance` label).

## Applicability

- Prometheus 2.x or 3.x (TSDB-based; legacy Prometheus 1.x is out of scope).
- Self-hosted Prometheus, kube-prometheus-stack, or Prometheus Operator deployments. Managed Prometheus services (Grafana Cloud, AMP, GMP) expose similar APIs but vendor consoles supersede some commands below.
- Requires HTTP access to the Prometheus admin endpoints (`/api/v1/query`, `/api/v1/status/tsdb`, `/api/v1/targets`, `/-/reload`). The `--web.enable-lifecycle` flag must be set to use the `/-/reload` endpoint.
- Requires write access to `prometheus.yml` and rule files, plus the ability to reload (HTTP POST `/-/reload`, `SIGHUP`, or Kubernetes ConfigMap update + pod restart).
- `promtool` CLI on the local host for rule and config validation.
- `curl` + `jq` for the diagnostic queries. `kubectl` if Prometheus runs on Kubernetes.
- Optional: `mimirtool` for identifying unused metrics against Grafana dashboards.

## Diagnostic Steps

### Step 1: Read current head series count and memory usage

```bash
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' \
  | jq -r '.data.result[0].value[1]'
curl -s 'http://localhost:9090/api/v1/query?query=process_resident_memory_bytes' \
  | jq -r '.data.result[0].value[1]'
```

Expected output: two numeric values. Head series under 5,000,000 is typical for a single instance; resident memory should sit well below the container or host limit. A series count above 10,000,000 or memory above 80% of the limit is consistent with a cardinality problem.

### Step 2: Rank metric names by series count via TSDB status API

```bash
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20' \
  | jq '.data.seriesCountByMetricName'
```

Expected output: array of `{name, value}` pairs sorted descending. The top entries are the highest-cardinality metric names. A single metric responsible for 30%+ of total series, or any metric over ~500,000 series on a small-to-mid instance, is the prime suspect.

### Step 3: Rank labels by distinct value count

```bash
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20' \
  | jq '.data.labelValueCountByLabelName'
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20' \
  | jq '.data.memoryInBytesByLabelName'
```

Expected output: two arrays sorted descending. `labelValueCountByLabelName` shows distinct values per label; `memoryInBytesByLabelName` shows memory consumed by label values. A label with more than 10,000 distinct values, or any label appearing first in both lists, is a cardinality driver.

### Step 4: Identify scrape targets contributing the most samples

```bash
curl -s 'http://localhost:9090/api/v1/query?query=topk(20,scrape_samples_post_metric_relabeling)' \
  | jq '.data.result[] | {job: .metric.job, instance: .metric.instance, samples: .value[1]}'
curl -s 'http://localhost:9090/api/v1/query?query=topk(10,rate(scrape_series_added[1h]))' \
  | jq '.data.result[] | {job: .metric.job, instance: .metric.instance, added_per_s: .value[1]}'
```

Expected output: top jobs/instances by post-relabeling sample count and by recent series-addition rate. A single target above ~100,000 samples per scrape, or `scrape_series_added` rate above ~50/s sustained, indicates a misbehaving target.

### Step 5: Locate the high-cardinality label on a specific metric

```bash
# Replace <metric> with the metric name from Step 2.
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=topk(10, count by (__name__) ({__name__="<metric>"}))'
# Then count distinct values for each candidate label:
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=count(count by (path) (<metric>))'
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=count(count by (user_id) (<metric>))'
```

Expected output: the per-label distinct-value count. Whichever label returns the largest count is the cardinality driver for that metric. Under 100 values is fine, 100–1,000 is borderline, above 10,000 is the cause.

### Step 6: Check whether sample/series limits are configured per scrape job

```bash
curl -s http://localhost:9090/api/v1/status/config \
  | jq -r '.data.yaml' \
  | grep -E 'sample_limit|target_limit|label_limit|label_value_length_limit' || echo "NO LIMITS CONFIGURED"
```

Expected output: any `sample_limit:`, `label_limit:`, or related lines, or the literal `NO LIMITS CONFIGURED`. Absence of these limits means a single misbehaving target can ingest arbitrary cardinality.

### Step 7: Inspect targets for scrape failures driven by limits

```bash
curl -s http://localhost:9090/api/v1/targets \
  | jq '.data.activeTargets[] | select(.lastError != "") | {job: .labels.job, instance: .labels.instance, lastError}'
```

Expected output: targets with non-empty `lastError`. Messages such as `sample limit exceeded`, `label limit exceeded`, or `label_value_length_limit exceeded` confirm that an upstream target is emitting more than the configured ceiling allows.

### Step 8: Correlate the spike with a recent deployment or config reload

```bash
curl -s 'http://localhost:9090/api/v1/query_range?query=prometheus_tsdb_head_series&start='$(date -u -d '24 hours ago' +%s)'&end='$(date -u +%s)'&step=300' \
  | jq '.data.result[0].values[-30:]'
curl -s 'http://localhost:9090/api/v1/query?query=changes(prometheus_config_last_reload_success_timestamp_seconds[24h])' \
  | jq '.data.result[0].value[1]'
```

Expected output: a series of `[timestamp, value]` pairs for head series and the count of successful reloads in the last 24 hours. A vertical step in head series at the same minute as a config reload, or coinciding with an application deployment, points to a label change or new target.

### Step 9: Identify metrics with no consumer (optional, requires Grafana access)

```bash
# mimirtool reads the Grafana service-account token from the MIMIR_API_KEY env var
# Set MIMIR_API_KEY in your shell first (do NOT hardcode it):
#   export MIMIR_API_KEY=...
mimirtool analyze prometheus --address=http://localhost:9090 \
  --grafana-address=http://grafana:3000
mimirtool analyze dashboard ./dashboards/*.json
mimirtool analyze rule-file ./rules/*.yml
```

Expected output: JSON report listing metrics scraped vs. metrics referenced by dashboards and recording/alerting rules. The "unused metrics" set is safe to drop with `metric_relabel_configs` `action: drop`.

## Causes

### Cause A: Unbounded-identifier label on an application metric

**Statement:** An application metric carries a label whose value space is unbounded (user ID, request ID, trace ID, session token, email), so every distinct request creates a new time series.

**Chain:**
- root: the application emits a label drawn from an unbounded identifier set (user ID, request ID, trace ID, email) on a metric.
- s1: Prometheus creates one time series per unique metric-name + label-value combination, so series count grows linearly with traffic.
- s2: the TSDB head block holds every active series in memory (~1–3 KB per series for index plus chunk references), so resident memory rises toward the host or container limit.
- D: the process hits the memory limit and is OOMKilled, while query evaluation (which scales with selected series) slows then fails — the Symptom.

**Indicators:**
- root: [Step 3] the same label name (`user_id`, `request_id`, `trace_id`, `email`) tops `labelValueCountByLabelName` with thousands of distinct values.
- s1: [Step 2] one application metric is at the top of `seriesCountByMetricName` with hundreds of thousands of series.
- s1: [Step 5] `count(count by (<label>) (<metric>))` for that metric returns a value above 10,000.

**Interventions:**
- **remediation** (root): stop emitting the unbounded label at the application source.

  ```bash
  # Permanent fix: stop emitting the unbounded label at the application source.
  # Go (prometheus/client_golang): remove the offending label from the metric definition.
  # Java (micrometer): remove the high-cardinality tag from .tag(...) in the timer/counter builder.
  # Python (prometheus_client): drop the kwarg from .labels(...) calls.
  # Then redeploy the application. Keep the metric_relabel_configs labeldrop in place as a belt-and-braces guard.
  ```

  **Verification:** Re-run Step 2 and Step 3 after 15 minutes; the metric must drop out of the top entries in `seriesCountByMetricName` and the offending label must drop out of `labelValueCountByLabelName`. `prometheus_tsdb_head_series` should plateau or decline within 2 hours.
- **mitigation** (s1): drop the unbounded label at scrape time so no new series are created from it.

  ```yaml
  # In prometheus.yml under the affected scrape_configs entry.
  metric_relabel_configs:
    - source_labels: [__name__]
      regex: '<metric>'
      action: keep
    - regex: 'user_id|request_id|trace_id|session_id|email'
      action: labeldrop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Dropping the label at scrape time hides the dimension from all consumers; verify no dashboard or alert filters on it before applying. **Duration:** Permanent at the scrape layer, but treat as a stopgap — fix the instrumentation upstream within days, not weeks. **Verification:** Re-run Step 3 after 15 minutes; the offending label must drop out of `labelValueCountByLabelName`.

### Cause B: HTTP path or URL label captures raw paths instead of route templates

**Statement:** A web framework's metrics middleware emits the raw request path (`/users/12345/orders/A7B9`) as a label, so each distinct URL produces a separate time series.

**Chain:**
- root: the instrumentation library reads `request.path` / `request.uri` instead of the matched route template, so every distinct URL becomes a label value.
- s1: for an API with path parameters, distinct path values grow with resources × routes, making the metric effectively unbounded.
- D: Prometheus index size, head memory, and query time grow linearly with the path label cardinality until OOM or query failure — the Symptom.

**Indicators:**
- root: [Step 5] a path/uri/url label on the HTTP metric has thousands of distinct values, many embedding numeric or UUID segments.
- s1: [Step 2] an HTTP-related metric (`http_requests_total`, `http_request_duration_seconds_count`, `http_server_requests_seconds_count`) dominates `seriesCountByMetricName`.
- s1: [Symptom] the cardinality spike correlates with traffic to a parameterised API route rather than with a deployment.

**Interventions:**
- **remediation** (root): emit the matched route template instead of the raw path at the application layer.

  ```text
  # Application-level fix per framework:
  # Express.js:      use req.route?.path (template) instead of req.path
  # Spring Boot:     micrometer's WebMvcMetricsFilter emits 'uri' as the matched mapping automatically
  # Django/DRF:      use resolver_match.route from request.resolver_match
  # Flask:           use request.url_rule.rule
  # FastAPI:         use request.scope['route'].path
  # Go (chi/gin/mux):use the router's matched pattern (RouteContext / FullPath / CurrentRoute)
  # After the application is redeployed, the relabel rule below is no longer needed but is harmless to leave.
  ```

  **Verification:** After redeploy, run `curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count(count by (path) (http_requests_total))'`; the result must be in the tens or low hundreds, matching the number of templated routes, not the number of unique URLs.
- **mitigation** (s1): normalise common ID shapes to a placeholder at scrape time so `/users/12345` collapses to `/users/{id}`.

  ```yaml
  # Normalise common ID shapes to a placeholder so /users/12345 collapses to /users/{id}.
  metric_relabel_configs:
    - source_labels: [path]
      regex: '(/[^/]+)/[0-9]+(.*)'
      target_label: path
      replacement: '${1}/{id}${2}'
    - source_labels: [path]
      regex: '(/[^/]+)/[0-9a-f-]{36}(.*)'
      target_label: path
      replacement: '${1}/{uuid}${2}'
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Stripping path segments at scrape time loses the dimension entirely; collapsing to a single bucket discards useful breakdowns. Verify dashboards before rolling out. **Duration:** Permanent as a scrape-time guard, but fix the middleware to emit the matched route template instead. **Verification:** Re-run the per-label count query; distinct `path` values must collapse toward the number of templated routes.

### Cause C: Kubernetes pod or container label propagated onto an application metric

**Statement:** Application metrics carry a `pod`, `pod_name`, `container_id`, or similar Kubernetes-injected label, so every pod rollout creates a fresh series cohort that lingers in the head block.

**Chain:**
- root: Kubernetes service discovery joins target labels into scraped series (or a sidecar injects pod metadata via `external_labels`/`honor_labels`), so each metric carries a per-pod identity label.
- s1: every deployment generates a new set of pod names, and series for terminated pods stay in the head block until they fall out of the lookback window.
- s2: each rolling restart multiplies the series count, so with frequent CI deployments cardinality grows on a daily cadence rather than stabilising.
- D: head series and resident memory climb per-deploy until OOM — the Symptom.

**Indicators:**
- root: [Step 3] `pod`, `pod_name`, `pod_uid`, or `container_id` is near the top of `labelValueCountByLabelName` with thousands of distinct values.
- s1: [Step 8] head series shows step changes at deployment times, not at traffic peaks.
- s2: [Symptom] series count grows on a daily or per-deploy cadence with no traffic correlation.

**Interventions:**
- **remediation** (root): strip the Kubernetes pod/container identity labels from the affected job's metrics in `prometheus.yml`.

  ```yaml
  # prometheus.yml — strip Kubernetes pod/container identity labels from the affected job's metrics.
  scrape_configs:
    - job_name: my-app
      metric_relabel_configs:
        - regex: 'pod|pod_name|pod_uid|container_id|instance'
          action: labeldrop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Verification:** Re-run Step 3 after 15 minutes; `pod` and `container_id` must no longer appear in the top entries of `labelValueCountByLabelName` for the affected job. `prometheus_tsdb_head_series` should plateau within one head-block boundary.
- **mitigation** (s1): apply a quick `labeldrop` for the pod identity labels to halt the per-deploy series growth.

  ```yaml
  metric_relabel_configs:
    - regex: 'pod|pod_name|pod_uid|container_id|instance'
      action: labeldrop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Dropping `pod` removes per-pod debuggability for that metric; ensure operators rely on logs or a separate `cadvisor`/`kubelet` metric for per-pod views. **Duration:** Permanent at the scrape layer; revisit if per-pod debugging on the metric becomes a frequent need. **Verification:** Re-run Step 3; the pod identity labels must drop out of the top entries.

### Cause D: Histogram with too many buckets or per-handler bucket explosion

**Statement:** A histogram metric is configured with a large `buckets` array and is sliced by additional labels, so the bucket count multiplies with each label combination.

**Chain:**
- root: a histogram is defined with a large `buckets` array (high N) and sliced by extra labels such as `method`, `status_code`, and `handler`.
- s1: a histogram with N buckets generates N+2 series per label combination (`_bucket` × N, `_sum`, `_count`), and the cumulative `le` label compounds — each extra bucket adds one series for every existing combination.
- s2: a single histogram emits thousands of series per pod and becomes the largest contributor in `seriesCountByMetricName`.
- D: head series and memory rise until OOM — the Symptom.

**Indicators:**
- root: [Step 3] the `le` label has more than 15 distinct values.
- s1: [Step 5] series count for the histogram equals roughly (bucket count + 2) × distinct combinations of other labels.
- s2: [Step 2] a metric ending in `_bucket` dominates `seriesCountByMetricName`.

**Interventions:**
- **remediation** (root): prune the buckets array at the instrumentation source, or switch to native histograms.

  ```text
  # Application-level fix: prune the buckets array at the instrumentation source.
  # Java/micrometer:      .serviceLevelObjectives(Duration.ofMillis(100), Duration.ofMillis(500), Duration.ofSeconds(1), Duration.ofSeconds(5))
  # Go/client_golang:     prometheus.HistogramOpts{Buckets: []float64{0.1, 0.5, 1, 5}}
  # Python/prom_client:   Histogram(..., buckets=(0.1, 0.5, 1, 5))
  # Alternatively, switch to native histograms (Prometheus 2.40+):
  #   enable per-scrape with --enable-feature=native-histograms and configure the client library to emit native histograms,
  #   which use a single series per label combination regardless of resolution.
  ```

  **Verification:** Re-run Step 2; the `_bucket` metric must show a series count consistent with `kept_buckets × distinct_combinations_of_other_labels`. `prometheus_tsdb_head_series` should drop within one head-block boundary.
- **mitigation** (s1): keep only SLO-critical bucket boundaries at scrape time and drop the rest.

  ```yaml
  # Keep only SLO-critical bucket boundaries (example: 100ms, 500ms, 1s, 5s).
  metric_relabel_configs:
    - source_labels: [__name__, le]
      regex: '.+_bucket;(0\.1|0\.5|1|5|\+Inf)'
      action: keep
    - source_labels: [__name__]
      regex: '.+_bucket'
      action: drop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Dropping bucket boundaries reduces resolution for SLO calculations near those quantiles; verify which `le` values are referenced by alerts and dashboards before dropping. **Duration:** Permanent. Revisit if SLO thresholds change. **Verification:** Re-run Step 2; the `_bucket` series count must reflect only the kept boundaries.

### Cause E: Exporter emitting high-cardinality default metrics (kube-state-metrics, cAdvisor, node_exporter)

**Statement:** A community exporter is enabled with its default metric set and exposes per-resource-version or per-container series that aren't consumed by any dashboard or alert.

**Chain:**
- root: a community exporter (kube-state-metrics, cAdvisor, node_exporter) runs with its default metric set and label collectors enabled.
- s1: kube-state-metrics emits one series per Kubernetes object per metric, cAdvisor emits hundreds of `container_*` metrics per container per node, and node_exporter's `textfile`/`systemd` collectors expand with unit count.
- s2: on a busy cluster with thousands of pods, these defaults account for the majority of head series — often for data nothing on the consuming side queries.
- D: head series and memory climb until OOM or query failure — the Symptom.

**Indicators:**
- root: [Step 2] metrics named `kube_pod_*`, `container_*`, or `node_*` occupy multiple of the top slots in `seriesCountByMetricName`.
- s1: [Step 4] the `kube-state-metrics`, `cadvisor`, or `node-exporter` job tops `scrape_samples_post_metric_relabeling`.
- s2: [Step 9] `mimirtool analyze` reports many of those metrics as unused.

**Interventions:**
- **remediation** (root): reduce exporter output at the source via resource/label allowlists and collector selection.

  ```bash
  # Reduce exporter output at the source.
  # kube-state-metrics: restrict collected resources and labels.
  kubectl set args -n kube-system deployment/kube-state-metrics \
    --resources=pods,nodes,deployments,services \
    --metric-labels-allowlist='pods=[app,team],nodes=[node-role]'
  # cAdvisor: configure kubelet flags to disable unneeded collectors via --disable-metrics on the kubelet args (cluster-specific).
  # node_exporter: explicitly select collectors and disable others.
  #   args: --collector.disable-defaults --collector.cpu --collector.meminfo --collector.filesystem --collector.netdev --collector.loadavg
  ```

  **Verification:** After rollout, re-run Step 2 and Step 4. The exporter job's `scrape_samples_post_metric_relabeling` must drop by the expected fraction, and the dropped metric families must no longer appear in `seriesCountByMetricName`.
- **mitigation** (s1): drop the confirmed-unused exporter metrics at scrape time (run Step 9 first).

  ```yaml
  # Drop unused kube_pod_* and container_* metrics at scrape time.
  scrape_configs:
    - job_name: kube-state-metrics
      metric_relabel_configs:
        - source_labels: [__name__]
          regex: 'kube_pod_(annotations|status_container_ready_time|init_container_.*|tolerations)'
          action: drop
    - job_name: cadvisor
      metric_relabel_configs:
        - source_labels: [__name__]
          regex: 'container_(blkio_.*|tasks_state|fs_.*_seconds|memory_failures_total)'
          action: drop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Dropping exporter metrics removes them from the TSDB; any future dashboard or alert that needs them will return empty. Always run Step 9 first to confirm no consumer. **Duration:** Permanent. Re-evaluate every quarter with `mimirtool analyze`. **Verification:** Re-run Step 2; the dropped metric families must no longer appear.

### Cause F: No per-job sample_limit or target_limit allows a single misbehaving target to dominate

**Statement:** Scrape jobs are configured without `sample_limit`, `target_limit`, or `label_value_length_limit`, so any target that starts emitting high-cardinality output is ingested in full.

**Chain:**
- root: scrape jobs lack `sample_limit` / `target_limit` / `label_value_length_limit`, so Prometheus accepts every sample a target emits regardless of cardinality.
- s1: a buggy release introducing an unbounded label, or a new SD-discovered target, spikes `scrape_samples_post_metric_relabeling` for that job by 10x or more within minutes, with no cap to stop it.
- s2: the new series flow uncapped into the head block, and the operator only learns of the spike after memory pressure or alert-evaluation degradation.
- D: head series grow without bound until OOM — the Symptom.

**Indicators:**
- root: [Step 6] `sample_limit` / `target_limit` / `label_limit` are absent from the active config.
- s1: [Step 4] one scrape job is responsible for the majority of samples and shows rapid growth in `scrape_series_added`.
- s2: [Step 8] head series shows a step change coinciding with `prometheus_config_last_reload_success_timestamp_seconds` updates or new targets appearing in `prometheus_sd_*` metrics.

**Interventions:**
- **remediation** (root): apply scrape-side cardinality limits to every job in `prometheus.yml`.

  ```yaml
  # prometheus.yml — apply scrape-side cardinality limits to every job.
  scrape_configs:
    - job_name: my-app
      sample_limit: 50000          # tune per-job baseline + 50% headroom
      label_limit: 30              # per-sample label-count cap
      label_value_length_limit: 200
      target_limit: 1000           # cap discovered targets per job
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Verification:** Re-run Step 7; a breaching target must show `sample limit exceeded` (or similar) in `lastError`, and overall `prometheus_tsdb_head_series` must stabilise rather than continue growing. Alert on `prometheus_target_scrapes_exceeded_sample_limit_total` to catch breaches.
- **mitigation** (s1): set a permissive `sample_limit` on the dominating job to cap the immediate breach.

  ```yaml
  scrape_configs:
    - job_name: my-app
      sample_limit: 50000          # tune per job baseline + 50% headroom
      label_limit: 30              # per-sample label count cap
      label_value_length_limit: 200
      target_limit: 1000           # cap discovered targets per job
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Setting a `sample_limit` too low causes legitimate scrapes to fail with `sample limit exceeded`; the target's metrics disappear from the TSDB for the duration of the breach. Start permissive. **Duration:** Permanent. Tune the value over time based on observed `scrape_samples_post_metric_relabeling` baselines. **Verification:** Re-run Step 7; the breaching target must surface `sample limit exceeded` and head series must stabilise.

### Cause G: Missing recording rules force every dashboard query to evaluate over millions of raw series

**Statement:** Dashboards and alerts query raw high-cardinality metrics at every refresh, so memory and CPU pressure comes from query evaluation rather than from ingestion alone.

**Chain:**
- root: dashboards and alerts query raw high-cardinality metrics directly, with no pre-aggregated recording rules.
- s1: PromQL evaluation loads all selected series into memory before aggregating, so a panel running `sum by (service)(rate(http_requests_total[5m]))` over a 1M-series metric materialises 1M series per evaluation.
- s2: with 30 s auto-refresh across multiple panels and users, the query layer dominates CPU and triggers `query processing would load too many samples into memory`.
- D: memory, latency, and OOM/query failures look like a cardinality problem even when ingest is steady — the Symptom.

**Indicators:**
- root: [Symptom] dashboards driven by raw high-cardinality metrics time out while alert evaluation lags (`rule_group_iterations_missed_total` increments).
- s1: [Symptom] PromQL queries fail with `query processing would load too many samples into memory`.
- s2: [Symptom] `prometheus_engine_query_duration_seconds` p99 climbs even when `prometheus_tsdb_head_series` is flat.

**Interventions:**
- **remediation** (root): pre-aggregate the high-cardinality metrics referenced by dashboards/alerts via recording rules.

  ```yaml
  # rules/cardinality.yml — pre-aggregate the high-cardinality metrics referenced by dashboards/alerts.
  groups:
    - name: precompute
      interval: 30s
      rules:
        - record: service:http_requests:rate5m
          expr: sum by (service, status_code) (rate(http_requests_total[5m]))
        - record: service:http_request_duration:p99_5m
          expr: histogram_quantile(0.99, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))
  ```

  ```bash
  promtool check rules rules/cardinality.yml
  # Add 'rule_files: [rules/cardinality.yml]' to prometheus.yml if not already present.
  curl -X POST http://localhost:9090/-/reload
  # Point dashboards and alerts at the recorded series (service:http_requests:rate5m, service:http_request_duration:p99_5m).
  ```

  **Verification:** After 5 minutes, run `curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=service:http_requests:rate5m'`; the recording rule must return non-empty results. Dashboard p99 query duration should drop below 1 s.
- **defensive_fix** (s1): land the recording rules and migrate dashboards/alerts to the recorded series so queries read pre-aggregated data instead of millions of raw series.

  ```yaml
  # rules/cardinality.yml
  groups:
    - name: precompute
      interval: 30s
      rules:
        - record: service:http_requests:rate5m
          expr: sum by (service, status_code) (rate(http_requests_total[5m]))
        - record: service:http_request_duration:p99_5m
          expr: histogram_quantile(0.99, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))
  ```

  ```bash
  promtool check rules rules/cardinality.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Verification:** After migrating dashboards, dashboard panels read tens to hundreds of recorded series instead of millions; query latency drops by 1–2 orders of magnitude and `too many samples` errors stop.

### Cause H: Series churn from short-lived targets prevents head block compaction

**Statement:** Service discovery cycles many short-lived targets (CI runners, batch jobs, autoscaler-spawned pods) in and out, so the head block accumulates series faster than it compacts.

**Chain:**
- root: service discovery cycles many short-lived targets (CI runners, batch jobs, autoscaler pods) in and out, each scraping a few times then disappearing.
- s1: a vanished target's series remain in the head block until `--storage.tsdb.min-block-duration` (default 2h) passes, so with aggressive turnover the head holds series for many dead cohorts simultaneously.
- s2: `scrape_series_added` shows sustained churn and resident memory rises proportionally even though no single label exploded.
- D: head series climb and the head block can't compact, driving memory pressure and OOM — the Symptom.

**Indicators:**
- root: [Step 3] `instance` and `pod` labels are near the top of `labelValueCountByLabelName` and grow continuously.
- s1: [Step 4] `scrape_series_added` rate is high relative to `scrape_samples_post_metric_relabeling`, indicating new series per scrape.
- s2: [Step 8] head series shows a sawtooth pattern aligned to deployment or batch-job cycles.

**Interventions:**
- **remediation** (root): route batch-job and short-lived workload metrics through Pushgateway instead of scrape.

  ```bash
  # Route batch-job and short-lived workload metrics through Pushgateway instead of scrape.
  # Deploy Pushgateway, configure a single scrape job for it with honor_labels: true,
  # and have batch jobs POST their metrics on completion:
  echo 'job_duration_seconds 42.5' | curl --data-binary @- \
    http://pushgateway:9091/metrics/job/nightly-etl/instance/run-2026-05-12
  # Series live on Pushgateway (a single stable target) instead of accumulating per-run in the TSDB head.
  ```

  **Verification:** After 2 hours (one head-block boundary), `prometheus_tsdb_head_series` must plateau. `scrape_series_added` rate for the affected job must drop toward zero.
- **mitigation** (s1): exclude the short-lived job pods from service discovery for the affected scrape job.

  ```yaml
  # Exclude short-lived job pods from service discovery for this scrape job.
  scrape_configs:
    - job_name: my-app
      kubernetes_sd_configs:
        - role: pod
      relabel_configs:
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_component]
          regex: 'job|batch|migration|ci-runner'
          action: drop
  ```

  ```bash
  promtool check config prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

  **Risk:** Filtering short-lived targets in service discovery removes them from monitoring entirely; ensure the short-lived workload is monitored via push-based gateway or batch-job report instead. **Duration:** Permanent. Pair with Pushgateway for batch-job metrics. **Verification:** After one head-block boundary, `scrape_series_added` rate for the affected job must drop toward zero.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirm head series or memory pressure but do not isolate which metric, label, or target is responsible — Causes A–H indicators do not match the gathered evidence.

**Chain:**
- root: cardinality growth is confirmed (Step 1) but the TSDB status API and scrape-target inspection do not converge on a single dominant metric, label, or job.
- s1: the driver may be distributed across many metrics, hidden by an in-flight rule reload, or rooted in a custom exporter not covered by the controlled vocabulary above.
- D: head series and memory pressure persist with no isolated root cause — the Symptom.

**Indicators:**
- [Default] head series count and memory pressure are confirmed (Step 1) but Causes A–H indicators do not match.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, raise memory limits as a holding action, and escalate to the observability SME/vendor.

  ```bash
  # Capture diagnostic artefacts for handoff.
  curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=50' > tsdb-status.json
  curl -s 'http://localhost:9090/api/v1/targets' > targets.json
  curl -s 'http://localhost:9090/api/v1/status/config' > config.json
  curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' > head-series.json
  # If running on Kubernetes, raise memory limits temporarily as a holding action:
  kubectl set resources statefulset/prometheus -n monitoring --limits=memory=<higher-value>
  ```

  **Risk:** Increasing memory limits buys time but does not fix the cause; if the spike continues, the same OOM recurs at the new ceiling within hours. **Duration:** Hours, not days. Use only while engaging the observability team or vendor support with the captured artefacts. **Verification:** Receiving engineer acknowledges the handoff; an incident ticket is opened with all four captured artefacts (`tsdb-status.json`, `targets.json`, `config.json`, head-series timeline) attached and a named owner assigned for follow-up.

## Prevention

- Treat every new metric label as a cardinality decision in code review. Reject labels whose value space is unbounded (IDs, emails, request UUIDs, raw paths). The Prometheus instrumentation guide recommends keeping per-metric cardinality "below 10" and limiting metrics with cardinality above 100 to a handful across the system.
- Set `sample_limit`, `label_limit`, `label_value_length_limit`, and `target_limit` on every scrape job. Limits convert silent cardinality explosions into loud scrape errors that surface in `prometheus_target_scrapes_exceeded_sample_limit_total`.
- Alert on cardinality growth before memory pressure: `prometheus_tsdb_head_series > 5000000` and `rate(scrape_series_added[5m]) > 100` per job, for 10 minutes.
- Audit exporters at deployment time. kube-state-metrics: pass `--resources` and `--metric-labels-allowlist` explicitly. cAdvisor: filter via `metric_relabel_configs` to keep only the `container_*` metrics actually consumed. node_exporter: enable only the collectors used.
- Use recording rules for any expression that runs on more than a few thousand series. Migrate dashboards and alerts to the recorded series; leave raw series for ad-hoc investigation only.
- Normalise HTTP paths at the instrumentation layer to use route templates (`/users/{id}`), not raw URLs. Most modern frameworks expose the matched route via the request object.
- Run `mimirtool analyze prometheus` quarterly against Grafana dashboards and rule files; drop unused metrics with `metric_relabel_configs action: drop`.
- Track TSDB self-monitoring metrics on a dedicated dashboard: `prometheus_tsdb_head_series`, `prometheus_tsdb_head_chunks`, `scrape_series_added`, `scrape_samples_post_metric_relabeling`, `process_resident_memory_bytes`, `prometheus_engine_query_duration_seconds`, and `prometheus_target_scrapes_exceeded_sample_limit_total`.
- For very large deployments, evaluate native histograms (Prometheus 2.40+, `--enable-feature=native-histograms`) — they replace classic bucket-based histograms with a single series per label combination.

## Sources

- [Prometheus — Instrumentation Best Practices](https://prometheus.io/docs/practices/instrumentation/) — Priority 1. Cardinality threshold guidance ("below 10", investigate above 100), label design rules, why high cardinality is problematic.
- [Prometheus — Metric and Label Naming](https://prometheus.io/docs/practices/naming/) — Priority 1. Explicit warning against labels with unbounded value spaces (user IDs, emails); naming conventions.
- [Prometheus — HTTP API: TSDB Status](https://prometheus.io/docs/prometheus/latest/querying/api/) — Priority 1. `/api/v1/status/tsdb` endpoint; `seriesCountByMetricName`, `labelValueCountByLabelName`, `memoryInBytesByLabelName`, `seriesCountByLabelPair` semantics.
- [Prometheus — Configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/) — Priority 1. `metric_relabel_configs` action vocabulary (drop, keep, labeldrop, labelkeep, replace); `sample_limit`, `label_limit`, `label_value_length_limit`, `target_limit` semantics.
- [Prometheus — Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Priority 1. Pre-aggregation pattern, `limit` per rule group, `promtool check rules` validation, SIGHUP reload.
- [Prometheus — Storage](https://prometheus.io/docs/prometheus/latest/storage/) — Priority 1. TSDB head block, WAL, ~1–2 bytes/sample compressed cost, why reducing series count is more effective than reducing scrape interval.
- [Grafana Labs — Managing High Cardinality in Prometheus and Kubernetes](https://grafana.com/blog/2024/06/05/how-to-manage-high-cardinality-metrics-in-prometheus-and-kubernetes/) — Priority 2. Histogram bucket-drop pattern, recording-rule aggregation idiom, mimirtool for unused metrics, scrape-interval-vs-series-count trade-off.
- [Grafana Labs — What Are Cardinality Spikes](https://grafana.com/blog/2022/02/15/what-are-cardinality-spikes-and-why-do-they-matter/) — Priority 2. `user_id` as canonical anti-pattern, active-series semantics, cardinality-management tooling references.
