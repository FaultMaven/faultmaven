# FaultMaven Workflow Design Review

**Date**: 2026-02-09
**Scope**: `opportunistic-investigation-framework.md`, `investigation-lifecycle-logic.md`, and their alignment with the implementation in `faultmaven/core/investigation/`.

---

## Overview

This review evaluates the investigation workflow design as documented in the opportunistic investigation framework and lifecycle logic specifications, cross-referenced against the actual implementation. After initial review, findings were discussed with the project owner and key design decisions were made.

---

## Design Decisions Made

### Decision A: LLM Structured Output Is Sole Authority for Milestones

**Problem**: Two independent pathways advanced milestones — LLM structured output and keyword-based evidence processing. The evidence processor used fragile keyword matching on LLM-generated analysis text.

**Decision**: The evidence processor is now a **validation layer**, not a discovery layer. The LLM states milestone claims in the `milestones` field of its response schema. The evidence processor validates that the LLM cited enough Evidence IDs to justify each claim, rather than independently "discovering" milestones via keyword matching.

**Changes**:
- `evidence_processor.py`: Rewritten from milestone-advancing to `validate_milestone_claims()` function
- `milestone_engine.py`: Dual pathway removed. Evidence processing block replaced with validation call
- `MilestoneUpdates` schema: `solution_verified` removed (see Decision B)

---

### Decision B: Terminal Transitions Require User-Agent Handshake

**Problem**: When `solution_verified=True`, the system auto-transitioned to RESOLVED with no user confirmation. The LLM's interpretation of user intent (e.g., "it works" could mean "this command works", not "the system is fixed") was treated as definitive for an irreversible state change.

**Decision**: Terminal transitions use a **User-Agent Handshake** pattern. The agent proposes that the solution is verified, and the transition only fires when the user explicitly confirms the "Resolution Proposal."

**Flow**:
1. Agent detects resolution conditions → includes `ProposedTransition` in response
2. System stores `pending_transition` on case (does NOT execute)
3. Agent's response asks user to confirm
4. Next turn: user confirms → system sets `solution_verified=True` and transitions to RESOLVED
5. If user declines → `pending_transition` cleared, investigation continues

**Changes**:
- `schemas.py`: Added `ProposedTransition` model; `solution_verified` removed from `MilestoneUpdates`
- `terminal_transitions.py`: Removed `check_terminal_transitions()` auto-transition; added `propose_transition()`, `confirm_pending_transition()`, `cancel_pending_transition()`
- `milestone_engine.py`: `_check_automatic_transitions()` rewritten to handle pending transitions and ProposedTransition from LLM responses

---

### Decision C: User-Submitted Data vs. Evidence (Two-Tier Model)

**Problem**: Every user message was auto-created as an `Evidence` object with `category=SYMPTOM_EVIDENCE`. This meant everything submitted by the user was immediately labeled as evidence, even if it was noise or a casual question.

**Decision**: User-submitted data is stored with an ID (so the LLM can reference it) but categorized as `UNCLASSIFIED` until the LLM determines it qualifies as evidence. Only the LLM promotes relevant items to a specific evidence category via `evidence_to_add`.

**Principle**: Evidence must be information submitted by the user. The LLM classifies which user submissions are evidence. The LLM never generates evidence from nothing.

**Changes**:
- `EvidenceCategory`: Added `UNCLASSIFIED` category for raw user data
- `milestone_engine.py`: Auto-created entries use `UNCLASSIFIED` instead of `SYMPTOM_EVIDENCE`
- `evidence_processor.py`: Validation skips `UNCLASSIFIED` items (they are not evidence yet)

**Future work**: Full `UserDataItem` model separate from `Evidence`, with explicit promotion flow.

---

### Decision D: ProposedTransition Schema

Added `ProposedTransition` to `schemas.py` and included `proposed_transition` field in `InvestigationResponse_Resolution` and `InvestigationResponse_General` schemas. This enables the agent to propose terminal transitions that require user confirmation.

---

### Decision: Remove completion_percentage

**Rationale**: Inaccurate (path-dependent milestone denominator) and non-essential. Milestone completion is tracked via `completed_milestones` and `pending_milestones` lists.

