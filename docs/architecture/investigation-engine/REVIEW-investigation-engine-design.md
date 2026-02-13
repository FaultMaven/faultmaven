# Investigation Engine Design Review

**Date:** 2026-02-13
**Scope:** `docs/architecture/investigation-engine/` (8 documents) cross-referenced against `faultmaven/core/investigation/` (14 source files)
**Method:** Documentation-to-implementation gap analysis with schema consistency validation

---

## Executive Summary

The investigation engine is architecturally ambitious — a milestone-based, opportunistic investigation framework with hypothesis lifecycle management, degraded mode handling, and multi-LLM structured output. The core concepts are sound: data-driven milestone completion, User-Agent Handshake for terminal transitions, and three-tier milestone attribution.

However, the review identified **7 major design flaws** and **12 significant gaps** spanning schema inconsistencies, unimplemented documented behavior, race conditions, and missing validation enforcement. The most critical issues are in the hypothesis confidence model, the `current_stage` computed property under MITIGATION_FIRST path, and the evidence validation pipeline being advisory-only.

---

## Major Design Flaws

### 1. `current_stage` Computed Property Breaks Under MITIGATION_FIRST Path

**Location:** `investigation-data-models.md` Section 1.4; implementation in Case domain model

**Problem:** The `current_stage` property determines the investigation stage — and therefore which schema the LLM receives — based on which milestones are completed:

```python
@property
def current_stage(self) -> InvestigationStage:
    if self.solution_proposed or self.solution_applied or self.solution_verified:
        return InvestigationStage.SOLUTION
    if self.root_cause_identified:
        return InvestigationStage.HYPOTHESIS_VALIDATION
    if self.symptom_verified:
        return InvestigationStage.HYPOTHESIS_FORMULATION
    return InvestigationStage.SYMPTOM_VERIFICATION
```

This assumes milestones are reached in ROOT_CAUSE order: symptom → hypothesis → root cause → solution. But the MITIGATION_FIRST path (`investigation-lifecycle-logic.md` Section 2.0, Section 4.4) reaches `mitigation_applied` and `solution_proposed` **before** `root_cause_identified`. This means:

- A MITIGATION_FIRST case that has applied mitigation will be classified as `SOLUTION` stage
- The LLM receives `InvestigationResponse_Resolution` (focused on solution verification)
- But the investigation still needs to perform root cause analysis — the schema lacks fields for `hypotheses_to_add`, `hypothesis_evidence_links`, and `root_cause_conclusion`
- The agent is structurally incapable of performing RCA after mitigation

**Impact:** Cases following the MITIGATION_FIRST path cannot properly transition to root cause analysis. The schema selection mechanism forces them into SOLUTION-only interactions.

**Recommendation:** Introduce an `investigation_path` field on InvestigationProgress that the stage computation respects. For MITIGATION_FIRST cases post-mitigation, `current_stage` should return `HYPOTHESIS_FORMULATION` or a new `POST_MITIGATION_RCA` stage rather than `SOLUTION`.

---

### 2. Hypothesis Confidence Model Has a Structural Flaw — Decay Applied to Stale Base

**Location:** `hypothesis_manager.py:353-377` (decay), `hypothesis_manager.py:186-240` (evidence-based update)

**Problem:** Two independent mechanisms modify hypothesis likelihood:

1. **Evidence-based update** (`update_likelihood_from_evidence`): recalculates from scratch using `initial_likelihood + (0.15 × supporting) - (0.20 × refuting)`
2. **Stagnation decay** (`apply_likelihood_decay`): applies `likelihood × 0.85^iterations_without_progress`

These interact badly. Evidence-based update resets likelihood to a formula-derived value, completely overwriting any decay. Conversely, decay operates on the current likelihood which may already incorporate evidence adjustments. The result:

- **Scenario A:** Hypothesis has 3 supporting evidence (likelihood = initial + 0.45). It then stagnates for 2 turns, decay brings it down. Then 1 more supporting evidence is added — `update_likelihood_from_evidence` recalculates from `initial + 0.60`, completely erasing the decay penalty.
- **Scenario B:** Decay runs on a likelihood that was already adjusted by evidence. When evidence-based update runs next, it resets to the formula, creating a "sawtooth" oscillation pattern.

