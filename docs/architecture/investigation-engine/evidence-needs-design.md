# Evidence Needs Design

**Status**: Design — Approved 2026-05-22, Implementation Pending
**Date**: 2026-05-22 (revised after design review)
**Related Documents**:
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md)
- [Investigation Data Models](./investigation-data-models.md)
- [WIP: Evidence Needs Implementation Plan](../../working/WIP-evidence-needs-implementation.md)

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

```
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

To make the presence/absence distinction structural rather than
inferred, `EvidenceCategory` carries four categories:

| Category | Polarity | Drives milestone | Example |
|---|---|---|---|
| `SYMPTOM_EVIDENCE` | presence | `symptom_verified` | `kubectl get pods` shows `CrashLoopBackOff` |
| `CAUSAL_EVIDENCE` | presence | `root_cause_identified` | `cat config.yaml` shows `max_connections=1` |
| `SYMPTOM_ABSENCE_EVIDENCE` | absence | `mitigation_verified` (and contributes to `solution_verified`) | `kubectl get pods` shows `Running` |
| `CAUSAL_ABSENCE_EVIDENCE` | absence | `solution_verified` | `cat config.yaml` shows `max_connections=100` |

Gate milestones (`mitigation_verified`, `solution_verified`) remain
LLM-set judgment calls. The new categories give the gate decision a
structural audit trail and let downstream consumers (e.g., the
runbook-generation pipeline's `Verification` section) extract
verification evidence by category.

### 4.4 One Need, Multiple Evidence Rows Across Stages

The same need produces evidence rows of different categories at
different stages:

```
Need: "API response time metrics from gateway"  (purpose=symptom_verification)
├── Turn 5 (DIAGNOSIS):    Evidence E1  category=SYMPTOM_EVIDENCE          → need FULFILLED
├── Turn 12 (MITIGATION):  Evidence E2  category=SYMPTOM_ABSENCE_EVIDENCE  → need stays FULFILLED
└── Turn 18 (TREATMENT):   Evidence E3  category=SYMPTOM_ABSENCE_EVIDENCE  → need stays FULFILLED

Need: "DB config: max_connections setting"  (purpose=causal_verification)
├── Turn 8  (DIAGNOSIS):   Evidence E4  category=CAUSAL_EVIDENCE           → need FULFILLED
└── Turn 19 (TREATMENT):   Evidence E5  category=CAUSAL_ABSENCE_EVIDENCE   → need stays FULFILLED
```

The need's `status` does not flip back to PENDING for re-verification
(per §7.2). The need acts as **memory of what to re-check**; new
evidence rows are the audit trail of presence and absence at different
times.

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
statement at Gate 1), the case enters the `pre_path_investigating`
state — INVESTIGATING with `path_selection is None` and Gate 2 still
pending (per INV-19, Gate 2 commits after `symptom_verified=True`).
This is where the LLM does symptom-validation work using the
`_PRE_PATH_DIAGNOSIS_BLOCK` dispatch block; it is also where the LLM
emits symptom needs.

```
Input:  Confirmed problem statement + initial symptoms + urgency context
Output: List of symptom-verification needs (motivating_hypothesis_ids=[])
```

Each symptom need carries `purpose=symptom_verification` and an empty
`motivating_hypothesis_ids` list — these are the "permanent" needs of
the case, motivated by the problem statement rather than by any
hypothesis. They are not subject to hypothesis-retirement supersession.

**Why `pre_path_investigating`, not "first INVESTIGATING turn".** The
engine's [path-conditional emission backstop](#73-engine-backstop)
rejects RCA-side emissions (`hypotheses_to_add`, `causal_evidence`,
`solutions_to_add`) before Gate 2 commits, but symptom-side work — and
symptom-need emission — is the *expected* activity in this window. The
window can span multiple turns (the agent may need several rounds of
data inspection to set `symptom_verified=True`); symptom-need
emission/refinement is allowed across all of them.

### 5.2 Trigger 2: Hypothesis Created → Pool Evaluation

When the LLM emits `hypotheses_to_add`, the same turn it evaluates the
new hypothesis against the existing evidence and needs pool. The output
shape is **pool-based**, not single-anchored:

```
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

