# Prompt-Sizing Optimization

> **Status: IMPLEMENTED — on `main`.** Prompt-sizing enforcement and the
> allocator single-path landed in #612; the observability that makes it
> measurable landed alongside — logging render (#617), `spend_weighted_tokens`
> emission + `gpt-4.1-mini` pricing (#618), and `claude-sonnet-4-5` pricing
> (#636). The remaining durable/ephemeral prefix segregation (§4.3) is deferred
> to issue #613.
>
> Umbrella design for cutting per-turn prompt token consumption. It sits above
> two existing detail docs and does not duplicate them:
>
> - Per-call budget & allocator: [`prompt-token-budget-allocation.md`](./prompt-token-budget-allocation.md)
> - Evidence block assembly: [`evidence-context-assembly.md`](./evidence-context-assembly.md)
>
> The per-turn budget and the tool-loop caching contract (§4.3) are new here; the
> per-call jar (§4.1) and the evidence render (§4.2) build on the two docs above.

---

## 1. The measured problem

Local measurement (playbook S9 — OpenSSH_2k.log, 5 turns with tool searches),
read from the `turn_token_spend` per-turn tracker and a per-call `llm_call`
probe, provider `fireworks`/`minimax-m3`:

| Metric | Measured |
|---|---|
| Prompt (input) tokens / turn | ~46.6K (232.9K over 5 turns) |
| Output tokens / turn | ~1.7K (8.5K over 5 turns) |
| **input : output** | **~27.5 : 1** (output = 2.5% of tokens) |
| LLM calls / turn | ~2.8 (14 over 5 turns) |
| Per-call prompt size | **every call 21.6K–26.1K — no cheap calls** |
| Prompt tokens / turn (in + cache_read) | ~66K |

The spend is **input-dominated**, not output- or fallback-driven: the tool loop
is 2–3 calls/turn (not a storm), and every call — including the ones that emit a
tiny tool-call (~180 output tokens) — carries a ~22K prompt.

## 2. Root causes

| # | Cause | Mechanism | Provider-dependence |
|---|---|---|---|
| **A** | **Evidence dump in the base prompt** | The assembled base is ~22K, of which **~19K is the evidence block**. `_effective_evidence_char_budget` fills `evidence_budget_fraction (0.6) × prompt_budget × 4` chars with the file `file_extract`, even in Directed-Analysis turns where `search_file` exists to fetch specifics on demand. | independent (assembly) |
| **B** | **Tool-loop re-send multiplier** | `_tool_augmented_generate` re-sends the growing `messages` list (base + accumulated tool calls/results) on every iteration. `cache_prompt=True` is set, but only Anthropic honors it — other providers pop it, so the full ~22K base is re-sent at full price each of the ~2.8 iterations. | worse off-Anthropic |
| **C** | **Whole-prompt jar not enforced** | The priority-greedy allocator that bounds the whole assembled prompt to `PROMPT_TARGET_TOKENS` was dark-launched behind a flag while the live path was an obsolete char-based assembly that capped sections independently, never summed them, and omitted the ~5K template — landing near budget by luck. *(Resolved: the allocator is now the only assembly path — §4.1.)* | independent |
| **D** | **No per-turn budget** | Nothing sums the tool-loop calls against a turn-level ceiling; the ~2.8× multiplier is invisible to any control. | independent |

`classify_query` is mechanical (regex, not billed) and is **not** a cause.

## 3. Goals (confirmed)

1. **Per-call prompt ceiling — 32K flat.** Keep `PROMPT_TARGET_TOKENS = 32000`,
   clamped by the model window. Enforce it by enabling the allocator (§4.1). The
   real savings come from goals 2–3, not from lowering this number.
2. **DA-turn evidence = structural index + addressable stub only.** In
   Directed-Analysis turns the base carries the file's addressable stub (`file_id`,
   `data_type`, `searchable`) + `search_map`, **not** the ~19K `file_extract`; the
   agent fetches specifics via `search_file`. Target: ~19K → ~3–5K base (§4.2).
3. **Per-turn total budget — ceiling + alert + cached base.** Introduce a
   per-turn token ceiling that logs at WARNING when exceeded, and make the stable
   tool-loop base cached / not-re-sent across **all** providers (§4.3).

**Non-goal / guardrail:** none of these may cause a wrong or collapsed
investigation. Per [soundness guarantees](../../../SOUNDNESS_ANALYSIS.md), goal 2
was validated by a sim/playbook eval (no conclusion regression) and is safe by
construction via its tool-availability gate (§4.2) before becoming the standing
behavior.

## 4. Strategy

Most of the machinery already exists; this is largely *enable + wire*, not
greenfield.

### 4.1 The per-call jar (goal 1) — the allocator is the only assembly path

The allocator (`context_builder._allocate_sections`,
`templates._assemble_allocated`) is now the **single** prompt-assembly path — the
obsolete first-draft ("legacy") char-based assembly and its
`PROMPT_ALLOCATOR_ENABLED` / `PROMPT_ALLOCATOR_SHADOW` gate flags have been
deleted (pre-production system, no users, no backward-compat). Two dormant defects
were fixed as part of collapsing to it (two others listed in earlier drafts were
already fixed on `main`):

- **Journal truncation kept the wrong end** — `_truncate_to` defaulted to
  `keep="head"`, dropping the *newest* entries. The journal is recency-ordered
  anti-amnesia memory → now `keep="tail"` (rank-ordered sections keep head).
- **conversation_history uncapped at priority #2** (`cap=section_budget`) could
  starve the lower-priority journal / KB / hypotheses. Now bounded by
  `PROMPT_CONVERSATION_HISTORY_MAX_TOKENS` (default 8000).

Both are covered by bite-verified regression tests.

### 4.2 DA-turn evidence = index + stub (goal 2) — the biggest single lever

**Status: implemented, validated, standing behavior (no flag).** In a
directed-analysis turn *with tools available*, historical evidence renders as its
addressable stub + `search_map` only (`_render_orphan_file_block(summary_only=…)`
and the Tier-A elision), dropping the `file_extract` body with a marked
`elided="directed_analysis"` note (INV-4). The current-turn upload always keeps its
full extract (freshness / INV-EC-1), and TRIAGE turns are unchanged (triage answers
*from* the structural index).

**Gated on tool-availability, not a flag.** The elision is only sound when
`search_file` will actually run — otherwise a tool-less / tool-incapable turn is
stranded with a stub pointing at an uncallable tool. So the condition is
`processing_mode == "directed_analysis" AND tools_available`, where
`tools_available` (investigation tools registered AND `supports_tool_calling`) is
computed by `milestone_engine._tools_effectively_available()` and threaded through
`get_prompt_for_case` → `build_investigation_context` → `_build_evidence_context`.

**Measured effect (offline A/B, deterministic):** the historical evidence block
drops **87–93%** — e.g. ~1,655 → 118 tokens for one file, and the whole block from
its ~5K budget cap to ~0.5K when several historical files are present (the saving
plateaus at ~4.6K tokens because OFF is already capped by the evidence budget). A
tool-less build keeps the full extract (safety verified). A playbook-S9 eval showed
**no conclusion regression**: search turns stay grounded because the agent uses
`search_file` to recover specifics.

### 4.3 Per-turn budget + cross-provider base caching (goal 3)

- **Per-turn ceiling + alert (implemented).** The tool loop already had a
  hard-coded 150K per-turn abort; it is now the configurable
  `PROMPT_TURN_TOKEN_CEILING` (150K default — a safety abort that forces the loop
  to wrap up schema-only on the next iteration, not the normal budget). Added a
  *soft* budget `PROMPT_TURN_TOKEN_BUDGET` (default 100K, ~1.5× measured normal):
  when a turn crosses it an end-of-turn WARNING (`turn_token_budget_exceeded`)
  logs the call breakdown. Observational only — no behavior change — so
  high-spend turns are surfaced without truncating a legitimately deep turn.
  - **Both guards compare a *cost-weighted* spend, not raw tokens.** The measure
    is `spend_weighted_tokens = input + output + cache_write + 0.25 × cache_read`:
    cache reads are real bytes in the window but billed at a fraction (~0.1× on
    Anthropic, ~0.25–0.5× on OpenAI), so they are down-weighted. Weighting on raw
    bytes would trip a cheap, heavily-cached tool loop; weighting on cost keeps
    the abort motivated by spend. `cache_write` (~1.25×) is counted in full. The
    per-call size ceiling (32K jar, §4.1) still bounds each individual call on
    raw bytes.
- **Tool-loop re-send is the dominant cost, and prefix caching is now the lever
  in play.** On playbook S9 the tool loop re-sends the *growing* message history
  (base + accumulated tool calls/results) on every iteration, so per-turn cost
  scales with iteration count, not base size (the §4.2 base shrink is real but
  ~10% of a tool-heavy turn). Provider **prompt caching** now absorbs most of that
  re-send and is observed live: OpenAI automatic prefix caching (nonzero
  `cache_read`, no separate `cache_write`) and Anthropic explicit `cache_control`
  (a `cache_write` on the prefix write, then `cache_read` on reuse). The
  cost-weighting above exists precisely so these cached re-sends register at their
  true (discounted) cost. A further architectural win — segregating the durable
  prefix from the ephemeral scratchpad so the stable prefix is never re-sent
  full-price (strategy 1) — is deferred to issue #613.

### 4.4 Instrument-gap fixes (found during measurement — now resolved)

Two gaps surfaced while measuring the per-turn `turn_token_spend` tracker; both
are now fixed on `main`:

- **Extra fields never rendered.** `turn_token_spend` logs via
  `logger.info("turn_token_spend", extra={...})`, but the stdlib logging path
  rendered only `%(message)s`, dropping every `extra` field. Fixed by routing all
  logs through a single `structlog.stdlib.ProcessorFormatter` with `ExtraAdder`
  (+ `PositionalArgumentsFormatter`), so stdlib `extra={...}` fields render as
  JSON facility-wide, not just for the token log. `turn_token_spend` now also
  emits `spend_weighted_tokens` (the guard measure) alongside the raw buckets.
- **Models served were unpriced.** `pricing.py` had no entry for the models
  actually served, so `estimated_cost_usd = 0` and every call was flagged
  `unpriced`. The default models are now priced — `gpt-4.1-mini` (OpenAI) and
  `claude-sonnet-4-5` (Anthropic); any remaining unpriced `(provider, model)`
  (e.g. a fireworks `minimax-m3`) is still surfaced via `unpriced_calls` rather
  than silently counted as free, so cost under-reporting stays visible.

See [`../../operations/monitoring/llm-cost-observability.md`](../../operations/monitoring/llm-cost-observability.md)
for the full metering surface (Prometheus counters, structured logs, Opik spans).

## 5. Rollout & acceptance — outcome

The allocator is the single assembly path (§4.1), covered by the full investigation
unit + integration suite. The DA index+stub (§4.2) was validated and is now the
standing tool-gated behavior (flag removed):

1. **A/B measurement (done).** The per-turn LLM A/B was confounded by tool-loop
   call-count variance, so the evidence-block delta was measured *offline* and
   deterministically: 87–93% reduction, plateauing at ~4.6K tokens (OFF is capped
   by the evidence budget). Tool-less builds keep the full extract.
2. **Eval (done).** Playbook S9 with the elision on showed no conclusion
   regression — search turns stay grounded (the agent recovers specifics via
   `search_file`); the current-turn floor (INV-1) holds.
3. **Flag collapsed.** With 1 + 2 passing and the tool-availability gate making it
   safe-by-construction, `DA_EVIDENCE_INDEX_ONLY` was removed — the elision is the
   behavior, not an option (same no-dead-flags principle that retired the allocator
   gate). `PROMPT_TURN_TOKEN_BUDGET` remains an operator-tunable observability knob,
   not a behavior gate.

## 6. Effect — measured, and where the real prize is

The §4.2 evidence shrink is real and safe but **modest relative to per-turn spend**:
~4.6K tokens off a base that, on tool-heavy turns, is re-sent 3–4× and swamped by
accumulated tool results (per-turn totals 115K–240K on S9). The measurement’s main
lesson is the §4.3 course correction: **the tool-loop re-send is the dominant cost
and the next lever.** Do not treat §4.2 as the finish line — it is one contributor;
the large win is in not re-paying for the stable prefix every tool iteration.
