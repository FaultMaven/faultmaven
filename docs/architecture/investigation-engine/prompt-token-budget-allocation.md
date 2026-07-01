# Prompt Context Management & Token-Budget Allocation

> **Status: ACTIVE — fully implemented.**
>
> The token-budget number itself (`PROMPT_TARGET_TOKENS`) and the model
> context-window registry already exist. The **allocation** of that budget
> across the prompt and the **compaction** that keeps each part within it — the
> subject of this document — are proposed and under review.
>
> **Authoritative source (after implementation):**
> `faultmaven/core/investigation/prompts/context_builder.py`
> (`build_investigation_context`, `TokenBudget`) and
> `faultmaven/core/investigation/prompts/templates.py` (`get_prompt_for_case`,
> `_budgeted_prompt`).
>
> **Related docs:**
>
> - Budget number + model registry: [`prompt-assembly-architecture.md` §6](./prompt-assembly-architecture.md)
> - Evidence block (the largest variable section): [`evidence-context-assembly.md`](./evidence-context-assembly.md)
> - Token counting util: [`../../development/token-estimation.md`](../../development/token-estimation.md)

---

## 1. The systemic problem: the budget does not reconcile

`PROMPT_TARGET_TOKENS` is meant to be a single jar — the whole assembled prompt
must fit inside it. Today the prompt is poured into that jar by several
independent mechanisms that do not share a unit, do not sum to the jar, and
shrink on the wrong signal. These are facets of one systemic issue in how
context is managed:

| Concern | Role | Current limitation |
|---|---|---|
| **Measurement** | the measuring cup | sections are measured in **characters** (`len(text)`, 4≈1 token), not real tokens, and inconsistently across sections. |
| **Aggregation** | the addition | every section is capped **independently**; nothing sums them against the jar. The one budget number is even applied **twice, in two units** (below). |
| **Compaction** | the shrink-to-fit lever | history compaction triggers on **turn count** (`current_turn > 15`), not on token pressure or the budget. |
| **Overflow** | the safety net | the graceful "minimal prompt" fallback is **unreachable** (defined but never called). |

### 1.1 Concrete illustration (target = 32K)

```text
System template (instructions/blocks)  ~4–6K tokens   ← NOT counted against anything
Evidence block        0.6 × 32K = ~19K tokens         ← its OWN budget, computed in chars×4
Conversation history  turn-based, ~2–8K               ← compacts on turn count, not tokens
KB results            5 × 800 chars ≈ ~1K
Hypotheses + journal  unbounded                        ← journal "always included in full"
                      ─────────────────────────
SUM:  never computed.  Lands near 32K by luck on the default model, not by design.
```

Two precise reconciliation failures:

1. **The budget number is used twice, two different ways.** The evidence block
   sizes itself as `0.6 × target × 4` *chars* (`_effective_evidence_char_budget`);
   separately `build_investigation_context` runs a `TokenBudget(target)` over the
   sections. Two budgets off the same number, different units, neither aware of
   the other or of the template.
2. **The template is never in the jar.** `PROMPT_TARGET_TOKENS` is defined as the
   *whole* prompt, but only the dynamic sections are budgeted to it; the ~4–6K
   fixed template is added afterward, so the real prompt is `target + template`.
   On big-window models the model's hard limit is far higher (e.g. 192K), so this
   overshoot is never caught — silent and permanent.

### 1.2 Compaction is decoupled from the budget

| Mechanism | Trigger | Tied to the budget? |
|---|---|---|
| History: verbatim vs summarized | `HISTORY_VERBATIM_TURNS=3` / `HISTORY_SUMMARY_MAX_TURNS=7` (turns) | ❌ |
| History → compact state summary | `current_turn > STATE_SUMMARY_TURN_THRESHOLD (15)` (turns) | ❌ |
| Agent-response truncation | `HISTORY_AGENT_TRUNCATE_THRESHOLD=600` (fixed chars) | ❌ |
| State-summary evidence digest | `8 × 180` (fixed chars) | ❌ |
| Evidence sliding window | `0.6 × target × 4` chars | ⚠️ yes, but double-budgeted + char-based |
| Investigation journal | none — "always in full", unbounded | ❌ (latent overflow on long cases) |

