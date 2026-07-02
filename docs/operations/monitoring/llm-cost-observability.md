# LLM Cost & Token Observability

FaultMaven meters **every billed LLM call** so a token-cost spike is
attributable in minutes instead of guessed at. Metering happens at one
chokepoint — the provider registry — so it captures the calls a naive
router-level hook would miss: **fallback-chain attempts and provider retries**,
which are the most common cause of a runaway bill.

## What is emitted

### Prometheus metrics (`GET /metrics`, requires `ENABLE_METRICS=true`)

| Metric | Labels | Meaning |
|---|---|---|
| `llm_cost_usd_total` | `provider`, `model` | Estimated USD spend per **provider API call** (from the price table). The dollar figure to alert on. |
| `llm_call_tokens_total` | `provider`, `model`, `token_type` | Tokens per API call, split into `input` / `output` / `cache_read` / `cache_write`. Buckets are disjoint. |
| `llm_provider_calls_total` | `provider`, `model`, `outcome` | API calls by disposition. `outcome=kept` = returned to caller; `outcome=low_confidence` = **billed then discarded** by the fallback chain (pure waste). |
| `llm_unpriced_calls_total` | `provider`, `model` | Calls whose `(provider, model)` had no price entry. **Non-zero ⇒ `llm_cost_usd_total` under-reports** — add the model to the price table. |

These sit alongside the pre-existing `llm_requests_total` (per-route outcome,
including `status="cached"` for local SemanticCache hits), `llm_latency`, and
`llm_tokens_total`. Note the deliberate difference in basis:
`llm_tokens_total` counts the *winning* response per route, while
`llm_call_tokens_total` counts *every* API call — `sum(llm_call_tokens_total) ≥
llm_tokens_total`, and the gap is the fallback overhead.

> **Cardinality:** only bounded labels are used. `case_id` / `user_id` /
> `request_id` are never metric labels — they appear only in the structured
> logs below.

### Structured logs

- **`llm_call`** — one per billed provider call: `provider`, `model`,
  `outcome`, the four token buckets, `total_tokens`, `prompt_cache_hit`,
  `estimated_cost_usd`, `cost_priced`, `latency_ms`. Per-call forensics.
- **`turn_token_spend`** — one per investigation turn: `case_id`, per-bucket
  totals, `total_tokens`, `total_calls`, `estimated_cost_usd`, `unpriced_calls`.
  This is the per-turn amplification signal — a turn making 40 calls stands out
  immediately.

### Opik spans

Each LLM span carries `usage` (`prompt_tokens` / `completion_tokens` /
`total_tokens`) plus `prompt_cache_hit` and cache-token metadata.

## The price table

Rates live in `faultmaven/infrastructure/llm/pricing.py` as
**operator-maintainable estimates** — provider prices drift, so treat the
dollar figures as directional. Two honesty guarantees:

- **Override without a code change:** set `LLM_PRICING_OVERRIDES` to a JSON
  object shaped like the table, e.g.
  `{"anthropic": {"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}}}`.
  Rates are per 1M tokens.
- **Unknown models are flagged, not guessed:** an unpriced `(provider, model)`
  contributes `0` to `llm_cost_usd_total` and increments
  `llm_unpriced_calls_total`, so a missing entry shows up as visible
  under-counting rather than a silently-wrong dollar figure. Self-hosted
  providers (`local`, `huggingface`) are priced at `$0` (known-free), not
  unpriced.

## Prompt caching

The tool-augmented investigation loop marks its calls cacheable
(`cache_prompt=True`). Only the **Anthropic** provider acts on it — it adds an
ephemeral (5-minute) `cache_control` breakpoint on the stable system + tools
prefix, so that prefix bills at the reduced cache-read rate across the loop's
iterations. Every other provider pops the flag (OpenAI-family cache prompts
automatically server-side; the flag must never leak into a request body or it
400s). Caching is transparent to model output — it changes only how the prefix
is billed. `cache_read` tokens are visible in `llm_call_tokens_total` and
`prompt_cache_hit` in the logs, so you can confirm cache hits are actually
landing.

## Diagnosing a cost spike

1. **Is it dollars or just tokens?** Graph `rate(llm_cost_usd_total[5m])` by
   `provider`,`model`. If it's flat but `llm_unpriced_calls_total` is climbing,
   the spend is real but unpriced — add the model to the table.
2. **Fallback amplification?** Compare `llm_provider_calls_total{outcome="low_confidence"}`
   against `{outcome="kept"}`. A high low-confidence ratio means the fallback
   chain is paying for discarded attempts — tune `confidence_threshold` or the
   chain order.
3. **Which turns?** Sort `turn_token_spend` logs by `total_calls` /
   `estimated_cost_usd`. A turn with an outlier `total_calls` points at a
   per-turn amplifier (tool-loop iterations, per-cause fan-out, retries).
4. **Is caching working?** Check `prompt_cache_hit` in `llm_call` logs and the
   `cache_read` series. Zero cache reads on Anthropic tool-loop turns means the
   prefix isn't being reused (e.g. turns > 5 min apart, so the ephemeral cache
   expired).

## Coverage note

Metering is at the registry chokepoint, so it covers all router-routed calls
(the default, and every capability-override path). A dedicated concrete DA
provider (`DA_PROVIDER` set) bypasses the registry on the tool-loop path; that
one call site meters itself directly, so DA-turn spend is still counted.