The two mechanisms have no coordination. The documentation (`hypothesis_manager.py` docstring line 22) says "Evidence-ratio based: initial + (0.15 × supporting) - (0.20 × refuting)" and separately "Confidence decay for stagnation: base × 0.85^iterations_without_progress" — but never describes how these compose.

**Impact:** Hypothesis confidence values are unreliable. The system may validate or refute hypotheses based on values that don't reflect actual investigative certainty.

**Recommendation:** Either (a) make decay a persistent modifier that evidence-based update incorporates (e.g., `(initial + evidence_delta) × decay_factor`), or (b) track decay and evidence contributions as separate signals that the confidence aggregator merges.

---

### 3. Evidence Validation Is Advisory-Only — Milestones Advance Regardless

**Location:** `evidence_processor.py:10-17` (design decision comment); `milestone_engine.py:197-390` (reasoning validation)

**Problem:** The evidence processor documentation is explicit:

> "The LLM is the sole authority for milestone advancement. If the LLM claims a milestone without citing evidence, the claim is logged as a warning but still applied."

Similarly, `validate_reasoning_first()` collects errors but the calling code in `_process_response_structured()` only logs them — it does not reject the milestone advancement.

This means:

- LLM can complete `root_cause_identified` without any CAUSAL_EVIDENCE (logged warning, milestone still advances)
- LLM can complete `symptom_verified` without analyzing any evidence (reasoning validation logs error, milestone still set)
- `validate_milestone_claims()` results are purely informational

The documented expectation (`prompt-engineering-guide.md` Section 13) is that the Reasoning-First pattern prevents arbitrary milestone completion. But there is no enforcement gate — only observability.

**Impact:** Investigation quality depends entirely on LLM prompt compliance. A poorly-performing LLM provider can advance milestones to RESOLVED without meaningful evidence, producing low-quality investigations with no system-level safeguard.

**Recommendation:** Add a configurable enforcement level. At minimum, `root_cause_identified` (which requires 2+ CAUSAL_EVIDENCE per `MILESTONE_EVIDENCE_EXPECTATIONS`) should reject advancement when validation fails. A three-tier enforcement model (PERMISSIVE/WARNING/STRICT) would allow gradual tightening.

---

### 4. Duplicate `return` Statement in `create_hypothesis` — Dead Code

**Location:** `hypothesis_manager.py:114-116`

```python
    def create_hypothesis(self, ...):
        ...
        hypothesis = Hypothesis(...)
        self.logger.info(...)
        return hypothesis    # Line 114 — returns here

        return hypothesis    # Line 116 — DEAD CODE, never reached
```

**Impact:** Functional: none (first return executes). But this is a code quality red flag indicating either a merge artifact or incomplete refactor. The dead code suggests the function body was modified without cleanup.

---

### 5. `update_hypothesis_likelihood` Has Inconsistent Decay Tracking

**Location:** `hypothesis_manager.py:245-285`

**Problem:** The method contains commented-out lines:

```python
if abs(new_likelihood - old_likelihood) >= 0.05:
    # hypothesis.last_progress_at_turn = current_turn # Removed from model
    # hypothesis.iterations_without_progress = 0 # Removed from model
```

But `update_likelihood_from_evidence` (line 228-232) still uses these same fields actively:

```python
if abs(new_likelihood - old_likelihood) >= 0.05:
    hypothesis.last_progress_at_turn = turn
    hypothesis.iterations_without_progress = 0
```

And `apply_likelihood_decay` (line 364-368) depends on `iterations_without_progress`:

```python
if hypothesis.iterations_without_progress > 0:
    decay_factor = 0.85**hypothesis.iterations_without_progress
```

So `last_progress_at_turn` and `iterations_without_progress` are NOT removed from the model — they exist and are used. The comments in `update_hypothesis_likelihood` are wrong, and the method fails to update stagnation tracking. If `update_hypothesis_likelihood` is called instead of `update_likelihood_from_evidence`, the stagnation counter never resets, causing premature decay.

**Impact:** Depending on which update path is taken, the same hypothesis can have divergent stagnation tracking behavior, leading to incorrect decay or missed stagnation detection.