**Path-conditional gating.** `hypotheses_to_add` is itself
path-restricted: per INV-19/INV-21, the engine backstop rejects
hypothesis emissions in the three restricted states
(`pre_path_investigating`, `pre_mitigation_mitigation_first`,
`gate3_pending`). Causal-purpose `evidence_need_updates` ride with the
same gate — they may only be emitted in states where hypothesis
creation is allowed (currently `_RCA_DIAGNOSIS_BLOCK` dispatch:
`ROOT_CAUSE` path, or `MITIGATION_FIRST` after Gate 3). The engine
backstop (§7.3) enforces this structurally so a non-compliant LLM
cannot create orphan causal needs during a path-restricted window.

**Same-turn ID resolution.** When the LLM creates a hypothesis and the
need that anchors to it in the same turn, the hypothesis has no DB ID
yet. Per the same pattern as `HypothesisEvidenceLinkToAdd` (PR #354 —
`_coerce_bare_int_to_new_index`), `EvidenceNeedUpdate.motivating_hypothesis_ids`
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

---

## 6. Usage

The evidence needs pool has three consumers.

### 6.1 File Processing — Search Agenda

A new `<evidence_needs>` section is added to the existing
`INVESTIGATION_BASE` prompt template. It slots between `{evidence}` and
`{hypotheses}` (analogous to how `{gate2_state}` is hooked in for the
Gate 2 reminder):

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

Evidence-gathering suggestions become **derived from the pool** rather
than improvised. The flow stays in the existing
[3-suggestion-type model](./investigation-data-models.md):
COOPERATIVE / EVIDENCE / FREE_SPEECH. Evidence needs map to
**EVIDENCE-type** suggestions:

- The LLM owns suggestion emission. It consults the needs pool and
  decides which unfulfilled needs to surface this turn.
- Each `SuggestedFollowUp` with `action_type=EVIDENCE` carries a new
  optional field: `evidence_need_id: Optional[str]` — the persistent
  need this suggestion derives from.
- The frontend uses `evidence_need_id` for visual linkage (highlight,
  dismiss, group by need). No backend click-to-upload flow yet — the
  intent type (§9.3) stays deferred.

General COOPERATIVE suggestions (path choices, confirmation prompts)
and FREE_SPEECH suggestions (open-ended questions) remain unchanged.
This is a *narrowing of the source* for EVIDENCE suggestions, not a
replacement of the suggestion mechanism.

### 6.3 Progress Tracking — Fulfillment Ratio

The pool provides a granular signal below milestones:

```
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
| Mitigation applied | LLM re-evaluates fulfilled symptom needs by attempting to extract `SYMPTOM_ABSENCE_EVIDENCE`; need status unchanged |
| Solution applied | LLM re-evaluates needs by attempting to extract `CAUSAL_ABSENCE_EVIDENCE` (and refresh symptom-absence) |
| LLM judges a need irrelevant | LLM emits update: status → `SUPERSEDED` (any time) |

### 7.3 Engine Backstop

The engine apply-layer for `evidence_need_updates` integrates with the
existing `_path_conditional_emission_restriction(case)` predicate in
[`milestone_engine.py`](../../../faultmaven/core/investigation/milestone_engine.py)
(the same predicate that gates `hypotheses_to_add`, `causal_evidence`,
and RCA-side milestone updates per INV-19 / INV-21).

**Purpose-aware rejection rule.** Not all `evidence_need_updates` are
RCA-side claims — symptom needs are emitted during the very window
where the restriction fires (`pre_path_investigating` is precisely the
symptom-validation window per §5.1). The apply-layer therefore must
distinguish by `purpose`:

```python
restricted_state = _path_conditional_emission_restriction(case)
for update in evidence_need_updates:
    if restricted_state is not None and update.purpose == NeedPurpose.CAUSAL_VERIFICATION:
        # Causal needs ride with hypotheses; same restriction applies.
        # Reject and re-surface in system_feedback so the LLM sees
        # the rejection on the next turn (mirrors the existing
        # hypotheses_to_add rejection path).
        block_name = _RESTRICTED_STATE_BLOCK_NAMES[restricted_state]
        deferral_clause = _restricted_state_deferral_clause(
            restricted_state, work="Causal evidence need"
        )
        _record_rejection(case, restricted_state, "evidence_need_updates",
                          purpose="causal_verification",
                          block_name=block_name,
                          deferral_clause=deferral_clause)
        continue
    apply_update(update)
