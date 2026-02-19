# Implementation Gap Analysis: Evidence-Driven Investigation Framework

> **Date**: 2026-02-19 (revised)
> **Baseline Design**: Updated design documents in `docs/architecture/investigation-engine/`
> **Baseline Code**: Current implementation including design flaw fixes

This document tracks the alignment between the **evidence-driven design** and the **current implementation**.

---

## Summary

The 3-stage evidence-driven investigation framework is **fully implemented**. The following table reflects current status after the design review and fix session.

| Area | Status | Notes |
| ---- | ------ | ----- |
| InvestigationStage enum | **Implemented** | 3 new values (DIAGNOSIS, MITIGATION, TREATMENT) + backward-compat aliases |
| InvestigationProgress model | **Implemented** | 4 stage-gate milestones + 6 progress indicators + computed properties |
| EvidenceCategory enum | **Implemented** | 5 categories (SYMPTOM, CAUSAL, MITIGATION, SOLUTION, CONTEXTUAL) |
| Compliance detection | **Implemented** | `compliance_detector.py` — post-LLM step, structural signal analysis |
| ProposedAction/ActionAttempt models | **Implemented** | Domain models + compliance chain wired via SolutionToAdd processing |
| InvestigationActionType enum | **Implemented** | MITIGATION, SOLUTION, DIAGNOSTIC |
| 3-stage prompt instructions | **Implemented** | DIAGNOSIS_INSTRUCTIONS, MITIGATION_INSTRUCTIONS, TREATMENT_INSTRUCTIONS active at runtime |
| Context builder | **Implemented** | Separates stage-gate milestones from progress indicators |
| Path routing | **Implemented** | HISTORICAL+HIGH/CRITICAL → USER_CHOICE (updated from blanket ROOT_CAUSE) |
| Milestone processing | **Implemented** | Progress indicators from LLM; stage-gates from compliance detection |
| State checkpointing / time travel | **Design Only** | `CaseCheckpoint` model defined, not instantiated |
| Knowledge fast-track resolution | **Design Only** | Data model exists, milestone engine wiring deferred |

---

## Remaining Gaps (Design Complete, Not Yet Implemented)

### 1. State Checkpointing and Time Travel

**Design**: Turn-based checkpointing with full state snapshots and semantic diffing.
**Status**: `CaseCheckpoint` model defined in contracts. `TurnProgress` records provide partial auditability.
**Priority**: Low — deferred to future release.

### 2. Knowledge Fast-Track Wiring

**Design**: Knowledge base matches during INQUIRY can skip investigation entirely.
**Status**: Data model exists (`KnowledgeMatch`, `KnowledgeResolution`). Milestone engine wiring not connected.
**Priority**: Medium — useful for repeat issues.

### 3. `solution_verified` Evidence Validation

**Design**: User-Agent Handshake handles TREATMENT → RESOLVED transition.
**Status**: ProposedTransition mechanism exists. No evidence quality check on the verification step.
**Priority**: Low — handshake pattern provides sufficient safety.

---

## Recently Resolved (This Session)

| Issue | Resolution |
| ----- | ---------- |
| ProposedAction never instantiated | Added creation in SolutionToAdd processing (`milestone_engine.py`) |
| DIAGNOSTIC action type missing | Added to `InvestigationActionType` enum |
| No hypothesis gate on SOLUTION actions | Added at ProposedAction creation — downgrades to DIAGNOSTIC if no hypothesis |
| `solution_proposed` dual ownership | Removed from `MilestoneUpdates`; set programmatically at ProposedAction creation |
| Mitigation flag reset missing | Added reset after `mitigation_verified=True` in `compliance_detector.py` |
| HISTORICAL+CRITICAL routing | Changed from blanket ROOT_CAUSE to USER_CHOICE for HIGH/CRITICAL |
| Deprecated fields in milestone processing | Removed `mitigation_applied`, `solution_applied` from `milestone_fields` |
| Escalation criteria contradiction | Fixed "2-3 cycles" to "degraded mode (capability-based)" in framework doc |
| `prompt-engineering-guide.md` outdated | Replaced with deprecation notice pointing to current docs |
| ProposedAction/ActionAttempt doc inconsistency | Aligned definitions across framework doc and data models doc |
| README broken link | Removed reference to non-existent `prompt-implementation-examples.md` |

---

## What's Aligned (No Changes Needed)

| Component | Notes |
| --------- | ----- |
| `CaseStatus` enum (4 values) | INQUIRY, INVESTIGATING, RESOLVED, CLOSED |
| `Hypothesis` model and lifecycle | CAPTURED → ACTIVE → VALIDATED/REFUTED/RETIRED |
| `hypothesis_manager.py` | Confidence scoring, anchoring detection, decay formula |
| Stagnation detection | `turns_without_progress` logic, `StagnationDetector` |
| Degraded mode | `DegradedMode` model and instructions |
| Error handling | LLM retry, state validation, stagnation breaking |
| State validator | Milestone ordering validation |
| `ProposedTransition` schema | User-Agent Handshake for terminal transitions |
| Working conclusion generator | Progress metrics calculation |
| INQUIRY and TERMINAL templates | Not affected by investigation stage changes |
| Streaming support | `ExecutionEvent` types, SSE integration |