**Recommendation:** Remove the misleading comments and restore the stagnation tracking logic in `update_hypothesis_likelihood`, or consolidate the two methods.

---

### 6. Natural Language Intent Detection Has Dangerous Pattern Overlaps

**Location:** `milestone_engine.py:940-1098`

**Problem:** The pattern matching for status transitions uses substring matching with overlapping patterns:

```python
abandonment_patterns = ["abandon", "give up", ...]
resolve_patterns = ["solution worked", "working now", ...]
```

The pattern `"abandon"` will match any message containing the substring "abandon", including:
- "I don't want to abandon this investigation" (user wants to CONTINUE, not close)
- "The team abandoned the old deployment" (discussing context, not requesting closure)

Similarly, `"working now"` matches:
- "I'm working now on getting the logs" (user is providing data, not confirming resolution)
- "The service is working now" (legitimate resolution confirmation)

The code checks abandonment before resolution (line 1028), meaning if a user says "The fix is working now, we can abandon the workaround", the case gets force-closed as abandoned.

**Impact:** False positive pattern matches can force irreversible terminal transitions (CLOSED) when the user did not intend to close the case.

**Recommendation:** These patterns need negative lookahead or context awareness. At minimum, require the matched pattern to be the dominant intent of the message (e.g., message starts with pattern, or pattern is in an imperative sentence). Better: route ambiguous matches through the LLM for intent confirmation rather than executing immediately.

---

### 7. Token Budget Uses Crude Character Approximation — Breaks for Non-Latin Text

**Location:** `context_builder.py:183-207`

```python
class TokenBudget:
    """Simple character-based token approximation (1 token ~= 4 chars)"""
    def __init__(self, limit_tokens: int = 8000):
        self.limit_chars = limit_tokens * 4
```

The 1:4 char-to-token ratio is a rough approximation for English ASCII text. It systematically underestimates tokens for:
- CJK characters (1 character ≈ 1-2 tokens)
- Code snippets with symbols and whitespace
- Log data with timestamps and structured formats
- JSON/XML in evidence

For a user investigating an issue with Chinese log messages, the budget could allow 2-3x more tokens than intended, causing context window overflow and LLM errors.

**Impact:** Context window overflows for non-English investigations. The LLM error handler would then retry with the same oversized context, eventually escalating to failure.

**Recommendation:** Use `tiktoken` (for OpenAI) or provider-specific tokenizers for accurate counts. As a lighter alternative, use a 1:2 ratio as a safer approximation, or provide per-provider character ratios.

---

## Significant Gaps

### Gap 1: No Persistence of Breakout Action Prompt Injection

**Location:** `stagnation_detector.py:332-338`, `milestone_engine.py:1254-1264`

When stagnation is detected, a `BreakoutAction` with a `prompt_injection` string is created and stored in metadata. But the next turn's prompt generation (`get_prompt_for_case`) does not consume this injection. The metadata key `breakout_prompt_injection` is recorded but never fed back into the prompt template. The stagnation detection fires, the breakout action is logged, but the LLM never receives the corrective instruction.