```

Symptom-purpose emissions are always allowed; causal-purpose
emissions are rejected (and re-surfaced) whenever the predicate
returns one of `pre_path_investigating`,
`pre_mitigation_mitigation_first`, or `gate3_pending`. The rejection
message uses the same `_RESTRICTED_STATE_BLOCK_NAMES` and
`_restricted_state_deferral_clause` helpers already centralized in
the milestone engine so error phrasing stays consistent with the
existing `hypotheses_to_add` rejection messages.

**Why backstop instead of pure prompt rule.** Prompt instructions are
necessary but not sufficient — per the existing path-restricted-state
composition seam (INV-19's "audit `templates.py:_select_diagnosis_block`,
`context_builder.py:gate2_state_str`, `milestone_engine.py:_gate2_is_pending`,
and `milestone_engine.py:_is_pre_path_investigating` together"), the
engine backstop is what prevents a non-compliant LLM from corrupting
the case during a restricted window. Evidence needs inherit the same
seam: prompt-side restriction lives in the dispatch blocks (§5
Phase 5 of the plan); the engine backstop catches a slip-through.

### 7.4 Hypothesis Retirement → Motivator-Based Supersession

This is a deterministic engine rule, not an LLM decision:

```python
def on_hypothesis_retired(case: Case, retired_hyp_id: str):
    for need in case.evidence_needs:
        if retired_hyp_id in need.motivating_hypothesis_ids:
            need.motivating_hypothesis_ids.remove(retired_hyp_id)
            if (not need.motivating_hypothesis_ids
                    and need.purpose == NeedPurpose.CAUSAL_VERIFICATION
                    and need.status != NeedStatus.FULFILLED):
                need.status = NeedStatus.SUPERSEDED
                need.superseded_reason = "all motivating hypotheses retired"
```

Notes:

- A need motivated by multiple hypotheses survives the retirement of
  any subset; supersession only fires when all motivators are gone.
- `symptom_verification` needs have `motivating_hypothesis_ids=[]`
  (motivated by the problem statement). They are exempt from this
  rule; only LLM judgment or problem-statement refinement can
  supersede them.
- FULFILLED needs are not auto-superseded — they remain as audit of
  what *was* collected, even if the hypothesis is later retired.
- The LLM can supersede explicitly at any time via update emissions.

### 7.5 Re-Verification After Mitigation/Solution

After mitigation or solution, the LLM does not mechanically flip need
statuses. Instead, the pool serves as **memory of what symptoms and
causes were confirmed**. The LLM uses this memory to determine what to
re-check, exercising judgment about appropriate verification for the
specific case (per §4.5).

A fulfilled symptom need does not flip back to PENDING. The need
remains FULFILLED (the symptom *was* confirmed). The LLM may create
new evidence rows of `SYMPTOM_ABSENCE_EVIDENCE` category reflecting the
post-fix state; these rows are linked to the same need via
`fulfilling_evidence_ids` for audit but the need's status stays
FULFILLED.

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
    EvidenceCategory.CAUSAL_EVIDENCE:          ["root_cause_identified", "solution_proposed"],
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE: ["mitigation_verified", "solution_verified"],
    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:  ["solution_verified"],
}
```

**Attribution, not auto-advancement.** The map is consumed via
intersection with milestones the LLM has *already completed this turn*
(via `MilestoneUpdates`). It does not cause the engine to advance a
milestone on evidence emission alone — gate milestones
(`mitigation_verified`, `solution_verified`) remain LLM-set via the
compliance-detection / handshake mechanism documented in
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
rule (§7.3). It is not a hard ownership association.

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
- During MITIGATION / TREATMENT, fulfilled symptom and causal needs
  are surfaced as a re-verification checklist (their FULFILLED state
  is exception to the filtering rule for these stages).
