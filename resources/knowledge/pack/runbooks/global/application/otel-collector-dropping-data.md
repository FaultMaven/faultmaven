---
id: "otel-collector-dropping-data"
title: "OpenTelemetry Collector dropping spans and metrics"
domain: application
service: opentelemetry
symptom_class: [data_loss, throughput_degradation]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [memory-limiter, sending-queue, batch-processor, send-failed-spans, refused-spans, backpressure]
difficulty: advanced
---

## Symptom Recognition

- Backend shows gaps: spans/metrics arriving at a lower rate than emitted by instrumented services.
- Collector log lines (debug/info):
  - `Dropping data because sending_queue is full. Try increasing queue_size.`
  - `data refused due to high memory usage` (from the `memory_limiter` processor)
  - `Exporting failed. Rejecting data.` / `context deadline exceeded`
  - `Exporting failed. No more retries left. Dropping data.`
- Internal telemetry on `:8888/metrics` shows nonzero/growing counters:
  - `otelcol_exporter_send_failed_spans`, `otelcol_exporter_send_failed_metric_points`
  - `otelcol_exporter_enqueue_failed_spans`
  - `otelcol_processor_refused_spans`, `otelcol_processor_refused_metric_points`
  - `otelcol_receiver_refused_spans`
  - `otelcol_exporter_queue_size` pinned at `otelcol_exporter_queue_capacity`

## Applicability

- OpenTelemetry Collector (core or contrib) v0.86.0+; config syntax current as of v0.100.0+.
- Read access to the Collector host/pod, its config file, and its stdout/stderr logs.
- Network reach to the Collector internal telemetry endpoint (default `127.0.0.1:8888`); set `host: '0.0.0.0'` when scraping from outside the pod.
- Tools: `curl`, `kubectl`/`docker` (for log access), a Prometheus or `grep`-able scrape of `:8888`.

## Diagnostic Steps

### Step 1: Scrape the Collector internal telemetry endpoint

```bash
curl -s http://127.0.0.1:8888/metrics \
  | grep -E 'otelcol_(exporter_send_failed|exporter_enqueue_failed|processor_refused|receiver_refused)'
```

Expected output: Prometheus counter lines. On a healthy Collector these stay at `0`; any growing value localizes the drop to the exporter (send/enqueue) or a processor/receiver (refused).

### Step 2: Read exporter queue depth vs capacity

```bash
curl -s http://127.0.0.1:8888/metrics \
  | grep -E 'otelcol_exporter_queue_(size|capacity)'
```

Expected output: two gauges, e.g. `otelcol_exporter_queue_size 1000` and `otelcol_exporter_queue_capacity 1000`. `size` riding at `capacity` means the sending queue is saturated.

### Step 3: Search Collector logs for drop/refuse reasons

```bash
kubectl logs deploy/otel-collector --tail=500 2>/dev/null || docker logs --tail 500 otel-collector
```

Expected output: explicit reason lines such as `sending_queue is full`, `data refused due to high memory usage`, or `No more retries left. Dropping data.`

### Step 4: Inspect the running pipeline config

```bash
grep -nE 'memory_limiter|batch|sending_queue|retry_on_failure|limit_mib|queue_size|send_batch_size|timeout' \
  /etc/otelcol/config.yaml
```

Expected output: the configured processor/exporter knobs, or their absence (e.g. no `sending_queue` block, no `memory_limiter` in the `service.pipelines` list).

### Step 5: Confirm backend reachability via the debug exporter and zpages

```bash
# Temporarily add a debug exporter (verbosity: detailed) and watch:
kubectl logs deploy/otel-collector -f 2>/dev/null | grep -i 'TracesExporter\|MetricsExporter'
# And inspect live span/error state:
curl -s http://127.0.0.1:55679/debug/tracez | head -40
```

Expected output: debug exporter prints payloads if data reaches the pipeline; `/debug/tracez` lists active/error spans. Connection-refused on `:55679` means the `zpages` extension is not enabled.