**Recommendation:** `build_investigation_context()` should check for pending breakout prompts (stored on the case or in the last turn's metadata) and include them in the system feedback section.

---

### Gap 2: `solution_verified` Milestone Missing From Evidence Expectations

**Location:** `evidence_processor.py:33-77`

`MILESTONE_EVIDENCE_EXPECTATIONS` defines requirements for 8 milestones but omits `solution_verified`. This milestone is arguably the most important (it gates RESOLVED status), yet has no evidence validation expectations. The User-Agent Handshake handles the transition, but there's no check that any verification evidence exists.

**Recommendation:** Add `solution_verified` with at minimum 1 RESOLUTION_EVIDENCE item expected.

---

### Gap 3: Degraded Mode Has No Exit Condition in Code

**Location:** `stagnation_detector.py:313-338`, `milestone_engine.py:1244-1264`

The `error-handling-and-recovery.md` document (Section 5) describes a `check_degraded_mode_exit()` function that exits degraded mode when `progress_made == True`. But the actual implementation in `milestone_engine.py` only enters degraded mode via `StagnationBreaker._handle_no_progress()` — there is no code path that clears `case.degraded_mode` when progress resumes. Once a case enters degraded mode, it stays there permanently.

**Recommendation:** Add degraded mode exit logic in the progress tracking section (after line 1244 in `milestone_engine.py`): if `progress_made` and `case.degraded_mode is not None`, set `case.degraded_mode.exited_at` and clear the mode.

---

### Gap 4: Stage-Specific Context Loading Is Not Actually Implemented

**Location:** `context_builder.py:468-503`

The code for "Gap #10: Stage-Specific Context Loading" contains only debug log statements and comments like "Already optimized" and "no change needed":

```python
if stage == InvestigationStage.SYMPTOM_VERIFICATION:
    logger.debug("Stage-specific loading: SYMPTOM_VERIFICATION - skipping hypothesis details")
    # This is a note for future optimization if needed
```

No context sections are actually suppressed or modified. Every stage loads the same context sections in the same order. The documentation (`prompt-engineering-guide.md` Section 11.4) describes aggressive stage-specific loading (e.g., skipping hypothesis details during verification, skipping evidence during solution stage), but none of it is implemented.

**Recommendation:** Implement the actual filtering. For SYMPTOM_VERIFICATION, skip hypothesis_str. For SOLUTION, condense evidence to only RESOLUTION_EVIDENCE. This would meaningfully reduce token usage.

---

### Gap 5: Post-Mitigation User Choice Point Lacks Schema Support

**Location:** `investigation-lifecycle-logic.md` Section 4.4; no corresponding schema field

The documentation describes a critical user interaction after mitigation is applied: the agent should ask whether to continue to root cause analysis or close the case. But:

- No field on `Case` or `InvestigationProgress` records the user's choice
- No `InvestigationPath` state is tracked after initial selection
- The LLM has no way to signal this choice point in its structured output
- `InvestigationResponse_Resolution` schema does not include a path selection field

This means the MITIGATION_FIRST path has no mechanism to branch into RCA vs closure after mitigation succeeds.

---

### Gap 6: Knowledge Resolution Fast-Track Is Documented But Never Populated

**Location:** `schemas.py:162-169` (KnowledgeResolution), `investigation-lifecycle-logic.md` Section 1.2

The `KnowledgeResolution` model and the `InquiryResponse.knowledge_resolution` field exist in the schema, but the `_process_response_structured()` method in `milestone_engine.py` does not process this field. Even if the LLM populates `knowledge_resolution` with a KB match, the system does not execute the INQUIRY→RESOLVED fast-track transition documented in the lifecycle logic.

---

### Gap 7: HypothesisStatus.INCONCLUSIVE Used in Stagnation Detection But Not in Hypothesis Manager

**Location:** `stagnation_detector.py:136-142,190-219` vs `hypothesis_manager.py:287-351`

`StagnationDetector._detect_category_anchoring()` checks for `HypothesisStatus.INCONCLUSIVE`, and `_detect_hypothesis_deadlock()` also checks for it. But `HypothesisManager._check_status_transition()` only transitions to VALIDATED, REFUTED, or RETIRED — never to INCONCLUSIVE. There is no code path that sets a hypothesis to INCONCLUSIVE status, making the deadlock and anchoring detection for this status dead code.

**Recommendation:** Either add an INCONCLUSIVE transition path in the hypothesis manager (e.g., when likelihood is between 0.3-0.5 for N turns with no evidence change), or remove INCONCLUSIVE checks from stagnation detection.

---

### Gap 8: Checkpoint/Time-Travel Documented But Not Implemented

**Location:** `orchestration-capabilities.md` Sections 1-2

The documentation describes a comprehensive checkpointing system with:
- Append-only, immutable turn-based checkpoints
- Read-only time travel to any previous turn
- Semantic diffing between investigation states

None of this appears in the source code. `TurnProgress` records exist in `case.turn_history`, but there is no `CaseCheckpoint` creation in `milestone_engine.py`, no snapshot serialization, no diff engine, and no replay capability. The `CaseCheckpoint` model is referenced in `contracts.py` exports but not instantiated anywhere in the investigation flow.

---

### Gap 9: EvidenceStance.NEUTRAL Inconsistency Between Docs and Code

**Location:** `investigation-data-models.md` (duplicate EvidenceStance definitions), `schemas.py:311` (HypothesisEvidenceLinkToAdd)

The data models document defines EvidenceStance twice — once with 2 values (SUPPORTS, REFUTES) and once with 3 (adding NEUTRAL). The schema `HypothesisEvidenceLinkToAdd.stance` uses `EvidenceStance` from contracts, which includes NEUTRAL. But `HypothesisManager.link_evidence()` only handles `supports=True/False` (binary), mapping to SUPPORTS or REFUTES. A NEUTRAL stance from the LLM would be stored but never processed by the confidence calculation, creating phantom links that don't affect likelihood.

---

### Gap 10: Diagnostic Reasoning Validator Is Post-Hoc Only

**Location:** `milestone_engine.py:1166-1183`, `diagnostic_reasoning_validator.py`

The diagnostic reasoning validator runs after the LLM response is processed and state is updated. Violations are logged as warnings and added to metadata, but the response is not regenerated or corrected. If the LLM produces a generic checklist response (explicitly prohibited in the docs), it gets delivered to the user unchanged. The validator creates observability but no enforcement.

---

### Gap 11: No Concurrency Protection on Case State

**Location:** `milestone_engine.py:677-1311` (process_turn)

The `process_turn()` method reads case state, invokes the LLM (which takes seconds), processes the response, and saves. If two concurrent requests arrive for the same case (e.g., user sends two messages quickly), both read the same state, both invoke LLM, and the second save overwrites the first's changes. There is no optimistic locking, no version field check on save, and no mutex.

The `repository.save()` call is a blind write. This can cause:
- Lost evidence from the first request
- Milestone regression
- Duplicate hypothesis creation
- Turn history corruption

---

### Gap 12: System Feedback Loop From Validation to Next Turn Is Incomplete

**Location:** `context_builder.py:461-466`, `milestone_engine.py:1267-1284`

The turn record includes `system_feedback` and `validation_repairs`, and `build_investigation_context()` includes feedback from the previous turn in the prompt. However, reasoning validation errors from `validate_reasoning_first()` are stored in `metadata["reasoning_validation_errors"]` but are NOT written to `turn_record.system_feedback`. Only validation repairs from `state_validator` make it into the turn record.

This means if the LLM fails reasoning validation (no justification for milestones), the corrective feedback is lost between turns. The next turn's LLM invocation has no knowledge that it previously failed validation.

**Recommendation:** Merge reasoning validation errors into `system_feedback` so they propagate to the next turn's context.

---

## Summary

| # | Finding | Severity | Category |
|---|---------|----------|----------|
| F1 | `current_stage` breaks under MITIGATION_FIRST | Major | Logic error |
| F2 | Hypothesis confidence: decay vs evidence conflict | Major | Model flaw |
| F3 | Evidence validation is advisory-only | Major | Missing enforcement |
| F4 | Duplicate return in `create_hypothesis` | Major | Dead code |
| F5 | Inconsistent stagnation tracking in hypothesis update | Major | Divergent behavior |
| F6 | NLP intent patterns cause false positives | Major | Safety |
| F7 | Token budget breaks for non-Latin text | Major | Internationalization |
| G1 | Breakout prompt injection never reaches LLM | Gap | Stagnation recovery |
| G2 | `solution_verified` has no evidence expectations | Gap | Validation |
| G3 | Degraded mode has no exit path in code | Gap | State machine |
| G4 | Stage-specific context loading not implemented | Gap | Performance |
| G5 | Post-mitigation path choice has no schema support | Gap | Schema |
| G6 | Knowledge resolution fast-track not wired | Gap | Feature |
| G7 | INCONCLUSIVE status referenced but never set | Gap | Dead code |
| G8 | Checkpoint/time-travel documented but unimplemented | Gap | Feature |
| G9 | NEUTRAL evidence stance stored but never processed | Gap | Data model |
| G10 | Diagnostic reasoning validation has no enforcement | Gap | Quality |
| G11 | No concurrency protection on case state | Gap | Data integrity |
| G12 | Reasoning validation errors not fed back to LLM | Gap | Feedback loop |
