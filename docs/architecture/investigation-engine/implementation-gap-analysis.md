# Implementation Gap Analysis: Evidence-Driven Investigation Framework

> **Date**: 2026-02-19
> **Baseline Design**: Updated design documents in `docs/architecture/investigation-engine/`
> **Baseline Code**: Current implementation as of this commit

This document identifies gaps between the **updated evidence-driven design** and the **current implementation**, organized by file/component, with specific changes needed.

---

## Summary

| Area | Gaps | Severity |
|------|------|----------|
| InvestigationStage enum | Old 4-stage values, needs 3-stage | **HIGH** (core model) |
| InvestigationProgress model | Missing stage-gate milestones, has old fields | **HIGH** (core model) |
| EvidenceCategory enum | Missing MITIGATION_EVIDENCE, has old RESOLUTION_EVIDENCE | **MEDIUM** |
| closure_reason validator | Missing "mitigation_sufficient" | **LOW** |
| schemas.py (LLM schemas) | Old stage dispatch, old milestone fields, old evidence categories | **HIGH** |
| milestone_engine.py | Old CATEGORY_MILESTONE_MAP, old dispatch, no compliance detection | **HIGH** |
| prompts/templates.py | Old STAGE_INSTRUCTIONS used at runtime (new ones exist but unused) | **HIGH** |
| prompts/context_builder.py | Old milestone formatting | **MEDIUM** |
| investigation_router.py | HISTORICAL+HIGH → USER_CHOICE (should be ROOT_CAUSE) | **LOW** |
| ProposedAction/ActionAttempt | Don't exist in domain models | **MEDIUM** (design-complete, deferred OK) |
| CaseStatusDTO | Extra statuses not in design | **LOW** (contracts layer) |

---

## 1. `faultmaven/modules/case/domain/models.py` — Domain Models

### 1.1 InvestigationStage Enum (Line 597) — **HIGH**

**Current (4 stages):**
```python
class InvestigationStage(str, Enum):
    SYMPTOM_VERIFICATION = "symptom_verification"
    HYPOTHESIS_FORMULATION = "hypothesis_formulation"
    HYPOTHESIS_VALIDATION = "hypothesis_validation"
    SOLUTION = "solution"
```

**Design (3 stages):**
```python
class InvestigationStage(str, Enum):
    DIAGNOSIS = "diagnosis"
    MITIGATION = "mitigation"
    TREATMENT = "treatment"
```

**Action**: Replace 4 enum values with 3. This is a **breaking change** that cascades to every file importing `InvestigationStage`.

---

### 1.2 InvestigationProgress Model (Line 290) — **HIGH**

**Gaps (fields to ADD):**

| Field | Type | Purpose |
|-------|------|---------|
| `mitigation_accepted` | `bool` | Stage-gate: DIAGNOSIS → MITIGATION transition |
| `solution_accepted` | `bool` | Stage-gate: DIAGNOSIS → TREATMENT transition |

These are **new stage-gate milestones** that drive transitions. Currently missing entirely.

**Gaps (fields to REMOVE/RENAME):**

| Current Field | Action | Reason |
|---------------|--------|--------|
| `solution_applied` | REMOVE | Replaced by `solution_accepted` + `solution_verified` |
| `mitigation_applied` | REMOVE | Replaced by `mitigation_accepted` + `mitigation_verified` |
| `mitigation_effectiveness` | REMOVE | Not in new design |
| `mitigation_solution_id` | REMOVE | Not in new design |

**Gaps (computed properties to REWRITE):**

