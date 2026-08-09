# Evidence Needs Design

**Status**: Shipped — approved 2026-05-22; shipped across PRs #384–#388 (§11 is the as-built map)
**Date**: 2026-05-22 (revised after design review)
**Related Documents**:

- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md)
- [Investigation Data Models](./investigation-data-models.md)

---

## Table of Contents

1. [Background: EVIDENCE_REQUEST Today](#1-background-evidence_request-today)
2. [The Gap](#2-the-gap)
3. [Design: Evidence Needs as a Pool](#3-design-evidence-needs-as-a-pool)
4. [The Presumption-Evidence Model](#4-the-presumption-evidence-model)
5. [Creation](#5-creation)
6. [Usage](#6-usage)
7. [Lifecycle](#7-lifecycle)
8. [Integration with Existing Architecture](#8-integration-with-existing-architecture)
9. [Design Decisions](#9-design-decisions)
10. [Risks and Mitigations](#10-risks-and-mitigations)
11. [As-Built Implementation Map (Phases 1–6)](#11-as-built-implementation-map-phases-16)

---

## 1. Background: EVIDENCE_REQUEST Today

### 1.1 What Exists

`EVIDENCE_REQUEST` is defined as an `IntentType` enum value in
[`api_models.py`](../../../faultmaven/models/api_models.py) and has a
`QueryIntent` validator requiring `evidence_id`. It appears in two
places in the codebase:

**API contract** (`IntentType` enum + `QueryIntent` validator):

```python
class IntentType(str, Enum):
    EVIDENCE_REQUEST = "evidence_request"  # Request specific evidence

class QueryIntent(BaseModel):
    evidence_id: Optional[str] = Field(
        default=None, description="For evidence_request: target evidence ID"
    )
```

**LLM response schema** (`EvidenceRequestToAdd` in
[`llm_schemas.py`](../../../faultmaven/models/llm_schemas.py)):

```python
class EvidenceRequestToAdd(BaseModel):
    request_text: str = Field(max_length=500)
    priority: Literal["high", "medium", "low"] = "medium"
    purpose: str = Field(max_length=500)

class InvestigationStateUpdate(BaseModel):
    evidence_requests: List[EvidenceRequestToAdd] = Field(default_factory=list)
    mentioned_request_ids: List[str] = Field(default_factory=list)
```

**Dispatch** (in
[`investigation_service.py`](../../../faultmaven/modules/agent/domain/services/investigation_service.py)):

```python
IntentType.EVIDENCE_REQUEST: _IntentDispatchKind.NOT_IMPLEMENTED,
```

### 1.2 What Actually Happens

Neither the intent handler nor a persistent collection was ever
implemented. The LLM can emit `EvidenceRequestToAdd` entries in its
structured output, but they are:

- Not stored on the Case
- Not fed back into subsequent LLM context
- Not tracked for fulfillment
- Not connected to hypotheses or the problem statement

The system currently relies on ad hoc `suggested_follow_ups` for
evidence-gathering guidance — ephemeral, per-turn suggestions with no
cross-turn memory. The existing plumbing is dead code and will be
removed wholesale during implementation (the system is pre-production;
no backcompat is preserved).

---

## 2. The Gap

### 2.1 The Missing Demand Side

The investigation system tracks evidence **supply** comprehensively:

- `UploadedFile` — raw file metadata
- `Evidence` — claim-anchored extracts with category, source type,
  summary, and extract
- `hypothesis_evidence` — links between evidence and hypotheses with
  stance (supports / refutes / related)
- `EvidenceCategory` — categories mapping to milestones

But there is no concept of evidence **demand** — what evidence the
investigation *needs* to advance. The investigation has goals (problem
statement, hypotheses) but no explicit registry of what data would
verify or refute those goals.

### 2.2 Consequences

**1. Ad hoc file processing.** When the user uploads a file, the LLM
sees the structural index and decides what to extract from first
principles each turn. The LLM may focus on the most obvious data and
miss evidence relevant to secondary hypotheses.

**2. Ephemeral suggestions.** Evidence-gathering requests live only in
`suggested_follow_ups`, regenerated each turn. The LLM forgets what it
asked for or repeats inconsistently.

**3. No structured progress signal below milestones.** Milestones are
binary flags. There is no way to show *how close* the investigation is
to flipping a milestone.

### 2.3 The Gap Statement

> The investigation lacks a persistent, structured registry of evidence
> requirements derived from its claims. This registry would make
> evidence-gathering purposeful rather than ad hoc, provide cross-turn
> memory for user guidance, and enable a search agenda for file
> processing.

---

## 3. Design: Evidence Needs as a Pool

### 3.1 Definition

An **evidence need** is a verification requirement the investigation
has identified — *"this kind of data would help advance the case."*
Needs are stored as a **flat pool** on the case, not anchored to
specific hypotheses. The hypothesis-evidence relationship is
established at evidence-collection time through the existing
`hypothesis_evidence` junction.

```text
Investigation Pool
├── Symptom needs (motivated by the problem statement)
│   ├── [eneed_001] Response time metrics from API gateway     [PENDING]
│   ├── [eneed_002] Error rate data from monitoring dashboard  [FULFILLED → ev_abc]
│   └── [eneed_003] User impact reports                        [PENDING]
└── Causal needs (motivated by one or more hypotheses)
    ├── [eneed_004] DB connection pool metrics                 [PENDING]
    │     motivated_by: [hyp_001, hyp_003]
    ├── [eneed_005] Application connection timeout logs        [FULFILLED → ev_def]
    │     motivated_by: [hyp_001]
    └── [eneed_006] DB slow query log around incident window   [PENDING]
          motivated_by: [hyp_002]
```

### 3.2 What It IS

- A **verification-requirements registry** anchored to the
  investigation's claims (the problem statement and the set of active
  hypotheses)
- The **demand side** of the evidence model — the counterpart to
  evidence rows (supply side)
- A **search agenda** that tells the LLM what to look for when
  processing uploaded files
- The **source of truth** for evidence-gathering suggestions
- A **fulfillment tracker** showing which requirements have been met

### 3.3 What It Is NOT

- **Not anchored to a single hypothesis.** Needs sit in a flat pool;
  hypotheses are evaluated *against* the pool rather than owning slices
  of it. The same need can be relevant to multiple hypotheses;
  hypothesis-evidence stance (supports / refutes) is recorded at
  evidence-collection time via the existing junction.
- **Not a record of requests already communicated to the user.** The
  need is the underlying *requirement*; communicating it is a separate
  concern (via EVIDENCE-type suggestions).
- **Not an evidence collection.** Evidence rows are what we *have*;
  needs are what we *require*.
- **Not a plan or sequence.** The user can fulfill needs in any order,
  compatible with the opportunistic investigation method.

### 3.4 Naming

`EVIDENCE_REQUEST` and `EvidenceRequestToAdd` are renamed to reflect
demand-side semantics. **No backcompat aliases are kept** — the system
is pre-production and the existing plumbing is dead.

| Old | New |
|---|---|
| `IntentType.EVIDENCE_REQUEST` | `IntentType.EVIDENCE_NEED` (stays `NOT_IMPLEMENTED`; see §9.3) |
| `EvidenceRequestToAdd` in `faultmaven/models/llm_schemas.py` | DELETED |
| `evidence_requests` field on legacy `InvestigationStateUpdate` | DELETED |
| `mentioned_request_ids` field on legacy `InvestigationStateUpdate` | DELETED (mention-decay is fully prompt-only — see §9.7) |
| `evidence_requests` field on `LLMResponse` in `faultmaven/models/api.py:245` | DELETED (orphan; never read in the current pipeline) |
| `QueryIntent.evidence_id` | `QueryIntent.evidence_need_id` |
| (no LLM schema class existed in the current pipeline) | `EvidenceNeedUpdate` in `faultmaven/core/investigation/schemas.py` |
| (no field existed) | `evidence_need_updates: List[EvidenceNeedUpdate]` added to `DiagnosisStateUpdate`, `MitigationStateUpdate`, `TreatmentStateUpdate`, and `GeneralStateUpdate` |
| (no domain model existed) | `EvidenceNeed` |
| (no field existed) | `Case.evidence_needs: List[EvidenceNeed]` |

**Schema-shape note (2026-05-26 audit).** The single
`InvestigationStateUpdate` referenced in earlier drafts of this design
no longer exists in the active pipeline — the stage-specific schemas
(`DiagnosisStateUpdate`, `MitigationStateUpdate`, `TreatmentStateUpdate`,
`GeneralStateUpdate`) under
[`faultmaven/core/investigation/schemas.py`](../../../faultmaven/core/investigation/schemas.py)
are the live targets. The legacy `InvestigationStateUpdate` in
`faultmaven/models/llm_schemas.py` is dead code (still imported via
`faultmaven/models/api.py` into a never-read `LLMResponse` field) and
is removed wholesale in Phase 2. `InquiryStateUpdate` deliberately
does **not** carry `evidence_need_updates` because INQUIRY creates no
evidence-side state (per INV-07).

---

## 4. The Presumption-Evidence Model

### 4.1 Evidence Tests Presumptions

Every piece of evidence exists to test a **presumption** — a claim the
investigation has made about reality. Presumptions form at structurally
significant moments; evidence needs are created at those same moments.

| Presumption | When formed | What it claims | Needs created (purpose) |
|---|---|---|---|
| Problem statement | INQUIRY → INVESTIGATING (Gate 1) | These symptoms exist | `symptom_verification` |
| Hypothesis | `hypotheses_to_add` | This cause exists | `causal_verification` |

### 4.2 Presence and Absence Symmetry

Evidence proves or disproves a presumption through **presence** or
**absence** of the expected data:

| Phase | What we check | Look for | Conclusion |
|---|---|---|---|
| DIAGNOSIS | Symptom needs | **Presence** of symptoms | Problem confirmed |
| DIAGNOSIS | Causal needs | **Presence** of cause | Hypothesis validated |
| MITIGATION | Symptom needs | **Absence** of symptoms | Mitigation worked (cause may still exist) |
| TREATMENT | Causal needs | **Absence** of cause | Root cause eliminated |
| TREATMENT | Symptom needs | **Absence** of symptoms | Solution sustained (no downstream debris) |

Mitigation and solution are distinct claims requiring distinct
evidence. A mitigation only proves *symptoms are masked*; a solution
proves *the cause is gone*. The agent must direct users to collect
both types of post-fix evidence — *"the app is running healthy"* is
not the same as *"the bad config has been corrected."*

### 4.3 Four Evidence Categories

> **As-built:** the live `EvidenceCategory` enum is exactly these four
> verification categories — the presence/absence quartet.

To make the presence/absence distinction structural rather than
inferred, `EvidenceCategory` carries four categories:

| Category | Polarity | Drives milestone / signal | Example |
|---|---|---|---|
| `SYMPTOM_EVIDENCE` | presence | `symptom_verified` | `kubectl get pods` shows `CrashLoopBackOff` |
| `CAUSAL_EVIDENCE` | presence | grounded cause signal (→ `cause_state=IDENTIFIED`) | `cat config.yaml` shows `max_connections=1` |
| `SYMPTOM_ABSENCE_EVIDENCE` | absence | `mitigation_verified` → `mitigation.verified` (and contributes to `solution_verified`) | `kubectl get pods` shows `Running` |
| `CAUSAL_ABSENCE_EVIDENCE` | absence | `solution_verified` | `cat config.yaml` shows `max_connections=100` |

Gate signals (`mitigation_verified` → `mitigation.verified`,
`solution_verified`) remain LLM-set judgment calls. The new categories give the gate decision a
structural audit trail and let downstream consumers (e.g., the
runbook-generation pipeline's `Verification` section) extract
verification evidence by category.

### 4.4 One Need, Multiple Evidence Rows Across Stages

The same need produces evidence rows of different categories at
different stages:

```text
Need: "API response time metrics from gateway"  (purpose=symptom_verification)
├── Turn 5 (DIAGNOSIS):    Evidence E1  category=SYMPTOM_EVIDENCE          → need FULFILLED
├── Turn 12 (MITIGATION):  Evidence E2  category=SYMPTOM_ABSENCE_EVIDENCE  → need stays FULFILLED
└── Turn 18 (TREATMENT):   Evidence E3  category=SYMPTOM_ABSENCE_EVIDENCE  → need stays FULFILLED

Need: "DB config: max_connections setting"  (purpose=causal_verification)
├── Turn 8  (DIAGNOSIS):   Evidence E4  category=CAUSAL_EVIDENCE           → need FULFILLED
└── Turn 19 (TREATMENT):   Evidence E5  category=CAUSAL_ABSENCE_EVIDENCE   → need stays FULFILLED
```

The need's `status` does not flip back to PENDING for re-verification
(per §7.2).

> **As-built (re-verification anchor).** The diagram above holds when a
> need exists, but the re-verification checklist is **anchored on the
> confirmed presence-evidence rows (`SYMPTOM_EVIDENCE` /
> `CAUSAL_EVIDENCE`), not on FULFILLED needs.** Needs are gap-conditional
> (created only when the verifying data wasn't already in hand — see
> §5.2 step 3), so most confirmed symptoms/causes have **no** need; a
> need-anchored re-check list would silently omit them. Evidence rows
> exist for every confirmed finding, so they are the complete record of
> "what to re-check." The post-fix absence rows are **stand-alone audit
> rows** (`source_file_id` + extract) — NOT linked to a need *or* a
> hypothesis. (A successful fix *confirms* the root-cause hypothesis; a
> confidence-bearing link would erode the very hypothesis it proves — see
> §11.6.) The before/after presence↔absence pairing is deferred. See
> §7.5, §8.4, and §11.6.

### 4.5 Re-Verification Is Judgment, Not Mechanism

Re-checking symptom needs after mitigation is the **minimum** baseline
for verifying a fix. But the fundamental question — *is the problem
gone?* — may have many answers depending on the case. The LLM should
exercise judgment:

- The simplest check is whether the original symptoms are still
  present.
- There may be other ways to verify, at different confidence levels.
- The needs list tells the LLM **what to re-check**. The LLM determines
  **how to ask for it** given current context (e.g., re-framing a
  request with a post-fix time window).

The system provides the checklist. The LLM applies judgment. We do not
hard-code rules like "you must re-check ALL symptom needs after
mitigation."

---

## 5. Creation

Needs are created at structurally significant moments. The LLM
determines content (what data is needed and why); the engine provides
the trigger and persists the result. This mirrors the existing
`hypotheses_to_add` / `evidence_to_add` pattern.

### 5.1 Trigger 1: Symptom-Validation Work → Symptom Needs

When INQUIRY → INVESTIGATING fires (user confirms the problem
statement at Gate 1), the case enters INVESTIGATING. Early on — while
`symptom_verified` is still False — the LLM does symptom-validation work
and emits symptom needs.

```text
Input:  Confirmed problem statement + initial symptoms + urgency context
Output: List of symptom-verification needs (motivating_hypothesis_ids=[])
```

Each symptom need carries `purpose=symptom_verification` and an empty
`motivating_hypothesis_ids` list — these are the "permanent" needs of
the case, motivated by the problem statement rather than by any
hypothesis. They are not subject to terminal-hypothesis supersession.

Symptom-need emission/refinement can span multiple turns (the agent may
need several rounds of data inspection to set `symptom_verified=True`).
Under the unified opportunistic flow there is no path gate on this window
— causal-side work simply follows the `cause_state` rule (it runs while
the cause is uncertain), not a path commit.

### 5.2 Trigger 2: Hypothesis Created → Pool Evaluation

When the LLM emits `hypotheses_to_add`, the same turn it evaluates the
new hypothesis against the existing evidence and needs pool. The output
shape is **pool-based**, not single-anchored:

```text
For each new hypothesis hyp_new:
  1. Scan existing evidence — does anything in the pool already
     speak to hyp_new?
     → If yes, emit hypothesis_evidence_links entries (resolved
       per HypothesisEvidenceLinkToAdd) with stance.
       (Hyp may immediately become VALIDATED or REFUTED.)
  2. Scan existing PENDING needs — would any of them, when fulfilled,
     plausibly answer hyp_new?
     → If yes, emit need updates appending hyp_new.hypothesis_id to
       their motivating_hypothesis_ids.
  3. Identify data hyp_new requires that the pool doesn't yet cover.
     → Emit new EvidenceNeedUpdate entries with motivating_hypothesis_ids=[hyp_new.id].
```

The hypothesis-need relationship is **discovered**, not declared at
creation. There is no duplication: an existing relevant need is shared
across hypotheses by appending IDs to `motivating_hypothesis_ids`.

**No path gate (unified flow).** The former path-conditional emission
backstop (INV-19 / INV-21) is removed. Hypothesis emission — and the
causal-purpose `evidence_need_updates` that ride with it — is no longer
hard-gated by engine state; it is prompt-guided to run while the cause is
uncertain (`cause_state ∈ {UNKNOWN, CANDIDATES}`). Orphan-need avoidance
now rests on the prompt mandate that pairs hypothesis emission with the
uncertainty signal, plus the per-milestone surgical strip, rather than a
reject-and-resurface backstop.

**Same-turn ID resolution.** When the LLM creates a hypothesis and the
need that anchors to it in the same turn, the hypothesis has no DB ID
yet. Via the shared `IdRef` type in `core/investigation/schemas.py`,
`EvidenceNeedUpdate.motivating_hypothesis_ids`
accepts `new_index_N` placeholders (or bare integers, coerced at
schema validation) that reference the corresponding entry in
`hypotheses_to_add`. The engine resolves these via the established
`_resolve_id_ref` helper at apply time. The same pattern applies to
`EvidenceNeedUpdate.fulfilling_evidence_ids` (resolves against
`evidence_to_add`) and `EvidenceNeedUpdate.need_id` for update
emissions referencing a need created earlier in the same
`evidence_need_updates` list.

### 5.3 Out-of-Order Data Arrival

Evidence and needs can arrive in any order across turns. The LLM
behavior contract on each INVESTIGATING turn:

1. **Answer the user's question first** (or the implicit "what's in
   this file?" if an upload arrived without a question).
2. **Process this turn's uploads against the pool**:
   - Extract evidence rows; link each to matching PENDING needs via
     `fulfilling_evidence_ids` (sets need status to `FULFILLED` or
     `PARTIALLY_MET`).
   - If an upload answers something not yet on the needs list (e.g.,
     proactive evidence), the LLM may emit a new need + immediate
     fulfillment in the same turn, OR record the evidence without a
     need link if no claim it speaks to.
3. **Update the pool from hypothesis or problem-statement changes**:
   - New hypotheses → §5.2 evaluation.
   - Refined problem statement → LLM may emit need updates revising
     symptom needs.
4. **Surface unfulfilled needs as EVIDENCE-type suggestions**
   (§6.2) where it would help advance the case.

Four arrival shapes the LLM must handle:

| Shape | Example | LLM behavior |
|---|---|---|
| Upload before any needs exist | Turn 1 INVESTIGATING upload during pre-path symptom validation | Create needs + extract evidence + fulfill in one turn |
| Upload for an existing PENDING need | User uploads logs the LLM asked for | Extract evidence, link `fulfilling_evidence_ids`, mark FULFILLED |
| Upload for no existing need (proactive) | User volunteers a related file | Extract evidence; optionally create+fulfill a new need |
| Need creation with no upload | LLM emits causal needs at hypothesis creation | Pool grows; EVIDENCE suggestions surface next turn |

**No separate `<this_turn>` / `<uploads_this_turn>` block is needed.**
The existing `<evidence_collected>` block in
[`context_builder.py`](../../../faultmaven/core/investigation/prompts/context_builder.py)
already partitions fresh-from-this-turn vs. prior items via the
`fresh="true"` attribute on `<uploaded_file>` and `<evidence>` rows
(PR #352 — fresh-vs-prior partition, duplicate signal, semantic
pasted labels, Rule 5 row). Evidence needs piggyback on that surface;
§6.1 documents the single new section the prompt gains
(`<evidence_needs>`), slotted into INVESTIGATION_BASE.

### 5.4 Trigger 3: KB Cause Seeded → Seed Rung-Needs (engine-minted)

The two triggers above are LLM-emitted. There is one narrow,
deterministic exception: when the [KB cause seeder](../knowledge-and-ai/kb-cause-seeder.md)
instantiates a retrieved runbook's cause chain as a CANDIDATE
hypothesis, it also mints that cause's `rung_indicators` as needs —
without an LLM turn.

```text
Trigger: A runbook cause is seeded (INQUIRY → INVESTIGATING transition)
Output:  One PENDING causal_verification need per rung indicator,
         motivating_hypothesis_ids=[seeded hypothesis], priority=LOW
```

This is a **prior, not a gate**, and every property keeps a seeded need
mechanically identical to an LLM-emitted one (it is subject to the same
lifecycle, surfacing, and wall rules; nothing reads its origin):

- **`priority=LOW`** so it sinks in the rendered `<evidence_needs>`
  ordering. (Surfacing *selection* itself is priority- and origin-blind —
  it ranks by `request_text` rarity + rotation — so this is not a
  suppression guarantee, just a rendering-order hint.)
- **`obtainability=UNKNOWN`** (the fail-safe default), so it never
  contributes to the §5.3 declared-data-wall on its own — but it makes
  the wall honestly computable for the seeded candidate, which
  previously arrived with zero discriminators.
- **Motivated solely by the seeded hypothesis**, so §7.4
  motivator-based supersession retires it for free when that hypothesis
  goes terminal — the seeder adds no bespoke lifecycle.
- **Never auto-fulfilled** — it grounds only when a real datum arrives.

The engine is a bounded *creator* here rather than only a *lifecycle
manager* (cf. §9.6): the content is copied verbatim from a curated
runbook, not reasoned, so this does not reopen "LLM determines content."
Seeding is feature-flagged (`FAULTMAVEN_KB_CAUSE_SEEDER`); with the flag
off, no seed needs are minted.

---

## 6. Usage

The evidence needs pool has three consumers.

### 6.1 File Processing — Search Agenda

A new `<evidence_needs>` section is added to the existing
`INVESTIGATION_BASE` prompt template. It slots between `{evidence}` and
`{entity_highlights}` — a named placeholder added to the template body
and populated by the context builder:

```text
…
{evidence}              ← existing <evidence_collected> block
                          (fresh/prior partitioning already lives here
                          via PR #352's fresh="true" attribute on
                          <uploaded_file> and <evidence> rows)

{evidence_needs}        ← NEW slot

{entity_highlights}
{hypotheses}
…
```

The rendered block:

```xml
<evidence_needs>
When examining uploaded files, also look for data matching these
outstanding needs. These are not the only things to look for —
unexpected findings are equally important and may lead to new
hypotheses or revised needs.

  - [eneed_001] Response time metrics from API gateway (SYMPTOM, HIGH)
      motivated_by: problem_statement
  - [eneed_003] DB connection pool metrics (CAUSAL, HIGH)
      motivated_by: [hyp_001, hyp_003]
  - [eneed_004] Application connection timeout logs (CAUSAL, MEDIUM)
      motivated_by: [hyp_001]
</evidence_needs>
```

Only PENDING and PARTIALLY_MET needs are rendered (FULFILLED and
SUPERSEDED excluded to save tokens). Stage-specific filtering applies
— see §8.4.

**What lives in the existing `<evidence_collected>` block, not here.**
Per-turn upload signal (file_id, structural_index, fresh attribute)
and per-row evidence detail (extract, source_file_id, hypothesis
links) stay in `<evidence_collected>`. The `<evidence_needs>` block is
purely the demand-side index — it points back at evidence rows via
need IDs, but does not duplicate their content.

### 6.2 Suggestions — EVIDENCE Type, LLM-Emitted

Evidence-gathering suggestions **may draw from the pool** rather than
being purely improvised — but the pool is **not** a precondition for an
EVIDENCE suggestion. The pool's primary, load-bearing job is the
file-search agenda (§6.1); surfacing a need as a suggestion is an
*optional* secondary use of the same structure. The flow stays in the
existing [3-suggestion-type model](./investigation-data-models.md):
DECIDE / RUN / EVIDENCE / FREE_SPEECH. Evidence needs map to
**EVIDENCE-type** suggestions:

- The LLM owns suggestion emission. When a pending need is worth
  surfacing this turn it may raise it as an EVIDENCE suggestion — but it
  may equally make a contextual EVIDENCE ask that no pool need backs
  (the pre-evidence-needs behavior, still valid).
- Each `SuggestedFollowUp` with `action_type=EVIDENCE` carries an
  **optional** field: `evidence_need_id: Optional[str]` — set *only when*
  the suggestion corresponds to a pending pool need. A purely contextual
  EVIDENCE ask (e.g. an immediate "send me the error log" before any need
  is recorded) leaves it `None` and is fully valid.
- The frontend uses `evidence_need_id`, when present, for visual linkage
  (highlight, dismiss, group by need). No backend click-to-upload flow
  yet — the intent type (§9.3) stays deferred.

General DECIDE suggestions (path choices, confirmation prompts)
and FREE_SPEECH suggestions (open-ended questions) remain unchanged.
Evidence needs are a *preferred source* for EVIDENCE suggestions, not a
mandatory one and not a replacement of the suggestion mechanism — the
LLM may always make a contextual EVIDENCE ask, leaving `evidence_need_id`
`None` when it does.

### 6.3 Progress Tracking — Fulfillment Ratio

The pool provides a granular signal below milestones:

```text
Symptom needs:  1 / 3 fulfilled
Causal needs:   1 / 3 fulfilled
```

Per-hypothesis progress is qualitative — derivable by the LLM scanning
`hypothesis_evidence` density, but not a mechanical ratio. The system
exposes pool-level fulfillment counts; per-hypothesis progress is LLM
judgment.

Fulfillment is **informational, not controlling**: needs don't gate
milestones. Milestone flags (`symptom_verified`,
`root_cause_identified`, gate milestones) remain LLM-set judgment
calls. See §10.4.

---

## 7. Lifecycle

### 7.1 Status Values

| Status | Meaning |
|---|---|
| `PENDING` | Need identified, not yet fulfilled |
| `PARTIALLY_MET` | Some evidence found but insufficient |
| `FULFILLED` | Sufficient evidence collected |
| `SUPERSEDED` | No longer relevant (all motivators retired, or LLM judged irrelevant) |

### 7.2 Lifecycle Events

| Event | Effect on the pool |
|---|---|
| Evidence found matching a need | LLM emits update: status → `FULFILLED`, `fulfilling_evidence_ids` appended |
| Partial evidence found | LLM emits update: status → `PARTIALLY_MET` |
| Hypothesis retired | Engine deterministically removes hyp_id from `motivating_hypothesis_ids`; if list becomes empty AND `purpose=causal_verification`, need → `SUPERSEDED` |
| Problem statement refined | LLM may emit updates revising symptom needs (rewrite, supersede, add) |
| Mitigation applied | LLM re-checks the confirmed `SYMPTOM_EVIDENCE` rows (the re-verification checklist, §8.4) by attempting to extract `SYMPTOM_ABSENCE_EVIDENCE`; absence row stands alone vs. the problem statement. Any FULFILLED need keeps its status |
| Solution applied | LLM re-checks confirmed `CAUSAL_EVIDENCE` rows by attempting to extract `CAUSAL_ABSENCE_EVIDENCE` (stand-alone audit row — not linked to a hypothesis; a fix confirms the cause), and refreshes symptom-absence |
| LLM judges a need irrelevant | LLM emits update: status → `SUPERSEDED` (any time) |

### 7.3 No Path Backstop (unified flow)

The former path-conditional engine backstop for `evidence_need_updates`
is **removed** along with `_path_conditional_emission_restriction` and the
three restricted states (`pre_path_investigating`,
`pre_mitigation_mitigation_first`, `gate3_pending`). Under the unified
opportunistic flow there is no window in which causal-purpose needs are
hard-rejected by engine state.

Causal-purpose `evidence_need_updates` ride with hypothesis emission,
which is **prompt-guided** to run while the cause is uncertain
(`cause_state ∈ {UNKNOWN, CANDIDATES}`). Symptom-purpose needs are
emitted freely during early INVESTIGATING (§5.1). Orphan-need avoidance
now rests on the prompt mandate that couples hypothesis emission to the
uncertainty signal, plus the per-milestone surgical reasoning strip,
rather than on a reject-and-resurface backstop. This is the intentional
tier shift documented in [investigation-lifecycle-logic.md §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert)
and the INV-17/INV-21 retirement note in the lifecycle invariant matrix.

### 7.4 Terminal Hypothesis → Motivator-Based Supersession

This is a deterministic engine rule, not an LLM decision. It fires when a
hypothesis reaches either terminal state — `REFUTED` or `RETIRED`:

```python
TERMINAL_HYPOTHESIS_STATES = {HypothesisState.REFUTED, HypothesisState.RETIRED}

def on_hypothesis_terminal(case: Case, terminal_hyp_id: str):
    for need in case.evidence_needs:
        if terminal_hyp_id in need.motivating_hypothesis_ids:
            need.motivating_hypothesis_ids.remove(terminal_hyp_id)
            if (not need.motivating_hypothesis_ids
                    and need.purpose == NeedPurpose.CAUSAL_VERIFICATION
                    and need.state != NeedState.FULFILLED):
                need.state = NeedState.SUPERSEDED
                need.superseded_reason = "all motivating hypotheses are terminal"


# End of every turn, before save: sweep the whole terminal set.
def sweep_needs_for_terminal_hypotheses(case: Case):
    for h_id, h in case.hypotheses.items():
        if h.state in TERMINAL_HYPOTHESIS_STATES:
            on_hypothesis_terminal(case, h_id)
```

Notes:

- Both terminal states are swept, not retirement alone. `REFUTED` and
  `RETIRED` are equally immutable (the apply-layer refuses to revive
  either) and equally out of the differential, so a discriminator
  motivated solely by a refuted cause discriminates nothing. This rule is
  the *only* GC for an LLM-authored causal need — nothing else retires
  one — so a state left out of the sweep leaves those needs `PENDING` for
  the life of the case, where they render in `<evidence_needs>`, surface
  as asks, and appear as unmet data on the insufficient-evidence report.
- A need motivated by multiple hypotheses survives a partial sweep;
  supersession only fires when all motivators are gone.
- `symptom_verification` needs have `motivating_hypothesis_ids=[]`
  (motivated by the problem statement). They are exempt from this
  rule; only LLM judgment or problem-statement refinement can
  supersede them.
- FULFILLED needs are not auto-superseded — they remain as audit of
  what *was* collected, even if the hypothesis later goes terminal.
- The LLM can supersede explicitly at any time via update emissions.

The rule is wired as an end-of-turn sweep over **every** terminal hypothesis,
not a diff of the ones that turned terminal this turn. The supersession helper
is idempotent — it removes the hypothesis id from each motivating list on the
first pass, so later passes short-circuit — which makes re-sweeping free in the
steady state and buys two things a diff cannot give:

- A need already carrying a terminal motivator heals itself. A diff can only
  ever reach needs whose motivator turned terminal in the *same* turn, so a
  need anchored to a hypothesis that went terminal before this rule existed
  would stay `PENDING` for the life of the case. The same applies to the
  subtler shape: a need motivated by `[terminal, active]` keeps the stale id
  (nothing pruned it), so when the active motivator later goes terminal the
  list still is not empty and the need survives. Sweeping everything resolves
  both without a backfill migration.
- There is no pre-turn snapshot to keep in sync with the post-turn diff.

The apply-layer closes the matching entry point: a need emitted with an
*already*-terminal motivator has that id dropped at create/update time (and a
causal need left with no valid motivator is rejected outright), so a need that
the sweep would immediately supersede is never created in the first place.

### 7.5 Re-Verification After Mitigation/Solution

After mitigation or solution, the LLM does not mechanically flip need
statuses. Instead, the **confirmed presence-evidence rows
(`SYMPTOM_EVIDENCE` / `CAUSAL_EVIDENCE`)** serve as the record of what
symptoms and causes were established. The context builder renders these
as the re-verification checklist (§8.4); the LLM uses it to determine
what to re-check, exercising judgment about appropriate verification for
the specific case (per §4.5).

**Why evidence rows, not FULFILLED needs.** Needs are gap-conditional
(created only when the verifying data wasn't already in hand — §5.2
step 3), so most confirmed symptoms/causes have no need at all. A
need-anchored re-check list would be empty in the common case. Presence-
evidence rows exist for every confirmed finding, so anchoring on them
makes the checklist complete.

The LLM creates new `SYMPTOM_ABSENCE_EVIDENCE` / `CAUSAL_ABSENCE_EVIDENCE`
rows reflecting the post-fix state. **Both are stand-alone audit rows**
(`source_file_id` + the re-checked extract) — they are NOT linked to a
hypothesis. A successful fix *confirms* the root-cause hypothesis, so a
confidence-bearing link (the apply-layer maps any non-SUPPORTS stance to
a likelihood penalty) would erode the very hypothesis the fix proves;
re-verification records that the fix held, it does not re-litigate the
diagnosis. The absence row is the positive audit record of resolution.
(Any FULFILLED need that does exist keeps its status — needs are never
auto-reopened — but the need is not what drives re-verification. The
before/after presence↔absence *pairing* is deferred to a later step,
since the data model has no evidence↔evidence link yet.)

---

## 8. Integration with Existing Architecture

### 8.1 Relationship to `EvidenceCategory`

Evidence needs map to the (now expanded) evidence taxonomy:

| Need purpose | Category produced — presence | Category produced — absence |
|---|---|---|
| `symptom_verification` | `SYMPTOM_EVIDENCE` | `SYMPTOM_ABSENCE_EVIDENCE` |
| `causal_verification` | `CAUSAL_EVIDENCE` | `CAUSAL_ABSENCE_EVIDENCE` |

`CATEGORY_MILESTONE_MAP` extension. The map values are `List[str]`
(matching the existing engine consumer at
`milestone_engine.py:_infer_milestones`), not scalars:

```python
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE:         ["symptom_verified"],
    EvidenceCategory.CAUSAL_EVIDENCE:          ["solution_proposed"],
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE: [],
    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:  [],
}
```

The map values are milestone names consumed by the attribution
intersect (`_infer_milestones`).
`root_cause_identified` is no longer among them (INV-35): cause
identification is engine-derived from the validated causal chain
(`cause_state`), never an LLM-claimed milestone, and `MilestoneUpdates`
no longer carries the boolean — a map entry for it could attribute to
nothing. The absence categories map to `[]` deliberately: the
verification gates (`mitigation_verified`, `solution_verified`) are set
via the User-Agent Handshake / compliance detection, not by evidence
category, and the absence rows' disposition role is read directly by the
readiness gates.

**Attribution, not auto-advancement.** The map is consumed via
intersection with milestones the LLM has *already completed this turn*
(via `MilestoneUpdates`). It does not cause the engine to advance a
milestone on evidence emission alone — the mitigation/solution gate
signals (`mitigation_verified`, `solution_verified`) remain LLM-set via
the compliance-detection / handshake mechanism documented in
investigation-lifecycle-logic.md §1.4. The map answers *"when the LLM
completes milestone M and emits evidence of category C, can this row
claim attribution?"* The combination of LLM-set completion + map
intersection produces the `advances_milestones` tag on the evidence
row, which downstream consumers (runbook generation, audit reports,
telemetry) use for filtering.

### 8.2 Relationship to Hypotheses

The `hypothesis_evidence` junction is already many-to-many between
hypotheses and evidence rows. Under the pool model, that junction
becomes the *only* place where hypothesis-evidence relationship is
recorded — needs do not anchor hypotheses.

The LLM uses `hypothesis_evidence.stance` (supports / refutes /
related) per-hypothesis. The same evidence row may support one
hypothesis and refute another; both links are independent.

`motivating_hypothesis_ids` on `EvidenceNeed` is informational — it
records *why* the need exists for context and for the supersession
rule (§7.4). It is not a hard ownership association.

### 8.3 Relationship to the Post-010 Evidence Model

The strict evidence model says: files are raw data; evidence is
claim-anchored extracts. Evidence needs fit cleanly as the
**claim-anchored demands** — the demand-side counterpart:

- The need says: "we need data testing claim X"
- The evidence row says: "here is data testing claim X"
- The need's `purpose` maps to the evidence's `category` (with
  presence/absence as the orthogonal axis)

### 8.4 Relationship to Context Builder

A new `<evidence_needs>` section renders in the INVESTIGATING prompt
context. Filtering rules:

- Only `PENDING` and `PARTIALLY_MET` rendered; `FULFILLED` and
  `SUPERSEDED` excluded (token budget).
- After `symptom_verified=True`, symptom needs may be summarized
  rather than fully rendered.
- During MITIGATION / TREATMENT, a **re-verification checklist** is
  rendered from the confirmed presence-evidence rows
  (`SYMPTOM_EVIDENCE` / `CAUSAL_EVIDENCE`) — **not** from FULFILLED
  needs. Needs are gap-conditional and gap-rare; presence-evidence rows
  exist for every confirmed finding, so anchoring the checklist on them
  makes it complete (see §11.6). The two sections (outstanding needs +
  re-verification findings) render under one `<evidence_needs>` block.
- No separate `<uploads_this_turn>` section is added. Per-turn upload
  surfacing rides on the existing `<evidence_collected>` block's
  `fresh="true"` attribute (PR #352).

Expected token cost: ~50 tokens per active entry. The block is omitted
entirely when there are no outstanding needs and no confirmed findings
to re-check (progressive activation, §10.6). Because presence-evidence
rows exist whenever a symptom/cause was confirmed, the re-verification
checklist activates in MITIGATION/TREATMENT for essentially every real
investigation — that is the intended completeness, not a regression.

### 8.5 Relationship to `suggested_follow_ups`

Evidence-gathering suggestions **may draw from the pool** (optional — see
§6.2; `evidence_need_id` is set only when a suggestion matches a pending
need, else `None`). DECIDE, RUN, and FREE_SPEECH suggestions remain
LLM-improvised. The `SuggestedFollowUp` schema gains one optional field:

```python
class SuggestedFollowUp(BaseModel):
    # ... existing fields ...
    evidence_need_id: Optional[str] = Field(
        default=None,
        description=(
            "For action_type=EVIDENCE: the persistent EvidenceNeed this "
            "suggestion derives from. Used for frontend linkage and "
            "cross-turn deduplication."
        ),
    )
```

### 8.6 LLM Schema: `EvidenceNeedUpdate` + Stage Hooks

The LLM schema lives at
[`faultmaven/core/investigation/schemas.py`](../../../faultmaven/core/investigation/schemas.py)
alongside the existing `EvidenceToAdd` / `HypothesisToAdd` /
`HypothesisEvidenceLinkToAdd` schemas. Per the stage-split refactor,
`evidence_need_updates` is added as a field on the stage-specific
state-update classes (one per non-INQUIRY stage), **not** on a single
`InvestigationStateUpdate` (which no longer exists — the legacy class
in `models/llm_schemas.py` is dead code removed in Phase 2):

```python
class EvidenceNeedUpdate(BaseModel):
    """LLM-emitted: create a new need OR update an existing one."""
    need_id: Optional[IdRef] = Field(
        default=None,
        description=(
            "Set to update an existing need; omit (or omit field) to "
            "create a new one. Accepts a real need_id, a 'new_index_N' "
            "placeholder, or a bare integer coerced to 'new_index_N' "
            "(referencing a need created earlier in this same "
            "evidence_need_updates list)."
        ),
    )
    # Create-only fields: REQUIRED on create (need_id is None), enforced
    # by the model validator; OMITTED on update (immutable / leave-as-is).
    # They are Optional on the field so a fulfill/status update can omit
    # them — see §11.6. Sending request_text/rationale on update revises;
    # priority defaults to MEDIUM on create (default applied engine-side).
    purpose: Optional[Literal["symptom_verification", "causal_verification"]] = None
    request_text: Optional[str] = Field(default=None, max_length=500)
    rationale: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Why this data would help advance the investigation.",
    )
    priority: Optional[Literal["high", "medium", "low"]] = None
    motivating_hypothesis_ids: List[IdRef] = Field(
        default_factory=list,
        description=(
            "Hypotheses that motivate this need. Each entry accepts a "
            "real hypothesis_id, a 'new_index_N' placeholder referencing "
            "an entry in hypotheses_to_add, or a bare integer (coerced)."
        ),
    )
    status: Optional[Literal["pending", "partially_met", "fulfilled", "superseded"]] = (
        Field(default=None, description="Set when updating; None for create.")
    )
    fulfilling_evidence_ids: List[IdRef] = Field(
        default_factory=list,
        description=(
            "Evidence rows that fulfill this need. Each entry accepts a "
            "real evidence_id, a 'new_index_N' placeholder referencing "
            "an entry in evidence_to_add, or a bare integer (coerced)."
        ),
    )
    superseded_reason: Optional[str] = Field(default=None, max_length=500)
```

The bare-int coercion is carried by the shared `IdRef` type
(`Annotated[str, BeforeValidator(_coerce_bare_int_to_new_index)]`), not by a
per-class validator. Every field whose consumer resolves a `new_index_N`
placeholder is annotated with it; `tests/unit/core/investigation/
test_id_ref_coercion.py` pins that pairing in both directions.

Stage hooks — `evidence_need_updates` is added to each of:

- `InvestigationResponse_Diagnosis.DiagnosisStateUpdate`
- `InvestigationResponse_Mitigation.MitigationStateUpdate`
- `InvestigationResponse_Treatment.TreatmentStateUpdate`
- `InvestigationResponse_General.GeneralStateUpdate`

`InquiryStateUpdate` deliberately does **not** carry the field —
INQUIRY creates no evidence-side state per INV-07. Early-INVESTIGATING
symptom needs surface via `DiagnosisStateUpdate` (symptom-validation work
is the DIAGNOSIS stage's first zone; there is no separate path-conditional
dispatch block).

**No `mentioned_need_ids` field.** Mention-decay is fully prompt-only
in the new design (§9.7) — the LLM relies on conversation history,
not an emitted list. Adding the field would create state-management
overhead with no enforcement consumer (the
`diagnostic_reasoning_validator` workstream was removed in PR #348).

### 8.7 Persistence: New Table

Needs are persisted in a new `evidence_needs` table — not JSONB on
`cases.metadata`. The schema is small enough to migrate cheaply, and
proper structure gives:

- FK integrity from `fulfilling_evidence_ids` (via junction) to
  `evidence.evidence_id`
- Indexable queries (by case, by status, by purpose)
- Cleaner OCC story (the existing `cases.metadata` blob already carries
  several mutable collections; piling needs on top would increase
  conflict surface)

Schema (Alembic migration follows existing patterns). **As-built (§11.5):**
the shipped migration `014` adds an `organization_id` FK to both tables
and a `created_at` column to the junction; the DDL below is the
conceptual shape, migration `014` is authoritative.

```sql
CREATE TABLE evidence_needs (
    need_id              VARCHAR PRIMARY KEY,
    case_id              VARCHAR NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    purpose              VARCHAR NOT NULL,  -- 'symptom_verification' | 'causal_verification'
    request_text         VARCHAR(500) NOT NULL,
    rationale            VARCHAR(500) NOT NULL,
    priority             VARCHAR NOT NULL,  -- 'high' | 'medium' | 'low'
    status               VARCHAR NOT NULL,  -- 'pending' | 'partially_met' | 'fulfilled' | 'superseded'
    motivating_hypothesis_ids  JSON NOT NULL DEFAULT '[]',  -- list of hyp IDs
    superseded_reason    VARCHAR(500),
    created_at_turn      INTEGER NOT NULL,
    created_at           TIMESTAMP NOT NULL,
    updated_at           TIMESTAMP NOT NULL
);

CREATE TABLE evidence_need_fulfillment (
    need_id        VARCHAR NOT NULL REFERENCES evidence_needs(need_id) ON DELETE CASCADE,
    evidence_id    VARCHAR NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    linked_at_turn INTEGER NOT NULL,
    PRIMARY KEY (need_id, evidence_id)
);

CREATE INDEX ix_evidence_needs_case_status ON evidence_needs(case_id, status);
CREATE INDEX ix_evidence_needs_case_purpose ON evidence_needs(case_id, purpose);
```

`motivating_hypothesis_ids` stays as a JSON list rather than a junction
table — the list is small, mutated as a unit, and never queried by ID
across cases.

---

## 9. Design Decisions

### 9.1 `EvidenceNeed` over `EvidenceRequest`

The concept is a verification *requirement*, not a request already
communicated. `EvidenceNeed` conveys demand-side semantics and avoids
confusion with user-initiated actions.

### 9.2 Simple Status Model (No Re-Verification State Machine)

After mitigation, symptom needs remain FULFILLED. The LLM reasons about
whether symptoms persist using the pool as context. We do not introduce
a `REVERIFYING` status or automatic status flips:

- The re-verification question is a judgment call, not a mechanical
  check.
- The investigation stage (MITIGATION / TREATMENT) already tells the
  LLM what mode it's in.
- New `*_ABSENCE_EVIDENCE` rows provide the audit trail without a
  re-verification state machine.

### 9.3 `IntentType.EVIDENCE_NEED` — Deferred

The pool operates within normal `CONVERSATION` and `ENGINE` turns. The
user uploads a file or asks a question; the LLM checks it against the
pool. No special intent type is required.

`IntentType.EVIDENCE_NEED` (renamed from `EVIDENCE_REQUEST`) stays
`NOT_IMPLEMENTED` until a frontend feature specifically needs it
(e.g., a "tell me more about this need" button). This avoids
unnecessary API surface changes and keeps the intent dispatch table
honest.

**Frontend contract decision (2026-06-07).** The copilot's `IntentType`
enum lists only the intent types the frontend actually *emits* — the
same rule by which `greeting` (a backend-only classifier result) is
absent from it. Because the EVIDENCE_NEED *emit* path is deferred (there
is no click-to-upload / "tell me more" affordance yet, per §6.2), no
copilot code path constructs this intent, so the enum member should
**not** be carried for parity; it is added back — alongside the emitter
and the backend dispatch flip out of `NOT_IMPLEMENTED` — when the
feature actually ships. This is distinct from the **live**
`SuggestedFollowUp.evidence_need_id` field (§6.2), which the frontend
*does* use for visual linkage of EVIDENCE-type suggestions and which is
unaffected by the deferred emit path. Removing the deferred intent
member must not touch that display-linkage field.

### 9.4 Pool Model over Per-Hypothesis Anchoring

A single evidence need is often relevant to multiple hypotheses, and
the same evidence row may support one and refute another. The pool
model with `motivating_hypothesis_ids` captures this naturally:

- No duplication across hypotheses.
- Cross-hypothesis sharing is the default, not a special case.
- The LLM appends hypothesis IDs to existing needs rather than
  creating new ones.
- The deterministic supersession rule (§7.4) preserves engine-side
  cleanup.

Per-hypothesis progress signal is sacrificed (it's now LLM judgment
rather than a mechanical ratio); pool-level fulfillment ratios remain
mechanical.

### 9.5 Four Evidence Categories — Presence and Absence as Structure

Adding `SYMPTOM_ABSENCE_EVIDENCE` and `CAUSAL_ABSENCE_EVIDENCE` as
distinct categories (rather than a `polarity` flag on Evidence) keeps
the milestone-attribution chain flat:

- `CATEGORY_MILESTONE_MAP` extends cleanly.
- Downstream consumers (runbook generation's `Verification` section,
  audit reports) can filter by category without flag logic.
- The LLM emits a single `category` field; no orthogonal axis to
  reason about.

Mitigation and solution become distinguishable claims:

- Mitigation verified = symptom absence (workaround masks symptom)
- Solution verified = cause absence (root cause eliminated) + symptom
  absence (no downstream debris)

### 9.6 LLM as Creator, System as Lifecycle Manager

The LLM determines *what* needs exist (content, motivation,
fulfillment, supersession judgments). The system manages *when* needs
can be created (triggers), *how* they're persisted, and one
deterministic lifecycle event (motivator-based supersession when a
hypothesis goes terminal). This division keeps the LLM focused on
reasoning while the system enforces consistency.

The one bounded exception is the KB cause seeder (§5.4): it mints seed
rung-needs deterministically, without an LLM turn. This does not erode
the principle — the content is copied verbatim from a curated runbook
rather than reasoned, and the seeded needs obey every lifecycle,
surfacing, and wall rule identically (they are prior-not-gate and
provenance-blind to safety). The engine acts as a bounded *creator*
only for content it did not author.

### 9.7 No Stored Mention-Count — Prompt-Only Decay

Mention-decay (§10.5) is enforced via prompt instruction rather than
stored state. The LLM has the conversation history in context and can
see what it has previously suggested. Adding a stored `mention_count`
or `last_mentioned_at_turn` introduces state-management overhead for
a behavior the LLM can self-regulate.

**Validator-removal context (PR #348).** The
`diagnostic_reasoning_validator` workstream was removed entirely; the
Rule-2 / compliance signal moved to offline eval/CI. There is no
longer a post-generation validator that could backstop a stored
mention-count anyway — runtime suggestion-quality enforcement is
prompt-side only. If observed nagging becomes a problem in evaluation,
the right response is a transcript-based eval rule (caught offline),
not a stored field. Avoid speculative state.

---

## 10. Risks and Mitigations

The investigation works today without evidence needs. Adding this
feature changes the LLM's context, attention, and behavior. The
guiding principle: **structure rather than constraint, hints rather
than rules.** Evidence needs should inform the LLM's reasoning without
overriding it.

### 10.1 Risk: Attention Dilution

**Problem.** The structured output already includes hypotheses,
evidence extracts, milestone flags, journal entries, working
conclusion, and suggested actions. Adding need updates as another
collection could degrade performance on existing tasks.

**Mitigation: Lightweight, event-driven output.**

`evidence_need_updates` defaults to an empty list. The LLM emits
updates only when something changes:

- At problem confirmation: create symptom needs
- At hypothesis creation: pool evaluation (§5.2)
- When evidence is found matching a need: update status
- When the LLM judges a need irrelevant: emit supersession

On turns where the user asks a follow-up question with no uploads and
no new hypothesis, the LLM has nothing to emit. The prompt does not
ask the LLM to re-enumerate the pool; only to emit *changes*.

### 10.2 Risk: Tunnel Vision

**Problem.** The opportunistic method works by being open to whatever
the user provides. A pool could narrow the LLM's search scope — it
focuses on checking needs off and misses unexpected findings.

**Mitigation.** Frame needs as *"also look for,"* not *"only look
for."* The prompt framing of the `<evidence_needs>` block (per §6.1)
is explicit: needs are supplementary guidance. Unexpected findings
remain equally important and may lead to new hypotheses or revised
needs.

### 10.3 Risk: Quality Cascade

**Problem.** A vague or wrong need sits in the context every turn,
consuming tokens and potentially misleading reasoning. Bad needs
compound.

**Mitigation: Needs are mutable, not rigid.**

The LLM can revise, merge, or supersede its own needs at any time via
`EvidenceNeedUpdate` emissions. The prompt frames needs as the LLM's
working notes, not as commitments:

> Evidence needs are your working list of what data would advance the
> investigation. You created them; you can update them. If a need
> turns out to be irrelevant, vague, or superseded by new
> understanding, update or supersede it.

A **staleness heuristic** (deferred until observed): if a need has
been PENDING for N turns with no mention, the engine could surface a
hint *"this need has been pending for N turns — still relevant?"* —
nudge, not auto-action.

### 10.4 Risk: False Progress Signal

**Problem.** "3/5 symptom needs fulfilled" looks like progress. But if
the needs were poorly targeted, fulfilling them doesn't advance the
investigation.

**Mitigation: Fulfillment is informational, never controlling.**

The fulfillment ratio must never gate milestones. Milestone flags
(`symptom_verified`, `root_cause_identified`, gate milestones) remain
the LLM's judgment calls based on actual evidence. The ratio is a
hint to user and LLM; it does not determine milestone state.

Needs are working notes. Milestones are conclusions. Working notes
inform conclusions; they don't determine them.

### 10.5 Risk: Nagging

**Problem.** Persistent needs could generate the same suggestion turn
after turn. If the user can't provide a particular piece of data,
being repeatedly asked is irritating.

**Mitigation: Prompt-only mention-decay (§9.7).**

The LLM has conversation history and can see what it has previously
suggested. Prompt rule:

- First mention: full suggestion with rationale.
- Second mention: brief reminder.
- Third+ mention: stop surfacing actively (the need remains in the
  pool for file-matching, but is no longer a suggestion).
- If the user asks "what else do you need?", the LLM surfaces all
  pending needs regardless.

No stored state; the LLM self-regulates. If observed nagging persists,
add `last_mentioned_at_turn` then.

### 10.6 Risk: Overhead for Simple Cases

**Problem.** A simple case (one upload, root cause identified
immediately) gains nothing from needs but pays the prompt cost.

**Mitigation: Progressive activation.**

The `<evidence_needs>` block is rendered only when the pool is
non-empty. Simple cases may never generate needs — the LLM goes
straight from problem confirmation to evidence extraction to root
cause. Zero prompt cost, zero cognitive load.

### 10.7 Summary: Structure, Not Constraint

| Principle | Application |
|---|---|
| Needs are hints, not rules | Prompt says "also look for," not "only look for" |
| Updates are event-driven, not mandatory | LLM emits changes only when something happens |
| The pool is mutable | LLM can revise, merge, or supersede its own needs |
| Fulfillment is informational | Needs don't control milestones |
| Suggestion decay is prompt-driven | LLM self-regulates from conversation history |
| Progressive activation | No cost for simple cases |

The common thread: the evidence needs pool gives the LLM **memory and
structure** for what the investigation requires, without overriding
the LLM's judgment about what matters on any given turn. The LLM
remains the reasoner. The pool is a tool it uses, not a set of
instructions it follows.

---

## 11. As-Built Implementation Map (Phases 1–6)

> **Status (2026-05-26):** Evidence Needs shipped across PRs #384–#388.
> Sections 1–10 above describe the *design rationale*; this section is
> the **as-built map** for someone debugging the running feature. Where
> the as-built reality differs from the rationale sections, this section
> is authoritative.

### 11.1 What shipped, by phase

| Phase | PR | What landed |
|---|---|---|
| 1–3 | #384 | Foundation: migration `014`, ORM models, `EvidenceNeed` domain model, `EvidenceNeedUpdate` LLM schema, engine apply-layer |
| 4 | #385 | `<evidence_needs>` context block in `context_builder.py` |
| 5 | #386 | Lifecycle directives in prompt templates (`_EVIDENCE_NEEDS_*_BLOCK`) |
| 6 | #387 | `evidence_need_id` wire-level rendering (LLM schema field → API response, Copilot UI) |
| — | #388 | `evidence_need_id_dropped_total` metric at the response-flattening seam |

Frontend: **Copilot shipped rendering** (`SuggestionCard.tsx` marks
EVIDENCE suggestions tied to a tracked need + tests). **Dashboard has
not** — consistent with the §10 "frontend deferred" stance, but note
Copilot is already live.

### 11.2 Where the code lives (file:line entry points)

| Concern | Location |
|---|---|
| Domain model `EvidenceNeed` + `NeedPurpose`/`NeedState`/`NeedPriority` | `faultmaven/modules/case/domain/models.py` (`EvidenceNeed` ~`:1954`; enums ~`:1901`–`:1941`) |
| `EvidenceCategory` enum | `faultmaven/modules/case/domain/models.py:1221` |
| LLM schema `EvidenceNeedUpdate` + stage hooks | `faultmaven/core/investigation/schemas.py:501`; `evidence_need_updates` on Diagnosis/Mitigation/Treatment/General state-updates (~`:1044`–`:1194`); **absent from `InquiryStateUpdate` by design (INV-07)** |
| `SuggestedFollowUp.evidence_need_id` + validators | `faultmaven/core/investigation/schemas.py:897`–`929` |
| Engine apply-layer `_apply_evidence_need_updates` | `faultmaven/core/investigation/milestone_engine.py:6310`–`6637` (invoked ~`:6137`) |
| ~~Engine backstop (path-conditional rejection)~~ | **Removed in the flow redesign** — `_path_conditional_emission_restriction` / `_RESTRICTED_STATE_BLOCK_NAMES` deleted; causal-need gating is now prompt-guided by `cause_state` (§7.3). |
| Terminal-hypothesis supersession | `milestone_engine.py:_supersede_needs_on_terminal_hypothesis` (+ `_TERMINAL_HYPOTHESIS_STATES`) |
| Wire-flattening seam (`new_index_N` → real ID) | `milestone_engine.py:_flatten_follow_ups` ~`:7476`–`7530` |
| Context block `<evidence_needs>` | `context_builder.py:_build_evidence_needs_block` ~`:1753`–`1892` (line render ~`:1737`) |
| Prompt directives | `prompts/templates.py:_EVIDENCE_NEEDS_LIFECYCLE_BLOCK` ~`:1170`, `_..._SYMPTOM_ONLY_ADDENDUM` ~`:1206`, `_..._RCA_POOL_EVAL_BLOCK` ~`:1222`, `_..._REVERIFICATION_ADDENDUM` ~`:1253` |
| Persistence (save/load) | `sqlite_case_repository.py:_upsert_evidence_needs` ~`:2320`, `_load_evidence_needs_for_case` ~`:633` |
| Migration | `alembic/versions/20260526_1000_014_evidence_needs.py` |
| Metrics | `faultmaven/core/investigation/lifecycle_metrics.py:137`–`194` |

(Line numbers drift — treat as starting points, grep the symbol names to confirm.)

### 11.3 Observability surface (NOT in §1–10)

The design rationale predates the metrics layer. Four Prometheus
counters in `lifecycle_metrics.py` instrument the feature — these are
the **first things to check when debugging Evidence Needs behavior**:

| Metric | Fires when | Debug signal |
|---|---|---|
| `faultmaven_evidence_need_created_total{purpose}` | A need is created in the apply-layer | Baseline volume; sudden zero ⇒ LLM stopped emitting `evidence_need_updates` |
| `faultmaven_evidence_need_status_changed_total` | A need transitions status (incl. → SUPERSEDED) | Lifecycle churn; supersession spikes ⇒ hypothesis thrash |
| `faultmaven_evidence_need_rejected_total{state}` | **Now inert** — fired only from the path-conditional backstop, which the flow redesign removed | The counter symbol still exists in `lifecycle_metrics.py` but no longer increments; a sustained zero is expected, not a signal. Causal-need gating is now prompt-guided by `cause_state`. |
| `faultmaven_evidence_need_id_dropped_total{reason}` | A `SuggestedFollowUp.evidence_need_id` can't be resolved at the flattening seam (`reason=out_of_range` \| `missing_metadata`) | LLM emitted a stale/mis-indexed `new_index_N` suggestion ref; sustained nonzero ⇒ Phase-5 same-turn-ID prompt rule needs sharpening |

### 11.4 The wire-flattening seam (Phase 6 detail)

§8.5 describes the `evidence_need_id` *field* but not how it reaches the
wire. The LLM may reference a need created **in the same turn** via a
`new_index_N` placeholder (the real `need_id` doesn't exist until the
apply-layer runs). At response-build time, `_flatten_follow_ups`
resolves `new_index_N` against `metadata["evidence_needs_updated"]`:

- **Resolved** → `suggestion["evidence_need_id"] = <real need_id>` on the wire.
- **Unresolvable** → the field is **dropped silently** from that suggestion (the suggestion itself still renders) and `evidence_need_id_dropped_total` increments with `reason=out_of_range` (index past the list) or `missing_metadata` (key absent). This is the demand-side mirror of `evidence_need_rejected_total` on the apply side.

### 11.5 As-built deltas from §1–10

- **`EvidenceCategory` matches §4.3 exactly** — the four-member
  presence/absence verification quartet (`SYMPTOM_EVIDENCE`,
  `CAUSAL_EVIDENCE`, `SYMPTOM_ABSENCE_EVIDENCE`, `CAUSAL_ABSENCE_EVIDENCE`).
  The legacy `MITIGATION_EVIDENCE` / `SOLUTION_EVIDENCE` stage-completion
  categories were removed in the GAP-5 legacy→absence migration.
- **DB tables carry two columns beyond the §8.7 DDL:** both
  `evidence_needs` and `evidence_need_fulfillment` have an
  `organization_id` FK (enterprise tenancy, matches every other case-
  child table) and the junction has a `created_at` timestamp. The §8.7
  DDL is the conceptual shape; migration `014` is authoritative.
- **`NeedPurpose` has two values** (`symptom_verification`,
  `causal_verification`) — matches §5/§7. There is no
  mitigation/solution *need purpose*; re-verification reuses the same two
  purposes (§7.5).

### 11.6 Post-rollout corrections (2026-06-03)

Two fixes landed after the Phase 1–6 rollout, from a holistic review of
the evidence-needs lifecycle. Where they touch §1–10, **this section is
authoritative**.

- **Fulfill-path crash fixed (the create→fulfill lifecycle now works).**
  `EvidenceNeedUpdate` required `purpose` / `request_text` / `rationale`
  on *every* emission, but those are create-only/immutable on the update
  path. A bare fulfill update (`need_id` + `status=FULFILLED` +
  `fulfilling_evidence_ids`) raised 3× "Field required" → HTTP 500 → the
  turn that ingests the verifying evidence was lost → the agent
  stuck-looped and the case never resolved. Invisible for the whole
  rollout because no prior run had *created* a need to then fulfill.
  **Fix:** those four fields (incl. `priority`) are now
  `Optional[...] = None`; the model validator requires the first three
  only on create (`need_id is None`); the engine applies the MEDIUM
  `priority` default on create and uses **revise-don't-clobber** on
  update (an omitted/`""` field leaves the stored value unchanged). See
  the updated §8.6 schema. Apply-layer:
  `milestone_engine._apply_evidence_need_updates`.

- **Re-verification checklist re-anchored on evidence rows, not FULFILLED
  needs.** The MITIGATION/TREATMENT re-verification checklist (§4.4,
  §7.5, §8.4) was built from FULFILLED needs. Because need creation is
  gap-conditional (§5.2 step 3) and the structural index pre-surfaces
  most verifying data, needs are gap-rare — so the need-anchored
  checklist was empty for every symptom/cause confirmed from
  already-available data (the common case). It is now built from the
  confirmed presence-evidence rows (`SYMPTOM_EVIDENCE` /
  `CAUSAL_EVIDENCE`), which exist for every confirmed finding →
  complete. Post-fix absence rows are **stand-alone audit rows**
  (`source_file_id` + extract) — not linked to a need *or* a hypothesis
  (a fix confirms the root-cause hypothesis; a confidence-bearing link
  would erode it, and the apply-layer at `milestone_engine.py:6118`
  coerces any non-SUPPORTS stance to a likelihood penalty). Before/after
  presence↔absence pairing is deferred to a later step (no
  evidence↔evidence link in the model yet).
  `context_builder._build_evidence_needs_block` (re-verification
  section) + `templates._EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM` + the
  per-stage EVIDENCE-TYPES sections and the classification decision-tree
  step 4. This makes the "always-create a need per hypothesis" idea
  unnecessary: the pool stays demand-side (outstanding needs to look
  for), and re-verification reads the supply-side evidence record.

---

## Appendix: Open Items Tracked Elsewhere

- **Frontend UX for the needs list** — display in case UI, fulfillment
  progress bar, EVIDENCE-suggestion linkage. Out of scope for this
  document; lives in a separate frontend design ticket once backend
  lands.
- **`IntentType.EVIDENCE_NEED` activation** — deferred until a
  frontend feature needs it (§9.3).
- **Staleness heuristic** — deferred per §10.3.
- **Per-hypothesis progress signal** — deferred per §9.4; reconsider
  if eval feedback indicates need.