Consequence: raising the budget to 64K does **not** keep more history (still
collapses at turn 15, wasting headroom); lowering it to 8K (local) does **not**
compact sooner (stays verbose to turn 15, overflowing). The budget and the
compaction are unaware of each other.

---

## 2. Principles

1. **One number, one jar.** The budget bounds the **whole** assembled prompt
   (template + every section).
2. **One allocator, top-down.** A single component reserves the fixed parts,
   computes what's left, and divides it across the variable sections. No section
   budgets itself independently.
3. **Token-native accounting, cheap selection.** The *authoritative* total and
   the overflow check are measured in real tokens (`estimate_tokens`). To bound
   latency, a section may *select* its compaction level with a cheap
   char→token proxy; the assembled total is then verified in real tokens (§8).
4. **Compaction is budget allocation.** A section that exceeds its sub-budget is
   *compacted to fit* — by budget pressure, not by turn count. Compaction
   degrades gracefully (summarize/elide with a marker), it never crudely
   tail-truncates load-bearing content.
5. **Invariants over luck.** The current-turn upload floor and the reserved
   blocks are guaranteed by the allocator's structure, not by how much budget a
   section happens to win (§5).
6. **Backstop, not primary.** Trimming inside the jar is the normal path; the
   minimal-prompt fallback fires when the budget is exhausted *either* by
   exceeding the model's hard limit *or* by starvation below a minimum-viable
   floor (§7).

---

## 3. Inputs: the resolved budget

The waterfall does **not** start from the raw `PROMPT_TARGET_TOKENS` knob. It
starts from the **resolved** budget for the active provider/model:

```text
resolved_budget = min(PROMPT_TARGET_TOKENS, context_window − response_reserve)
```

`response_reserve` (room for the model's output) is held out of the window
*before* anything else; the small/local-model clamp therefore drives allocation
directly. `response_reserve` is a **per-model field of the model registry**
(`faultmaven/utils/model_context.py`, e.g. 8K for Claude, 16K for the 1M-window
models), with a default for registry entries that don't specify one; it is part
of the same registry an operator can override. When the window is unknown
(uncurated/local model) there is no clamp — the resolved budget is
`PROMPT_TARGET_TOKENS` and the operator owns fitting it. Throughout this document
"the budget" means `resolved_budget`.

---

## 4. The budget waterfall

```text
resolved_budget                         (§3; e.g. 32K, or clamped lower on a small model)
│
├─ 1. RESERVE (counted first, always present; every item bounded — see §6):
│        system template (skeleton) + identity + core_context + milestones
│        + inquiry_state + pending_action + user_message(bounded) + system_feedback
│        + CONTINUITY: guaranteed via the conversation section's floor (§6 as-built)
│        + CURRENT-TURN EVIDENCE FLOOR: ≥1 fresh upload present (INV-1; §6 as-built)
│
├─ 2. section_budget = max(0, resolved_budget − reserved)
│        if reserved alone is too large (§6) → starvation backstop (§7)
│
├─ 3. ALLOCATE section_budget across variable sections — PRIORITY-GREEDY with
│      per-section FLOOR + CAP (§5.1), not fixed fractional splits:
│        pass A: give each section (priority order) its floor
│        pass B: distribute the remainder by priority, each up to its cap
│        → underused/absent-section budget flows down to the next priority
│
├─ 4. COMPACT each section to its allotment (select level cheaply, render once, §5.2):
│        history:  verbatim → graduated → state-summary → oldest-first elision
│        journal:  keep oldest anchor + newest + {decision,finding,ruled_out,blocker};
│                  elide the middle with a [… N entries …] marker
│        evidence: Tier A/B/C/D window over its allotment (full current-turn extract
│                  here if it fits; the stub is already reserved in step 1)
│        KB / hypotheses: rank, keep what fits, drop lowest with a marker
│
├─ 5. ASSEMBLE once; MEASURE the real assembled total in tokens (§8).
│
└─ 6. BACKSTOP (§7): if total > model HARD limit → trim lowest-priority → if still
         over → minimal FALLBACK_* prompt. (Starvation case handled at step 2.)
```

The **reserve** is non-negotiable; everything else competes for `section_budget`
by priority. Underuse and absent sections are reclaimed automatically by the
greedy second pass — no section's share is stranded.

---

## 5. Allocation and compaction

### 5.1 Priority-greedy with floors and caps (not fixed fractions)

Fixed fractional sub-budgets are rejected: they waste budget (evidence takes its
0.6 even when it needs 0.2) and strand the share of absent sections (no
hypotheses in INQUIRY, no evidence early on) — the very "shrink on the wrong
signal" failure §1 sets out to kill, merely relocated.

Instead, each variable section declares a **floor** (a minimum it needs to be
useful at all) and a **cap** (a ceiling so a high-priority section can't hog the
jar). Allocation is two passes over the priority order:

- **Pass A — floors:** grant each section its floor, highest priority first,
  while budget remains. A section whose floor doesn't fit is omitted (with a
  marker), not partially rendered into uselessness.