## Causes

### Cause A: Exporter sending queue saturated by a slow or unreachable backend
**Statement:** The downstream backend cannot keep up with (or is unreachable for) the export rate, so the exporter's `sending_queue` fills to `queue_size` and every further item is dropped before it ever reaches retry logic.
**Chain:**
- root: backend export throughput is lower than ingest rate (slow/erroring/unreachable backend)
- s1: in-flight items accumulate in the exporter sending queue
- s2: queue depth reaches `queue_size`, new items cannot be enqueued
- D: spans/metrics dropped at the exporter (data_loss)
**Indicators:**
- root: [Step 1] `otelcol_exporter_send_failed_spans` / `_metric_points` growing, indicating export attempts are failing or timing out
  <!-- match: {"step": 1, "predicate": "contains", "target": "otelcol_exporter_send_failed_spans"} -->
- s2: [Step 2] `otelcol_exporter_queue_size` equals `otelcol_exporter_queue_capacity`
- s2: [Step 1] `otelcol_exporter_enqueue_failed_spans` is nonzero and increasing
  <!-- match: {"step": 1, "predicate": "contains", "target": "otelcol_exporter_enqueue_failed_spans"} -->
- s2: [Step 3] log line `Dropping data because sending_queue is full`
  <!-- match: {"step": 3, "predicate": "contains", "target": "sending_queue is full"} -->
**Interventions:**
- **remediation** (root): Restore backend throughput — fix the backend outage, raise its ingest quota, or scale the gateway Collector tier so export keeps pace with ingest.
  ```bash
  curl -so /dev/null -w '%{http_code} %{time_total}s\n' https://<backend-endpoint>/v1/traces
  ```
  **Verification:** Re-run Step 1; `otelcol_exporter_send_failed_spans` stops increasing and Step 2 shows `queue_size` draining below capacity.
- **defensive_fix** (s1): Enlarge the queue and add durable retry so transient backend slowness is absorbed instead of dropped.

  ```yaml
  exporters:
    otlp:
      endpoint: <backend-endpoint>
      sending_queue:
        enabled: true
        num_consumers: 10
        queue_size: 5000
      retry_on_failure:
        enabled: true
        initial_interval: 5s
        max_interval: 30s
        max_elapsed_time: 300s
  ```

  **Verification:** Re-run Step 2 after a traffic spike; `queue_size` rises but stays under capacity and `otelcol_exporter_enqueue_failed_spans` (Step 1) stays at 0.
- **mitigation** (s1): Add a `file_storage` persistent queue so buffered data survives restarts during a backend outage.

  ```yaml
  extensions:
    file_storage/otc:
      directory: /var/lib/otelcol/queue
  exporters:
    otlp:
      sending_queue:
        storage: file_storage/otc
  ```

  **Risk:** Consumes disk; an unbounded persistent queue can fill the volume. **Duration:** Until the backend recovers (hours). **Verification:** Files appear under `/var/lib/otelcol/queue` and drain once Step 1 send-failures return to 0.

### Cause B: memory_limiter refusing data under memory pressure
**Statement:** Collector memory usage crosses the `memory_limiter` soft/hard threshold (`limit_mib` minus `spike_limit_mib`), so the processor refuses incoming batches to avoid an OOM, dropping them at the front of the pipeline.
**Chain:**
- root: Collector RSS exceeds the `memory_limiter` threshold (under-provisioned memory or undersized limit vs traffic)
- s1: `memory_limiter` enters refusing state on its `check_interval`
- s2: receivers/processors get refused responses and shed incoming data
- D: spans/metrics refused before reaching the exporter (data_loss)
**Indicators:**
- s2: [Step 1] `otelcol_processor_refused_spans` / `_metric_points` increasing
  <!-- match: {"step": 1, "predicate": "contains", "target": "otelcol_processor_refused_spans"} -->
- s1: [Step 3] log line `data refused due to high memory usage`
  <!-- match: {"step": 3, "predicate": "contains", "target": "high memory usage"} -->
