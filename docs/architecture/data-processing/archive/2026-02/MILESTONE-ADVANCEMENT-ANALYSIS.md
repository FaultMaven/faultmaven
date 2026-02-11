# Milestone Advancement - Two Sources of Truth Analysis

**Date:** 2026-02-11
**Issue:** `advances_milestones` on Evidence vs. `MilestoneUpdates` in turn response
**Status:** ✅ RESOLVED - Option 2.5 (Hybrid: System-Inferred with Optional LLM Override)
**Decision:** User-endorsed three-tier logic with CATEGORY_MILESTONE_MAP inference

---

## The Problem

**User's Question:**
> "advances_milestones on evidence vs. MilestoneUpdates in turn response — two sources of truth. The new design puts advances_milestones on each evidence record (per-evidence). But milestone state changes come from MilestoneUpdates in the turn-level structured output (per-turn). This creates two sources of milestone information that should be consistent but are set independently."

**Potential Scenario:**
```
LLM creates evidence with advances_milestones: ["symptom_verified"]
BUT
Turn-level MilestoneUpdates doesn't set symptom_verified: true

OR VICE VERSA
```

---

## Current Implementation Analysis

### 1. Evidence Model (Per-Evidence)

**File:** `faultmaven/modules/case/domain/models.py:1496-1499`

```python
class Evidence(BaseModel):
    # ... other fields ...

    advances_milestones: List[str] = Field(
        default_factory=list,
        description="Which milestones this evidence helped complete",
    )
```

**Docstring says (lines 1397-1406):**
> "NOTE: Evidence.category is SYSTEM-INFERRED, not LLM-specified!
> System categorizes based on:
> - Which milestones are incomplete (if symptom not verified -> SYMPTOM_EVIDENCE)
> - Hypothesis evaluation results (if creates hypothesis_evidence links -> CAUSAL_EVIDENCE)
> - Solution state (if solution proposed -> RESOLUTION_EVIDENCE)
>
> LLM provides: summary, analysis
> LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
> **System infers: category, advances_milestones**"

### 2. LLM Response Schema (Per-Turn)

**File:** `faultmaven/core/investigation/schemas.py:162-178`

```python
class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case."""

    summary: str
    content_ref: Optional[str] = None
    category: EvidenceCategory        # LLM specifies
    source_type: EvidenceSourceType   # LLM specifies
    likelihood: float = 0.8

    # NOTE: NO advances_milestones field!
```

**And separately (lines 126-150):**
```python
class MilestoneUpdates(BaseModel):
    """Milestones LLM can set to True (never False)."""

    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = None
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None
    mitigation_applied: Optional[bool] = None
    # solution_verified excluded — requires User-Agent Handshake
```

### 3. How Milestones Are Actually Updated

**File:** `faultmaven/core/investigation/milestone_engine.py:1313-1357`

```python
async def _apply_investigation_updates(...):
    # 1. Update Milestones FROM TURN-LEVEL MilestoneUpdates
    if updates.milestones:
        m = updates.milestones
        p = case.progress
        milestone_fields = [
            "symptom_verified",
            "scope_assessed",
            "timeline_established",
            "changes_identified",
            "root_cause_identified",
            "mitigation_applied",
            "solution_proposed",
            "solution_applied",
        ]
        for field in milestone_fields:
            if getattr(m, field, False):
                # Only append if transitioning from False to True
                if not getattr(p, field, False):
                    setattr(p, field, True)                          # ← Sets milestone on case.progress
                    metadata["milestones_completed"].append(field)   # ← Tracks completion
```

**Key Finding:** Milestones are updated from **MilestoneUpdates** only, NOT from `Evidence.advances_milestones`!

### 4. How Evidence.advances_milestones Is Set

**Current implementation (line 1838):**
```python
evidence = Evidence(
    # ... other fields ...
    advances_milestones=[],  # Calculated later
)
```

**Comment says "Calculated later"** but I found **NO CODE** that actually calculates/populates this field!

**Search results:**
```bash
$ grep -r "advances_milestones.*=" faultmaven/core/investigation/
# Only found initialization to empty list []
# NO calculation logic found!
```

---

## Current State: Inconsistency Identified

