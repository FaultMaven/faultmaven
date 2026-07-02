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
- Evidence and investigation lifecycle metrics — see
  [Evidence Metrics](./evidence-metrics.md) and
  [Lifecycle Metrics](./lifecycle-metrics.md)

## Overview

| Document | Description |
|----------|-------------|
| [Architecture](./architecture.md) | Logging system design |
| [Configuration](./configuration.md) | Setup and configuration |
| [Logging Policy](./logging-policy.md) | Standards and policies |
| [LLM Cost & Token Observability](./llm-cost-observability.md) | LLM spend metrics, price table, and cost-spike diagnosis |

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
