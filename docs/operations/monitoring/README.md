# Monitoring

Logging, observability, and metrics documentation.

## Prometheus metrics

`/metrics` is mounted when `METRICS_EXPORTER=prometheus_http` (recording also
requires `ENABLE_METRICS=true`). Key first-party metric families:

- `http_requests_total` / `http_request_duration_seconds` — HTTP traffic
- `llm_requests_total{provider,model,status}` (status: success/error/cached),
  `llm_request_duration_seconds`, `llm_tokens_total{provider,model}` — per-route
  outcome and the winning response's total tokens, recorded by the LLM router
- `llm_cost_usd_total{provider,model}` (estimated USD spend),
  `llm_call_tokens_total{provider,model,token_type}` (input/output/cache_read/
  cache_write), `llm_provider_calls_total{provider,model,outcome}` (kept vs
  low_confidence), `llm_unpriced_calls_total{provider,model}` — per **billed
  provider call**, recorded at the registry chokepoint so fallback/retry spend
  is counted (not just the winner). See
  [LLM Cost & Token Observability](./llm-cost-observability.md) for the price
  table, the `LLM_PRICING_OVERRIDES` env override, the structured `llm_call` /
  `turn_token_spend` logs, and a cost-spike diagnosis playbook.
- `sla_status{component}` (3=meeting, 2=at_risk, 1=breached, 0=unknown),
  `sla_availability_ratio`, `sla_response_time_p95_seconds`,
  `sla_error_rate_ratio`, `sla_active_breaches` — SLA tracker gauges, recomputed
  at every scrape from real request observations (alert on `sla_status < 3`);
  same data source as `/health/sla`
- `component_health_status{component,fatal,fails_per_replica}`
  (3=healthy, 2=degraded, 1=unhealthy, 0=unknown) — dependency health, and
  **the only surface a dependency outage can page from**. See
  [Alerting on component health](#alerting-on-component-health) below
- Evidence and investigation lifecycle metrics — see
  [Evidence Metrics](./evidence-metrics.md) and
  [Lifecycle Metrics](./lifecycle-metrics.md)
- `faultmaven_tool_result_relayed_total{tool}`,
  `faultmaven_tool_result_truncated_total{tool}`,
  `faultmaven_tool_result_chars{tool}` — what the investigation tool loop relays into the
  model's context and what the `TOOL_RESULT_MAX_CHARS` cap cuts off, per tool.
  See [Tool-Result Context Budget](./tool-result-budget.md) for the clip-rate
  query and what raising the cap would actually cost.

## Alerting on component health

`/health` reports a dependency outage honestly and **always answers 200**: it
is the liveness surface, and a 503 there restarts pods that restarting cannot
fix. So no Kubernetes probe acts on it, and nothing else was reading it either
— the verdict was correct and unconsumed (#1547). `component_health_status` is
the machine-readable copy of that verdict, published from the same computation
that grades the body, so the two cannot disagree.

Both classifications travel as **labels**, so a rule selects the set instead of
naming components — the fatal set is data, and a rule that hardcodes `database`
goes stale the moment the set changes:

| Label | Means |
|-------|-------|
| `fatal="true"` | the process cannot usefully serve without this component |
| `fails_per_replica="true"` | it can fail for one pod while its siblings keep serving |

The page-a-human set is `fatal` **and not** `fails_per_replica`: down for every
replica at once, so no readiness probe can shed around it.

```promql
# A shared dependency is down fleet-wide. Nothing automatic will fix this.
max by (component) (
  component_health_status{fatal="true", fails_per_replica="false"}
) == 1
```

The rule that evaluates this lives in `faultmaven-enterprise-infra`, not here.
Notes for whoever reads or changes it:

- **`max by (component)`, not `min`.** Higher is healthier, so the maximum
  across replicas is the best any pod can reach: `== 1` means no replica can
  reach the dependency, which is what "down for the fleet" has to mean before
  it wakes anyone.
- **Alert on `== 1`, not `< 3`.** `0` is UNKNOWN — "we could not determine it",
  which is also every component's value in a process that has been scraped but
  never had `/health` called. `2` is DEGRADED, which still serves (the database
  reports it for an RLS-bypassing role).
- **Every component is exported, healthy ones included**, so absence of a
  series means "not scraped", never "fine".
- **Where the rule is.** `kubernetes/platform/monitoring/rules/faultmaven-workload-rules.yaml`,
  group `faultmaven.workload.dependencies`, alert `FaultMavenSharedDependencyDown`,
  with promtool unit tests in `rules/tests/` — added by
  `faultmaven-enterprise-infra#326`. Until that lands and is applied, the
  expression above is a specification and nothing evaluates it. Note also that
  `kubernetes/platform/**` is on no CD path in that repo: merging the rule does
  not apply it.
- Values are refreshed at scrape from the last probe sweep; the liveness probe
  runs that sweep far more often than the scrape interval.
- ‼ **A failed publish leaves the series FROZEN, not missing — and that is
  the dangerous one.** If the metrics export refuses a write, the gauge child
  keeps whatever value it last held; only the narrow label-flip case (a
  component re-registering with different declarations) removes it. Measured
  against a real `prometheus_client` registry: publish `database` HEALTHY,
  then flip it to UNHEALTHY while the registry refuses every write, and a
  scrape still reports `component_health_status{component="database"} == 3`.
  So the page rule above **does not fire** during a fatal outage, and there is
  no gap in the series to notice. Do not go looking for one.

  What to look for instead: the API logs
  `component_health_status is not being published for <component>` at WARNING —
  immediately the first time, then at most once per component per five
  minutes, carrying `(N further failures suppressed)` so the throttle cannot
  hide the scale. When it clears, `component_health_status publishing
  recovered for <component> after N failed publish(es)` closes it, also at
  WARNING, so a deployment that emits one emits the other. Between those two
  lines, treat every value of this metric as unverified (#1568).

## Overview

| Document | Description |
|----------|-------------|
| [Architecture](./architecture.md) | Logging system design |
| [Configuration](./configuration.md) | Setup and configuration |
| [Logging Policy](./logging-policy.md) | Standards and policies |
| [LLM Cost & Token Observability](./llm-cost-observability.md) | LLM spend metrics, price table, and cost-spike diagnosis |
| [Tool-Result Context Budget](./tool-result-budget.md) | Per-tool truncation clip rate and size distribution for the investigation tool loop |

## Guides

| Document | Description |
|----------|-------------|
| [Implementation Guide](./implementation-guide.md) | How to implement logging |
| [Developer Guide](./developer-guide.md) | Logging for developers |
| [Testing Guide](./testing-guide.md) | Testing logging functionality |
| [Operations Runbook](./operations-runbook.md) | Operational procedures |

## Quick Links

- **[Architecture](../architecture/)** - System design
- **[Security](../security/)** - Security documentation
- **[Runbooks](../runbooks/)** - Troubleshooting guides