- **Pass B — greedy remainder, strict-priority sequential:** section #1 grows to
  its cap first, then #2, then #3, … (sequential, **not** proportional).
  Whatever a section doesn't take (because it's small, or absent) flows to the
  next priority.

**Every variable section has a cap** — none is left uncapped, or an uncapped
mid-priority section (e.g. working_conclusion at #3) would consume the entire
remainder in pass B before journal/entity-highlights get anything. The caps:

| Section | Cap |
|---|---|
| Evidence | `EVIDENCE_CONTEXT_BUDGET_FRACTION` (× `section_budget`) + `EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM` per item |
| Older conversation | history-turn limits (`HISTORY_*`) |
| Working conclusion | small fixed cap (already bounded in code: `reasoning[:1000]`) |
| KB results | `KB_MAX_SOLUTION_CHARS` × top-N |
| Hypotheses | ranked, small per-item cap × N |
| Evidence needs | small fixed cap |
| Journal | strict ~1.5K-token cap via its middle-out keep-set (§5.2) — small by design, so the cap rarely binds |
| Entity highlights | already bounded (per-type limit) |

Most are the "upper bounds" the codebase already has; the few currently-unbounded
sections (working_conclusion is naturally small; hypotheses/evidence_needs/journal)
get an explicit cap so pass B can never let one section starve the rest. Floors
are new and **default to 0** — with continuity
already reserved (§4), pass A is typically a no-op and allocation degenerates to
pure strict-priority greedy fill; a floor is added only if a section proves it is
routinely starved (§15.1).

> **Caps deliberately strand headroom — do not "fix" this.** With a 0.6 evidence
> cap, evidence stops at `0.6 × section_budget` even when budget remains and no
> lower-priority section needs it. That is intentional: prompt tokens are a
> scarce resource budgeted to force the agent onto RAG tools (`search_file`/KB)
> rather than lazy context-dumping (the rationale behind the flat
> `PROMPT_TARGET_TOKENS`). A future maintainer must not relax the cap to "use the
> unused headroom" — that reintroduces context-dumping.

**Priority order, highest first** (one global order; composes with the existing
stage-specific section loading, which already zeroes irrelevant sections so their
budget redistributes automatically):

1. **Evidence** (historical/Tier-A–D — the *current-turn floor stub is reserved
   in §4 step 1*, so this competes only for the richer extract + older tiers).
2. **Older conversation** — turns *beyond* the reserved last exchange. Continuity
   is guaranteed by the reserve (the last user+assistant turn, §4), so this
   variable section carries only the deeper history and needs no separate floor.
3. **Investigation journal** — durable anti-amnesia memory; compacts middle-out;
   strictly capped (§5.2). Placed high *on purpose*: it is high-density,
   low-footprint (~50 tokens/entry; ~1–1.5K for a 50-turn case) and prevents the
   agent from re-treading ruled-out hypotheses and known blockers. Preserving it
   yields far more reasoning value per token than verbose KB runbook text, so it
   outranks working_conclusion / KB / hypotheses. (It is *also* carried in the
   `FALLBACK_*` templates — §7 — so anti-amnesia survives the tight-budget case
   that would otherwise drop it.)
4. **Working conclusion.**
5. **KB results.**
6. **Hypotheses.**
7. **Evidence needs.**
8. **Entity highlights** — convenience surface; first to go.

### 5.2 Compaction selects cheaply, renders once

For each section, do **not** render every fidelity level and then measure (it
multiplies per-turn cost). Instead: use a cheap analytic size estimate
(char→token) to **choose** the highest fidelity level that fits the allotment,
**render that level once**, and rely on the single final token-count of the
assembled prompt (§4 step 5) for the authoritative check.

Per-section policies:

- **Conversation history** — the *older* turns beyond the reserved last exchange
  (§4); levels chosen by estimate, highest-fidelity-that-fits: older turns
  summarized → compact state-summary → state-summary with oldest turns elided.
  Turn count may survive only as a cheap pre-filter hint, never as the authority.
- **Investigation journal** — never tail-truncate (that drops the *newest*
  entries). Keep the oldest anchor entry, the most recent N, and all high-signal
  types (`decision`, `finding`, `ruled_out`, `blocker`); elide the middle with a
  `[… N entries …]` marker. (`ruled_out`/`blocker` are kept because re-treading
  dead ends and known blockers is exactly what the journal exists to prevent.)
  Strictly capped at ~1.5K tokens — high-density and low-footprint, so the cap
  binds only on pathologically long cases, and even then the keep-set preserves
  the anti-amnesia essentials.
- **Evidence** — the existing Tier A/B/C/D sliding window, drawing from its
  allotment. The current-turn item renders **once**: the reserve (§4) guarantees
  a stub-sized floor for it, and the evidence section **upgrades that same block
  in place** to a fuller extract when the allotment allows. It is not emitted
  twice and its tokens are counted once.
- **KB results** — ranked; keep top matches that fit, per-item capped by
  `KB_MAX_SOLUTION_CHARS`.
- **Hypotheses** — ranked by likelihood; lowest dropped beyond the allotment.

---

## 6. Bounding the reserve

The reserve must itself be bounded, or "one jar" is a fiction: a user pasting a
30K-token log into chat would make `reserved > resolved_budget`, driving
`section_budget` negative and zeroing every variable section (and possibly
exceeding the hard limit).

- **`user_message`** is capped. Oversized pasted *data* should be routed through
  the existing intake file-ification (pasted content → `UploadedFile`) so it
  lands in evidence (compactable) rather than the un-trimmable reserve; what
  remains in `user_message` is truncated with a visible `[… truncated, N
  tokens …]` marker.
- **Current-turn evidence floor** is reserved as a *bounded addressable stub*
  (file_id, filename, data_type, `searchable`, a short head + the `search_file`
  pointer), **not** the full extract — so a huge fresh upload cannot blow the
  reserve while INV-1 still holds.
- **Continuity** is guaranteed but **bounded**. *As built*, this is realized via
  the conversation section's **floor** rather than a separately-reserved
  last-exchange render: the conversation section's lowest fidelity is the compact
  history (`_build_compact_history`), which *always* includes the latest turn
  (and the previous turn when available). Granting that floor in pass A (the
  conversation section is high priority) gives the same guarantee — "continuity
  is never zero" — without rendering the last turn twice (once reserved, once in
  history). The compact history is bounded by construction (state-summary +
  short turn digests).
  > **Behavior change worth noting:** the *guaranteed* continuity is now the
  > latest turn (carried by the compact-history floor), down from the prior
  > `HISTORY_VERBATIM_TURNS = 3` always-verbatim turns. Deeper history is
  > *best-effort* via the graduated history when budget allows. This is the
  > intended trade — latest turn guaranteed, the rest pressure-driven — not a
  > regression.