- root: [Step 4] `memory_limiter` present with a `limit_mib` at/below the container memory request
  <!-- match: {"step": 4, "predicate": "contains", "target": "limit_mib"} -->
**Interventions:**
- **remediation** (root): Raise the container/pod memory and align `limit_mib` to ~80% of it (keep `spike_limit_mib` ≈ 25% of `limit_mib`); add `batch` upstream of the exporter to cut per-item overhead.

  ```yaml
  processors:
    memory_limiter:
      check_interval: 1s
      limit_mib: 4000
      spike_limit_mib: 1000
  ```

  **Verification:** Re-run Step 1; `otelcol_processor_refused_spans` stops increasing under steady load.
- **mitigation** (s1): Scale out the Collector (add replicas / shard load) so per-instance memory pressure drops immediately.

  ```bash
  kubectl scale deploy/otel-collector --replicas=4
  ```

  **Risk:** More replicas increase backend connection count and cost; may shift backpressure to the backend (see Cause A). **Duration:** Until vertical sizing is corrected. **Verification:** Step 3 stops logging `high memory usage` and Step 1 refused counters flatten.

### Cause C: Batch processor backpressure from an undersized or slow exporter
**Statement:** The `batch` processor blocks on a downstream exporter whose timeout/queue cannot drain batches fast enough, so batches time out or back up until upstream data is dropped.
**Chain:**
- root: exporter drains batches slower than `batch` produces them (high `timeout`/small queue vs ingest rate)
- s1: batches accumulate and backpressure propagates upstream through the pipeline
- s2: receivers refuse or exporter enqueue fails as buffers fill
- D: spans/metrics dropped under sustained throughput (throughput_degradation)
**Indicators:**
- s2: [Step 1] `otelcol_receiver_refused_spans` increasing alongside `otelcol_exporter_send_failed_spans`
  <!-- match: {"step": 1, "predicate": "contains", "target": "otelcol_receiver_refused_spans"} -->
- root: [Step 4] `batch` processor with a large `timeout` or missing `send_batch_max_size` cap
  <!-- match: {"step": 4, "predicate": "contains", "target": "send_batch_size"} -->
- s1: [Step 5] `/debug/tracez` shows long-running/error export spans backing up
**Interventions:**
- **defensive_fix** (root): Tune the `batch` processor — cap `send_batch_max_size` and shorten `timeout` so batches flush at a steady, exporter-digestible rate.

  ```yaml
  processors:
    batch:
      timeout: 5s
      send_batch_size: 8192
      send_batch_max_size: 10000
  ```

  **Verification:** Re-run Step 1; `otelcol_receiver_refused_spans` flattens and `otelcol_processor_batch_batch_send_size` reports batches near `send_batch_size`.
- **remediation** (root): Increase exporter parallelism so the downstream drains batches faster than they form.

  ```yaml
  exporters:
    otlp:
      sending_queue:
        num_consumers: 20
        queue_size: 5000
  ```

  **Verification:** Step 2 shows `otelcol_exporter_queue_size` staying well below capacity under peak load.

### Cause D: Pipeline misconfiguration — exporter/processor not wired into the service pipeline
**Statement:** A receiver, processor, or exporter is defined in the config but omitted from the relevant `service.pipelines` list (or the data type routes to the wrong pipeline), so matching telemetry is silently never exported.
**Chain:**
- root: target component is absent from the `service.pipelines.<traces|metrics>` chain
- s1: telemetry enters via the receiver but is never routed to the intended exporter
- D: that signal type never reaches the backend (data_loss)
**Indicators:**
- root: [Step 4] exporter/processor defined under `exporters:`/`processors:` but missing from the pipeline's `exporters:`/`processors:` list
  <!-- match: {"step": 4, "predicate": "absent", "target": "service.pipelines"} -->