| Property | Current | Design |
|----------|---------|--------|
| `current_stage` | Complex 4-stage dispatch based on old milestones | Simple 3-rule dispatch: solution_accepted → TREATMENT, mitigation_accepted → MITIGATION, else DIAGNOSIS |
| `stage_display_name` | Maps 4 stages to 3 names ("Understanding", "Diagnosing", "Resolving") | 1:1 mapping: DIAGNOSIS→"Diagnosing", MITIGATION→"Mitigating", TREATMENT→"Resolving" |
| `verification_complete` | Exists (checks 4 verification milestones) | REMOVE — not in new design (progress indicators are advisory) |
| `investigation_complete` | Exists | REMOVE — not in new design |
| `resolution_complete` | Exists | REMOVE — not in new design |
| `completed_milestones` | Lists 9 milestones including `solution_applied`, `mitigation_applied` | Update to list 10 fields: 4 stage-gate + 6 progress indicators |
| `pending_milestones` | Lists 8 milestones | Update to match new model |

**Gaps (validators to UPDATE):**

| Validator | Current | Design |
|-----------|---------|--------|
| `solution_ordering` | Checks `solution_applied` requires `solution_proposed` | Should check `solution_verified` requires `solution_accepted` |
| Timestamps | `verification_completed_at`, `investigation_completed_at`, `resolution_completed_at` | May need renaming, but low priority |

---

### 1.3 EvidenceCategory Enum (Line 1209) — **MEDIUM**

**Current:**
```python
SYMPTOM_EVIDENCE = "symptom_evidence"
CAUSAL_EVIDENCE = "causal_evidence"
RESOLUTION_EVIDENCE = "resolution_evidence"   # ← OLD NAME
CONTEXTUAL_EVIDENCE = "contextual_evidence"
REJECTED = "rejected"
```

**Design:**
```python
SYMPTOM_EVIDENCE = "symptom_evidence"
CAUSAL_EVIDENCE = "causal_evidence"
MITIGATION_EVIDENCE = "mitigation_evidence"   # ← NEW
SOLUTION_EVIDENCE = "solution_evidence"        # ← RENAMED
CONTEXTUAL_EVIDENCE = "contextual_evidence"
REJECTED = "rejected"
```

**Actions:**
1. ADD `MITIGATION_EVIDENCE = "mitigation_evidence"` (new category for mitigation verification data)
2. RENAME `RESOLUTION_EVIDENCE` → `SOLUTION_EVIDENCE` (with backward-compat alias if needed)

---

### 1.4 InvestigationPath Enum (Line 2419) — **LOW**

**Current**: `MITIGATION_FIRST`, `ROOT_CAUSE`, `USER_CHOICE` — docstrings reference "4-stage workflow"

**Design**: Same enum values, but descriptions reference "3-stage workflow"

**Action**: Update docstrings/descriptions. The enum values themselves are correct.

---

### 1.5 closure_reason Validator (Line ~3506) — **LOW**

**Current allowed values:**
```python
allowed = ["resolved", "abandoned", "escalated", "inquiry_only", "duplicate", "other"]
```

**Design adds:** `"mitigation_sufficient"`

**Action**: Add `"mitigation_sufficient"` to the allowed list.

---

### 1.6 PathSelection / determine_investigation_path — **LOW**

**Current** (in `investigation_router.py` line 73-82):
```python
# HISTORICAL + HIGH/CRITICAL → USER_CHOICE
```

**Design**: HISTORICAL + HIGH → ROOT_CAUSE (not USER_CHOICE)

**Action**: Update the path matrix in `investigation_router.py`.

---

## 2. `faultmaven/core/investigation/schemas.py` — LLM Structured Output

### 2.1 MilestoneUpdates Schema (Line 186) — **HIGH**

**Current:**
```python
class MilestoneUpdates(BaseModel):
    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = None
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None       # ← REMOVE
    mitigation_applied: Optional[bool] = None      # ← REMOVE
    root_cause_method: Optional[str] = None
```

**Design**: Only 6 progress indicators are LLM-settable. Stage-gate milestones are set by compliance detection, not LLM.

**Actions:**
1. REMOVE `solution_applied` (replaced by `solution_accepted`, set by compliance detection)
2. REMOVE `mitigation_applied` (replaced by `mitigation_accepted`, set by compliance detection)
3. `solution_proposed` stays BUT design says it's set programmatically when ProposedAction is created — may need to move out of LLM schema eventually

---