- **Engine-generated blocks** (`identity`, `core_context`, `milestones`,
  `inquiry_state`, `pending_action`) are bounded by construction — short,
  fixed-shape strings the engine emits. `system_feedback` can grow with
  accumulated validation/feedback text, so it is **capped** with a marker like
  any other reserved item. This is what substantiates INV-2 for the whole
  reserve, not just `user_message`.
- **Template skeleton** is measured each turn, not cached: format the active
  template with empty section placeholders and token-count it. Caching by
  (state, stage, knowledge_query) is unsafe — the DIAGNOSIS skeleton also varies
  by zone (`_get_diagnosis_focus_emphasis`), and any template edit would silently
  invalidate the cache (a drift class this codebase has been bitten by). The
  extra format+count per turn is negligible and always correct.
- **`reserved > resolved_budget`:** degrade deterministically — apply the
  `user_message`/stub caps first; if the reserve still exceeds the budget,
  `section_budget` floors at 0 and the **starvation backstop (§7)** switches to
  the minimal template. The result is never a negative budget.
- **`margin`:** `section_budget = resolved_budget − reserved − margin`. The
  margin is a small buffer absorbing `estimate_tokens` error (tiktoken used as an
  Anthropic proxy is inexact). It only bites when `target ≈ hard limit` (small
  models); on the flat-32K-under-a-big-window case the estimation error is
  harmless.