- s1: [Step 1] `otelcol_receiver_accepted_spans` increases while `otelcol_exporter_sent_spans` for that signal stays flat
- s1: [Step 5] debug exporter prints nothing for the affected signal type
**Interventions:**
- **remediation** (root): Wire the component into the correct pipeline and restart the Collector.

  ```yaml
  service:
    pipelines:
      traces:
        receivers: [otlp]
        processors: [memory_limiter, batch]
        exporters: [otlp]
  ```

  **Verification:** Re-run Step 1; `otelcol_exporter_sent_spans` for the signal rises in step with `otelcol_receiver_accepted_spans`.

### Cause Z: Unidentified
**Statement:** None of the above roots match; the drop mechanism is not yet localized to exporter queue, memory_limiter, batch backpressure, or pipeline wiring.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the observability/SRE SME.

  ```bash
  ts=$(date +%s)
  curl -s http://127.0.0.1:8888/metrics > "/tmp/otelcol-metrics-$ts.txt"
  curl -s http://127.0.0.1:55679/debug/tracez > "/tmp/otelcol-tracez-$ts.txt"
  curl -s http://127.0.0.1:55679/debug/pipelinez > "/tmp/otelcol-pipelinez-$ts.txt"
  cp /etc/otelcol/config.yaml "/tmp/otelcol-config-$ts.yaml"
  (kubectl logs deploy/otel-collector --tail=2000 || docker logs --tail 2000 otel-collector) \
    > "/tmp/otelcol-logs-$ts.txt" 2>&1
  ```

  **Risk:** Read-only capture; safe. **Duration:** N/A. **Verification:** Snapshot files exist and are attached to the escalation ticket.

## Prevention

- Set `service.telemetry.metrics` to export `:8888` to Prometheus and alert on any increase in `otelcol_exporter_send_failed_*`, `otelcol_exporter_enqueue_failed_*`, and `otelcol_processor_refused_*`.
- Alert when `otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.8` for 5m (early backpressure warning).
- Always include `memory_limiter` as the FIRST processor in every pipeline, sized to ~80% of the container memory limit, with `batch` immediately after it.
- Set explicit `sending_queue.queue_size` and `retry_on_failure` on every exporter; for outage tolerance, back the queue with `file_storage` persistence.
- Enable the `zpages` extension (`:55679`) in non-prod and a `debug` exporter behind a flag for fast triage.
- Run a gateway Collector tier with horizontal autoscaling so backend-side backpressure is absorbed by added replicas, not by drops.

## Sources

- [Troubleshooting](https://opentelemetry.io/docs/collector/troubleshooting/) — "Collector is dropping data" guidance; `memory_limiter` as a refusal cause; `debug` exporter `verbosity: detailed`; `zpages` extension on `:55679` with `/debug/tracez`.
- [Internal telemetry](https://opentelemetry.io/docs/collector/internal-telemetry/) — exact `:8888` metric names (`otelcol_exporter_send_failed_spans`, `otelcol_exporter_send_failed_metric_points`, `otelcol_exporter_sent_spans`, `otelcol_exporter_queue_size`, `otelcol_exporter_queue_capacity`, `otelcol_exporter_enqueue_failed_spans`, `otelcol_receiver_refused_spans`, `otelcol_receiver_accepted_spans`, `otelcol_processor_batch_batch_send_size`) and the prometheus self-telemetry config (default `127.0.0.1:8888`).
- [Configuration](https://opentelemetry.io/docs/collector/configuration/) — `memory_limiter` example (`check_interval`, `limit_mib`, `spike_limit_mib`); pipeline/`service.pipelines` wiring.
- [README.md](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md) — `sending_queue` (`enabled`, `num_consumers` default 10, `queue_size` default 1000), `retry_on_failure` (`enabled`, `initial_interval` 5s, `max_interval` 30s, `max_elapsed_time` 300s, `multiplier`), queue-full drop behavior, `file_storage` persistent queue.
- [Scaling](https://opentelemetry.io/docs/collector/scaling/) — `otelcol_processor_refused_*` semantics under `memory_limiter` and scale-out guidance.
