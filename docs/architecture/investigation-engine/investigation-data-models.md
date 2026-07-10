# Investigation Data Models

This document defines the core data models used in FaultMaven's evidence-driven investigation framework.

**Related Documents**:

- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Overview and philosophy
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) - State transitions and path routing

---

## Table of Contents

- [Field Naming Conventions](#field-naming-conventions)
- [Core Data Models](#1-core-data-models)
- [Evidence Model](#2-evidence-model)
- [Hypothesis Workflow](#3-hypothesis-workflow)

---

## Field Naming Conventions

To ensure consistency across the codebase, use these standard field names:

### Probability/Confidence Fields

| Field Name | Type | Range | Used For |
|------------|------|-------|----------|
| `likelihood` | `float` | 0.0-1.0 | Numeric probability score (primary field) |
| `confidence_level` | `ConfidenceLevel` | enum | Categorical confidence (derived from `likelihood`) |

**Conversion Rules** (implemented in `ConfidenceLevel.from_score()`):

- `likelihood < 0.5` → `SPECULATION`
- `likelihood 0.5-0.69` → `PROBABLE`
- `likelihood 0.7-0.89` → `CONFIDENT`
- `likelihood ≥ 0.9` → `VERIFIED`

### Where Applied

| Model | Primary Field | Derived Field |
|-------|---------------|---------------|
| `Hypothesis` | `likelihood: float` | - |
| `InvestigationProgress` | `root_cause_likelihood: float` | - |
| `RootCauseConclusion` | `likelihood: float` | `confidence_level: ConfidenceLevel` |
| `WorkingConclusion` | `likelihood: float` | - |
| `Evidence` (in hypothesis links) | `stance_confidence: float` | - |

### Deprecated Names (Do Not Use)

These field names are deprecated and should not be used in new code:

| Deprecated | Replace With |
|------------|--------------|
| `confidence` | `likelihood` |
| `confidence_score` | `likelihood` |
| `root_cause_confidence` | `root_cause_likelihood` |
| `completeness` | `stance_confidence` |

---

## 1. Core Data Models

### 1.1 CaseState

```python
class CaseState(str, Enum):
    """
    Case lifecycle status (4 values: 2 phases + 2 dispositions).
    Two dispositions: RESOLVED (with solution) and CLOSED (without solution).
    """

    INQUIRY = "inquiry"
    """
    PHASE: Pre-investigation exploration.
    User asking questions, agent providing quick guidance.
    No formal investigation commitment yet.
    """

    INVESTIGATING = "investigating"
    """
    PHASE: Active formal investigation.
    Working through verification, diagnosis, and resolution.
    Problem not yet fixed.
    """

    RESOLVED = "resolved"
    """
    DISPOSITION: Case closed WITH solution.
    Problem was fixed and verified.

    closure_reason = None  (resolution itself is the categorization)
    """

    CLOSED = "closed"
    """
    DISPOSITION: Case closed WITHOUT solution.
    Investigation completed without a verified fix, or inquiry-only.

    closure_reason = "inquiry_only" | "closed_after_investigation"
    Engine-derived via derive_closure_reason(). Never authored by the LLM.
    Note: a case stabilized then closed is simply "closed_after_investigation"
    (the former "mitigation_sufficient" reason was folded in — the documented
    mitigation is preserved on the closed case).
    """
```

**Key Points**:

- **RESOLVED** and **CLOSED** are both dispositions (terminal — no further case actions)
- **RESOLVED** = Problem fixed (has solution, solution_verified=True)
- **CLOSED** = Problem not fixed (no solution, or mitigation-only, or inquiry-only)
- Agent doesn't care about cases after they reach a disposition

### 1.2 InvestigationProgress

Under the unified opportunistic flow (see
[investigation-lifecycle-logic.md §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert)), progress
tracking carries three kinds of state: **action-compliance gates** (did the user
accept/verify a proposed action), **progress indicators** (advisory, non-driving),
and **assessment variables** (engine-derived truth signals about what we know).
The path enum is gone; the mitigation gate is a single forward-only record.

```python
# Illustrative subset — see faultmaven/modules/case/domain/models.py for the canonical
# InvestigationProgress (additionally exposes verification_completed_at,
# investigation_completed_at, resolution_completed_at timestamp fields).
class InvestigationProgress(BaseModel):
    """
    Evidence-driven progress tracking with three kinds of state:

    1. ACTION-COMPLIANCE GATES: Drive the derived stage label + resolution
       handshake. Materialized by the LLM's compliance signals (the user's
       action is the trigger; the LLM recognizes it). The mitigation gate
       is a single record, not booleans.
    2. PROGRESS INDICATORS: Provide LLM context and analytics. Non-driving.
    3. ASSESSMENT VARIABLES (engine-derived): Truth signals the engine
       recomputes every turn. NEVER path-stripped. Drive whether the
       diagnostic machinery runs.
    """

    # ============================================================
    # ACTION-COMPLIANCE GATES
    # ============================================================
    mitigation: Optional[MitigationRecord] = Field(
        default=None,
        description=(
            "Mitigation insert record. Materialized by the engine from the "
            "LLM's mitigation accept/verify gate signals (still EMITTED as "
            "`mitigation_accepted` / `mitigation_verified` in the schema) plus "
            "the workaround ProposedAction. Replaces the mitigation_* booleans "
            "and path_selection.mitigation_completed_at_turn. Its existence marks "
            "the case 'stabilized'."
        ),
    )

    solution_accepted: bool = Field(
        default=False,
        description=(
            "User acknowledges executing proposed solution. "
            "Drives the derived 'Resolving' stage label."
        )
    )

    solution_verified: bool = Field(
        default=False,
        description=(
            "Solution effectiveness verified via User-Agent Handshake. "
            "NOT directly settable by LLM — requires explicit user confirmation. "
            "Triggers TREATMENT → RESOLVED transition."
        )
    )

    # ============================================================
    # PROGRESS INDICATORS (LLM context, non-stage-driving)
    # ============================================================
    symptom_verified: bool = Field(
        default=False,
        description="Symptom confirmed with evidence"
    )

    solution_proposed: bool = Field(
        default=False,
        description=(
            "Engine-derived at the assessment recompute (INV-32): True iff a "
            "LIVE ProposedAction with action_type=SOLUTION stands "
            "(state pending/accepted) or the gate ladder advanced "
            "(solution_accepted/solution_verified). Not set by LLM; not a "
            "write-once latch — a superseded or license-lost offer drops it."
        )
    )

    # ============================================================
    # ASSESSMENT VARIABLES (engine-derived knowledge state)
    # Truth signals, recomputed every turn, NEVER path-stripped.
    # ============================================================
    cause_state: CauseState = Field(
        default=CauseState.UNKNOWN,
        description=(
            "Engine-derived knowledge state of the root cause "
            "(UNKNOWN | CANDIDATES | IDENTIFIED). Replaces the boolean "
            "root_cause_identified. IDENTIFIED == the old True (grounded "
            "cause-known signal); CANDIDATES is derived from >=2 ACTIVE "
            "hypotheses. Drives whether the diagnostic machinery runs."
        ),
    )

    solution_state: SolutionState = Field(
        default=SolutionState.UNKNOWN,
        description=(
            "Knowledge state of the fix (UNKNOWN | SELECTED this round). "
            "CANDIDATES (multi-solution deliberation) is reserved for a follow-on."
        ),
    )

    solution_feasible: SolutionFeasible = Field(
        default=SolutionFeasible.NOW,
        description=(
            "Whether the SELECTED solution can be applied this session "
            "(NOW | DEFERRED). DEFERRED routes to CLOSE-with-documented-solution."
        ),
    )

    # ============================================================
    # Root Cause Metadata (populated when cause_state == IDENTIFIED)
    # ============================================================
    root_cause_likelihood: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Likelihood of root cause identification (0.0-1.0)"
    )

    root_cause_method: Optional[str] = Field(
        default=None,
        description="direct_analysis | hypothesis_validation | single_shot_validation | correlation | user_provided | other"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> InvestigationStage:
        """
        Compute investigation stage as a DERIVED UI VIEW (redesign R4).
        The stage no longer drives prompt dispatch — it is a pure display
        label over the action-compliance gates.

        Returns one of 3 InvestigationStage enum values:
        - MITIGATION: a mitigation is accepted but not yet verified
        - TREATMENT: solution accepted but not yet verified
        - DIAGNOSIS: everything else (default investigating view)
        """
        # MITIGATION: mitigation accepted but not yet verified.
        if (
            self.mitigation is not None
            and self.mitigation.accepted
            and not self.mitigation.verified
        ):
            return InvestigationStage.MITIGATION

        # TREATMENT: solution_accepted but not yet verified
        if self.solution_accepted and not self.solution_verified:
            return InvestigationStage.TREATMENT

        # Default: DIAGNOSIS. Sub-phase distinguished by symptom_verified /
        # cause_state, not by the stage enum.
        return InvestigationStage.DIAGNOSIS

    @property
    def stage_display_name(self) -> str:
        """
        User-facing stage name for UI display (redesign R4).

        - DIAGNOSIS → "Investigating"
        - MITIGATION → "Mitigating"
        - TREATMENT → "Resolving"
        """
        stage = self.current_stage
        if stage == InvestigationStage.DIAGNOSIS:
            return "Investigating"
        elif stage == InvestigationStage.MITIGATION:
            return "Mitigating"
        else:  # TREATMENT
            return "Resolving"
```

**Assessment variables vs. the old boolean.** `cause_state` replaces the boolean
`root_cause_identified` cleanly (no compat shim). Read sites use
`cause_state == CauseState.IDENTIFIED`. The LLM still *emits* a grounded
"cause identified" signal; the engine computes the stored enum each turn via
`_recompute_assessment_state` — `IDENTIFIED` when the grounded signal is set (and
passes the self-naming-aware justification), else `CANDIDATES` when
`count_active_hypotheses(case) >= 2`, else `UNKNOWN`. The enum is never
path-stripped (the linchpin of the redesign).

#### Progress Milestone Evidence Expectations

The milestone engine validates evidence claims for **progress indicators** (non-stage-driving) using a category-count check:

| Progress / Assessment signal | Min Evidence | Expected Categories |
|-------------------|-------------|---------------------|
| `symptom_verified` | 1 | SYMPTOM |
| grounded cause signal (→ `cause_state = IDENTIFIED`) | 2 | CAUSAL (or a self-naming-error extract) |
| `solution_proposed` | 0 | (engine-derived from live SOLUTION ProposedActions, INV-32) |

**Gate milestones** are NOT evidence-validated — they are set by the LLM in structured output when it detects user compliance with a ProposedAction (Framework §4.2). The mitigation gate signals (still EMITTED as `mitigation_accepted` / `mitigation_verified`) materialize into `progress.mitigation` rather than booleans:

| Gate signal (LLM emission) | Materializes as | Trigger |
|---------------------|-----------------|---------|
| `mitigation_accepted` | `mitigation.accepted` | User acknowledges executing proposed mitigation (workaround) |
| `mitigation_verified` | `mitigation.verified` (+ `completed_at_turn`) | User confirms the mitigation stabilized the situation |
| `solution_accepted` | `solution_accepted` | User acknowledges executing proposed solution |
| `solution_verified` | `solution_verified` | User confirmed fix worked (User-Agent Handshake) |

**How progress milestone validation works:**

1. LLM sets progress milestone = True in structured output
2. System extracts evidence IDs from `internal_reasoning.evidence_analyzed`
3. System counts cited evidence matching expected categories
4. If count < minimum: warning logged (milestone still set, but flagged)

Validation is **advisory, not blocking**. The LLM's progress milestone assertions are trusted, with validation providing quality feedback for monitoring.

```python
class InvestigationStage(str, Enum):
    """
    Investigation stage within the Investigating Phase.

    2-stage model with mitigation detour:
    - DIAGNOSIS: Understand, diagnose, propose actions (core stage)
    - TREATMENT: Apply permanent fix, verify resolution (core stage)
    - MITIGATION: Apply and verify temporary fix (optional detour)

    Stage transitions are inference-based (user compliance).
    """
    DIAGNOSIS = "diagnosis"
    """Understand problem, diagnose root cause, propose actions."""

    MITIGATION = "mitigation"
    """Apply and verify temporary fix. Returns to DIAGNOSIS when verified."""

    TREATMENT = "treatment"
    """Apply permanent fix, verify resolution. Extended diagnosis if fix fails."""

class TemporalState(str, Enum):
    """Problem temporal state for routing decisions"""
    ONGOING = "ongoing"
    HISTORICAL = "historical"
```

### 1.3 TurnProgress

```python
class TurnProgress(BaseModel):
    """Record of what happened in one turn"""

    turn_number: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # What advanced this turn
    milestones_completed: List[str] = Field(default_factory=list)
    evidence_added: List[str] = Field(default_factory=list)
    hypotheses_generated: List[str] = Field(default_factory=list)
    hypotheses_validated: List[str] = Field(default_factory=list)
    solutions_proposed: List[str] = Field(default_factory=list)

    # Progress assessment
    progress_made: bool

    # Outcome
    outcome: TurnOutcome

    # User interaction
    user_message_summary: Optional[str] = None
    agent_response_summary: Optional[str] = None

class TurnOutcome(str, Enum):
    """
    Turn outcome classification.

    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress, progress monitoring).
    """

    MILESTONE_COMPLETED = "milestone_completed"
    """One or more milestones completed"""

    DATA_PROVIDED = "data_provided"
    """User provided data/evidence this turn"""

    DATA_REQUESTED = "data_requested"
    """Agent requested data from user"""

    DATA_NOT_PROVIDED = "data_not_provided"
    """Agent requested data, user didn't provide (may follow up or pivot)"""

    HYPOTHESIS_TESTED = "hypothesis_tested"
    """Hypothesis validated or refuted"""

    CASE_RESOLVED = "case_resolved"
    """Solution verified, case can transition to RESOLVED status"""

    CONVERSATION = "conversation"
    """Normal Q&A, no data requests or milestones"""

    OTHER = "other"
    """Doesn't fit standard outcomes"""

    # NOTE: No "BLOCKED" - investigation stalls naturally via turns_without_progress
    # Progress monitor activates transparent mode at 5+ investigative turns without progress (prompt hints, not mode changes)
```

### 1.4 Assessment Variables & the Mitigation Record

The path fork (`InvestigationPath` / `PathSelection`) is **removed**. The single
prospective fork conflated two independent questions — "do we know the cause?"
(certainty) and "is something hurting now we can't fully resolve this session?"
(mitigation gap). The redesign decouples them: **assessment variables** encode
certainty (engine-derived, never path-stripped), and a **mitigation record**
captures the optional inserted mitigation sub-activity. See
[investigation-lifecycle-logic.md §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert) for the design
rationale.

```python
class CauseState(str, Enum):
    """Engine-derived knowledge state of the root cause (assessment variable).

    Replaces the boolean root_cause_identified. Recomputed every turn from the
    LLM's grounded cause-identification signal plus the active-hypothesis count.
    NEVER path-stripped — recording a cause the engine legitimately knows is a
    truth signal, not an earned process milestone. Drives whether the diagnostic
    machinery (hypothesis formulation + evidence-needs) runs this turn.
    """
    UNKNOWN = "unknown"        # No cause hypothesis yet. Diagnostic machinery active.
    CANDIDATES = "candidates"  # >=2 ACTIVE hypotheses. Diagnostic machinery active.
    IDENTIFIED = "identified"  # Single cause known (== old root_cause_identified True).


class SolutionState(str, Enum):
    """Engine-derived knowledge state of the fix (assessment variable).

    UNKNOWN | SELECTED only this round. CANDIDATES (multi-solution deliberation,
    redesign §6) is RESERVED for the follow-on that reuses the hypothesis
    machinery and is intentionally not produced yet.
    """
    UNKNOWN = "unknown"        # No solution chosen yet.
    CANDIDATES = "candidates"  # RESERVED — multi-solution deliberation; not produced this round.
    SELECTED = "selected"      # A single solution has been chosen.


class SolutionFeasible(str, Enum):
    """Whether the SELECTED solution can be applied within this session.

    LLM-settable, defaults NOW. DEFERRED routes to CLOSE-with-documented-solution.
    """
    NOW = "now"            # Implementable during this troubleshooting session.
    DEFERRED = "deferred"  # Known, but implementation takes time / happens out-of-band.


class MitigationRecord(BaseModel):
    """A single forward-only mitigation (the inserted "stop the bleeding" move).

    Replaces the path-coupled mitigation gates + path_selection.mitigation_completed_at_turn.
    The engine materializes this record from the LLM's accept/verify gate signals
    (still emitted under the names `mitigation_accepted` / `mitigation_verified`)
    plus the workaround ProposedAction. Single record per investigation for now;
    the flow stays open to user-led action so a non-mitigating insert is never a
    dead-end.
    """
    proposed_at_turn: Optional[int] = Field(
        default=None, description="Turn a workaround mitigation was first proposed"
    )
    accepted: bool = Field(
        default=False, description="User complied with the proposed mitigation"
    )
    verified: bool = Field(
        default=False, description="User confirmed the mitigation stabilized the situation"
    )
    completed_at_turn: Optional[int] = Field(
        default=None,
        description="Turn `verified` flipped True — boundary for up-weighting "
                    "pre-mitigation evidence in any later RCA",
    )

    @model_validator(mode="after")
    def _verified_requires_accepted(self) -> "MitigationRecord":
        # Forward-only: verified ⇒ accepted.
        if self.verified and not self.accepted:
            raise ValueError("mitigation.verified=True requires mitigation.accepted=True")
        return self
```

**Retrospective shape (derived, not stored).** A case is described after the fact
as **direct** (no mitigation) or **mitigated** (`mitigation is not None`).
There is no prospective path commit — the agent proposes a mitigation in-prompt
when an Axis-B (mitigation-gap) judgment fires, and the user accepts or declines.

### 1.5 Complete Case Model

> **Illustrative subset** — this snippet covers the investigation-engine-facing fields. The canonical model in `faultmaven/modules/case/domain/models.py` additionally exposes `description`, `is_archived`, `archived_at`, `last_activity_at`, `pending_transition`, `last_suggestions`, `kb_context`, `messages`, `message_count`, and `investigation_journal`.

```python
class Case(BaseModel):
    """Complete case model with evidence-driven investigation architecture"""

    # ============================================================
    # Core Identity
    # ============================================================
    case_id: str = Field(default_factory=lambda: f"case_{uuid4().hex[:12]}")
    user_id: str
    organization_id: str
    title: str

    # ============================================================
    # Status (PRIMARY - User-Facing Lifecycle)
    # ============================================================
    state: CaseState = Field(default=CaseState.INQUIRY)

    action_history: List[CaseAction] = Field(
        default_factory=list,
        description="Audit trail of case actions (phase transitions and disposition changes)"
    )

    closure_reason: Optional[str] = Field(
        default=None,
        description="None for RESOLVED. For CLOSED: inquiry_only | closed_after_investigation. Engine-derived via derive_closure_reason(); never set by the LLM."
    )

    # ============================================================
    # Investigation Progress (SECONDARY - Internal Detail)
    # ============================================================
    progress: InvestigationProgress = Field(default_factory=InvestigationProgress)

    # ============================================================
    # Turn Tracking
    # ============================================================
    current_turn: int = Field(default=0)
    turns_without_progress: int = Field(default=0)
    turn_history: List[TurnProgress] = Field(default_factory=list)

    # ============================================================
    # Problem Context
    # ============================================================
    inquiry: InquiryData = Field(default_factory=InquiryData)
    problem_verification: Optional[ProblemVerification] = None

    # ============================================================
    # Investigation Data
    # ============================================================
    uploaded_files: List[UploadedFile] = Field(
        default_factory=list,
        description="Raw file metadata (files uploaded in any phase)"
    )
    evidence: List[Evidence] = Field(default_factory=list)
    hypotheses: Dict[str, Hypothesis] = Field(default_factory=dict)
    solutions: List[Solution] = Field(default_factory=list)
    proposed_actions: List[ProposedAction] = Field(
        default_factory=list,
        description="Actions proposed by agent during DIAGNOSIS"
    )
    action_attempts: List[ActionAttempt] = Field(
        default_factory=list,
        description="History of all mitigation and solution attempts"
    )

    # ============================================================
    # Cross-Cutting State
    # ============================================================
    working_conclusion: Optional[WorkingConclusion] = None
    root_cause_conclusion: Optional[RootCauseConclusion] = None
    investigation_journal: List[JournalEntry] = Field(
        default_factory=list,
        description="Append-only log of key findings, decisions, and context. "
        "Always included in full in LLM context."
    )

    # ============================================================
    # Special States
    # ============================================================
    escalation_state: Optional[EscalationState] = None

    # ============================================================
    # Documentation
    # ============================================================
    documentation: DocumentationData = Field(default_factory=DocumentationData)

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = Field(
        default=None,
        description="When case reached RESOLVED status"
    )
    closed_at: Optional[datetime] = Field(
        default=None,
        description="When case reached a disposition (RESOLVED or CLOSED)"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """Investigation stage (only when INVESTIGATING)"""
        if self.status != CaseState.INVESTIGATING:
            return None
        return self.progress.current_stage

    # is_stuck removed — replaced by ProgressMonitor transparent mode
    # See: docs/architecture/investigation-engine/progress-transparency.md

    @property
    def is_terminal(self) -> bool:
        """Check if case has reached a disposition (terminal)"""
        return self.status in [CaseState.RESOLVED, CaseState.CLOSED]

    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        """Time from creation to disposition"""
        if self.closed_at:
            return self.closed_at - self.created_at
        return None

class CaseAction(BaseModel):
    """Record of a case action (phase transition or disposition change)"""
    from_state: CaseState
    to_state: CaseState
    triggered_at: datetime
    triggered_by: str
    reason: str
```

### 1.5.1 Terminal Summary Report Types

When a case reaches terminal state, the system auto-generates a structured summary report.

```python
class ReportType(str, Enum):
    """Type of case documentation report"""

    # Auto-generated on terminal transition
    RESOLUTION_SUMMARY = "resolution_summary"  # RESOLVED cases (always generated)
    CLOSURE_SUMMARY = "closure_summary"        # CLOSED cases (subject to substance gate)

    # User-requested via ConversionService
    RUNBOOK = "runbook"                        # From RESOLVED cases only (requires root cause)


# Illustrative subset — see faultmaven/modules/case/domain/owned_models/report.py for the canonical model.
class CaseReport(BaseModel):
    """Case documentation report (stored in reports table)"""

    report_id: str
    case_id: str
    report_type: ReportType
    title: str = Field(min_length=10, max_length=200)
    content: str                    # Markdown
    format: Literal["markdown"]
    generation_status: ReportStatus
    generated_at: str               # ISO 8601
    updated_at: Optional[str] = None  # ISO 8601 — set on regeneration
    generation_time_ms: int = Field(ge=0, le=120000)  # 2-minute hard ceiling
    is_current: bool = True
    version: int = Field(default=1, ge=1, le=5)  # 5-version cap; drives remaining_regenerations
    linked_to_closure: bool = False
    auto_generated: bool = Field(
        default=False,
        description="True for terminal summaries (RESOLUTION_SUMMARY, CLOSURE_SUMMARY)."
    )
    metadata: Optional[RunbookMetadata] = None
```

**Key properties** (stored on `CaseReport`):

- `auto_generated=True` — distinguishes from user-requested reports
- `linked_to_closure=False` — auto-summaries do NOT freeze the case; only user-requested reports do
- `version=1` — no versioning for auto-summaries (generated once); the `version` field caps at 5 for user-requested reports
- The model has no `created_by` field; system vs user origin is inferred from `auto_generated`

The content-focus table (which fields each summary type covers), the substance gate (evidence / hypotheses / completed_milestones — see `should_generate_terminal_summary`), the set of terminal transition paths that synchronously generate, the chat-side rendering of the summary inline at the moment of generation, and the SYNTHESIS-capability generation path are canonical in:

See **[Investigation Lifecycle Logic §4.5.0](./investigation-lifecycle-logic.md#450-auto-generated-terminal-summary)** (full specification) and **[§1.7.3](./investigation-lifecycle-logic.md#173-auto-generated-terminal-summary)** (post-terminal lifecycle view).

### 1.5.2 Resolution & Runbook Readiness Models

Two validation checks gate the resolve and runbook generation flows. Both are in `terminal_transitions.py`.

**ResolutionReadiness** (`assess_resolution_readiness(case)`) — minimum bar for marking a case as RESOLVED.

`ResolutionReadiness` is a **constructed class** (not an enum), defined in `faultmaven/core/investigation/terminal_transitions.py`. Verdict strings:

```python
# Constructor: ResolutionReadiness(verdict, message, missing)
class ResolutionReadiness:
    # Verdict values:
    #   "ready"          — Root cause + solution present → propose RESOLVED transition
    #   "needs_info"     — One missing (e.g., root cause but no solution) → propose with needs_info=True
    #   "suggest_close"  — No root cause, no solution, no evidence → pivot to CLOSED (both UI-dropdown and LLM-emit paths)
    verdict: str
    message: str        # Human-facing explanation
    missing: List[str]  # Field names that need to be filled before READY
```

Checks: `root_cause_conclusion` (or `working_conclusion` with likelihood ≥0.6), `solutions` list, `problem_verification`, `evidence` list.

For `NEEDS_INFO`, the system stores the pending transition with `needs_info=True`. This remembers the user's intent to resolve. On subsequent turns, `_check_automatic_transitions` re-evaluates readiness. When the case becomes READY, the LLM response is overridden with a deterministic confirmation prompt.

For `SUGGEST_CLOSE`, the engine pivots the pending proposal to CLOSED immediately (both UI-dropdown and LLM-emit paths). The user sees the close confirmation pair rather than a resolve prompt.

**RunbookReadiness** (`assess_runbook_readiness(case)`) — higher bar for quality runbook generation.

Constructed class (not an enum):

```python
# Constructor: RunbookReadiness(verdict, message, section_coverage)
class RunbookReadiness:
    # Verdict values:
    #   "ready"             — Problem + root cause + actionable solution (commands/steps)
    #   "needs_enrichment"  — Critical sections OK, 2+ enrichment gaps
    #   "not_suitable"      — Missing problem definition or root cause with fix
    verdict: str
    message: str                   # Human-facing explanation
    section_coverage: Dict[str, bool]  # Per-section presence map
```

Maps case data to the 7 canonical runbook sections:

| Runbook Section | Case Data Source | Required? |
|---|---|---|
| Problem Definition | `problem_verification.symptom_statement` | Critical |
| Root Cause Resolution | `root_cause_conclusion` + solution with commands/steps/longterm_fix | Critical |
| Diagnostic Steps | `evidence[].summary` + `hypotheses` | Enrichment |
| Mitigation | `action_attempts` (MITIGATION) or `solutions[].immediate_action` | Enrichment |
| Verification | `solutions[].verification_method` | Enrichment |
| Prevention | LLM-generated from context | Always available |
| Sources | Case ID reference | Always available |

**RunbookSuggestion** (`evaluate_runbook_suggestion(case, runbook_kb)`) — evaluated when user accepts the suggestion (not at suggestion time):

1. Content readiness (RunbookReadiness check — no I/O)
2. Deduplication (ChromaDB vector search via `runbook_kb` — ≥85% = existing covers, 70-84% = suggest with caveat)

If eligible, `ConversionService.convert_from_case()` runs as a fire-and-forget background task. The draft appears in Dashboard Knowledge > Drafts.

**ClosureReadiness** (`assess_closure_readiness(case)`) — investigation summary for the CLOSED confirmation prompt.

Constructed class (not an enum):

```python
# Constructor: ClosureReadiness(verdict, message)
class ClosureReadiness:
    # Verdict values:
    #   "has_substance"  — Investigation produced meaningful work → show summary
    #   "trivial"        — Minimal data → warn user before closing
    verdict: str
    message: str  # Human-facing summary text
```

Summarizes what was accomplished: evidence count, hypotheses explored, milestones completed, root cause (if identified), and solutions (if any). Used by both the dropdown and NLP CLOSED paths to show a meaningful confirmation prompt before the user commits to an irreversible closure. The actual CLOSURE_SUMMARY report is generated only after the user confirms.

### 1.6 ProblemVerification

```python
class ProblemVerification(BaseModel):
    """
    Consolidated problem verification data.
    Created when investigation starts (INQUIRY → INVESTIGATING).
    Combines symptom verification, timeline analysis, and affected components.
    """

    # ============================================================
    # Symptom
    # ============================================================
    symptom_statement: str = Field(
        description="User's description of the problem"
    )
    symptom_indicators: List[str] = Field(
        default_factory=list,
        description="Specific metrics/observations confirming symptom"
    )

    # ============================================================
    # Scope
    # ============================================================
    affected_services: List[str] = Field(
        default_factory=list,
        description="Services/components affected by problem"
    )
    affected_users: Optional[str] = Field(
        default=None,
        description="User impact: 'all', '10%', 'premium tier', etc."
    )
    affected_regions: List[str] = Field(
        default_factory=list,
        description="Geographic regions or data centers affected"
    )
    severity: str = Field(
        description="CRITICAL | HIGH | MEDIUM | LOW"
    )
    user_impact: Optional[str] = Field(
        default=None,
        description="Description of impact on users"
    )

    # ============================================================
    # Timeline
    # ============================================================
    started_at: Optional[datetime] = Field(
        default=None,
        description="When problem started (if known)"
    )
    noticed_at: Optional[datetime] = Field(
        default=None,
        description="When problem was first noticed"
    )
    resolved_naturally_at: Optional[datetime] = Field(
        default=None,
        description="If problem resolved on its own, when? (for historical problems)"
    )
    duration: Optional[timedelta] = Field(
        default=None,
        description="How long problem lasted (for historical problems)"
    )
    temporal_state: Optional[TemporalState] = Field(
        default=None,
        description="ONGOING | HISTORICAL (determined during verification)"
    )

    # ============================================================
    # Changes
    # ============================================================
    recent_changes: List[Change] = Field(
        default_factory=list,
        description="Recent changes that may be relevant"
    )
    correlations: List[Correlation] = Field(
        default_factory=list,
        description="Correlations between changes and symptom"
    )
    correlation_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in change-symptom correlation"
    )

    # ============================================================
    # Urgency Assessment
    # ============================================================
    urgency_level: UrgencyLevel = Field(
        default=UrgencyLevel.UNKNOWN,
        description="CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN"
    )
    urgency_factors: List[str] = Field(
        default_factory=list,
        description="Factors contributing to urgency assessment"
    )

    # ============================================================
    # Diagnostic Feasibility (Advisory)
    # ============================================================
    rca_infeasible: bool = Field(
        default=False,
        description=(
            "Advisory signal: root cause analysis is infeasible for this problem. "
            "Set by the LLM during verification when the problem involves "
            "uncontrollable external dependencies, deprecated/EOL systems, "
            "or known intractable conditions where mitigation is the accepted strategy. "
            "Does NOT affect path selection — influences post-mitigation agent behavior only."
        ),
    )
    rca_infeasible_rationale: Optional[str] = Field(
        default=None,
        description=(
            "Why RCA is infeasible. Populated by the LLM when rca_infeasible=True. "
            "Examples: 'Black-box 3rd-party API with no internal telemetry', "
            "'System scheduled for decommission — user declined RCA', "
            "'Known transient network jitter — retry loop is accepted strategy'."
        ),
        max_length=500,
    )

    # ============================================================
    # Metadata
    # ============================================================
    verified_at: Optional[datetime] = Field(
        default=None,
        description="When verification was completed"
    )
    verification_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in verification completeness"
    )
```

**Design Decision: `rca_infeasible` as Advisory Signal**

Root cause analysis is sometimes infeasible — uncontrollable external dependencies (black-box 3rd-party APIs), deprecated systems scheduled for decommission, or known intractable conditions where a retry loop is the accepted permanent strategy.

`rca_infeasible` is a **boolean + rationale** rather than a taxonomy enum (e.g., `UNCONTROLLABLE_EXTERNAL`, `DEPRECATED_LEGACY`). This avoids a growing taxonomy that requires prompt/test updates for each new category. The rationale string captures the "why" for context.

**What it does:** After a mitigation is verified, if `rca_infeasible=True` and the cause remains uncertain, the agent proposes closure instead of pushing further RCA. The agent says: *"The mitigation is verified. Since [rationale], shall we close this case?"*

**What it does NOT do:**

- Does not select an investigation path — there is no path fork (unified opportunistic flow)
- Does not force closure — user can still request RCA
- Does not skip hypothesis formulation — lightweight hypotheses still have diagnostic value
- Does not create a new terminal state — uses existing `CLOSED(closed_after_investigation)`

See [Investigation Lifecycle Logic §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert) for behavioral specification.

```python
class Change(BaseModel):
    """Recent change that may be relevant to the problem"""
    description: str = Field(
        description="Description of the change"
    )
    occurred_at: datetime = Field(
        description="When the change occurred"
    )
    change_type: str = Field(
        description="deployment | config | scaling | code | infrastructure | data | other"
    )
    change_id: Optional[str] = Field(
        default=None,
        description="Deployment ID, PR number, or change ticket"
    )
    changed_by: Optional[str] = Field(
        default=None,
        description="Who made the change (user, system, team)"
    )
    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional structured details (version numbers, config values, etc.)"
    )

class Correlation(BaseModel):
    """Correlation between a change and the symptom"""
    change_description: str = Field(
        description="Description of the change"
    )
    timing_description: str = Field(
        description="Temporal relationship: '2 minutes before', 'immediately after', etc."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this correlation"
    )
    correlation_type: str = Field(
        description="temporal | causal | coincidental"
    )

class UrgencyLevel(str, Enum):
    """Problem urgency level for routing decisions"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"
```

### 1.7 InquiryData

> **Illustrative subset** — the canonical `InquiryData` in `faultmaven/modules/case/domain/models.py` additionally exposes `knowledge_matches: List[KnowledgeMatch]`, `knowledge_resolution: Optional[KnowledgeResolution]`, and `preliminary_urgency: Optional[PreliminaryUrgency]` (the KB-resolution sub-model).

```python
class InquiryData(BaseModel):
    """
    Pre-investigation inquiry phase data.
    Captures early problem exploration before formal investigation commitment.
    """

    # NOTE: initial_description REMOVED (v2.0)
    # Reason: Violates LLM/System-only principle (accumulated raw user input)
    # Instead: System provides conversation history in prompt,
    #          LLM creates proposed_problem_statement directly

    problem_confirmation: Optional[ProblemConfirmation] = Field(
        default=None,
        description="Agent's initial understanding of the problem"
    )

    # ============================================================
    # Problem Statement Confirmation Workflow
    # ============================================================
    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="""
        Agent's formalized problem statement - ITERATIVE REFINEMENT pattern.

        UI Display:
        - When None: Display "To be defined" or blank (no problem detected yet)
        - When set: Display the statement text

        LLM creates and revises based on conversation until user confirms without reservation.
        Becomes immutable once problem_statement_confirmed = True.
        See investigation-lifecycle-logic.md §1.2 for the iterative refinement pattern.
        """,
        max_length=1000
    )

    problem_statement_confirmed: bool = Field(
        default=False,
        description="User confirmed the formalized problem statement"
    )

    problem_statement_confirmed_at: Optional[datetime] = Field(
        default=None,
        description="When user confirmed the problem statement"
    )

    # ============================================================
    # Investigation Decision
    # ============================================================
    decided_to_investigate: bool = Field(
        default=False,
        description="Whether user committed to formal investigation"
    )

    decision_made_at: Optional[datetime] = Field(
        default=None,
        description="When user decided to investigate (if decided)"
    )

    inquiry_turns: int = Field(
        default=0,
        description="Number of turns spent in inquiry phase"
    )

class ProblemConfirmation(BaseModel):
    """Agent's initial problem understanding during inquiry"""
    problem_type: str = Field(
        description="Category or type of problem"
    )
    severity_guess: str = Field(
        description=(
            "Initial severity assessment: critical | high | medium | low | unknown. "
            "NOTE: lowercase, 5 values. The downstream `ProblemVerification.severity` "
            "field currently rejects 'unknown' and the impedance mismatch causes a 500 "
            "on INQUIRY → INVESTIGATING transition when the LLM returns it."
        )
    )
    preliminary_guidance: str = Field(
        description="Initial guidance or suggestions provided"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
```

### 1.8 Solution

```python
class Solution(BaseModel):
    """
    Proposed or applied solution to the problem.
    May be mitigation (quick fix) or root cause fix (permanent solution).
    """

    solution_id: str = Field(default_factory=lambda: f"sol_{uuid4().hex[:12]}")

    # ============================================================
    # Solution Type
    # ============================================================
    solution_type: SolutionType

    # ============================================================
    # Solution Details
    # ============================================================
    title: str = Field(
        description="Short solution title"
    )

    immediate_action: Optional[str] = Field(
        default=None,
        description="Quick fix or mitigation (for MITIGATION path)"
    )

    longterm_fix: Optional[str] = Field(
        default=None,
        description="Permanent solution (the durable fix, as opposed to a mitigation workaround)"
    )

    # ============================================================
    # Implementation
    # ============================================================
    implementation_steps: List[str] = Field(
        default_factory=list,
        description="Step-by-step implementation instructions"
    )

    commands: List[str] = Field(
        default_factory=list,
        description="Commands to execute (if applicable)"
    )

    risks: List[str] = Field(
        default_factory=list,
        description="Risks or side effects of this solution"
    )

    # ============================================================
    # Lifecycle
    # ============================================================
    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    proposed_by: str = Field(
        default="agent",
        description="Who proposed: 'agent' or user_id"
    )

    applied_at: Optional[datetime] = Field(
        default=None,
        description="When solution was applied"
    )

    applied_by: Optional[str] = Field(
        default=None,
        description="Who applied the solution"
    )

    verified_at: Optional[datetime] = Field(
        default=None,
        description="When solution effectiveness was verified"
    )

    # Verification
    verification_method: Optional[str] = Field(
        default=None,
        description="How solution was verified: metrics | logs | manual_test | etc."
    )

    verification_evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID that verified solution effectiveness"
    )

    effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well solution worked (0.0-1.0)"
    )

class SolutionType(str, Enum):
    """Type of solution"""
    ROLLBACK = "rollback"              # Revert deployment or change
    CONFIG_CHANGE = "config_change"    # Update configuration
    RESTART = "restart"                # Restart service or component
    SCALING = "scaling"                # Scale resources up/down
    CODE_FIX = "code_fix"              # Fix code bug
    WORKAROUND = "workaround"          # Temporary workaround
    INFRASTRUCTURE = "infrastructure"  # Infrastructure change
    DATA_FIX = "data_fix"              # Fix data corruption or inconsistency
    OTHER = "other"                    # Other solution type
```

### 1.9 ProposedAction and ActionAttempt

```python
class InvestigationActionType(str, Enum):
    """Type of proposed action."""
    MITIGATION = "mitigation"   # Temp fix → compliance triggers DIAGNOSIS → MITIGATION
    SOLUTION = "solution"       # Permanent fix → compliance triggers DIAGNOSIS → TREATMENT
    DIAGNOSTIC = "diagnostic"   # Data collection → no stage transition on compliance

class ProposedAction(BaseModel):
    """
    A concrete action proposed by the agent for the user to execute.

    ProposedActions are created when the agent proposes a solution (via SolutionToAdd).
    User compliance with a proposed action triggers gate milestone
    transitions via compliance detection. The user's action IS acceptance —
    no explicit confirmation step required.
    """
    action_id: str = Field(default_factory=lambda: f"act_{uuid4().hex[:12]}")
    case_id: str = Field(description="Case this action belongs to")
    action_type: InvestigationActionType
    description: str = Field(description="Human-readable description of the proposed action", max_length=2000)
    commands: List[str] = Field(default_factory=list, description="Specific commands for the user to execute")
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    proposed_in_turn: int = Field(description="Turn number when this action was proposed")
    state: str = Field(default="pending", description="pending | accepted | rejected | superseded")
    superseded_in_turn: Optional[int]  # turn the engine superseded this action (INV-32)
    superseded_reason: Optional[str]   # 'reproposal' | 'license_lost' (forensic; only pending actions render)

class ActionAttempt(BaseModel):
    """
    Records a user's attempt to execute a ProposedAction.

    When the user submits results after executing (or attempting to execute)
    a proposed action, an ActionAttempt is created. Compliance detection
    analyzes the attempt to determine if gate milestones should be set.

    The compliance flags on InvestigationProgress represent the current cycle;
    the action_attempts list provides history. The mitigation gate is a
    single forward-only `MitigationRecord` (accepted/verified are never
    reset); the completed mitigation attempt remains in the list.
    """
    attempt_id: str = Field(default_factory=lambda: f"att_{uuid4().hex[:12]}")
    action_id: str = Field(description="ProposedAction this attempt relates to")
    user_message: str = Field(description="The user's message containing attempt results", max_length=10000)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    compliance_detected: bool = Field(default=False, description="Whether user appears to have executed the proposed action")
    compliance_confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence that user complied")
```

### 1.10 WorkingConclusion

```python
class WorkingConclusion(BaseModel):
    """
    Agent's current best understanding of the problem.
    Updated iteratively as investigation progresses.
    Less authoritative than RootCauseConclusion.
    """

    statement: str = Field(
        description="Current conclusion statement"
    )

    likelihood: float = Field(
        ge=0.0,
        le=1.0,
        description="Likelihood of this conclusion (0.0-1.0)"
    )

    reasoning: str = Field(
        description="Why agent believes this conclusion"
    )

    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description="Evidence IDs supporting this conclusion"
    )

    caveats: List[str] = Field(
        default_factory=list,
        description="Limitations or uncertainties"
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    supersedes_conclusion_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of previous conclusion this replaces"
    )
```

### 1.11 RootCauseConclusion

```python
class RootCauseConclusion(BaseModel):
    """
    Final determination of root cause.
    More authoritative than WorkingConclusion.
    Created when cause_state becomes IDENTIFIED.
    """

    root_cause: str = Field(
        description="Definitive statement of root cause"
    )

    likelihood: float = Field(
        ge=0.0,
        le=1.0,
        description="Numeric confidence score (0.0-1.0)"
    )

    confidence_level: ConfidenceLevel = Field(
        description=(
            "Categorical confidence (stored field, not computed). "
            "Cross-validated against `likelihood` by a model_validator: callers must "
            "supply both, and the validator rejects mismatched pairs. Use "
            "ConfidenceLevel.from_score(likelihood) when constructing if you want "
            "the canonical mapping."
        )
    )

    mechanism: str = Field(
        description="How this root cause produced the symptom"
    )

    evidence_basis: List[str] = Field(
        default_factory=list,
        description="Evidence IDs supporting this conclusion"
    )

    validated_hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID validated (if identified via hypothesis testing)"
    )

    contributing_factors: List[str] = Field(
        default_factory=list,
        description="Secondary factors that made problem worse or enabled it"
    )

    determined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    determined_by: str = Field(
        default="agent",
        description="Who determined: 'agent' or user_id"
    )

class ConfidenceLevel(str, Enum):
    """Confidence level in root cause determination"""
    SPECULATION = "speculation"     # < 0.5 confidence
    PROBABLE = "probable"           # 0.5-0.69 confidence
    CONFIDENT = "confident"         # 0.7-0.89 confidence
    VERIFIED = "verified"           # ≥ 0.9 confidence

    @staticmethod
    def from_score(score: float) -> 'ConfidenceLevel':
        """Convert numeric score to categorical level"""
        if score < 0.5:
            return ConfidenceLevel.SPECULATION
        elif score < 0.7:
            return ConfidenceLevel.PROBABLE
        elif score < 0.9:
            return ConfidenceLevel.CONFIDENT
        else:
            return ConfidenceLevel.VERIFIED
```

### 1.12 JournalEntry (Investigation Journal)

```python
class JournalEntry(BaseModel):
    """A single append-only entry in the investigation journal.

    Captures a distilled insight, decision, or context the agent needs
    to remember across the entire investigation. Always included in the
    LLM context.
    """

    turn: int
    entry_type: Literal[
        "finding", "decision", "user_context",
        "ruled_out", "blocker", "milestone"
    ]
    content: str  # max 200 chars
    evidence_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
```

Stored on `Case.investigation_journal` (append-only list) and persisted in the metadata JSONB blob. Always included in full in the LLM context as an `<investigation_journal>` XML block.

See **[Investigation Journal](./investigation-journal.md)** for the canonical design: entry-type taxonomy with examples, creation rules, LLM-context injection format, and the Phase 2/3 implementation plan.

### 1.13 EscalationState

```python
class EscalationState(BaseModel):
    """
    Investigation escalated to human expert.
    Tracks escalation lifecycle and resolution.
    """

    escalation_type: EscalationType

    reason: str = Field(
        description="Why escalation was needed"
    )

    escalated_to: Optional[str] = Field(
        default=None,
        description="Team or person escalated to"
    )

    escalated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ============================================================
    # Context Transfer
    # ============================================================
    context_summary: str = Field(
        description="Summary of investigation so far for escalation recipient"
    )

    key_findings: List[str] = Field(
        default_factory=list,
        description="Key findings to communicate to expert"
    )

    # ============================================================
    # Resolution
    # ============================================================
    resolution: Optional[str] = Field(
        default=None,
        description="How escalation was resolved"
    )

    resolved_at: Optional[datetime] = Field(
        default=None,
        description="When escalation was resolved"
    )

    @property
    def is_active(self) -> bool:
        """Check if escalation is still active"""
        return self.resolved_at is None

class EscalationType(str, Enum):
    """Type of escalation"""
    EXPERTISE_REQUIRED = "expertise_required"      # Need domain expert
    PERMISSIONS_REQUIRED = "permissions_required"  # Need elevated permissions
    NO_PROGRESS = "no_progress"                    # Stuck, need help
    USER_REQUEST = "user_request"                  # User explicitly escalated
    CRITICAL_SEVERITY = "critical_severity"        # Too critical for agent-only
    OTHER = "other"                                # Other escalation reason
```

### 1.14 DocumentationData

```python
class DocumentationData(BaseModel):
    """
    Documentation generated when case closes.
    Captures lessons learned and knowledge for future reference.
    """

    documents_generated: List[GeneratedDocument] = Field(
        default_factory=list,
        description="Documents created from this case"
    )

    runbook_entry: Optional[str] = Field(
        default=None,
        description="Runbook entry created from this case"
    )

    lessons_learned: List[str] = Field(
        default_factory=list,
        description="Key takeaways from investigation"
    )

    what_went_well: List[str] = Field(
        default_factory=list,
        description="Positive aspects of investigation"
    )

    what_could_improve: List[str] = Field(
        default_factory=list,
        description="Areas for improvement"
    )

    preventive_measures: List[str] = Field(
        default_factory=list,
        description="How to prevent recurrence"
    )

    monitoring_recommendations: List[str] = Field(
        default_factory=list,
        description="Monitoring/alerts to add"
    )

    generated_at: Optional[datetime] = Field(
        default=None,
        description="When documentation was generated"
    )

    generated_by: str = Field(
        default="agent",
        description="Who generated: 'agent' or user_id"
    )

class GeneratedDocument(BaseModel):
    """A generated document from the case"""
    document_id: str = Field(
        default_factory=lambda: f"doc_{uuid4().hex[:12]}",
        description="Unique document identifier"
    )
    document_type: DocumentType
    title: str = Field(description="Document title")
    content_ref: str = Field(
        description="Reference to document content (S3 URI, file path, etc.)"
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    format: str = Field(
        default="markdown",
        description="Document format: markdown | pdf | html | json | other"
    )
    size_bytes: Optional[int] = Field(
        default=None,
        description="Document size in bytes"
    )

class DocumentType(str, Enum):
    """Type of generated document."""
    RUNBOOK = "runbook"
    CHAT_SUMMARY = "chat_summary"
    TIMELINE = "timeline"
    EVIDENCE_BUNDLE = "evidence_bundle"
    OTHER = "other"
```

---

## 2. Evidence Model

> **Evidence model — canonical references**
>
> - **Data model:** [evidence-driven-investigation-framework.md §5](./evidence-driven-investigation-framework.md#5-evidence-model)
> - **DB schema:** [data-and-storage/schemas/case-schema.md](../data-and-storage/schemas/case-schema.md) §"Evidence"
> - **Preprocessing pipeline:** [data-processing/data-preprocessing-design-specification.md](../data-processing/data-preprocessing-design-specification.md)
> - **Pipeline flows + sequence diagrams:** [data-processing/evidence-flow-architecture.md](../data-processing/evidence-flow-architecture.md)
>
> Categories: `symptom_evidence`, `causal_evidence`, `symptom_absence_evidence`, `causal_absence_evidence`. Source is expressed by `Evidence.source_type` + `Evidence.source_file_id`; the `evidence_source_invariant` DB CHECK requires `source_file_id IS NOT NULL OR source_type = 'user_description'`.

### 2.1 Evidence Fields Used by Investigation Engine

The investigation engine primarily interacts with these `Evidence` fields:

```python
# Evidence fields the investigation engine reads/writes directly
evidence_id: str                 # Reference in LLM responses
category: EvidenceCategory       # Determines milestone advancement
summary: str                     # Brief description (<500 chars, NOT NULL)
extract: Optional[str]           # Verbatim quote that grounds the summary
analysis: Optional[str]          # LLM-written interpretation of the evidence
primary_purpose: Optional[str]   # What this evidence is intended to establish

# Source identification
source_type: EvidenceSourceType  # logs | metrics | configuration | code | text |
                                 # image | user_description
source_file_id: Optional[str]    # FK → UploadedFile (NULL only when
                                 # source_type='user_description')

# Hypothesis evaluation lives on HypothesisEvidenceLink, not on Evidence.
# See the table immediately below for the full move map.

# Milestone tracking
advances_milestones: List[str]   # System-inferred from category
collected_at_turn: int           # Turn this evidence was added
collected_at: datetime
collected_by: Optional[str]
is_primary: bool                 # Marked as a primary piece of evidence

# Processing metadata
processing_mode: Optional[str]
reliability_score: Optional[float]
vectorized: bool                 # Whether the source file has been vectorized
                                 # (gated by Evidence.source_file_id → UploadedFile)
```

**Fields previously listed here that live on other models:**

| Field | Where it actually lives | Why |
| ----- | ----------------------- | --- |
| `data_type` | `UploadedFile.data_type` | File-level classification produced by preprocessing; Evidence rows reference it via `source_file_id → UploadedFile`. |
| `original_filename` | `UploadedFile.filename` | Same: file-level attribute, accessed via the FK. |
| `content_ref` | `UploadedFile.storage_ref` | Storage pointer is on the file, not the per-extract Evidence row. |
| `tests_hypothesis_id` | `HypothesisEvidenceLink.hypothesis_id` | Hypothesis–evidence is many-to-many. The junction row carries the link. |
| `stance` | `HypothesisEvidenceLink.stance` | One Evidence can support hypothesis A and refute hypothesis B; stance is per-link, not per-evidence. |
| `stance_confidence` | `HypothesisEvidenceLink.stance_confidence` | Same: per-link. |

This split was finalized by [migration 010 (strict evidence-model redesign)](../data-and-storage/schemas/case-schema.md). Pipeline tools that need any of the above fields read them through `source_file_id` (for file-level data) or through `hypothesis.evidence_links` (for hypothesis-side data).

### 2.2 Evidence Stance

```python
class EvidenceStance(str, Enum):
    """
    Simplified 3-state stance for LLM consistency.

    Use stance_confidence (0.0-1.0) for granularity instead of
    STRONGLY_SUPPORTS vs SUPPORTS distinctions, which LLMs score inconsistently.

    NEUTRAL stance handling:
    NEUTRAL evidence links are stored for audit trail completeness but do NOT
    affect hypothesis confidence calculations. Only SUPPORTS and REFUTES stances
    modify the hypothesis likelihood score.
    """
    SUPPORTS = "supports"
    """Evidence supports the hypothesis. Increases likelihood by +0.15."""

    REFUTES = "refutes"
    """Evidence contradicts the hypothesis. Decreases likelihood by -0.20."""

    NEUTRAL = "neutral"
    """Evidence neither supports nor refutes. Stored for audit trail, no confidence effect."""
```

---

## 3. Hypothesis Workflow

### 3.1 Hypothesis Schema

```python
class Hypothesis(BaseModel):
    """Hypothesis for systematic root cause exploration"""

    hypothesis_id: str = Field(default_factory=lambda: f"hyp_{uuid4().hex[:12]}")
    statement: str
    category: HypothesisCategory
    state: HypothesisState
    likelihood: float = Field(ge=0.0, le=1.0)

    # Evidence relationships (many-to-many via HypothesisEvidenceLink)
    # Each list entry is a row from the hypothesis_evidence junction table
    # binding (hypothesis_id, evidence_id, stance, stance_confidence, reasoning).
    evidence_links: List[HypothesisEvidenceLink] = Field(default_factory=list)

    # Computed properties (filter evidence_links by stance)
    @property
    def supporting_evidence(self) -> List[str]:
        return [link.evidence_id for link in self.evidence_links
                if link.stance == EvidenceStance.SUPPORTS]

    @property
    def refuting_evidence(self) -> List[str]:
        return [link.evidence_id for link in self.evidence_links
                if link.stance == EvidenceStance.REFUTES]

    # Metadata
    generated_at_turn: int
    generation_mode: HypothesisGenerationMode
    rationale: str

    # Testing history
    tested_at: Optional[datetime] = None
    concluded_at: Optional[datetime] = None

class HypothesisCategory(str, Enum):
    """Hypothesis categories for anchoring detection"""
    CODE = "code"
    CONFIG = "config"
    ENVIRONMENT = "environment"
    NETWORK = "network"
    DATA = "data"  # Data quality, corruption, consistency
    DATABASE = "database"  # Database performance, queries, indexes
    HARDWARE = "hardware"
    SECURITY = "security"  # Security issues, authentication/authorization failures
    EXTERNAL = "external"
    HUMAN = "human"
    OTHER = "other"  # Doesn't fit above categories

class HypothesisState(str, Enum):
    CAPTURED = "captured"       # Initial state, just recorded
    ACTIVE = "active"           # Under active investigation
    VALIDATED = "validated"     # likelihood ≥ 0.70 + 2+ supporting evidence
    REFUTED = "refuted"         # likelihood ≤ 0.20 + 2+ refuting evidence
    INCONCLUSIVE = "inconclusive"  # likelihood 0.3–0.5 + stagnant 3+ turns (no evidence change)
    RETIRED = "retired"         # Abandoned without disproof — set by LLM, system thresholds, or user intent (see transition table)
```

**Hypothesis state transitions — LLM-driven, system-automated, and user-intent paths:**

`ACTIVE → RETIRED` is the one transition with multiple legitimate paths. The LLM picks RETIRED for "no disproof" cases (`HypothesisUpdate` schema accepts this — see the schema's `refutation_reason` docstring). The system auto-retires on low confidence, anchoring drift, and deadlock repair. The engine sets RETIRED when the user explicitly clicks a retire DECIDE suggestion. All five paths converge on the same final state; only the rationale and the actor differ.

| Status | Trigger | Who sets it |
|---|---|---|
| `CAPTURED` → `ACTIVE` | LLM starts investigating the hypothesis | LLM (structured output) |
| `ACTIVE` → `VALIDATED` | likelihood ≥ 0.70 with 2+ supporting evidence links | LLM (structured output) |
| `ACTIVE` → `VALIDATED` | User clicks a "validate" DECIDE suggestion (`hypothesis_action` intent) | **Engine** (`milestone_engine.py` `hypothesis_action` handler — sets `likelihood = 1.0`) |
| `ACTIVE` → `REFUTED` | likelihood ≤ 0.20 with 2+ refuting evidence links | LLM (structured output) |
| `ACTIVE` → `REFUTED` | User clicks a "refute" DECIDE suggestion (`hypothesis_action` intent) | **Engine** (delegates to `hypothesis_manager.refute_hypothesis(...)` with `reason=user_message`) |
| `ACTIVE` → `INCONCLUSIVE` | likelihood 0.3–0.5 **and** `iterations_without_progress ≥ 3` | **System-automated** (`hypothesis_manager.py`) |
| `ACTIVE` → `RETIRED` | LLM judgment: abandoning without disproof (no `refutation_reason` required) | LLM (structured output via `HypothesisUpdate.status = RETIRED`) |
| `ACTIVE` → `RETIRED` | likelihood < 0.30 (low-confidence decay) | **System-automated** (`hypothesis_manager.py:396`) |
| `ACTIVE` → `RETIRED` | Anchoring prevention — too many hypotheses in one category, low progress | **System-automated** (`hypothesis_manager.py:584`, retires the lowest-progress members of the over-represented category) |
| `INCONCLUSIVE` → `RETIRED` | Deadlock repair — all hypotheses inconclusive, repair retires them en masse to free the LLM to generate fresh ones | **System-automated** (`progress_monitor.py:691`) |
| `ACTIVE` → `RETIRED` | User clicks a "retire" DECIDE suggestion (`hypothesis_action` intent) | **Engine** (sets `retirement_reason = user_message`) |

The system-automated `INCONCLUSIVE` and decay-based `RETIRED` transitions run after each evidence update in `HypothesisManager.update_hypothesis()`. They are mechanically applied thresholds, not LLM judgment calls — they prevent stale low-confidence hypotheses from consuming LLM context across turns when no new evidence is advancing them. The user-intent paths (validate / refute / retire) apply the state change **before** LLM processing on the same turn so the agent's reply sees the updated hypothesis and can acknowledge it — see [Choice-Response Resolution §5.2](./choice-response-resolution.md#52-state-transitions-applied-by-the-engine).

```python
class HypothesisGenerationMode(str, Enum):
    OPPORTUNISTIC = "opportunistic"
    SYSTEMATIC = "systematic"
    FORCED_ALTERNATIVE = "forced_alternative"

class EvidenceRequirement(BaseModel):
    """
    Evidence needed to test a hypothesis.
    Part of hypothesis definition.
    Agent uses this to request specific diagnostic data.
    """

    description: str = Field(
        description="What evidence is needed"
    )

    evidence_type: str = Field(
        description="log_file | metrics | config | code | trace | etc."
    )

    acquisition_guidance: Optional[str] = Field(
        default=None,
        description="How to collect this evidence (commands, tools, etc.)"
    )

    criticality: str = Field(
        default="required",
        description="required | preferred | optional"
    )
```

### 3.2 Anchoring Detection

**Level 1: Hypothesis Category Anchoring** — detect when the agent is stuck testing the same hypothesis category (4+ REFUTED or INCONCLUSIVE hypotheses in the same `category`). The threshold (`category_anchoring_threshold=4`), repair action (ban the anchored category), and detection-flow placement within `ProgressMonitor.check_progress()` are canonical in:

See **[Progress Transparency — Agent State Repair](./progress-transparency.md#agent-state-repair-exception-handling)** (HYPOTHESIS_ANCHORING row + threshold + repair-flow diagram).

**Level 2: Evidence Purpose Anchoring**

```python
def detect_evidence_anchoring(case: Case) -> Optional[str]:
    """Detect if agent stuck requesting same evidence category"""

    recent_turns = case.turn_history[-4:]
    recent_evidence = []
    for turn in recent_turns:
        recent_evidence.extend(turn.evidence_added)

    if len(recent_evidence) >= 4:
        categories = [e.category for e in case.evidence if e.evidence_id in recent_evidence]
        if len(set(categories)) == 1:
            cat = categories[0]
            if cat == EvidenceCategory.SYMPTOM_EVIDENCE:
                return "Requested symptom verification 4 times. Move to root cause investigation."

    return None
```

---
