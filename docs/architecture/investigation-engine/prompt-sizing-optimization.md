# Prompt-Sizing Optimization

> **Status: PROPOSED — design under review (pre-implementation).**
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
| **C** | **Whole-prompt jar not enforced** | The priority-greedy allocator that bounds the whole assembled prompt to `PROMPT_TARGET_TOKENS` exists but is dark-launched (`PROMPT_ALLOCATOR_ENABLED=false`). The live legacy path caps sections independently, never sums them, and omits the ~5K template — landing near budget by luck. | independent |
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
investigation. Per [soundness guarantees](../../../SOUNDNESS_ANALYSIS.md), the behavioral
changes (2, and enabling 1) ship behind shadow-mode + a sim/playbook eval for
conclusion regressions before the flag flips.

## 4. Strategy

Most of the machinery already exists; this is largely *enable + wire*, not
greenfield.

### 4.1 Enforce the per-call jar (goal 1) — enable + harden the allocator

The allocator (`context_builder._allocate_sections`, `templates._assemble_allocated`)
and its 17-test matrix are implemented and dark-launched. Enabling it requires
first fixing four dormant defects:

1. **Journal truncation keeps the wrong end** — falls through to `_truncate_to`
   default `keep="head"` (drops the *newest* entries). The journal is
   recency-ordered; use the middle-out keep-set (oldest anchor + newest N +
   high-signal types) per `prompt-token-budget-allocation.md` §5.2.
2. **Five sections effectively uncapped** — `cap=section_budget` for
   working_conclusion, kb_results, hypotheses, evidence_needs, entity_highlights;
   give each an explicit `min(section_budget, N)` cap so pass B can't let one
   section starve the rest.
3. **Double assembly** in `_assemble_allocated` — measure template overhead once,
   then assemble at the tightened budget.
4. **conversation_history uncapped at priority #2** — cap it so journal /
   conclusion / KB (floor 0) aren't starved on long verbose cases.

Fixes 1/2/3/4 are mechanical and unit-testable against the existing matrix.

### 4.2 DA-turn evidence = index + stub (goal 2) — the biggest single lever

The render already exists: `_render_orphan_file_block(..., summary_only=True)`
emits the addressable stub (id, filename, data_type, `searchable`) **without** the
`file_extract` body, and the structural index is already split into
`file_extract` / `search_map` / `file_meta`. The change is to *select* that render
in Directed-Analysis turns:

- In DA turns, historical evidence renders as stub + `search_map` only (no
  `file_extract`). The current-turn upload keeps its INV-EC-1 floor (a fresh
  upload is never dropped) but at a **tightened** DA extract cap.
- TRIAGE turns are unchanged — triage answers *from* the structural index, so it
  must stay in the prompt.
- Mechanically: gate the evidence extract on `processing_mode` (already threaded
  into `build_investigation_context` / `get_prompt_for_case`), and lower the DA
  effective extract budget.

The prompt already instructs the agent to prefer `search_file` in DA mode
(templates.py §DA guidance), so removing the redundant inline extract aligns the
context with the instructions rather than contradicting them.

### 4.3 Per-turn budget + cross-provider base caching (goal 3)

- **Per-turn ceiling + alert (implemented).** The tool loop already had a
  hard-coded 150K per-turn abort; it is now the configurable
  `PROMPT_TURN_TOKEN_CEILING` (same 150K default — a safety abort that forces the
  loop to wrap up, not the normal budget). Added a *soft* budget
  `PROMPT_TURN_TOKEN_BUDGET` (default 100K, ~1.5× measured normal): when a turn's
  total spend exceeds it, an end-of-turn WARNING (`turn_token_budget_exceeded`)
  logs the call breakdown. Observational only — no behavior change — so
  high-spend turns are surfaced without truncating a legitimately deep turn.
- **Cross-provider base caching (deferred).** The tool loop re-sends the stable
  prefix each iteration with `cache_prompt=True`, which only Anthropic honors.
  Making other providers honor it is provider-layer work (each provider must
  translate the flag to its own caching API, and support varies — some don't
  cache at all). It belongs with the LLM-provider workstream, and its value is
  largely absorbed by §4.2: the DA base shrinks from ~22K to a stub, so the
  re-sent tool-loop base is small regardless of caching. Deferred; not on this
  branch.

### 4.4 Instrument-gap fixes (found during measurement — tracked separately)

Two gaps surfaced while measuring, but both belong to the **per-call metering
PR** (`feat/llm-cost-observability`: `metering.py` / `pricing.py`), which is not
yet on `main`; they are *not* part of this prompt-sizing branch:

- `pricing.py` has no entry for the fireworks models actually served
  (`minimax-m3`, and `deepseek-v3` billing) → `cost_usd=0`, all calls flagged
  unpriced. Add them so cost is real.
- `turn_token_spend` logs via `logger.info("turn_token_spend", extra={...})` but
  the active handler prints only `%(message)s`, so the token fields never render.
  Fold the key fields into the message (or route through the structured logger).

This branch's shadow/eval instrument is the `prompt_allocator_shadow` log (§5),
which is already on `main` and independent of the per-call metering.

## 5. Rollout & acceptance

Per prior hot-path regressions and the soundness guarantee, ship behind
validation, not a flip:

1. Land §4.1 fixes + §4.4 instrument fixes (mechanical, unit-tested) first.
2. Implement §4.2 / §4.3 behind the allocator flag.
3. **Shadow mode** (`PROMPT_ALLOCATOR_SHADOW=true`): assemble both prompts, log
   size/section deltas, send the legacy one. Confirm the projected per-turn and
   per-call reductions on real traffic.
4. **Eval**: run the sim / playbook suite and confirm no conclusion regression
   (INV-1 current-turn floor holds, no wrong/collapsed investigations) between
   legacy and allocator prompts.
5. Flip `PROMPT_ALLOCATOR_ENABLED=true` only after 3 + 4 pass.

## 6. Projected effect

With §4.2 (evidence ~19K → ~4K) the base drops to ~7–9K; with §4.3 the tool-loop
re-send stops multiplying that base. Per-turn prompt tokens project from ~66K to
the ~15–25K range (dominated by the one full reasoning call under the 32K jar),
a ~60–75% reduction — to be **confirmed in shadow mode**, not assumed.