### 2.2 Stage-Specific Schemas (Lines 566-717) — **HIGH**

**Current dispatch** (`get_schema_for_stage`, line 705):
```python
SYMPTOM_VERIFICATION → InvestigationResponse_Verification
HYPOTHESIS_FORMULATION, HYPOTHESIS_VALIDATION → InvestigationResponse_Hypothesis
SOLUTION → InvestigationResponse_Resolution
fallback → InvestigationResponse_General
```

**Design (3-stage dispatch):**
```python
DIAGNOSIS → InvestigationResponse_Diagnosis (or reuse Verification + Hypothesis merged)
MITIGATION → InvestigationResponse_Mitigation (new)
TREATMENT → InvestigationResponse_Treatment (or reuse Resolution)
fallback → InvestigationResponse_General
```

**Actions:**
1. Create new schema classes or rename existing ones to match 3-stage model
2. Add MITIGATION-specific response schema (may have `mitigation_evidence` fields)
3. Update `get_schema_for_stage()` dispatch to use new `InvestigationStage` values

---

### 2.3 EvidenceToAdd Category References — **MEDIUM**

**Current**: References `EvidenceCategory` which has `RESOLUTION_EVIDENCE`
**After EvidenceCategory rename**: Will need `SOLUTION_EVIDENCE` and `MITIGATION_EVIDENCE` support

---

## 3. `faultmaven/core/investigation/milestone_engine.py` — Orchestrator

### 3.1 CATEGORY_MILESTONE_MAP (Line 116) — **HIGH**

**Current:**
```python
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: ["symptom_verified", "scope_assessed", "timeline_established", "changes_identified"],
    EvidenceCategory.CAUSAL_EVIDENCE: ["changes_identified", "root_cause_identified", "solution_proposed"],
    EvidenceCategory.RESOLUTION_EVIDENCE: ["solution_applied"],   # ← OLD
    EvidenceCategory.CONTEXTUAL_EVIDENCE: [],
}
```

**Design:**
```python
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: ["symptom_verified", "scope_assessed", "timeline_established", "changes_identified"],
    EvidenceCategory.CAUSAL_EVIDENCE: ["changes_identified", "root_cause_identified", "solution_proposed"],
    EvidenceCategory.MITIGATION_EVIDENCE: [],      # ← NEW (mitigation doesn't drive progress indicators)
    EvidenceCategory.SOLUTION_EVIDENCE: [],         # ← RENAMED, solution_verified is stage-gate
    EvidenceCategory.CONTEXTUAL_EVIDENCE: [],
}
```

**Action**: Update map keys and values for new evidence categories.

---

### 3.2 Schema Dispatch (Line ~1200) — **HIGH**

**Current:**
```python
schema_model = get_schema_for_stage(case.current_stage or InvestigationStage.SYMPTOM_VERIFICATION)
```

**Design**: Should reference `InvestigationStage.DIAGNOSIS` as default.

**Action**: Update after InvestigationStage enum change.

---

### 3.3 Compliance Detection — **HIGH** (NEW)

**Current**: Does NOT exist. Stage transitions are driven by the old `current_stage` computed property based on milestones the LLM sets directly.

**Design**: Post-LLM processing step that:
1. Checks if user's submission shows compliance with a previously proposed action
2. If compliance detected, sets stage-gate milestones (`mitigation_accepted`, `solution_accepted`)
3. Stage transitions happen based on these milestones, not LLM's direct milestone updates

**Action**: Implement compliance detection logic in `process_turn()` or as a new module. This is the single biggest new feature.

---

### 3.4 Milestone Processing — **MEDIUM**

**Current**: `_apply_milestones()` method applies `MilestoneUpdates` directly from LLM response to `case.progress`.

**Design**: LLM only sets progress indicators. Stage-gate milestones are set by compliance detection.

**Action**: Separate milestone application into two paths:
1. Progress indicators: applied from LLM response (as today)
2. Stage-gate milestones: applied from compliance detection (new)

---

## 4. `faultmaven/core/investigation/prompts/templates.py` — Prompt Templates