### What Actually Happens Today

1. **LLM provides** (in structured output):
   - `evidence_to_add`: List of Evidence to create (NO `advances_milestones` field)
   - `milestones`: MilestoneUpdates object (turn-level)

2. **System applies**:
   - Creates Evidence records with `advances_milestones = []` (always empty!)
   - Updates `case.progress.milestone_field = True` from `MilestoneUpdates`

3. **Result**:
   - `Evidence.advances_milestones` is **NEVER POPULATED** (dead field)
   - `case.progress.*` milestones are the **ONLY SOURCE OF TRUTH**

### So There Is NO Two-Source Problem Today

**Because:** `Evidence.advances_milestones` is not actually used! It's a vestigial field that was intended but never implemented.

---

## Design Options Going Forward

### Option 1: Keep Current Design (Turn-Level Only)

**Remove** `Evidence.advances_milestones` field entirely.

**Rationale:**
- Milestones are turn-level decisions, not evidence-level
- Evidence **contributes to** milestone completion, but doesn't **own** it
- LLM evaluates all evidence together to decide milestone completion

**Example:**
```
Turn 5: User uploads 3 log files
↓
LLM evaluates all 3 together
↓
LLM decides: "With all 3 logs, symptom_verified = true"
↓
Turn-level MilestoneUpdates: {symptom_verified: true}
↓
case.progress.symptom_verified = true
```

**Pros:**
- ✅ Simple (one source of truth)
- ✅ Matches current implementation
- ✅ Aligns with turn-based processing

**Cons:**
- ❌ Can't answer "Which specific evidence advanced which milestone?"
- ❌ Less granular tracking

---

### Option 2: Populate Evidence.advances_milestones (System-Inferred)

**Keep** both, but **system populates** `Evidence.advances_milestones` based on turn-level milestone completion.

**Algorithm:**
```python
# After applying MilestoneUpdates
for milestone in metadata["milestones_completed"]:  # e.g., ["symptom_verified"]
    # Attribute this milestone to all evidence created this turn
    for evidence in evidence_created_this_turn:
        evidence.advances_milestones.append(milestone)
```

**Rationale:**
- Evidence created in the turn that completed a milestone gets credit
- System-inferred (not LLM-specified) so no inconsistency risk

**Pros:**
- ✅ Can answer "Which evidence led to symptom_verified?"
- ✅ Granular attribution for analytics
- ✅ Turn-level remains source of truth

**Cons:**
- ❌ Ambiguous attribution (all evidence in turn gets credit, not just the critical piece)
- ❌ Extra complexity

---

### Option 3: LLM Specifies Both (Two-Phase Consistency Check)

**Add** `advances_milestones` to `EvidenceToAdd` schema.

**LLM provides:**
```python
class EvidenceToAdd(BaseModel):
    summary: str
    category: EvidenceCategory
    source_type: EvidenceSourceType
    advances_milestones: List[str] = []  # NEW: LLM specifies

# AND separately:
class MilestoneUpdates(BaseModel):
    symptom_verified: Optional[bool] = None
    # ... other milestones
```

**System validates:**
```python
# Check consistency
llm_milestones_from_evidence = set()
for ev in state_updates.evidence_to_add:
    llm_milestones_from_evidence.update(ev.advances_milestones)

llm_milestones_from_updates = {
    m for m, v in state_updates.milestones.model_dump().items() if v is True
}

if llm_milestones_from_evidence != llm_milestones_from_updates:
    raise ValidationError("Inconsistent milestone specifications!")
```

**Pros:**
- ✅ Explicit linkage between evidence and milestones
- ✅ LLM must think through which evidence matters
- ✅ Consistency enforced by validation

**Cons:**
- ❌ High cognitive load on LLM (must specify twice)
- ❌ Risk of inconsistency (LLM forgets to sync)
- ❌ Validation failures = poor UX

---

### Option 4: Evidence-Driven Milestones (Evidence as Source of Truth)

**Remove** MilestoneUpdates, **infer** from Evidence.

**LLM only provides:**
```python
class EvidenceToAdd(BaseModel):
    summary: str
    category: EvidenceCategory
    advances_milestones: List[str]  # LLM specifies
    # NO separate MilestoneUpdates
```

