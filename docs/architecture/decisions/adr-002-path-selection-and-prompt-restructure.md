# ADR-002: Investigation-Engine Path-Selection Commit Timing & Prompt Restructure

**Date:** 2026-05-24
**Status:** Draft — under review
**Decision Makers:** TBD (pending review)
**Affects:** Investigation engine state-flow (`milestone_engine.py`, `terminal_transitions.py`), prompt templates (`templates.py`), context builder, case UI adapter, `PathSelection` schema

---

## Context

Run 26 (istio-503-upstream scenario, Gemini 2.5 Pro) — the verification run for PR #356 — exposed three symptoms with a shared root cause:

| Observation | Where it manifests |
| --- | --- |
| **(a)** Agent at T2 jumped to hypothesis formulation (RCA-style) despite case being on MITIGATION_FIRST path | Conversation transcript |
| **(b)** UI case header showed RCA-path milestones on a MITIGATION_FIRST case | Frontend display |
| **(c)** T10–T15 oscillation between "DR deleted" and "DR still active"; 3 apologies; persona followed agent's lead | Conversation transcript |

Downstream of (c): 504 cascade T17–T25 (each turn ~128s, breaching 120s timeout). Cumulative-context explanation: confused conversation kept growing; Gemini latency scaled with prompt size.

### Root cause — structural, not behavioral

Two compounding architectural issues, both visible in code:

**Issue 1 — Conflicting prompt signals.** The generic `DIAGNOSIS_INSTRUCTIONS` ([templates.py:1001-1080](../../../faultmaven/core/investigation/prompts/templates.py)) mandates `"1. CREATE a hypothesis... Never skip step 1."` The mitigation-first prefix ([templates.py:2098-2176](../../../faultmaven/core/investigation/prompts/templates.py)) adds `"a causal hypothesis is NOT required at this point"` on top. The LLM resolves the contradiction by following the more imperative directive ("MUST") → hypothesizes → behaves RCA-style despite MITIGATION_FIRST path.

The mitigation-first prefix is **permissive** ("hypothesis not required") rather than **prohibitive** ("don't propose hypothesis until mitigation verified"). The permission doesn't override the universal mandate.

**Issue 2 — `path_selection` auto-computed before user commits.** At Gate 1 confirmation ([milestone_engine.py:2469](../../../faultmaven/core/investigation/milestone_engine.py)), the engine writes `case.path_selection = determine_investigation_path(...)` while `user_confirmed=False`. The path exists in case state before Gate 2 is even shown to the user. The UI displays this uncommitted path. The agent receives path-conditional prompts as if the path were chosen. Result: a "computed-but-not-confirmed" intermediate state that violates the framework's "INQUIRY is minimal" principle.

### Why this matters

The compounding effect produces the observed cascade:

1. **T1–T4:** Agent behaves RCA-style (universal hypothesis mandate wins). Hypotheses formulated. Milestones drift toward RCA-side (`root_cause_identified`).
2. **T4–T9:** Agent proposes mitigation (eventually). User accepts. mitigation_verified fires.
3. **T9+:** Case is now in post-mitigation territory. The agent's conversation history reads like RCA work; the case state is mitigation_first post-mitigation; the accepted `ProposedAction` vanishes from `<pending_action>` context. The agent has no anchor for "what was confirmed and when."
4. **T10–T15:** Agent oscillates. Persona follows.
5. **T17+:** Cumulative context (apologies, re-confirmations) breaches Gemini latency budget → 504s.

A path-conditional prompt restructure plus clean path-commit semantics eliminates the upstream cause. Everything downstream becomes structurally impossible.

---

## Architectural Principles

These are not new principles — they're explicit in [investigation-lifecycle-logic.md](../investigation-engine/investigation-lifecycle-logic.md) and [evidence-driven-investigation-framework.md](../investigation-engine/evidence-driven-investigation-framework.md). The decisions in this ADR align code with stated design.