### 4.1 Stage Dispatch in `get_prompt_for_case()` (Line 1284) — **HIGH**

**Current** (line 1286):
```python
stage = case.current_stage or InvestigationStage.SYMPTOM_VERIFICATION
adaptive_instr = STAGE_INSTRUCTIONS.get(stage, ...)
```

Uses `STAGE_INSTRUCTIONS` dict keyed by old 4-stage enum values.

**Design**: Should use new 3-stage instructions (`DIAGNOSIS_INSTRUCTIONS`, `MITIGATION_INSTRUCTIONS`, `TREATMENT_INSTRUCTIONS`).

**Note**: The new instruction constants **already exist** in templates.py (lines 800-1036) but are **NOT USED** at runtime. The old `STAGE_INSTRUCTIONS` dict (line 599) is what gets dispatched.

**Action**: Update `get_prompt_for_case()` to dispatch to new instructions:
```python
if stage == InvestigationStage.DIAGNOSIS:
    adaptive_instr = DIAGNOSIS_INSTRUCTIONS
elif stage == InvestigationStage.MITIGATION:
    adaptive_instr = MITIGATION_INSTRUCTIONS
elif stage == InvestigationStage.TREATMENT:
    adaptive_instr = TREATMENT_INSTRUCTIONS
```

---

### 4.2 Evidence Category References in Prompts — **MEDIUM**

**Current**: All prompt text references `resolution_evidence` (lines 113, 236, 268, 582).

**Design**: Should reference `solution_evidence` and `mitigation_evidence`.

**Action**: Update all prompt template strings after EvidenceCategory rename.

---

### 4.3 Old STAGE_INSTRUCTIONS Dict — **MEDIUM** (cleanup)

**Current**: `STAGE_INSTRUCTIONS` dict (line 599) with keys `SYMPTOM_VERIFICATION`, `HYPOTHESIS_FORMULATION`, `HYPOTHESIS_VALIDATION`, `SOLUTION`.

**Action**: Remove after switching to new instruction constants. The new constants already exist.

---

## 5. `faultmaven/core/investigation/prompts/context_builder.py` — Context Building

### 5.1 Milestone Formatting (Line ~360) — **MEDIUM**

**Current:**
```python
milestones_str = "<milestones_completed>\n"
for milestone, completed in p.dict().items():
    if isinstance(completed, bool) and completed:
        milestones_str += f"- {milestone}\n"
```

**Design**: Should separate stage-gate milestones from progress indicators in the prompt context.

**Action**: Update to show two sections:
- `<stage_gate_milestones>` — mitigation_accepted, mitigation_verified, solution_accepted, solution_verified
- `<progress_indicators>` — symptom_verified, scope_assessed, etc.

---

### 5.2 Stage References — **LOW**

The context builder references `InvestigationStage.HYPOTHESIS_FORMULATION` etc. in stage-specific loading logic.

**Action**: Update after enum change.

---

## 6. `faultmaven/modules/case/domain/services/investigation_router.py` — Path Router

### 6.1 HISTORICAL + HIGH Path — **LOW**

**Current** (line 73-82): Returns `USER_CHOICE` for HISTORICAL + HIGH/CRITICAL.

**Design**: HISTORICAL + HIGH → ROOT_CAUSE.

**Action**: Update the last else branch to only return USER_CHOICE for truly ambiguous cases (ONGOING + LOW/MEDIUM).

---

## 7. New Models: ProposedAction, ActionAttempt — **MEDIUM** (Design Complete, Implementation Deferred)

**Current**: Do NOT exist anywhere in the domain models.

**Design** (investigation-data-models.md Section 1.9):
```python
class ActionType(str, Enum):
    MITIGATION = "mitigation"
    SOLUTION = "solution"

class ProposedAction(BaseModel):
    action_id: str
    case_id: str
    action_type: ActionType
    description: str
    commands: List[str]
    proposed_at: datetime
    proposed_in_turn: int
    status: Literal["pending", "accepted", "rejected", "superseded"]

class ActionAttempt(BaseModel):
    attempt_id: str
    action_id: str
    user_message: str
    submitted_at: datetime
    compliance_detected: bool
    compliance_confidence: float
```