**System infers:**
```python
# Collect all milestones from evidence
milestones_to_complete = set()
for ev in state_updates.evidence_to_add:
    milestones_to_complete.update(ev.advances_milestones)

# Apply to case
for milestone in milestones_to_complete:
    setattr(case.progress, milestone, True)
```

**Pros:**
- ✅ Single source of truth (evidence-driven)
- ✅ Clear attribution per evidence

**Cons:**
- ❌ Milestones not always evidence-driven (e.g., timeline established from conversation)
- ❌ Can't complete milestone without adding evidence (awkward for pure analysis)

---

## Recommendation: Option 2.5 (Hybrid: System-Inferred with Optional LLM Override)

**Final Design Decision** (User-Endorsed)

**Why:**

1. **Maintains turn-level source of truth** (MilestoneUpdates drives state, unchanged)
2. **Adds intelligent attribution** via system inference (handles 90% of cases)
3. **Allows LLM override** when explicit attribution needed (handles 10% edge cases)
4. **No risk of inconsistency** (system controls defaults, LLM can enhance)
5. **Enables analytics** ("Which evidence was most impactful?")
6. **Zero extra token cost** for common cases (inference is deterministic)

### Three-Tier Logic

```
1. MilestoneUpdates drives state (turn-level, LLM specifies) → UNCHANGED
2. System infers advances_milestones from category (NEW — handles 90%)
3. LLM overrides when explicit (NEW — handles 10%)
```

**Key Insight:** With one-file-per-turn constraint (UI limitation), inference is **unambiguous** — there's only one evidence record to attribute to, so all milestones completed that turn that match the category get attributed to it. No guessing.

---

## Detailed Implementation: Option 2.5

### 1. Derive Category-Milestone Mapping

**Source of Truth:** `MILESTONE_EVIDENCE_EXPECTATIONS` (already defined in codebase)

**Derived Mapping:**
```python
# faultmaven/core/investigation/milestone_engine.py

# This mapping is derived from MILESTONE_EVIDENCE_EXPECTATIONS
# It defines which milestones each evidence category can potentially advance
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified",
        "scope_assessed",
        "timeline_established",
        "changes_identified",
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        "changes_identified",
        "root_cause_identified",
        "solution_proposed",
    ],
    EvidenceCategory.RESOLUTION_EVIDENCE: [
        "solution_applied",
    ],
    EvidenceCategory.CONTEXTUAL_EVIDENCE: [
        # Contextual evidence provides baseline/environmental info
        # It can inform scope/timeline but doesn't directly advance milestones
    ],
}
```

**Why This Mapping?**

- **SYMPTOM_EVIDENCE** helps verify the problem exists and understand its scope, timeline, and what changed
- **CAUSAL_EVIDENCE** helps identify what changed, determine root cause, and propose solutions
- **RESOLUTION_EVIDENCE** demonstrates solution effectiveness
- **CONTEXTUAL_EVIDENCE** provides supporting context but doesn't directly advance investigation milestones

**Note:** `changes_identified` appears in both SYMPTOM and CAUSAL because:
- SYMPTOM evidence can show "what changed" (deployment logs, config diffs)
- CAUSAL evidence can identify "which change caused the problem"

### 2. Inference Function

```python
def _infer_milestones(
    category: EvidenceCategory,
    milestones_completed_this_turn: List[str]
) -> List[str]:
    """
    Infer which milestones this evidence likely advanced.

    Args:
        category: The evidence category (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL)
        milestones_completed_this_turn: Milestones completed this turn from MilestoneUpdates

    Returns:
        List of milestone names this evidence contributed to

    Logic:
        - Get eligible milestones for this category from CATEGORY_MILESTONE_MAP
        - Intersect with milestones completed this turn
        - Result = milestones this evidence can claim credit for

    Example:
        category = SYMPTOM_EVIDENCE
        milestones_completed_this_turn = ["symptom_verified", "scope_assessed"]
        eligible = ["symptom_verified", "scope_assessed", "timeline_established", "changes_identified"]
        result = ["symptom_verified", "scope_assessed"]
    """
    eligible = CATEGORY_MILESTONE_MAP.get(category, [])
    return [m for m in milestones_completed_this_turn if m in eligible]
```

