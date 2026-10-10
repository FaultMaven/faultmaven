# LLM Cost & Token Observability

FaultMaven meters **every billed LLM call** so a token-cost spike is
attributable in minutes instead of guessed at. Metering happens at one
chokepoint — the provider registry — so it captures the calls a naive
router-level hook would miss: **fallback-chain attempts and provider retries**,
which are the most common cause of a runaway bill.

## What is emitted

### Prometheus metrics (`GET /metrics`)

> Requires **both** `ENABLE_METRICS=true` (collect — off by default, in
> `shims/metrics.py`) **and** `METRICS_EXPORTER=prometheus_http` (expose the
> `/metrics` endpoint, in `main.py`). With only the first, metrics are recorded
> but not scrapeable; with only the second, the endpoint serves no LLM series.

| Metric | Labels | Meaning |
|---|---|---|
| `llm_cost_usd_total` | `provider`, `model` | Estimated USD spend per **provider API call** (from the price table). The dollar figure to alert on. |
| `llm_call_tokens_total` | `provider`, `model`, `token_type` | Tokens per API call, split into `input` / `output` / `cache_read` / `cache_write`. Buckets are disjoint. |
| `llm_provider_calls_total` | `provider`, `model`, `outcome` | API calls by disposition. `outcome=kept` = returned to caller; `outcome=low_confidence` = **billed then discarded** by the fallback chain (pure waste). |
| `llm_unpriced_calls_total` | `provider`, `model` | Calls whose `(provider, model)` had no price entry. **Non-zero ⇒ `llm_cost_usd_total` under-reports** — add the model to the price table. |
| `llm_usage_unpersisted_calls_total` | `reason` | Billed calls the [usage ledger](#the-usage-ledger) did not persist. **Non-zero ⇒ the ledger under-reports** by exactly this many calls. |

These sit alongside the pre-existing `llm_requests_total` (per-route outcome,
including `status="cached"` for local `LLMResponseCache` hits), `llm_latency`, and
`llm_tokens_total`. Note the deliberate difference in basis:
`llm_tokens_total` counts the *winning* response per route, while
`llm_call_tokens_total` counts *every* API call — `sum(llm_call_tokens_total) ≥
llm_tokens_total`, and the gap is the fallback overhead.

> **Cardinality:** only bounded labels are used. `case_id` / `user_id` /
> `request_id` are never metric labels — they appear only in the structured
> logs below.

### Structured logs

- **`llm_call`** (DEBUG) — one per billed provider call: `provider`, `model`,
  `outcome`, the four token buckets, `total_tokens`, `prompt_cache_hit`,
  `estimated_cost_usd`, `cost_priced`, `latency_ms`. Per-call forensics —
  logged at DEBUG (it fires on every call); enable DEBUG to see it.
- **`turn_token_spend`** — one per investigation turn: `case_id`, per-bucket
  totals, `total_tokens`, `spend_weighted_tokens`, `total_calls`,
  `estimated_cost_usd`, `unpriced_calls`. This is the per-turn amplification
  signal — a turn making 40 calls stands out immediately. `spend_weighted_tokens`
  is the cost-weighted measure (cache reads down-weighted 0.25×) that the
  soft-budget alert and the per-turn ceiling (a net; see
  [`prompt-sizing-optimization.md`](../../architecture/investigation-engine/prompt-sizing-optimization.md)
  §4.3) compare against — prefer it over
  raw `total_tokens` when judging how close a turn ran to the budget.

**Watch it without any infra.** `token_spend_watch.py` (in `faultmaven-doc-internal`
at `operations/scripts/token_spend_watch.py`) reads the `turn_token_spend` lines
straight from a log, groups them by run (`case_id`), and prints a per-turn table
plus aggregates — cost-weighted spend vs the soft budget / hard ceiling, cache
usage, unpriced-call flagging, and cost. Stdlib only.

```bash
./token_spend_watch.py /tmp/faultmaven-dev.log         # latest run
./faultmaven.sh logs api | ./token_spend_watch.py -    # pipe live logs
./token_spend_watch.py --follow                        # live watch during a run
```

### Opik spans

Each LLM span carries `usage` (`prompt_tokens` / `completion_tokens` /
`total_tokens`) plus `prompt_cache_hit` and cache-token metadata.

## The usage ledger

Everything above is either per process (Prometheus), per log line or per
trace, and none of it carries a tenant. The usage ledger is the persisted,
tenant-attributed record of the same spend (#640): two tables in the
application database, written at the same chokepoint, surviving restarts and
summing correctly across replicas because every write is an atomic
`INSERT … ON CONFLICT DO UPDATE SET col = col + excluded.col`. It needs no
Prometheus. The in-product view over it — `GET /admin/llm/usage` and the
Dashboard's Usage page — is filed as #1764 and #1765.

| Table | One row per | Holds |
|---|---|---|
| `llm_usage_daily` | enterprise, UTC day, billing subject, actor, `provider`, `model`, `outcome` | the four token buckets, `estimated_cost_usd`, `calls`, `unpriced_calls` |
| `llm_turn_spend` | engine turn that made a billed call, addressed by `(enterprise_id, case_id, turn_number)` | the same buckets, `spend_weighted_tokens`, `calls`, `low_confidence_calls`, `unpriced_calls`, `estimated_cost_usd`, `investigation_turn`, the actor and payer, `occurred_at` |

**How a call reaches a row.** Inside an engine turn every billed call accrues to
the turn's tracker, and the end of `process_turn` writes the turn in one
transaction: one `llm_usage_daily` increment per `(provider, model, outcome)`
plus the `llm_turn_spend` row. Every other billed call — title generation, an
out-of-band aside, tier-2 preprocessing, and a call made
after its turn already flushed (the fire-and-forget runbook conversion) —
writes a daily row of its own. Every billed call lands in exactly one daily
row. A turn that made no billed call writes no turn row.

**Who it is attributed to**, captured when the spend is incurred:

- the **enterprise** bound to the request (the RLS key);
- the **billing subject** from the same rule the turn cap charges with — the
  account's organization when it has one, else the account — or `none` when
  there is neither: a call with no paying organization and no actor, for
  example a standalone call made outside an authenticated request;
- the **actor**: the turn's user inside an engine turn; otherwise the user
  `require_authentication` resolved for the request; otherwise `''`. A route
  that authenticates another way records no actor — attribution lost, never
  misattributed.

**What the figures mean.**

- `estimated_cost_usd` is estimated **at call time** from the price table then
  in force, over priced calls only. `unpriced_calls` counts the calls it leaves
  out, so a row with unpriced calls is a lower bound. Tokens are always stored,
  so the figures can be re-priced. Self-hosted providers are priced at $0, not
  unpriced.
- `usage_date` is the **UTC day at write time**: a turn's flush, or the call
  for its own row. A turn that spans midnight is charged to the day it ended.
- `outcome=low_confidence` rows are the fallback chain's billed-and-discarded
  attempts, kept apart as on the Prometheus counter.
- The tables **start empty** when this ships. There is no backfill, because
  neither Prometheus nor the logs carry a tenant; a window before the first row
  is "not recorded", never "no spend".

**Deletions.**

- Deleting a case deletes its `llm_turn_spend` rows (`ON DELETE CASCADE`). The
  daily rows name no case and keep the spend, so after a case deletion the
  per-turn figures shrink and the daily totals do not.
- A deleted user's id **stays** in both tables — there is no foreign key on the
  actor or the billing subject, as for `turn_usage`. `SET NULL` would merge a
  deleted user's rows into the no-actor rows and `CASCADE` would erase their
  spend from the enterprise's totals.

**It does not reconcile with `turn_usage`.** The turn cap's ledger counts
turns, is written before the model runs, fails closed, and is written only
under multi-tenancy for engine turns. This ledger records spend for every
billed call the API process makes, in both modes and asides included, after the
call. It will always name more subjects than `turn_usage`; do not expect the two
to agree.

**Jobs are not in the ledger.** No job calls an LLM today, and the job runner
(`python -m faultmaven.jobs.run`) installs no ledger, so a job's call would be
counted `not_composed`. A job that starts calling an LLM must install the ledger
in the runner.

### When a write fails

The ledger **fails open**: a failed write never fails a call or a turn. Every
billed call that does not reach a row increments
`llm_usage_unpersisted_calls_total{reason}`, so the gap between
`llm_provider_calls_total` and the persisted calls is observable:

| `reason` | Meaning |
|---|---|
| `store_error` | The write raised (the database was unreachable or locked) or was cancelled. A raise is also logged at WARNING as `llm_usage_unpersisted`, naming the reason and the exception type — never the row. A write still in flight when shutdown's 5-second drain gives up is cancelled and counted here. |
| `no_tenant` | Under `TENANT_PROVIDER=multi`, a call with no usable enterprise bound. RLS would refuse the row, so none is attempted. |
| `attribution_error` | Capturing who pays raised, so there is nothing to stamp the row with. Logged at WARNING with the exception type. The turn or call itself carries on. |
| `no_loop` | A call outside any turn metered with no running event loop to write it from. |
| `not_composed` | No ledger is installed — the composition root did not run (a unit test, or a process that never booted the app). Not logged. |

A **turn row lost on its own** is logged, not counted. It is written in a
savepoint, so when it fails by itself (its case was deleted mid-turn) it rolls
back alone and the turn's daily increments still commit. Every call of the turn
reached a row, which is all the counter measures, so it does not move. The loss
is logged at WARNING as `llm_usage_turn_row_unpersisted`, with the exception
type and never the row.

### Retention

| Variable | Default | Deletes |
|---|---|---|
| `LLM_USAGE_DAILY_RETENTION_DAYS` | 400 | `llm_usage_daily` rows whose UTC `usage_date` is older than today minus this |
| `LLM_USAGE_TURN_RETENTION_DAYS` | 90 | `llm_turn_spend` rows whose `occurred_at` is older than now minus this |

Thirteen months of daily rows puts the same month last year beside this one;
the turn rows are what grows, so they keep less. Both must be at least 1. The
prune is the `llm_usage_retention` job:

```bash
python -m faultmaven.jobs.run llm_usage_retention
```

- **Cloud (`TENANT_PROVIDER=multi`)**: the job is `cross_tenant`, so it runs only
  on the audited maintenance path (`--cross-tenant-maintenance`, as the
  `faultmaven_maintenance` role, which needs `DELETE` on both tables). The
  CronJob is `faultmaven-enterprise-infra` work, filed as #1766.
- **Standalone**: with `RUN_SCHEDULER=true` the API prunes in-process, once at
  start and every 24 hours. `RUN_SCHEDULER` is **off by default, so a default
  standalone install does not prune.** At 100 turns a day the ledger grows by
  about 2 MB per 90 days; run the job from cron, or set `RUN_SCHEDULER=true`,
  if that matters.

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
(`cache_prompt=True`). The **Anthropic** provider acts on it: it adds an
ephemeral (5-minute) `cache_control` breakpoint on the stable system + tools
prefix, and a second one at the end of the investigation prompt's durable
prefix (the `CACHE_BOUNDARY` line, #613), so the standing instructions bill at
the reduced cache-read rate across the loop's iterations and across
consecutive turns inside the TTL. The **local** provider's llama.cpp transport
forwards it as llama.cpp's own `cache_prompt` body field, which reuses the KV
cache for a shared prompt prefix. Every other provider pops the flag
(OpenAI-family, Gemini and Fireworks cache prompts automatically server-side,
and the prompt's durable-first layout is what gives them a prefix to reuse;
the flag must never leak into their request bodies or they 400). Caching is transparent to model output — it changes only how the prefix
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
   `spend_weighted_tokens` (the cost-faithful measure — a heavily-cached turn
   with a large `total_tokens` may be cheap). A turn with an outlier
   `total_calls` points at a per-turn amplifier (tool-loop iterations, per-cause
   fan-out, retries); an outlier `spend_weighted_tokens` points at prompt bloat.
4. **Is caching working?** Check `prompt_cache_hit` in `llm_call` logs (DEBUG) and the
   `cache_read` series. Zero cache reads on Anthropic tool-loop turns means the
   prefix isn't being reused (e.g. turns > 5 min apart, so the ephemeral cache
   expired).

## Coverage note

Metering is at the registry chokepoint, so it covers all router-routed calls
(the default, and every capability-override path). A dedicated concrete DA
provider (`DA_PROVIDER` set) bypasses the registry on the tool-loop path; that
one call site meters itself directly, so DA-turn spend is still counted.

One billed call is not metered: the LLM connection test
(`POST /admin/llm/config/test`, a single 50-token "Say hello" an operator
triggers). Every provider call site in the package is declared — router,
self-metering, the chokepoint, or this one exception — by
`tests/unit/architecture/test_llm_call_metering_census.py`, so a new unmetered
call fails a test rather than going unseen.