**Changes**: Removed from `InvestigationProgress` model, `CaseDetail` API model, `investigation_service.py`, and architecture docs.

---

### Decision: Terminal States Stay Terminal (Phase 1)

Terminal states (RESOLVED, CLOSED) have no outbound transitions. This is a deliberate scope decision for phase 1. Case reopening/regression handling is deferred to a future phase.

---

### Decision: Mitigation Follow-Up Is Advisory

The agent advises users to revert temporary workarounds after the permanent fix. This is intentionally advisory, not mandated. Enforcing workaround reversion would add complexity without proportional value in phase 1.

---

### Decision: Agent Accesses KB and Web, Not User Systems

The agent can access the knowledge base, web search, and uploaded files. It cannot access the user's production systems, run commands, or collect data directly. It processes queries and files submitted by the user.

---

### Decision: Path Selection Timing Is Acceptable

The 3-turn path selection timeline is acceptable. What matters is the agent's urgency awareness and communication, not the specific turn count. The preliminary urgency assessment in Turn 1 allows the agent to acknowledge urgency immediately.

---

### Decision: Mitigation-First Has Two Sub-Scenarios, No Additional Logic Needed

**Question**: The MITIGATION_FIRST path has two possible outcomes — full path (mitigation + RCA + permanent fix) and quick path (mitigation only). Does the system need separate routing or tracking?

**Decision**: No additional code, data elements, or routing logic. The milestone state already distinguishes the two paths retrospectively:

| Field | Full Path | Quick Path |
|-------|-----------|------------|
| `mitigation_applied` | True | True |
| `mitigation_verified` | True | True |
| `root_cause_identified` | True | **False** |
| `solution_applied` | True | **False** |
| `solution_verified` | True | True |

The distinction is a **user choice** after mitigation is verified, not a system routing decision. The agent offers the choice ("continue with RCA or close?"), factoring in `mitigation_effectiveness`. Both paths end with the User-Agent Handshake for resolution.

**Changes**: Updated lifecycle doc Section 4.4 with both sub-scenarios and agent behavior guidance. Updated framework doc resolution paths table.

---

## Remaining Items (Refinements, Not Fundamentals)

These items from the initial review remain as future improvement areas. They are refinements, not fundamental design issues.

### Degraded Mode Entry/Exit

The LLM can trivially exit degraded mode by generating a hypothesis or adding marginal evidence. A sliding-window approach for stagnation detection would be more robust. Entry criteria for LIMITED_DATA and EXTERNAL_DEPENDENCY degraded mode types need documentation.

### Hypothesis Auto-Validation Threshold

With initial likelihood 0.5 and 2 supporting evidence items, a hypothesis reaches 0.8, auto-validating. Now that evidence is user-submitted only (Decision C), the risk is reduced but not eliminated. Consider requiring at least one piece of classified (non-UNCLASSIFIED) evidence for validation.

### Turn-Based Progress Tracking

Stagnation detection cannot distinguish "agent is stuck" from "user hasn't responded." A `waiting_for_user` state tied to `TurnOutcome.DATA_REQUESTED` would prevent false degraded-mode triggering.

### Single-Shot Validation Audit Trail

When used, annotate hypothesis with `validation_method: "direct_analysis"` and record at least one dismissed alternative for genuine audit value.

### USER_CHOICE Path Decision Support

The agent should make a recommendation even when it cannot auto-select a path, explaining tradeoffs between mitigation-first and root-cause approaches.

---

## Summary of Changes

| Area | Change | Files |
|------|--------|-------|
| Milestone authority | LLM sole authority; evidence processor → validation-only | `evidence_processor.py`, `milestone_engine.py` |
| Terminal transitions | User-Agent Handshake; no auto-transition | `terminal_transitions.py`, `milestone_engine.py` |
| Schemas | Added `ProposedTransition`; removed `solution_verified` from `MilestoneUpdates` | `schemas.py` |
| Evidence classification | UNCLASSIFIED category for raw user data | `models.py`, `milestone_engine.py` |
| completion_percentage | Removed | `models.py`, `api_models.py`, `investigation_service.py`, docs |