**Why This Works:**

1. **Deterministic:** Same inputs always produce same output
2. **Unambiguous:** One evidence per turn (UI constraint) means clear attribution
3. **Aligned with expectations:** Uses existing MILESTONE_EVIDENCE_EXPECTATIONS logic
4. **Efficient:** Simple set intersection, no LLM call needed

### 3. Evidence Creation with Inference

```python
async def _apply_investigation_updates(...):
    # ... existing code to update milestones ...

    # Track milestones completed this turn
    milestones_completed = metadata["milestones_completed"]  # e.g., ["symptom_verified", "scope_assessed"]

    # Create evidence (existing code)
    evidence_created_this_turn = []
    for ev_item in updates.evidence_to_add:
        # Check if LLM explicitly specified advances_milestones (optional override)
        if hasattr(ev_item, 'advances_milestones') and ev_item.advances_milestones:
            # Tier 3: LLM explicitly specified (handles edge cases)
            advances = ev_item.advances_milestones
            logger.info(
                f"Using LLM-specified advances_milestones for {ev_item.category}: {advances}"
            )
        else:
            # Tier 2: System infers from category (handles common cases)
            advances = _infer_milestones(ev_item.category, milestones_completed)
            logger.info(
                f"Inferred advances_milestones for {ev_item.category}: {advances}"
            )

        evidence = Evidence(
            # ... other fields ...
            category=ev_item.category,
            advances_milestones=advances,  # Set via inference or LLM override
        )
        case.evidence.append(evidence)
        evidence_created_this_turn.append(evidence)

    logger.info(
        f"Created {len(evidence_created_this_turn)} evidence records with milestone attribution"
    )
```

### 4. Optional LLM Override Schema

**Add optional field to EvidenceToAdd:**

```python
# faultmaven/core/investigation/schemas.py

class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case."""

    summary: str
    content_ref: Optional[str] = None
    category: EvidenceCategory
    source_type: EvidenceSourceType
    likelihood: float = 0.8

    # OPTIONAL: LLM can specify if inference would be wrong
    advances_milestones: Optional[List[str]] = Field(
        None,
        description=(
            "OPTIONAL: Explicitly specify which milestones this evidence advances. "
            "If not provided, system will infer based on category. "
            "Only specify when inference would be incorrect (rare: ~10% of cases)."
        )
    )
```

**Prompt Guidance:**

```
# Milestone Attribution (Optional)

By default, the system automatically infers which milestones each evidence advances
based on its category:

- SYMPTOM_EVIDENCE → symptom_verified, scope_assessed, timeline_established, changes_identified
- CAUSAL_EVIDENCE → changes_identified, root_cause_identified, solution_proposed
- RESOLUTION_EVIDENCE → solution_applied

You only need to specify `advances_milestones` explicitly when:
1. Evidence doesn't contribute to the usual milestones for its category
2. Evidence advances a milestone outside its typical category
3. You want to be more specific about attribution

For 90% of cases, leave `advances_milestones` unspecified and let inference handle it.
```

### 5. Benefits of Option 2.5

**Compared to Option 1 (Remove field):**
- ✅ Preserves traceability ("Which evidence led to symptom_verified?")
- ✅ Enables analytics (evidence impact, attribution metrics)
- ✅ Supports forensic review ("How did we conclude X?")

**Compared to Option 2 (Pure inference):**
- ✅ Handles edge cases where inference would be wrong
- ✅ LLM can be more precise when needed
- ✅ Doesn't force incorrect attributions

**Compared to Option 3 (Always LLM-specified):**
- ✅ Zero token cost for common cases (90%)
- ✅ No LLM cognitive load for obvious mappings
- ✅ No risk of inconsistency (inference is always correct for defaults)
- ✅ Simpler schema (optional field vs required)

**Compared to Option 4 (Evidence-driven milestones):**
- ✅ Maintains turn-level state control (MilestoneUpdates)
- ✅ Allows milestone completion without evidence (pure analysis turns)
- ✅ Preserves existing architecture

---

## User's Question: User Confirmation

