# Evidence Context Assembly

> **Authoritative source:** `faultmaven/core/investigation/prompts/context_builder.py`
> (`_build_evidence_context`, `_score_evidence_for_tier_a`, `get_token_budget_for_provider`).
>
> This document specifies how the `<evidence_collected>` block presented to the
> LLM each turn is assembled from a case's `Evidence` rows and `UploadedFile`
> rows under a bounded token budget.
>
> **Related docs:**
>
> - Evidence model (two-table separation): [`evidence-driven-investigation-framework.md` §5](./evidence-driven-investigation-framework.md#5-evidence-model)
> - Prompt structure and where this block is injected: [`prompt-assembly-architecture.md` §2.1](./prompt-assembly-architecture.md)
> - Lifecycle (when evidence is born): [`investigation-lifecycle-logic.md` §1.2.1](./investigation-lifecycle-logic.md)

---

## 1. What this block must do

The `<evidence_collected>` block is the LLM's window onto the case's data. Per
turn it renders two kinds of item:

- **Evidence rows** (`case.evidence`) — claim-anchored extracts, each with a
  `source_file_id` back to an `UploadedFile` (or `source_type=USER_DESCRIPTION`
  for chat-quoted rows with no file).
- **Uploaded files** (`case.uploaded_files`) — raw data with a preprocessing
  `structural_index`. A file becomes "orphan" when no `Evidence` row references
  it yet — which is always true of a file on the turn it is uploaded, because
  evidence is born reactively (the LLM emits `evidence_to_add` only after it
  reads the file's content).

Two hard requirements follow:

1. **The current-turn upload must always be visible.** The single highest-value
   item for a turn's task is the file the user just uploaded. The prompt's
   `_FILE_SELECTION_DEFAULT` rule even tells the LLM "default search target: the
   file uploaded this turn." The context builder must honor that — the
   current-turn file is never evicted by older evidence.
2. **No item silently vanishes.** Under budget pressure, items degrade from full
   structural index to summary-only; they are not dropped without a trace.

## 2. Model: current-turn priority floor + graceful tiered fill

Assembly renders a **reserved current-turn floor first**, then fills the
remaining budget with the historical evidence tiers, which degrade gracefully
(full structural index → summary) instead of dropping at a cliff.

### 2.1 Current-turn floor (reserved, highest priority, bounded)

Current-turn items — each orphan `UploadedFile` with `uploaded_at_turn ==
current_turn`, and each file-backed `Evidence` row with `collected_at_turn ==
current_turn` — are prioritized into a **reserved slice** of the budget
(`current_turn_reserve_fraction`, default `0.5`, of the evidence budget,
floored at one `max_chars_per_item`):

- **Current-turn orphan files** render first, in a dedicated floor pass. Each one
  is **always present**: rendered in full (structural index, per-item capped)
  while the reserve has room — the *first* is guaranteed full even if it alone
  exceeds the reserve — and otherwise as a **summary stub** (the `<uploaded_file>`
  tag with `file_id`/`searchable` and a "use search_file" note, no body). They
  are marked handled so the historical tiers neither re-render nor drop them.
- **Current-turn file-backed evidence** is forced into Tier A and rendered before
  historical Tier-A items, and is **exempt from the budget downgrade only while
  the reserve has room**. Once the reserve is spent it degrades to a Tier-B
  summary like any other item — so N current-turn evidence rows cannot render in
  full without bound and blow the budget.

The guarantee is therefore: a current-turn item is **always present** (full
within the reserve, summary beyond), never evicted by historical evidence, and
the current-turn spend is **bounded** by the reserve (except the single
guaranteed-full first orphan). This honors the prompt's `_FILE_SELECTION_DEFAULT`
rule without letting current-turn input overflow the evidence budget.

Scope note: the floor covers current-turn **file-backed** items. Current-turn
**chat-extracted** evidence (`source_file_id IS NULL`, e.g. a snippet the user
pasted into chat this turn) is rendered in Tier C under the shared budget, not
the reserved floor.

### 2.2 Historical tiers (graceful fill of remaining budget)

The remaining budget renders the historical items through the existing tiers,
ranked by `_score_evidence_for_tier_a` (hypothesis linkage +3, diagnostic data
type +2/+1, structural richness +1, time-window coverage +4, pre-mitigation
up-weight +5, recency 0–1). The +5 pre-mitigation term fires only after a
mitigation verifies (`progress.mitigation.completed_at_turn` set and the
current turn past it) and applies to evidence collected at or before that
boundary — post-mitigation telemetry typically shows a stabilized system that
no longer exhibits the root cause's signature, so the RCA-relevant window is
deliberately weighted to match/exceed the time-window bonus (INV-24 context;
see [Lifecycle Logic §2](./investigation-lifecycle-logic.md)):

- **Tier A** — top `recent_count` file-backed evidence by score → full structural
  index.
- **Tier B** — remaining file-backed evidence → summary only.
- **Tier C** — chat-extracted evidence (`source_file_id IS NULL`) → summary only.
- **Tier D** — historical orphan uploads (older than the current turn, no Evidence
  row) → full structural index, newest-first.

### 2.3 No cliff — graceful degradation

When the budget is exhausted mid-fill, the builder **skips** (does not `break`)
the over-budget item and continues — so a single large item never drops every
lower-ranked item behind it. A Tier-A item that doesn't fit downgrades to a
Tier-B summary; a historical orphan that doesn't fit is skipped. Historical
orphans are filled **greedily, newest-first** — newer orphans are *attempted*
before older ones, but this is a greedy fit, not a strict newest-wins policy: a
large newer orphan may be skipped while a smaller older one fits. Current-turn
items are unaffected by this — they are handled by the reserved floor (§2.1)
before the historical fill runs, so nothing here can evict them.

## 3. Token budget (model-aware)

The budget is derived from the active model, not a fixed character count.

- `build_investigation_context` already receives `provider_name` / `model_name`
  and computes the whole-prompt budget via `get_token_budget_for_provider`.
  `_build_evidence_context` receives the same values and sizes the evidence block
  as a fraction (`evidence_budget_fraction`, default `0.6`) of that prompt
  budget, clamped to `[min_total_tokens, max_total_tokens]`.
- Accounting is in **tokens** (via `faultmaven.utils.token_estimation`), with the
  char≈token/4 fallback when a tokenizer is unavailable. The per-item cap
  (`max_chars_per_item`) still bounds any one structural index.
- Defaults (see `InvestigationContextSettings`): a Gemini-class 15K-token prompt
  budget yields ~9K tokens (~36K chars) for evidence — more than double the old
  flat 16K-char cap — while a 6K-token Cohere/Fireworks budget yields a
  proportionally smaller, safe evidence block. The budget shrinks and grows with
  the model instead of being a single magic constant tuned for none of them.
- Accounting stays character-based internally (the `TokenBudget` 4-chars≈1-token
  approximation). When no `provider_name` is supplied (tests, internal callers),
  the effective cap falls back to `max_total_chars` (default 16000) so existing
  behavior and budget-squeeze tests are unchanged. The current-turn floor reserves
  `current_turn_reserve_fraction` (default `0.5`) of the effective cap.

## 4. Tool addressability of fresh uploads

Visibility (§2) is necessary but not sufficient: the LLM must also be able to
*target* a fresh file. Both raw-data tools resolve a `file_id` as well as an
`ev_…` id:

- `search_file` already resolves an `evidence_id` against `case.evidence` and
  falls back to `case.uploaded_files` by `file_id`.
- `deep_analysis` resolves the same way (this redesign adds the `file_id`
  fallback; previously it accepted only `evidence_id`, so a fresh upload with no
  `Evidence` row was unreachable — the LLM would reuse the nearest existing
  `evidence_id` and analyze the wrong file).

Together, §2 + §4 close the upload→visibility→addressability loop without
requiring an `Evidence` row to exist first.

## 5. Rejected alternative — auto-promoting uploads to Evidence stubs

An earlier inline note in the Tier-D code anticipated a "layer-3 fix:
engine-side auto-promotion of `UploadedFile` to `Evidence` stubs" so that every
upload always has an `Evidence` row (and thus an `evidence_id` handle).

This is **rejected**. The post-010 evidence model defines `Evidence` as
*claim-anchored*: a row asserts a specific symptom/causal/mitigation/solution
claim and feeds `CATEGORY_MILESTONE_MAP`, hypothesis linkage, and the Tier-A
value score. A stub has no claim; injecting claimless rows would pollute
milestone attribution and evidence scoring, and would contradict the
source-invariant's intent. The current-turn floor (§2) plus `file_id`
addressability (§4) deliver the same outcome — the fresh file is always seen and
always targetable — without manufacturing semantically-empty evidence. The LLM
still creates real `Evidence` for the file when it makes a claim about it, on the
same turn or a later one.

## 6. Invariants (for regression tests)

- **INV-EC-1 (current-turn presence):** when one or more files are uploaded on
  turn `T`, the rendered block at turn `T` contains **every** such file's
  `file_id` (full or as a summary stub), even when the case already has `Evidence`
  rows that fill the historical budget and even for multiple uploads in one turn.
- **INV-EC-1b (current-turn bound):** total current-turn full-renders are bounded
  by the reserve (except the single guaranteed-full first orphan); the block does
  not overshoot the budget by an unbounded amount when many current-turn items
  exist.
- **INV-EC-2 (no cliff):** an oversized item is skipped and the fill continues;
  it does not `break` and drop every lower-ranked item behind it.
- **INV-EC-3 (graceful degrade):** a Tier-A evidence item that cannot be rendered
  in full downgrades to a Tier-B summary, and a current-turn orphan that cannot
  fit the reserve degrades to a summary stub — rather than vanishing. Historical
  orphans are filled greedily newest-first (a large newer orphan may be skipped
  while a smaller older one fits — a greedy fit, not a strict newest-wins rule).
- **INV-EC-4 (model-aware):** the evidence token budget scales with
  `get_token_budget_for_provider(provider, model)`.
- **INV-EC-5 (addressable):** `deep_analysis` and `search_file` both resolve a
  bare `file_id` for a file that has no `Evidence` row.