**Note**: An `ActionType` enum exists in `faultmaven/models/contracts/core_contracts.py` but it's for **policy evaluation** (COMMAND_EXECUTION, etc.), not investigation actions. These are different.

**Action**: Create these models in domain models. These are needed for compliance detection to work properly. Can be deferred if compliance detection uses a simpler initial approach.

---

## 8. `faultmaven/modules/case/contracts.py` — Module Contracts

### 8.1 CaseStatusDTO — **LOW**

**Current** (line 466): Has extra statuses not in the design: `DOCUMENTING`, `RESOLVED_WITH_WORKAROUND`, `RESOLVED_BY_USER`, `ABANDONED`.

**Design**: 4 statuses: INQUIRY, INVESTIGATING, RESOLVED, CLOSED.

**Action**: Low priority — CaseStatusDTO is used for cross-module communication and may intentionally be broader. Review if it causes issues.

---

## Implementation Order (Recommended)

Changes have cascading dependencies. Recommended order:

| Phase | What | Dependency |
|-------|------|------------|
| **1** | Update `InvestigationStage` enum (4→3 values) | None |
| **2** | Update `InvestigationProgress` (add stage-gate milestones, remove old fields, rewrite properties) | Phase 1 |
| **3** | Update `EvidenceCategory` (add MITIGATION_EVIDENCE, rename RESOLUTION→SOLUTION) | None (parallel with 1-2) |
| **4** | Update `closure_reason` validator, `InvestigationPath` docstrings, path router | Phase 1 |
| **5** | Update `schemas.py` (MilestoneUpdates, stage dispatch, response schemas) | Phases 1-3 |
| **6** | Update `milestone_engine.py` (CATEGORY_MILESTONE_MAP, schema dispatch) | Phases 1-3, 5 |
| **7** | Update `prompts/templates.py` (switch to new instructions, update evidence refs) | Phase 1 |
| **8** | Update `prompts/context_builder.py` (milestone formatting, stage refs) | Phases 1-2 |
| **9** | Create ProposedAction/ActionAttempt models | Phase 2 |
| **10** | Implement compliance detection | Phases 2, 9 |
| **11** | Fix all tests | All above |

**Phases 1-4** are foundational model changes.
**Phases 5-8** are logic/template updates that depend on models.
**Phases 9-10** are new features (can be deferred).
**Phase 11** runs throughout, but concentrated at end.

---

## What's Already Correct (No Change Needed)

These components are already aligned or are unchanged between designs:

| Component | Status | Notes |
|-----------|--------|-------|
| `CaseStatus` enum (4 values) | **Aligned** | INQUIRY, INVESTIGATING, RESOLVED, CLOSED — correct |
| `Hypothesis` model and lifecycle | **Aligned** | CAPTURED → ACTIVE → VALIDATED/REFUTED/RETIRED unchanged |
| `hypothesis_manager.py` | **Aligned** | Confidence scoring, anchoring detection, decay formula unchanged |
| Stagnation detection | **Aligned** | `turns_without_progress` logic unchanged |
| Degraded mode | **Aligned** | DegradedMode model and instructions unchanged |
| Error handling (LLM retry, stagnation) | **Aligned** | Error handler logic unchanged |
| State validator | **Aligned** | Milestone ordering validation (will need minor update for new fields) |
| `ProposedTransition` schema | **Aligned** | User-Agent Handshake for terminal transitions unchanged |
| Working conclusion generator | **Aligned** | Progress metrics calculation unchanged |
| INQUIRY and TERMINAL templates | **Aligned** | Not affected by investigation stage changes |
| New 3-stage instruction constants | **Exist** | `DIAGNOSIS_INSTRUCTIONS`, `MITIGATION_INSTRUCTIONS`, `TREATMENT_INSTRUCTIONS` already in templates.py but unused |