> "this might be our deliberate design choice but I don't remember the reason behind. is this what would happen when the user does not confirm the state transition?"

**Answer: NO, this is unrelated to user confirmation.**

**User confirmation** applies to:
1. **Problem statement confirmation** (INQUIRY → INVESTIGATING transition)
2. **Solution verification** (solution_applied → solution_verified)

**NOT to milestone completion during investigation.**

**Milestones during INVESTIGATING phase** (`symptom_verified`, `root_cause_identified`, etc.) are **LLM-driven, not user-confirmed**.

The LLM sets these based on evidence analysis, and they apply immediately without requiring user confirmation.

---

## Decision Summary

**FINAL DECISION: Option 2.5 (Hybrid: System-Inferred with Optional LLM Override)**

### User Endorsement

> "option 2.5 is an enhancement. It solves a real problem mine didn't address: who populates advances_milestones when the LLM doesn't specify it? My approach left that as the LLM's responsibility 100% of the time, which is unnecessary token cost for a value that's usually inferrable."

### Three-Tier Logic

```text
1. MilestoneUpdates drives state (turn-level, LLM specifies) → UNCHANGED
2. System infers advances_milestones from category (NEW — handles 90%)
3. LLM overrides when explicit (NEW — handles 10%)
```

**Key Constraint:** One-file-per-turn (UI limitation) makes inference unambiguous.

---

## Action Items

### Phase 1: Update Design Documents ✅

1. ✅ MILESTONE-ADVANCEMENT-ANALYSIS.md - Documented Option 2.5 with corrected CATEGORY_MILESTONE_MAP
2. **TODO**: Update EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md with milestone advancement section
3. **TODO**: Update EVIDENCE-REDESIGN-IMPLEMENTATION-PLAN.md with Option 2.5 implementation tasks

### Phase 2: Implementation

1. **Add CATEGORY_MILESTONE_MAP** to `milestone_engine.py`
   - Derive from existing MILESTONE_EVIDENCE_EXPECTATIONS (single source of truth)
   - Document rationale for each mapping

2. **Implement _infer_milestones() function**
   - Category + milestones_completed → inferred advances_milestones
   - Simple set intersection logic

3. **Update _apply_investigation_updates()**
   - Check for LLM override (Tier 3: optional advances_milestones field)
   - Fall back to inference (Tier 2: system inference)
   - Set Evidence.advances_milestones appropriately

4. **Add optional field to EvidenceToAdd schema**
   - advances_milestones: Optional[List[str]] = None
   - Clear docstring: "Only specify when inference would be incorrect"

5. **Update prompt templates**
   - Add guidance on when to specify advances_milestones
   - Emphasize that 90% of cases don't need it

### Phase 3: Testing

1. **Test system inference** (Tier 2)
   - SYMPTOM_EVIDENCE → infers symptom_verified, scope_assessed, etc.
   - CAUSAL_EVIDENCE → infers root_cause_identified, solution_proposed, etc.
   - RESOLUTION_EVIDENCE → infers solution_applied

2. **Test LLM override** (Tier 3)
   - LLM specifies advances_milestones explicitly
   - System uses LLM value instead of inference

3. **Test edge cases**
   - Multiple evidence records in one turn (should not happen with UI constraint)
   - Evidence category with no eligible milestones (CONTEXTUAL_EVIDENCE)
   - Turn completes no milestones (advances_milestones = [])

---

## Resolution of User's Original Question

**User's Question:**
> "advances_milestones on evidence vs. MilestoneUpdates in turn response — two sources of truth. This creates two sources of milestone information that should be consistent but are set independently."

**Answer:**

1. **Today:** Not actually a problem — `advances_milestones` is a dead field (never populated)

2. **Going Forward:** Option 2.5 eliminates the two-source problem by making MilestoneUpdates the **single source of truth** and `advances_milestones` a **derived attribute**:

   - MilestoneUpdates (turn-level) → drives case.progress state changes
   - advances_milestones (evidence-level) → system-inferred from category + completed milestones
   - LLM can optionally override inference for edge cases

3. **No inconsistency risk:** System controls both values with clear derivation logic

4. **Benefits:** Granular attribution for analytics without LLM burden or inconsistency risk