---

## 7. Overflow & starvation backstop

The minimal `FALLBACK_*` template (which carries the load-bearing safety
constraints: no-confabulation, hypothesis-evidence ordering, closed-case
boundary) **must include a current-turn evidence stub slot** so INV-1 survives
the fallback. Today's `FALLBACK_INVESTIGATION_TEMPLATE` has no evidence slot at
all — so firing it on a fresh upload would drop the just-uploaded file, which is
the original turn-8 bug re-triggered in the *exact* tight-budget scenario INV-1
exists to prevent. The fix is part of this design: the `FALLBACK_*` templates
gain a minimal current-turn slot (the reserved addressable stub: `file_id` +
`searchable` + a one-line head), and the fallback path injects the reserved stub
into it. The `FALLBACK_*` templates also carry a **compact journal slot**
(strictly capped, §5.2) — the journal is the anti-amnesia memory, and the
tight-budget case that triggers the fallback is precisely when re-treading
ruled-out hypotheses is most likely, so it must survive the fallback too.

The fallback is reachable by **two** triggers, not just hard-limit overflow:

1. **Hard-limit overflow.** After assembly, if the measured total exceeds the
   model's hard limit (`window − response_reserve`): trim lowest-priority
   sections further; if still over, switch to `FALLBACK_*`.
