# FaultMaven Workflow Design Review

**Date**: 2026-02-09
**Scope**: `opportunistic-investigation-framework.md`, `investigation-lifecycle-logic.md`, and their alignment with the implementation in `faultmaven/core/investigation/`.

---

## Overview

This review evaluates the investigation workflow design as documented in the opportunistic investigation framework and lifecycle logic specifications, cross-referenced against the actual implementation. Findings are organized into design flaws (problems that can cause incorrect behavior) and improvement areas (weaknesses that limit the system's effectiveness or maintainability).

---

## Design Flaws

### 1. Keyword-Based Milestone Validation Is Fragile and Gameable

**Location**: `evidence_processor.py:125-282`, referenced by lifecycle doc Section 3.1

The evidence processor determines milestone advancement through simple keyword matching against LLM-generated `evidence.analysis` text. For example, `validates_symptom()` checks whether the analysis contains words like "confirms", "shows", or "error" AND a word from the symptom statement.

**Problems**:
- The LLM generates the analysis text AND the system uses that same text to decide milestone advancement. This creates a circular dependency where the LLM effectively controls its own milestone progression by choosing words.
- The keyword lists are English-only and brittle. "At " (with trailing space) matches any sentence containing "at " — which is nearly all of them. "Specific" in the scope keywords matches phrases that have nothing to do with scope assessment.
- A single piece of vague evidence mentioning "error" and one word from the symptom statement will advance `symptom_verified`. There is no confidence threshold on the evidence itself, no minimum evidence count, and no cross-validation.

**Recommendation**: Replace keyword matching with structured fields in the LLM response schema. The LLM should explicitly declare which milestones a piece of evidence advances and provide justification, which is then validated against the `internal_reasoning` block that already exists. The existing `advances_milestones` field on Evidence should be set by the schema output rather than inferred post-hoc from free text.

---

### 2. Terminal Transition on `solution_verified` Is Automatic and Irreversible Without Safeguards

**Location**: Lifecycle doc Section 1.4, `terminal_transitions.py`

When `solution_verified` becomes `True`, the system automatically transitions to RESOLVED with no confirmation step. The transition is irreversible — RESOLVED and CLOSED are terminal states with no outbound transitions.

**Problems**:
- If the LLM incorrectly sets `solution_verified` (which is plausible given Flaw #1), the case is permanently closed. There is no undo mechanism.
- The `can_mark_resolved()` function checks `any(s.verified_at is not None for s in case.solutions)`, but `verified_at` is set by the LLM's structured output, not by independent verification.
- The docs acknowledge RESOLVED is terminal but provide no mechanism for reopening a case if the solution turns out not to work. In real incident management, solutions frequently regress.

**Recommendation**:
- Add a grace period or confirmation step before terminal transition. After the agent marks `solution_verified`, prompt the user: "The solution appears to have worked. Should I close this case?"
- Consider adding a REOPENED transition from RESOLVED back to INVESTIGATING for cases where the fix regresses. The current design forces creating an entirely new case for the same problem.

---

### 3. Degraded Mode Entry/Exit Has Race Conditions

**Location**: Lifecycle doc Section 3.2, `stagnation_detector.py`

Degraded mode is entered after 3 turns without progress and exited when any progress is made. But the definition of "progress" is very broad (any evidence added, any hypothesis generated, any milestone change).

**Problems**:
- The LLM can trivially exit degraded mode by generating a new hypothesis or adding marginal evidence, even if the investigation is genuinely stuck. The system counts hypothesis generation as progress, so the agent can oscillate between degraded and normal mode indefinitely.
- Stagnation is only checked when `case.degraded_mode is None` (milestone_engine.py:862). If the agent exits degraded mode on turn N via trivial progress, the 3-turn counter resets to 0, and the agent must stagnate for another 3 turns before re-entering. This creates a loophole where one superficial action every 3 turns prevents degraded mode from ever being effective.
- The docs mention `LIMITED_DATA` and `EXTERNAL_DEPENDENCY` degraded mode types but the lifecycle document provides no entry criteria for them.

**Recommendation**:
- Require sustained progress (e.g., at least one milestone advancement, not just evidence addition) to exit degraded mode.
- Use a sliding window for stagnation detection (e.g., "fewer than N milestone advancements in the last M turns") rather than a simple consecutive counter that resets on any activity.
- Document entry criteria for all degraded mode types, not just `NO_PROGRESS`.

---

### 4. Path Selection Happens Too Late for MITIGATION_FIRST to Be Effective

**Location**: Lifecycle doc Section 2.0 (Phase 2), milestone_engine.py:1338-1351

Formal path selection occurs AFTER `symptom_verified = True`. For an ongoing critical outage, the timeline described in the docs is:

```
Turn 1 (INQUIRY):     Preliminary urgency assessed
Turn 2 (INQUIRY→INVESTIGATING): Status transition
Turn 3 (INVESTIGATING): symptom_verified = True → path selected → mitigation applied
```

**Problem**: For a CRITICAL/ONGOING issue, waiting until Turn 3 to start mitigation contradicts the stated goal of "stop impact first." Three conversational turns could take significant elapsed time during an active outage. The preliminary urgency assessment in Turn 1 already identifies the severity, but it explicitly "does NOT determine path yet."

**Recommendation**: Allow MITIGATION_FIRST path activation during INQUIRY when preliminary urgency is CRITICAL/ONGOING, collapsing the two-step confirmation flow. The "early path hint" the docs mention ("Should I focus on quick mitigation first?") should be actionable, not advisory. If the user confirms urgency during Turn 1, mitigation should begin immediately in Turn 2 without waiting for formal symptom verification.

---

### 5. Completion Percentage Is Misleading for ROOT_CAUSE Path

**Location**: Lifecycle doc Section 1.2 (stage detail in UI), data models doc `InvestigationProgress`

The UI displays `completion_percentage` computed from all 9 milestones. But the `mitigation_applied` milestone only applies to the MITIGATION_FIRST path. For ROOT_CAUSE investigations, this milestone will never be set, meaning the maximum achievable completion is 8/9 (~89%). The UI would never show 100% for a successfully resolved ROOT_CAUSE investigation.

**Recommendation**: Calculate completion percentage based on the milestones applicable to the active path. ROOT_CAUSE path should use 8 milestones as the denominator; MITIGATION_FIRST should use all 9. This could also be extended: `mitigation_verified` is mentioned in Section 4.4 of the lifecycle doc but does not appear in the 9-milestone list, suggesting the milestone set itself is incomplete.

---

### 6. Hypothesis Auto-Validation Threshold Is Too Permissive

**Location**: `hypothesis_manager.py:287-351`, lifecycle doc Section 1.2 (Single-Shot Validation)

A hypothesis auto-transitions to VALIDATED when `likelihood >= 0.70 AND supporting_evidence >= 2`. The confidence formula is:

```
likelihood = initial + (0.15 × supporting) - (0.20 × refuting)
```

**Problem**: With an initial likelihood of 0.5 (a reasonable default) and just 2 supporting pieces of evidence with 0 refuting, the likelihood reaches `0.5 + 0.3 = 0.8`, exceeding the 0.70 threshold. This means any hypothesis with 2 supporting evidence items and no contradictory evidence gets auto-validated. In practice, the LLM generates both the hypothesis and the supporting evidence, so it can validate its own hypotheses in 1-2 turns without meaningful external input.

**Recommendation**:
- Require at least one piece of evidence to be user-provided (not LLM-generated) before auto-validation.
- Increase the validation threshold to 0.85+ or require a minimum of 3 supporting evidence items.
- Add a "user-confirmed" flag on Evidence to distinguish user-supplied data from agent-derived analysis.

---

## Design Improvement Areas

### 7. No Case Reopening or Regression Handling

**Location**: Lifecycle doc Section 1.3 (Valid Transitions Summary)

The state machine is strictly forward-only. RESOLVED and CLOSED have empty transition lists. While this simplifies the design, it fails to model a common incident management scenario: the solution regresses after the case is closed.

**Impact**: Teams must create a new case for the same problem, losing the context, evidence, and hypothesis trail from the original investigation. The knowledge flywheel — a core value proposition — is undermined because the causal link between the original case and the regression is not captured.

**Recommendation**: Add a REOPENED state or allow RESOLVED → INVESTIGATING transition with a "regression" reason. Link the reopened case to the original via a `parent_case_id` or `regression_of` field to preserve investigative continuity.

---

### 8. USER_CHOICE Path Lacks Decision Support

**Location**: Lifecycle doc Section 2.1 (Path Selection Matrix)

When the urgency/temporal combination is ambiguous (e.g., ONGOING + MEDIUM, HISTORICAL + CRITICAL), the system falls back to USER_CHOICE with no structured guidance. The agent asks the user to pick between mitigation-first and root-cause, but the user may not understand the tradeoffs.

**Recommendation**: Provide the agent with a decision framework to present when USER_CHOICE is triggered. For example: "Your issue is ongoing but medium severity. Option A (mitigation first) would stabilize the system now but we may not find the root cause. Option B (root cause analysis) takes longer but gives you a permanent fix. Based on [specific evidence], I'd lean toward [X] because [reasoning]." The agent should make a recommendation even when it cannot auto-select.

---

### 9. Confirmation Flow Is Heavyweight for Experienced Users

**Location**: Lifecycle doc Sections 1.2, 1.5

The INQUIRY → INVESTIGATING transition requires a multi-step confirmation flow: agent proposes problem statement → user confirms → agent transitions. For experienced users with well-defined problems, this adds friction without value. The doc acknowledges this with "WHEN TO SKIP CONFIRMATION" heuristics, but these are vague ("Context is clear and user needs direct answer").

**Recommendation**: Implement a confidence-based skip. If the user's initial message contains a clear problem statement with specific symptoms, timeline, and affected systems (high-information density), the agent should transition directly to INVESTIGATING in the same turn, noting the inferred problem statement in the response rather than asking for confirmation. The two-step flow should be reserved for ambiguous or incomplete problem descriptions.

---

### 10. Mitigation Follow-Up Is Advisory, Not Enforced

**Location**: Lifecycle doc Section 2.3 (MITIGATION_FIRST, "Mitigation Follow-up Requirement")

The docs note that temporary workarounds should be tracked and reverted after the permanent fix. The spec mentions `has_temporary_workaround = True` and suggests a `workaround_reverted` milestone, but both are described as "considerations" rather than requirements.

**Impact**: Without enforcement, temporary workarounds (disabled security checks, manual overrides, increased resource limits) become permanent technical debt. The docs explicitly acknowledge this risk ("Without follow-up: Temporary workarounds become permanent technical debt, creating security holes or degraded functionality") but don't prescribe a mechanism to prevent it.

**Recommendation**: Make `workaround_reverted` a required milestone for MITIGATION_FIRST path cases. Block the RESOLVED transition until the workaround is either reverted or explicitly acknowledged as permanent by the user. This converts the advisory guidance into an enforced safeguard.

---

### 11. Agent Role Constraints Create an Awkward Tool Gap

**Location**: Lifecycle doc Section 1.6

The doc states the agent is an ADVISOR only — it cannot execute commands, access systems, or retrieve data. But the CLAUDE.md and codebase show the agent has actual tools (`web_search.py`, `read_file_tool.py`, `knowledge_base.py`, `document_qa_tool.py`, `case_evidence_qa.py`). The agent CAN search the knowledge base, read uploaded files, and search the web.

**Problem**: The constraint as documented is too absolute. Telling the agent it "CANNOT access systems, logs, or metrics directly" is accurate for external production systems, but the agent demonstrably can access the knowledge base, uploaded evidence, and web search results. This mismatch between documented constraints and actual capabilities could confuse both the LLM (via system prompts) and developers maintaining the code.

**Recommendation**: Reframe the constraint as "the agent cannot execute actions in the user's environment" rather than "the agent cannot access systems." Explicitly enumerate what the agent CAN do (search KB, read uploaded files, search web) vs. what it cannot (SSH into servers, run kubectl commands, execute database queries). The prohibited/correct phrase lists should be updated to reflect this nuance.

---

### 12. Turn-Based Progress Tracking Conflates Agent Turns with Calendar Time

**Location**: Lifecycle doc Section 3.2, milestone_engine.py:877-895

Stagnation detection, degraded mode, and progress metrics are all turn-based ("3 turns without progress"). But turns have no fixed duration — a user might respond in seconds or leave for hours. The system cannot distinguish "the agent is stuck" from "the user hasn't responded yet."

**Problem**: The lifecycle doc explicitly notes this: "Waiting for user to provide requested evidence does NOT count against progress." But the implementation has no mechanism to detect this. `turns_without_progress` increments on every turn where `progress_made=False`, regardless of whether the agent is waiting for user input.

**Recommendation**: Add a `waiting_for_user` state to the progress tracker. When the agent's last action was a data request (TurnOutcome.DATA_REQUESTED), pause the stagnation counter until the user responds with relevant data. This prevents false degraded-mode triggering when the bottleneck is user responsiveness, not agent capability.

---

### 13. Single-Shot Validation Undermines Audit Trail Value

**Location**: Framework doc Section 1.2 (Decision 4)

The Single-Shot Validation pattern creates a hypothesis, links evidence, and validates it all in one turn when the root cause is "obvious." While this preserves the formal audit trail structure, it reduces the trail to a rubber stamp — the hypothesis is never genuinely tested because it's created already validated.

**Impact**: Post-incident reviewers see a hypothesis with VALIDATED status and supporting evidence, which looks indistinguishable from a hypothesis that was rigorously tested. The audit trail technically exists but carries no diagnostic value — it doesn't show what alternatives were considered or what evidence would have refuted the hypothesis.

**Recommendation**: When Single-Shot Validation is used, annotate the hypothesis with `validation_method: "direct_analysis"` (as opposed to `"hypothesis_testing"`) and require the agent to record at least one alternative explanation that was considered and why it was dismissed. This preserves the speed benefit while adding genuine audit value.

---

### 14. No Concurrency Model for Multi-User Cases

**Location**: Not addressed in either document

The lifecycle documents assume a single agent working a single case with a single user. There is no mention of:
- Multiple users contributing to the same case (common in incident response)
- Concurrent turn processing (what happens if two users submit queries simultaneously?)
- Ownership transfer (user A starts investigation, user B takes over)

**Recommendation**: Document the concurrency model explicitly, even if the initial implementation is single-user. At minimum, add a `locked_by` field with optimistic concurrency control to prevent conflicting updates. For multi-user support, consider a "participants" list with role assignments (owner, contributor, observer).

---

### 15. Knowledge Pre-Check Confidence Threshold Is Arbitrary

**Location**: Framework doc Section 1.2 (Decision 5)

The fast-track resolution triggers when KB match confidence exceeds 70%. This threshold is not justified in the docs and has significant implications:
- Too low: Users get irrelevant solutions, eroding trust in the system.
- Too high: The fast-track path is rarely triggered, reducing its value.

There is no feedback mechanism to adjust this threshold based on actual fast-track success rates.

**Recommendation**: Make the threshold configurable and add telemetry to track fast-track outcomes (confirmed resolution vs. user rejected solution vs. user tried solution but it didn't work). Use this data to calibrate the threshold. Consider a tiered approach: 90%+ auto-suggest, 70-90% mention as possibility, <70% don't surface.

---

### 16. Confidence Decay Formula Lacks Empirical Basis

**Location**: `hypothesis_manager.py:353-377`, framework doc milestones reference

The decay formula `base * 0.85^iterations_without_progress` reduces a 70% likelihood hypothesis to below 30% (retirement threshold) after ~7 stagnant iterations. This is a made-up decay curve — the 0.85 factor and retirement at 0.30 are not justified by any empirical data or established methodology.

**Impact**: If the decay is too aggressive, valid hypotheses that simply need more evidence (which may take time to gather) get prematurely retired. If too lenient, genuinely weak hypotheses linger and consume investigation focus.

**Recommendation**: Make the decay factor configurable (0.85 is a reasonable default, but different problem domains may need different values). Add telemetry to track whether retired hypotheses were later validated under new evidence — a high rate of "retired then validated" indicates the decay is too aggressive.

---

## Summary

| # | Category | Severity | Area |
|---|----------|----------|------|
| 1 | Design Flaw | High | Keyword-based milestone validation is fragile |
| 2 | Design Flaw | High | Auto-terminal transition with no safeguards |
| 3 | Design Flaw | Medium | Degraded mode entry/exit race conditions |
| 4 | Design Flaw | Medium | Path selection too late for MITIGATION_FIRST |
| 5 | Design Flaw | Low | Completion percentage wrong for ROOT_CAUSE |
| 6 | Design Flaw | Medium | Hypothesis auto-validation too permissive |
| 7 | Improvement | Medium | No case reopening or regression handling |
| 8 | Improvement | Low | USER_CHOICE path lacks decision support |
| 9 | Improvement | Low | Confirmation flow heavyweight for experienced users |
| 10 | Improvement | Medium | Mitigation follow-up not enforced |
| 11 | Improvement | Medium | Agent role constraints inaccurate |
| 12 | Improvement | Medium | Turn-based tracking conflates agent/calendar time |
| 13 | Improvement | Low | Single-shot validation weakens audit trail |
| 14 | Improvement | Medium | No concurrency model |
| 15 | Improvement | Low | KB confidence threshold arbitrary |
| 16 | Improvement | Low | Confidence decay lacks empirical basis |

**Top 3 priorities**: Items 1, 2, and 6 form a chain — the LLM controls evidence analysis text (#1), which drives milestone advancement, which can auto-validate hypotheses (#6), which can trigger irreversible case closure (#2). Addressing these three together would significantly improve the reliability of the investigation lifecycle.