1. **INQUIRY is minimal.** Only state changes allowed during INQUIRY:
   - Gate 1: problem statement confirmation (`inquiry.problem_statement_confirmed = True`)
   - Gate 2: path selection (user click → `case.path_selection` created with user's choice)
   - **No** `problem_verification` record (created at INQUIRY→INVESTIGATING transition)
   - **No** Evidence rows
   - **No** Hypothesis rows
   - **No** auto-computed `path_selection` materialization

2. **Hypothesis is RCA-path work only.** Hypothesis as **structured emission** (`hypotheses_to_add` in the response schema) belongs to RCA-direction work:
   - ROOT_CAUSE path: always
   - MITIGATION_FIRST path: post-Gate-3 (after `rca_after_mitigation_confirmed=True`)

   Pre-mitigation MITIGATION_FIRST work focuses on **symptom + failing-component identification**, NOT hypotheses.

3. **Evidence dependency chain.** Evidence categories have prerequisites enforced by case state:

   | Category | Requires |
   | --- | --- |
   | `symptom_evidence` | Problem statement |
   | `causal_evidence` | Hypothesis row |
   | `mitigation_evidence` | Proposed mitigation |
   | `solution_evidence` | Proposed solution |

   Without a Hypothesis, the LLM cannot create `causal_evidence` (the row would have no `linked_hypothesis_id` to reference). This naturally enforces sequencing without an explicit gate.

4. **Conversational hypothesizing is allowed; structured emission is gated.** The LLM can discuss possible causes in prose ("I suspect the routing is misconfigured because...") during any stage — investigation requires this kind of reasoning. The constraint is on **structured `hypotheses_to_add` emission**, which gates downstream evidence types. The framework's no-Evidence-during-INQUIRY rule is schema-enforced ([schemas.py:776-826](../../../faultmaven/core/investigation/schemas.py): `InquiryResponse` has no `hypotheses_to_add` field).

5. **`case.path_selection` is a commitment, not a recommendation.** When `case.path_selection` exists, the case has committed to that path. Recommendations are computed on-demand (`determine_investigation_path()` called at Gate 2 button-render time) and not stored until commitment.

6. **One gate, one purpose.** Each gate has a distinct, non-overlapping role:
   - Gate 1 — problem statement confirmation
   - Gate 2 — investigation path selection
   - Gate 3 — post-mitigation continuation (continue RCA vs close as mitigation_sufficient)

   Repurposing one gate for another's job (e.g., using Gate 3 for path selection) is rejected — it collapses semantic distinctions the framework needs.

---

## Decision

Four changes, four distinct concerns. Sequencing in [Implementation Plan](#implementation-plan) below.

### Decision 1 — Prompt restructure (template-level)

Split `DIAGNOSIS_INSTRUCTIONS` into reusable composable blocks. Path-conditional dispatch picks the right block; no more "permission on top of universal mandate."

**Current structure** ([templates.py:1001-1080](../../../faultmaven/core/investigation/prompts/templates.py)):
- `DIAGNOSIS_INSTRUCTIONS` (monolithic) mandates hypothesis creation, prescribes RCA-style diagnostic flow
- Mitigation-first prefix at [templates.py:2098-2176](../../../faultmaven/core/investigation/prompts/templates.py) — added *on top* with a permissive override

**Proposed structure** — two reusable composable blocks (module-level constants):

```python
_SYMPTOM_VALIDATION_BLOCK = """
Pre-mitigation diagnostic discipline:
- Verify the user's symptom claim against case evidence — at least one
  SYMPTOM_EVIDENCE row attributable to the current incident.
- Identify the specific failing component the proposed mitigation targets.
- A causal hypothesis is NOT required at this stage. Mitigations link to
  observed failing components, not to hypothesized causes.
- If you don't yet have symptom confirmation + failing component, your next
  action is to request or search for the specific evidence — not to propose
  a mitigation, not to formulate hypotheses.

You may discuss possible causes in prose as part of natural diagnostic
reasoning, but DO NOT emit `hypotheses_to_add` until the case is in an
RCA-appropriate stage.
"""

_RCA_DIAGNOSIS_BLOCK = """
RCA diagnostic flow:
1. CREATE a hypothesis grounded in evidence (emit `hypotheses_to_add`).
2. Categorize evidence with the hypothesis it supports or refutes
   (`causal_evidence` requires `linked_hypothesis_id`).
3. Validate or refute hypotheses with additional evidence.
4. When root cause is established with sufficient confidence, propose a
   solution (emit `solutions_to_add`).
[...full RCA-style flow as today's DIAGNOSIS_INSTRUCTIONS]
"""
```

**Path-conditional assembly** at [templates.py:2098-2176](../../../faultmaven/core/investigation/prompts/templates.py) (the existing dispatch point):

```python
ps = case.path_selection

if ps is None:
    # Pre-INVESTIGATING state. Should not happen in INVESTIGATING after
    # Decision 2 lands. Defensive only.
    adaptive_instr = _SYMPTOM_VALIDATION_BLOCK + adaptive_instr

elif ps.path == InvestigationPath.ROOT_CAUSE:
    adaptive_instr = _RCA_DIAGNOSIS_BLOCK + adaptive_instr

elif ps.path == InvestigationPath.MITIGATION_FIRST:
    if ps.mitigation_completed_at_turn is None:
        # Pre-mitigation: symptom validation only, no RCA work
        adaptive_instr = _SYMPTOM_VALIDATION_BLOCK + adaptive_instr
    elif not ps.rca_after_mitigation_confirmed:
        # Gate 3 pending: announce mitigation success, surface choice
        adaptive_instr = _GATE3_PENDING_BLOCK + adaptive_instr  # existing
    else:
        # Post-Gate-3 RCA work
        adaptive_instr = _RCA_DIAGNOSIS_BLOCK + adaptive_instr
```

**Key property:** the hypothesis mandate appears ONLY in `_RCA_DIAGNOSIS_BLOCK`. Pre-mitigation MITIGATION_FIRST cases never see it. The conflicting-signal problem disappears by construction.

**What stays unchanged:** `INQUIRY_TEMPLATE`, `_EVIDENCE_GROUNDING_BLOCK`, `_DIAGNOSTIC_REASONING_BLOCK`, `_ADVISOR_ROLE_CONSTRAINT`, `_ACTION_IMPACT_BLOCK`, `_READING_DISCIPLINE_BLOCK`, all Rule 1–8 prompt injections, the DA system instruction.

### Decision 2 — `path_selection` commit timing

`case.path_selection` exists if and only if the case has committed to a path. No intermediate "computed-but-not-confirmed" state.

**Current behavior:**
- Gate 1 confirmation triggers `_compute_inquiry_path_selection` ([milestone_engine.py:2469](../../../faultmaven/core/investigation/milestone_engine.py)) → writes `case.path_selection` with `user_confirmed=False`
- Defensive backup at [milestone_engine.py:5414](../../../faultmaven/core/investigation/milestone_engine.py) when `symptom_verified` completes (post-INVESTIGATING) — redundant if Gate 2 fired cleanly
- Mutation watchers ([milestone_engine.py:5072-5082, 5581-5585](../../../faultmaven/core/investigation/milestone_engine.py)) clear `path_selection` when `preliminary_urgency` changes — only relevant in the "computed-but-not-confirmed" window

**Proposed behavior:**
- Delete the writing parts of `_compute_inquiry_path_selection` (or rename to read-only `recommend_investigation_path_for_case()` — pure function, returns `PathSelection` without writing to case)
- Gate 1 confirmation handler ([milestone_engine.py:2462-2475](../../../faultmaven/core/investigation/milestone_engine.py)) only sets `inquiry.problem_statement_confirmed = True` — no path computation
- Gate 2 click handler ([milestone_engine.py:2484-2549](../../../faultmaven/core/investigation/milestone_engine.py)) becomes the SOLE creation site for `case.path_selection` — user's choice IS the commit
- `_path_selection_suggestions(case)` ([milestone_engine.py:628-666](../../../faultmaven/core/investigation/milestone_engine.py)) refactored to compute recommendation on-demand via the pure helper, no longer reads `case.path_selection`
- Delete the defensive write at [milestone_engine.py:5414](../../../faultmaven/core/investigation/milestone_engine.py) (or convert to `raise` if `path_selection` is somehow None when entering INVESTIGATING — invariant violation)
- Delete mutation watchers (nothing to clear if nothing was prematurely written)

**Invariant after Decision 2:** `case.path_selection is not None ⟺ case has committed to a path AND is INVESTIGATING-bound.`

### Decision 3 — `user_confirmed` field removal

After Decision 2, `case.path_selection is not None` implies user commitment. The `user_confirmed` field becomes redundant.

**Current field** ([models.py:2835-2844](../../../faultmaven/modules/case/domain/models.py)):
```python
user_confirmed: bool = Field(
    default=False,
    description="User has confirmed the selected path (Gate 2). "
    "Required before INQUIRY -> INVESTIGATING transition.",
)
user_confirmed_at_turn: Optional[int] = Field(...)
```

**Proposed:**
- Remove `user_confirmed` field entirely
- Remove `user_confirmed_at_turn` field. The `selected_by` field (line 2822-2825) already captures user-vs-system attribution; `selected_at` (line 2817-2820) captures timestamp
- Update Gate 2 enforcement check at [milestone_engine.py:6146-6149](../../../faultmaven/core/investigation/milestone_engine.py): from `path_selection is not None AND path_selection.user_confirmed=True` to just `path_selection is not None`

**Migration:** pre-production system. Per the project's no-backcompat principle (memory note `feedback_no_backcompat_pre_data`), no shim, no `_legacy_` alias. Existing cases in dev/test DBs with `user_confirmed=False` and `path_selection` set would be in an inconsistent state under the new invariant. Acceptable: clean DBs are the baseline.

### Decision 4 — Gate 2 enforcement strengthening (prompt-layer)

During the period after Gate 1 confirms but before Gate 2 is clicked, ensure the agent keeps surfacing the path-choice question prominently rather than engaging with user data without a committed path.

**Observation from Run 26 analysis:**

The engine correctly enforces `path_selection` existence before INQUIRY→INVESTIGATING. But the LLM's INQUIRY-phase prompt doesn't explicitly tell it "if Gate 2 is pending, reassert the path-choice question prominently rather than discussing user-provided data." User keeps providing data without clicking; LLM keeps engaging; the path question fades into conversation noise.

**Proposed prompt addition** (in `INQUIRY_TEMPLATE`, conditional block fires when Gate 1 passed + Gate 2 pending):

```python
_GATE2_PENDING_REMINDER = """
The user has confirmed the problem statement. You CANNOT proceed to
investigation work until the user picks an investigation path
(mitigation-first or root-cause).

If the user provides data or asks questions without picking a path,
acknowledge what they shared, then re-ask the path question
prominently. The COOPERATIVE path-selection buttons remain attached
to your response automatically.

You MUST NOT formulate hypotheses, categorize evidence, or propose
mitigations until the path is committed.
"""
```

The deterministic engine-side affordance pair (`_path_selection_suggestions(case)`) already attaches Gate 2 COOPERATIVE buttons on every turn until clicked — that backstop stays.

---

## Consequences

### What we gain

- **Eliminates the conflicting-signal problem** that produced Run 26's RCA-style drift on MITIGATION_FIRST cases (root cause of the deletion-confusion loop)
- **Aligns code with stated framework design** — "INQUIRY is minimal", "hypothesis is RCA work", "path_selection is a commitment"
- **Single source of truth** for path commitment: `case.path_selection is not None`. UI, prompts, gates all read the same signal.
- **Removes redundant state field** (`user_confirmed`) and four code paths (the two auto-compute writes + two mutation watchers) that exist solely to manage the "computed-but-not-confirmed" intermediate state
- **Probably eliminates 504 cascade** as a downstream effect — confusion loop produces context bloat; preventing the confusion at the source keeps context within Gemini's latency budget

### What we accept

- **Behavioral test surface change.** Some existing tests assume `case.path_selection` exists during INQUIRY; they'll need updating to assert `is None` instead. Counted in implementation scope.
- **Recommendation logic runs on-demand** at button-render time. Slightly more CPU work per Gate 2 surfacing (vs reading pre-computed value), but the function is microseconds.
- **Lifecycle docs require updates.** INV-19 wording (Gate 2 commit semantic), INV-20 (mutation watcher removal). Counted in implementation scope.
- **Pre-production data inconsistency.** Existing dev/test cases with `user_confirmed=False` and `path_selection` set become inconsistent under the new invariant. Per no-backcompat principle, this is acceptable for pre-production systems.

### What we explicitly defer

- **Action-tracking gap** (`<applied_actions>` block): the original investigation flagged that accepted `ProposedAction` rows disappear from `<pending_action>` context after acceptance ([context_builder.py:1792-1824](../../../faultmaven/core/investigation/prompts/context_builder.py)), potentially contributing to the T10–T15 deletion-confusion in Run 26. **Rationale to defer:** Decision 1's prompt restructure may make this moot — if the agent doesn't go RCA-style in the first place, the conversation history stays focused on mitigation work and the agent has clearer context for "what was confirmed." Verify after PR #2 ships; revisit if a subsequent run still shows action-tracking confusion.

- **UI display filter for path-aware milestones**: Observation (b) in [Context](#context) — UI shows RCA-side milestones on MITIGATION_FIRST cases. After Decision 1, RCA-side milestones shouldn't be set on MITIGATION_FIRST pre-Gate-3 cases (no hypothesis means no `root_cause_identified`). The UI display issue may resolve automatically. Revisit after PR #2 ships.

- **504 cascade**: hypothesized as downstream of confusion loop (context bloat → Gemini latency → timeout). Expected to disappear when Decision 1 prevents the confusion. If 504s persist after PR #2, that's a separate investigation (tool-loop iteration cap, response_schema vs function-calling routing, context-size budget enforcement).

- **Path-default-after-N-turns mechanism**: open question of "what if user persistently provides data without clicking Gate 2?" Currently case stays in INQUIRY indefinitely (forcing function). Acceptable as designed; flagging in case stakeholder UX consideration suggests adding a fallback. Not in scope.

---

## Implementation Plan

Three PRs in logical order. Each is independently shippable and testable.

### PR #1 — path_selection commit timing + user_confirmed removal (Decisions 2 + 3)

**Why first:** Foundational state-flow cleanup. Establishes the invariant `path_selection is not None ⟺ committed` that subsequent PRs can rely on. Removes the "computed-but-not-confirmed" intermediate state that complicates Decision 1's path-conditional dispatch.

**Scope:**
- Engine changes (delete auto-compute, refactor button-render, delete mutation watchers)
- Schema change (remove `user_confirmed` fields)
- Context-builder and UI-adapter read-side updates
- Test updates for invariant changes
- Lifecycle-doc updates for INV-19 / INV-20

**Estimated size:** ~150-250 lines code change, ~50-100 lines test updates

### PR #2 — Prompt restructure (Decision 1)

**Why second:** Builds on PR #1's clean path-commit semantic. The path-conditional dispatch can reliably read `case.path_selection.path` knowing it's committed.

**Scope:**
- New `_SYMPTOM_VALIDATION_BLOCK` and `_RCA_DIAGNOSIS_BLOCK` constants
- Refactor of lines 2098-2176 dispatch
- Removal of mitigation-first prefix (folded into the dispatch)
- Refactor of generic `DIAGNOSIS_INSTRUCTIONS` to ensure it's used only by the RCA branch
- New tests pinning path-conditional assembly
- Updates to behavioral-rules doc for path-conditionality

**Estimated size:** ~200-300 lines code change (including new tests + updated existing tests)

### PR #3 — Gate 2 enforcement strengthening (Decision 4)

**Why third:** Smaller, more targeted change. Builds on PR #1's commit-semantic and PR #2's restructured prompts.

**Scope:**
- New `_GATE2_PENDING_REMINDER` block in INQUIRY_TEMPLATE
- Conditional inclusion logic
- Test pinning the prompt content fires when Gate 2 pending
- Test that the deterministic Gate 2 button-affordance still attaches

**Estimated size:** ~50-100 lines

### Verification milestones

| After | Run | Pass criteria |
| --- | --- | --- |
| PR #1 | unit tests, manual smoke | No `path_selection` writes during INQUIRY. Lifecycle invariants pass. UI doesn't crash on `path_selection is None`. |
| PR #2 | real test scenario (istio-503-upstream) | Agent doesn't formulate hypotheses pre-mitigation on MITIGATION_FIRST cases. Conversation history stays focused on symptom + failing component until path-appropriate. Case reaches a proper terminal state without confusion loops. |
| PR #3 | scenario where persona provides data without clicking Gate 2 | Agent re-asks the path question prominently rather than engaging with the data. |

---

## Test Plan

### Unit tests (per PR)

| Test | What it pins |
| --- | --- |
| `test_path_selection_not_written_during_inquiry` | After Gate 1 confirmation, `case.path_selection is None` |
| `test_path_selection_created_on_gate2_click` | Gate 2 click creates `case.path_selection` with user's choice |
| `test_recommendation_helper_pure` | `recommend_investigation_path_for_case(case)` returns expected PathSelection without mutating case |
| `test_gate2_button_render_uses_recommendation_on_demand` | `_path_selection_suggestions` calls the helper at render time |
| `test_inv19_enforcement_path_not_none` | INQUIRY→INVESTIGATING blocked when `path_selection is None` (replaces old `user_confirmed=False` check) |
| `test_symptom_validation_block_in_premitigation_prompt` | Pre-mitigation MITIGATION_FIRST case → prompt contains `_SYMPTOM_VALIDATION_BLOCK`, NOT hypothesis mandate |
| `test_rca_diagnosis_block_in_root_cause_prompt` | ROOT_CAUSE case → prompt contains `_RCA_DIAGNOSIS_BLOCK` |
| `test_rca_diagnosis_block_after_gate3` | MITIGATION_FIRST post-Gate-3 case → prompt contains `_RCA_DIAGNOSIS_BLOCK` |
| `test_gate3_pending_block_at_gate3` | MITIGATION_FIRST + mitigation_verified + RCA not confirmed → Gate-3 block in prompt |
| `test_gate2_pending_reminder_in_inquiry_prompt` | INQUIRY + Gate 1 passed + Gate 2 not committed → reminder block in prompt |
| `test_no_gate2_reminder_when_gate1_not_passed` | Gate 1 not passed → no Gate 2 reminder (premature) |

### Integration tests

Reuse existing lifecycle-invariant test infrastructure. Key invariants to re-verify:
- INV-19 (Gate 2 commit before INVESTIGATING)
- INV-20 (mutation watcher removal doesn't break behavior)
- INV-21 (Gate 3 milestone gate)

### Real test scenarios

After all three PRs merge, run istio-503-upstream scenario. Pass criteria:
- T1: engine asks path question; T2+ if persona provides data without picking, agent re-asks rather than engaging in hypothesis work
- After Gate 2 click on MITIGATION_FIRST: agent focuses on stopping impact only
- After mitigation_verified: Gate 3 surfaces correctly
- Case reaches terminal state in ≤ 15 turns (Run 24 baseline)
- No 504 cascade (confusion loop eliminated → context stays small → turns fit in 120s budget)

---

## Alternatives Considered

| Alternative | Why rejected |
| --- | --- |
| **Modify Gate 3 to handle path selection** | Gate 3 has distinct purpose (post-mitigation continuation). Conflating with Gate 2 collapses semantics. Repurposing one gate for another's job creates a unified "disposition decision" mechanism that's harder to reason about. |
| **Add a new gate for path selection** | Gate 2 IS the path-selection gate. The issue is enforcement strengthening, not a missing gate. Adding a redundant gate increases conceptual surface without addressing the root cause. |
| **Just strengthen the mitigation-first prefix to be prohibitive (single line change)** | Treats the symptom, not the structural problem. Leaves the contradictory-signal architecture in place. Future prompt edits could re-introduce the conflict. Path-conditional dispatch is the right structural answer. |
| **Auto-default path on Gate 1 confirmation if user provides data without clicking** | Either default-to-RCA or default-to-mitigation has wrong-case scenarios (user might have already mitigated elsewhere; signal-based default isn't safe). Explicit user input via Gate 2 is the right design; we strengthen its enforcement in Decision 4 rather than bypass it. |
| **Keep `user_confirmed` field as audit trail** | After Decision 2, the field is structurally redundant — `path_selection` existence implies commitment. The `selected_by` and `selected_at` fields already capture audit information. Keeping `user_confirmed` adds schema bloat without information. |
| **Ship all 4 decisions as one PR** | Larger surface, harder to review, riskier per change. PR #1's invariant change is foundational; PR #2 can rely on it cleanly. Splitting also lets each verification milestone (especially PR #2's real-scenario run) generate empirical evidence before subsequent work. |

---

## References

- [Run 26 verdict notes](../../../.claude/projects/-home-swhouse-product/memory/project_resolution_gate_stuck_loop.md) — what motivated this design
- [investigation-lifecycle-logic.md](../investigation-engine/investigation-lifecycle-logic.md) — INQUIRY/INVESTIGATING phase semantics, gate definitions
- [evidence-driven-investigation-framework.md](../investigation-engine/evidence-driven-investigation-framework.md) — Evidence categories and dependencies
- [agent-behavioral-rules.md](../investigation-engine/agent-behavioral-rules.md) — Rule 2 (Evidence-Grounded), prompt-layer enforcement framework
- PR #356 — resolution-gate stuck loop fix (predecessor; this ADR addresses the deeper bug Run 26 revealed)
- Memory: `feedback_no_backcompat_pre_data` — basis for Decision 3's clean schema change without backcompat shim

---

## Review Checklist

- [ ] Architectural principles in [Architectural Principles](#architectural-principles) match reviewer's reading of the framework's design intent
- [ ] No conflation in the prompt restructure (Decision 1) between "structured hypothesis emission" and "conversational hypothesis discussion"
- [ ] Path-selection commit timing change (Decision 2) doesn't break any dependency missed in [Implementation Plan](#implementation-plan)
- [ ] `user_confirmed` removal (Decision 3) is acceptable per no-backcompat principle for pre-production system
- [ ] Gate 2 enforcement (Decision 4) prompt addition is appropriate scope (not over-prescriptive)
- [ ] Implementation sequencing makes sense — particularly the dependency from PR #1 to PR #2
- [ ] Test plan covers the right invariants
- [ ] Deferred items (action-tracking, UI filter, 504s) properly deferred (not prematurely closed or wrongly bundled)
- [ ] Alternatives Considered cover the design space (no major option missing)

---

## Decision Log

| Date | Decision | Rationale |
| --- | --- | --- |
| 2026-05-24 | Defer `<applied_actions>` block | May become moot after Decision 1; verify empirically before building |
| 2026-05-24 | Defer UI milestone-display filter | May resolve automatically when Decision 1 prevents RCA-milestone drift on MITIGATION_FIRST cases |
| 2026-05-24 | Reject "modify Gate 3 for path selection" | Gate 3 has distinct purpose; conflating with Gate 2 collapses semantics |
| 2026-05-24 | Reject "new gate for path selection" | Gate 2 is the right gate; what's needed is enforcement strengthening (Decision 4) |
| 2026-05-24 | Remove `user_confirmed` field as part of cleanup | Becomes redundant after Decision 2; pre-production system, no backcompat needed |
| 2026-05-24 | Sequence as 3 separate PRs not 1 bundled | Each is independently testable; PR #1 establishes invariants PR #2 relies on; PR #2's real-scenario run generates evidence before PR #3 |