2. **Starvation (the common painful case).** If `section_budget` — or more
   precisely the **minimum-viable set**, which is just the reserve itself
   (system template + identity/core/milestones + bounded `user_message` +
   `system_feedback` + the **reserved last exchange** + the **current-turn
   stub**) — leaves less than `PROMPT_MIN_VIABLE_TOKENS` for any variable
   content, switch to `FALLBACK_*` *proactively*, independent of the hard-limit
   check. Its smaller skeleton frees the room a normal template would have
   wasted, so a small-target deployment gets a usable prompt (reserve + stub +
   last exchange, per the fallback's slots) instead of a near-empty one that's
   technically "under limit."

**The fallback is a fixed minimal template, not a re-allocation.** When it fires,
the engine switches to the `FALLBACK_*` template with only its fixed slots
(reserve + current-turn stub + last exchange + problem/milestone/hypothesis
summaries — no evidence tiers, journal, KB, or entity highlights) and does **not**
re-run the allocator against the smaller skeleton. The fallback is a *safety
mode*, not an optimization: simpler, predictable, and small-model deployments are
degraded by nature. (The "frees the room" phrasing means the smaller skeleton is
what lets the fixed slots fit — not that freed budget is re-poured into the full
section set.)

When the window is unknown (local/uncurated), there is no hard limit to check;
the section budgeter still bounds to the resolved budget, and the starvation
trigger still applies.

Every backstop event logs at WARNING (`prompt_overflow_trimmed` /
`prompt_overflow_fallback` / `prompt_starvation_fallback`) with token counts and
the action taken — rare, visible, never silent.

> **Default (legacy, allocator-off) path:** the pre-send overflow/starvation
> backstop above runs only in the allocator path. The legacy assembly does **not**
> measure the whole prompt against the ceiling every turn — that per-turn
> full-prompt tokenization is a cost we deliberately avoid on the hot path while
> the allocator is the future home of pre-send budgeting. The legacy path's net
> is the **runtime recovery (§7.1)**: reactive, but it costs only one failed
> request on the rare turn that actually overflows, rather than a tokenization on
> every turn. (Pre-allocator, there was no overflow handling at all, so this is
> strictly better; once the allocator is enabled, handling is proactive.)

### 7.1 Runtime recovery: provider context-length rejection

The pre-send checks above assume our *estimate* of the model's window is right.
It may not be: a corporate proxy or an aggregator (e.g. OpenRouter) can enforce a
**smaller** limit than the registry says, a model may be served with a reduced
context, or the tokenizer may underestimate. In those cases the provider returns
a context-length error (typically HTTP 400 "maximum context length" /
"reduce the length") even though our pre-send measurement passed — and, left
unhandled, the case is **permanently blocked** (every turn re-fails identically).

So the backstop is also an **exception handler**, not only a pre-send gate:

1. Classify the provider error as context-length-exceeded (provider-agnostic:
   match the common signals — HTTP 400 + "context length" / "maximum context" /
   "too many tokens" / "reduce the length"). This is a *distinct* recovery from
   the generic retry path — a context-length 400 is otherwise non-retryable.
2. Recompile the turn with the minimal `FALLBACK_*` template and **retry once**.
3. If it still fails, surface a clear, actionable error (the operator's
   `PROMPT_TARGET_TOKENS` exceeds what their gateway/model actually accepts).

This turns a permanent block from a configuration/estimate mismatch into a
one-time graceful degradation, and is the runtime complement to the registry
being best-effort (the operator owns the number; the runtime catches when the
number is wrong). Logged at WARNING (`prompt_context_error_recovered`).

---

## 8. Token accounting

`token_estimation.estimate_tokens(text, provider, model)` provides real token
counts:

- tiktoken `cl100k_base` for OpenAI/Fireworks **and** as an offline proxy for
  Anthropic (the network counter is avoided — no per-item round-trip),
- character fallback only when no tokenizer is available.

Two tiers, by design (principle 3):

- **Selection** (which compaction level for a section) may use a cheap char→token
  estimate to avoid rendering every level — bounded latency.
- **Authoritative** accounting — the reserve total, `section_budget`, and the
  final assembled-prompt measure that the backstop checks — is token-native.

---

## 9. Settings / knobs

| Setting | Default | Role |
|---|---|---|
| `PROMPT_TARGET_TOKENS` | 32000 | the jar — whole-prompt budget |
| `MODEL_CONTEXT_WINDOWS` | — | operator override of the model registry (window **and** `response_reserve`) |
| `response_reserve` (per model) | 8K–16K by model | registry field; held out of the window before allocation (§3) |
| `EVIDENCE_CONTEXT_BUDGET_FRACTION` | 0.6 | evidence **cap** (ceiling), as a fraction of `section_budget` |
| `EVIDENCE_CONTEXT_CURRENT_TURN_RESERVE_FRACTION` | 0.5 | bound on the reserved current-turn stub |
| `EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM` | 4000 | per-item upper cap |
| `KB_MAX_SOLUTION_CHARS` | 800 | per-KB-item upper cap |
| per-section caps | reuse `HISTORY_*` etc. | ceilings for pass B (already exist as "upper bounds") |
| `PROMPT_JOURNAL_MAX_TOKENS` | ~1500 | strict cap on the (high-priority) journal section (§5.1/§5.2) |
| per-section floors | new, **default 0** | optional minimums for pass A; see §15.1 (continuity is already reserved, so likely unused initially) |
| `PROMPT_USER_MESSAGE_MAX_TOKENS` | new | cap on the reserved `user_message` (§6) |
| `PROMPT_LAST_EXCHANGE_MAX_TOKENS` | new | cap on the reserved last exchange (§6) |
| `PROMPT_MIN_VIABLE_TOKENS` | new | starvation-backstop threshold (§7) |

The existing per-section constants are reinterpreted as **caps**, not
independent budgets. Floors are new and small.

---

## 10. Edge cases

- **Small/local target (e.g. 8K).** Template (~4–6K) dominates; `section_budget`
  is tiny. The **starvation backstop (§7)** fires: the small `FALLBACK_*`
  template replaces the large one, and because it now carries a current-turn stub
  slot, the prompt still contains the reserve + last exchange + the
  just-uploaded file's addressable stub (INV-1 holds) + a little content.
  Coherent, no overflow, not near-empty, and the fresh upload is never dropped.
- **Large target (e.g. 64K on an advanced model).** Greedy pass B fills more
  history verbatim, more evidence tiers, fuller journal — automatically, because
  allocation is pressure-driven. No code change, just the number.
- **Huge pasted chat message.** `user_message` cap + intake file-ification keep
  the reserve bounded (§6); the pasted data becomes compactable evidence.
- **Unknown model.** Window unknown → trust the target; no hard-limit check;
  section budgeter + starvation trigger still apply.

---

## 11. Observability

- Per-turn structured log of the section-by-section token allocation (floor
  granted / greedy fill / cap hit / compaction level chosen) and the assembled
  total vs the resolved budget vs the hard limit.
- Backstop events logged at WARNING (overflow and starvation).
- `/debug/llm-providers` surfaces the resolved budget (`prompt_target_tokens`,
  `window_known`, hard limit).

---

## 12. Invariants the allocator must satisfy

These are properties of the allocator's structure, asserted by tests (§14):

- **INV-1 (current-turn floor / INV-EC-1):** ≥1 current-turn upload is always
  present, at minimum as its `search_file`-addressable stub, regardless of how
  tight the budget is. *As built*: in the normal path via evidence being
  priority #1 with a floor (+ the evidence block's internal current-turn render);
  in the tightest-budget path via the **`FALLBACK_*` current-turn stub slot**, so
  INV-1 holds even on the starvation/overflow fallback (verified by
  `test_prompt_budget_allocator.py`).
- **INV-2 (reserve present & bounded):** every reserved item — system template,
  security constraints, `identity`/`core_context`/`milestones`/`inquiry_state`/
  `pending_action` (small by construction), `user_message`, `system_feedback`,
  the last exchange, and the current-turn stub — is bounded (§6), so the reserve
  cannot grow without bound.
- **INV-3 (fits the budget):** the assembled prompt's measured tokens are **≤ the
  model hard limit** (strict — the safety-critical bound), and **≤ resolved
  budget within the §6 `margin` tolerance** (not exact equality, since the
  Anthropic-proxy estimate is inexact).
- **INV-4 (no silent loss):** any compaction/elision/drop leaves a marker; a
  section is never quietly removed.
- **INV-5 (never negative):** `section_budget` floors at 0; an oversized reserve
  degrades via §6 caps and the §7 starvation backstop, never a negative budget.

---

## 13. Implementation status

| Component | Status |
|---|---|
| Budget number + model context-window registry | Implemented |
| Resolved budget as allocator input (§3) | Implemented |
| Top-down priority-greedy allocator with floors + caps (§4–§5) | Implemented — `context_builder._allocate_sections` |
| Pressure-driven compaction (§5.2) | Implemented (conversation fidelity by fit; caps replace turn-count triggers) |
| Reserve bounding incl. current-turn floor (§6, INV-1/INV-2) | Implemented (continuity via compact-history floor; INV-1 via evidence floor + fallback slot) |
| Overflow **and starvation** backstop (§7) | Implemented — `templates._assemble_allocated` |
| `FALLBACK_*` templates: current-turn stub slot + compact journal slot (§7) | Implemented |
| Runtime context-length-error recovery → one `FALLBACK_*` retry (§7.1) | Implemented — `milestone_engine._generate_structured_output` wrapper |
| Token-native accounting (§8) | Implemented (`TokenBudget` + sub-budget checks) |
| Rollout: `PROMPT_ALLOCATOR_ENABLED` / `PROMPT_ALLOCATOR_SHADOW` flags (§14) | Implemented (**both default OFF**) |
| Invariant test matrix (§14) | Implemented — `tests/.../test_prompt_budget_allocator.py` (17 tests) |

**Default behavior is unchanged:** both flags ship OFF, so production assembly is
the legacy path until shadow-validated and explicitly enabled.

Evidence-creation timing (when `Evidence` rows are born) is a separate concern
and is not covered here.

---

## 14. Acceptance criteria & rollout

This changes the prompt assembly on **every turn**, so it ships behind
validation, not as a flip:

**Acceptance — invariant test matrix.** Across
`{small, large target} × {short, long history} × {few, many evidence} ×
{KB on/off} ×` every dispatch path (INQUIRY, INVESTIGATING DIAGNOSIS
zones/MITIGATION/TREATMENT, knowledge-query, TERMINAL):

- assembled tokens **≤ hard limit (strict)** and **≤ resolved budget within the
  `margin` tolerance** (INV-3 — assert with tolerance, not exact equality, or the
  test flakes on estimation drift),
- the reserve is always present (INV-2) and a current-turn upload survives
  (INV-1),
- **the starvation fallback still satisfies INV-1** — a dedicated case that fires
  the `FALLBACK_*` path on a fresh upload and asserts the file's stub is present
  in the fallback prompt (so this hole cannot silently reopen),
- **the fallback retains the journal** — assert the compact journal survives the
  `FALLBACK_*` path (anti-amnesia in the tight-budget case),
- **runtime context-length recovery** — simulate a provider context-length 400
  and assert exactly one `FALLBACK_*` retry, then a clear error if it still fails,
- the starvation/overflow backstop is reachable and the fallback retains its
  safety constraints,
- no section is dropped without a marker (INV-4),
- `section_budget` is never negative (INV-5).

**Rollout — shadow before flip.** Behind a feature flag, run the new allocator
in **shadow mode**: assemble both the current and the new prompt for N turns,
log the size/section deltas (do not send the new one), and add a golden-prompt
regression test. Flip to the new allocator only after shadow output is validated.
Given prior hot-path regressions, shadow-validate first.

---

## 15. Open decisions for review

1. **Do any variable sections still need floors?** Continuity is now guaranteed
   by the reserved last exchange (§4) and the current-turn stub, so the variable
   sections may need *no* floors at all — caps + strict-priority greedy fill may
   suffice. *Recommended: start with no variable-section floors; add one only if
   a section proves it's routinely starved.*
2. **Priority of older conversation vs *historical* evidence** (§5.1 items 1–2):
   keeping evidence as one high-priority block is simplest. Splitting historical
   evidence below older conversation is more faithful but more complex.
   *Recommended: keep as one block initially.*
3. **`PROMPT_MIN_VIABLE_TOKENS` value** (§7 starvation threshold) — what minimum
   variable-content headroom is "worth a full template" before the fallback is
   the better use of the budget.