- A new `<uploads_this_turn>` section surfaces files received this
  turn (separate from the `<evidence>` block) so the LLM can process
  them against the pool.

Expected token cost: ~50 tokens per active need. With ≤10 active
needs, ~500 tokens — modest in an 8,000+ token budget. The section is
omitted entirely when the pool is empty (progressive activation, §10.6).

### 8.5 Relationship to `suggested_follow_ups`

Evidence-gathering suggestions become **derived from the pool**.
COOPERATIVE and FREE_SPEECH suggestions remain LLM-improvised. The
`SuggestedFollowUp` schema gains one optional field:

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
    need_id: Optional[str] = Field(
        default=None,
        description=(
            "Set to update an existing need; omit (or omit field) to "
            "create a new one. Accepts a real need_id, a 'new_index_N' "
            "placeholder, or a bare integer coerced to 'new_index_N' "
            "(referencing a need created earlier in this same "
            "evidence_need_updates list)."
        ),
    )
    purpose: Literal["symptom_verification", "causal_verification"]
    request_text: str = Field(max_length=500)
    rationale: str = Field(
        max_length=500,
        description="Why this data would help advance the investigation.",
    )
    priority: Literal["high", "medium", "low"] = "medium"
    motivating_hypothesis_ids: List[str] = Field(
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
    fulfilling_evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence rows that fulfill this need. Each entry accepts a "
            "real evidence_id, a 'new_index_N' placeholder referencing "
            "an entry in evidence_to_add, or a bare integer (coerced)."
        ),
    )
    superseded_reason: Optional[str] = Field(default=None, max_length=500)

    @field_validator("motivating_hypothesis_ids", "fulfilling_evidence_ids",
                     "need_id", mode="before")
    @classmethod
    def _coerce_bare_int_to_new_index(cls, v):
        # Same coercion shape as HypothesisEvidenceLinkToAdd (PR #354).
        ...
```

Stage hooks — `evidence_need_updates` is added to each of:

- `InvestigationResponse_Diagnosis.DiagnosisStateUpdate`
- `InvestigationResponse_Mitigation.MitigationStateUpdate`
- `InvestigationResponse_Treatment.TreatmentStateUpdate`
- `InvestigationResponse_General.GeneralStateUpdate`

`InquiryStateUpdate` deliberately does **not** carry the field —
INQUIRY creates no evidence-side state per INV-07. Pre-path symptom
needs surface via `DiagnosisStateUpdate` (the `_PRE_PATH_DIAGNOSIS_BLOCK`
dispatch is still inside the DIAGNOSIS stage; only the rendered prompt
block changes by path).

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

Schema (Alembic migration follows existing patterns):

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

### 9.4 Pool Model over Per-Hypothesis Anchoring

A single evidence need is often relevant to multiple hypotheses, and
the same evidence row may support one and refute another. The pool
model with `motivating_hypothesis_ids` captures this naturally:

- No duplication across hypotheses.
- Cross-hypothesis sharing is the default, not a special case.
- The LLM appends hypothesis IDs to existing needs rather than
  creating new ones.
- The deterministic supersession rule (§7.3) preserves engine-side
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
deterministic lifecycle event (motivator-based supersession on
hypothesis retirement). This division keeps the LLM focused on
reasoning while the system enforces consistency.

### 9.7 No Stored Mention-Count — Prompt-Only Decay

Mention-decay (§10.5) is enforced via prompt instruction rather than
stored state. The LLM has the conversation history in context and can
see what it has previously suggested. Adding a stored `mention_count`
or `last_mentioned_at_turn` introduces state-management overhead for
a behavior the LLM can self-regulate.

**Validator-removal context (PR #348).** The
`diagnostic_reasoning_validator` workstream was removed entirely; the
Rule-2 / compliance signal moved to offline eval/CI per the
[project_rule2_eval_workstream](../../../docs/working/) note. There is
no longer a post-generation validator that could backstop a stored
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
