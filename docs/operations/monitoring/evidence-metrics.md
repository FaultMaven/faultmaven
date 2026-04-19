# Evidence Failure-Mode Metrics & Alerts

**Status:** Active. Metric definitions landed per the M2 monitoring scaffolding (plan file deleted 2026-04-19 after landing).

Covers the Prometheus metrics and Grafana/Prometheus alert rules for the
evidence-handling failure surface: content-hash deduplication, orphan-file
cleanup, and async turn-retry recovery.

Alert rules live **outside this repository** — in the Grafana / Prometheus
config maintained by the infrastructure team. This document is the canonical
definition; treat any divergence in the infra repo as a bug to reconcile.

## Metrics

All metrics are defined in
[`faultmaven/infrastructure/observability/evidence_metrics.py`](../../../faultmaven/infrastructure/observability/evidence_metrics.py).
Names follow the `faultmaven_` prefix convention shared with
`tracing.py` so they scrape into the same namespace.

| Metric | Type | Labels | Source | Status |
| --- | --- | --- | --- | --- |
| `faultmaven_evidence_dedup_hits_total` | counter | — | `InvestigationService._preprocess_attachment` | **Live** |
| `faultmaven_evidence_orphan_files_found_total` | counter | — | `faultmaven.modules.agent.jobs.storage_cleanup` | **Live** |
| `faultmaven_evidence_orphan_files_deleted_total` | counter | — | `faultmaven.modules.agent.jobs.storage_cleanup` | **Live** |
| `faultmaven_evidence_turn_async_retry_enqueued_total` | counter | `reason` | Turn retry path (async-turn-retry plan, deferred) | Scaffolded only; no emit sites (async retry plan deferred 2026-04-19) |
| `faultmaven_evidence_turn_async_retry_outcome_total` | counter | `outcome` | Turn retry path (async-turn-retry plan, deferred) | Scaffolded only; no emit sites |
| `faultmaven_evidence_turn_async_retry_latency_seconds` | histogram | — | Turn retry path (async-turn-retry plan, deferred) | Scaffolded only; no emit sites |

**Label values:**

- `async_retry_enqueued_total{reason}`: `timeout | 5xx | rate_limit | network_error | other`
- `async_retry_outcome_total{outcome}`: `success | failure | timeout | superseded | cancelled`

## Alert rules

### `evidence_orphan_file_rate_high`

**Fires when:** more than 10 files get marked as orphaned within any 1-hour
window. Signals a bug in the link/store path — files are being stored but
never linked to an Evidence row.

```promql
# Grafana / Prometheus Alert Rule (canonical definition)
- alert: evidence_orphan_file_rate_high
  expr: increase(faultmaven_evidence_orphan_files_found_total[1h]) > 10
  for: 15m
  labels:
    severity: warning
    team: platform
  annotations:
    summary: "Orphan file rate high — {{ $value }} orphans in the last hour"
    description: |
      More than 10 files were found orphaned (past TTL with linked=False).
      This indicates files are being stored but Evidence rows aren't being
      created / linked. Check `faultmaven/modules/agent/domain/services/
      investigation_service.py::_preprocess_attachment` for errors between
      `store_file` and Evidence persistence.
    runbook_url: "https://docs.faultmaven.internal/runbooks/orphan-files"
```

### `evidence_turn_terminal_failure`

**Fires when:** any turn retry exhausts max attempts and lands in the
`failed` terminal state. Needs on-call review — a user-visible failure.

```promql
- alert: evidence_turn_terminal_failure
  expr: increase(faultmaven_evidence_turn_async_retry_outcome_total{outcome="failure"}[5m]) > 0
  for: 1m
  labels:
    severity: critical
    team: platform
  annotations:
    summary: "Turn retry terminally failed — {{ $value }} in the last 5 minutes"
    description: |
      A turn-retry job exceeded its max-attempts budget. The user saw a
      user-actionable error. Investigate the LLM-provider status and the
      retry backoff schedule.
    runbook_url: "https://docs.faultmaven.internal/runbooks/async-turn-retry"
```

### `evidence_dedup_rate_unexpected` (optional, tunable)

**Fires when:** dedup hit rate deviates materially from the modeled baseline
(~5% of attachments). Either a sudden spike (users re-uploading the same
files en masse — UX problem in the upload flow) or a flat-zero (dedup broken
and silently no-oping).

```promql
# Example: warn if 1-hour dedup rate exceeds 20% or drops below 1% after having
# been non-zero in the previous 24h. Tune thresholds after 2 weeks of baseline.
- alert: evidence_dedup_rate_spike
  expr: |
    rate(faultmaven_evidence_dedup_hits_total[1h])
      / rate(faultmaven_requests_total{endpoint=~"/cases/.*/turns"}[1h])
    > 0.20
  for: 30m
  labels:
    severity: info
    team: platform
  annotations:
    summary: "Dedup hit rate {{ $value | humanizePercentage }} exceeds 20%"
    description: |
      Users may be re-uploading the same files repeatedly. Investigate the
      upload UX — is there a failure mode where the user thinks an upload
      didn't happen and retries?
```

## Dashboards

Not yet built. Suggested panels for a future Grafana dashboard:

1. **Dedup hit rate** — `rate(faultmaven_evidence_dedup_hits_total[5m])`
2. **Orphan file backlog** — `faultmaven_evidence_orphan_files_found_total - faultmaven_evidence_orphan_files_deleted_total` (gauge-style running delta)
3. **Turn retry latency** — `histogram_quantile(0.95, faultmaven_evidence_turn_async_retry_latency_seconds)`
4. **Retry outcome breakdown** — stacked bar of `async_retry_outcome_total` by `outcome` label

## Testing

Metric registration is exercised by
[`tests/unit/infrastructure/observability/test_evidence_metrics.py`](../../../tests/unit/infrastructure/observability/test_evidence_metrics.py).
Emission is covered for `evidence_dedup_hits_total`; the rest will be
covered by the M1 (orphan cleanup) and async-turn-retry test suites as those
plans implement their emission sites.

Alert rules cannot be tested from this repository — they live in the infra
repo. When you change a rule here, file a linked PR there.
