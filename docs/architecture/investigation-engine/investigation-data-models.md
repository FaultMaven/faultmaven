# Investigation Data Models

This document defines the core data models used in FaultMaven's opportunistic investigation framework.

**Related Documents**:
- [Opportunistic Investigation Framework](./opportunistic-investigation-framework.md) - Overview and philosophy
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) - State transitions and path routing

---

## Table of Contents

1. [Field Naming Conventions](#field-naming-conventions)
2. [Core Data Models](#1-core-data-models)
3. [Evidence Model](#2-evidence-model)
4. [Hypothesis Workflow](#3-hypothesis-workflow)
5. [Degraded Mode](#4-degraded-mode)
6. [Checkpointing Model](#5-checkpointing-model)

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

### 1.1 CaseStatus

```python
class CaseStatus(str, Enum):
    """
    Case lifecycle status (4 states).
    Two terminal states: RESOLVED (with solution) and CLOSED (without solution).
    """

    INQUIRY = "inquiry"
    """
    Pre-investigation exploration.
    User asking questions, agent providing quick guidance.
    No formal investigation commitment yet.
    """

    INVESTIGATING = "investigating"
    """
    Active formal investigation.
    Working through verification, diagnosis, and resolution.
    Problem not yet fixed.
    """

    RESOLVED = "resolved"
    """
    TERMINAL STATE: Case closed WITH solution.
    Problem was fixed and verified.

    closure_reason = "resolved"
    """

    CLOSED = "closed"
    """
    TERMINAL STATE: Case closed WITHOUT solution.
    Investigation abandoned, escalated, or inquiry-only.

    closure_reason = "abandoned" | "escalated" | "inquiry_only" | "duplicate" | "other"
    """
```

**Key Points**:
- **RESOLVED** and **CLOSED** are both terminal (no further state)
- **RESOLVED** = Problem fixed (has solution)
- **CLOSED** = Problem not fixed (no solution, or inquiry-only)
- Agent doesn't care about cases after they reach terminal state

### 1.2 InvestigationProgress

```python
class InvestigationProgress(BaseModel):
    """
    Milestone-based progress tracking.
    Tracks what's been completed, not what phase we're in.
    """

    # ============================================================
    # Verification Milestones
    # ============================================================
    symptom_verified: bool = Field(
        default=False,
        description="Symptom confirmed with evidence"
    )

    scope_assessed: bool = Field(
        default=False,
        description="Scope determined (affected users, services, regions)"
    )

    timeline_established: bool = Field(
        default=False,
        description="Timeline determined (when started, when noticed)"
    )

    changes_identified: bool = Field(
        default=False,
        description="Recent changes identified"
    )

    # ============================================================
    # Investigation Milestones
    # ============================================================
    root_cause_identified: bool = Field(
        default=False,
        description="Root cause determined"
    )

    root_cause_likelihood: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Likelihood of root cause identification (0.0-1.0)"
    )

    root_cause_method: Optional[str] = Field(
        default=None,
        description="direct_analysis | hypothesis_validation | correlation | other"
    )

    # ============================================================
    # Resolution Milestones
    # ============================================================
    solution_proposed: bool = Field(
        default=False,
        description="Solution or mitigation proposed"
    )

    solution_applied: bool = Field(
        default=False,
        description="Solution has been applied"
    )

    solution_verified: bool = Field(
        default=False,
        description=(
            "Solution effectiveness verified via User-Agent Handshake. "
            "NOT directly settable by LLM — requires explicit user confirmation. "
            "Set by confirm_pending_transition() after user approves resolution."
        )
    )

    # ============================================================
    # Mitigation Tracking (Available During Any Stage)
    # ============================================================
    mitigation_applied: bool = Field(
        default=False,
        description="""
        Quick mitigation applied to stop immediate impact.

        Mitigation is a TOOL available during diagnosis, not a stage jump.
        Both paths follow linear progression: 1 → 2 → 3 → 4

        MITIGATION_FIRST path:
        - Mitigation applied opportunistically during stages 1-2 when
          correlation is strong enough (e.g., recent deployment + error timing)
        - Investigation continues to root cause for permanent fix

        ROOT_CAUSE path:
        - Mitigation typically not applied until root cause confirmed
        - May still apply mitigation if situation becomes urgent

        Note: Different from solution_applied - mitigation is quick correlation-based fix,
        solution is comprehensive permanent fix after RCA.
        """
    )

    mitigation_verified: bool = Field(
        default=False,
        description="Mitigation effectiveness confirmed (problem stopped)"
    )

    mitigation_effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well mitigation worked: 1.0 = fully resolved, 0.5 = partially, 0.0 = ineffective"
    )

    mitigation_solution_id: Optional[str] = Field(
        default=None,
        description="Solution ID of applied mitigation (links to case.solutions)"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> InvestigationStage:
        """
        Compute investigation stage from milestones.
        For optional progress detail, not workflow control.

        Returns one of the 4 InvestigationStage enum values:
        - SYMPTOM_VERIFICATION: Initial verification (where/when)
        - HYPOTHESIS_FORMULATION: Generating theories (why)
        - HYPOTHESIS_VALIDATION: Testing theories (why really)
        - SOLUTION: Applying fix (how)
        """
        # Stage 4: Solution phase
        if (self.solution_proposed or
            self.solution_applied or
            self.solution_verified):
            return InvestigationStage.SOLUTION

        # Stage 3: Hypothesis validation (root cause being identified)
        if self.root_cause_identified:
            return InvestigationStage.HYPOTHESIS_VALIDATION

        # Stage 2: Hypothesis formulation (symptom verified, exploring cause)
        if self.symptom_verified:
            return InvestigationStage.HYPOTHESIS_FORMULATION

        # Stage 1: Symptom verification (initial state)
        return InvestigationStage.SYMPTOM_VERIFICATION

    @property
    def stage_display_name(self) -> str:
        """
        User-facing stage name for UI display.

        Maps internal 4-stage system to 3 user-friendly names:
        - SYMPTOM_VERIFICATION → "Understanding"
        - HYPOTHESIS_FORMULATION, HYPOTHESIS_VALIDATION → "Diagnosing"
        - SOLUTION → "Resolving"
        """
        stage = self.current_stage
        if stage == InvestigationStage.SYMPTOM_VERIFICATION:
            return "Understanding"
        elif stage in (InvestigationStage.HYPOTHESIS_FORMULATION,
                      InvestigationStage.HYPOTHESIS_VALIDATION):
            return "Diagnosing"
        else:  # SOLUTION
            return "Resolving"

    @property
    def verification_complete(self) -> bool:
        """All verification milestones completed"""
        return (
            self.symptom_verified and
            self.scope_assessed and
            self.timeline_established and
            self.changes_identified
        )

    # completion_percentage removed — inaccurate and non-essential.
    # Milestone completion tracked via completed_milestones/pending_milestones.

class InvestigationStage(str, Enum):
    """
    Investigation stage within INVESTIGATING phase (4 stages).
    Computed from milestones for optional progress detail.

    Stage Progression (Linear for Both Paths):
    1 → 2 → 3 → 4 (Verify → Hypothesize → Validate → Resolve)

    Path Differences:
    - MITIGATION_FIRST: Mitigation available as tool during stages 1-2
    - ROOT_CAUSE: Full RCA before any fix applied
    """
    SYMPTOM_VERIFICATION = "symptom_verification"
    """Stage 1: Symptom verification (where and when)"""

    HYPOTHESIS_FORMULATION = "hypothesis_formulation"
    """Stage 2: Hypotheses formulation (why)"""

    HYPOTHESIS_VALIDATION = "hypothesis_validation"
    """Stage 3: Hypothesis validation (why really)"""

    SOLUTION = "solution"
    """Stage 4: Solution (how)"""

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
    actions_taken: List[str] = Field(default_factory=list)

    # Outcome
    outcome: TurnOutcome

    # User interaction
    user_message_summary: Optional[str] = None
    agent_response_summary: Optional[str] = None

class TurnOutcome(str, Enum):
    """
    Turn outcome classification.

    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress, degraded_mode).
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
    # Degraded mode triggers at 3 turns without progress (system-managed)
```

### 1.4 InvestigationPath

```python
class InvestigationPath(str, Enum):
    """
    Investigation routing based on temporal state and urgency.

    Both paths follow LINEAR stage progression: 1 → 2 → 3 → 4
    (Verify → Hypothesize → Validate → Resolve)

    The difference is WHEN mitigation is applied:
    - MITIGATION_FIRST: Mitigation available as tool during stages 1-2
    - ROOT_CAUSE: Full RCA before any fix applied
    """
    MITIGATION_FIRST = "mitigation_first"
    """
    Mitigation-first path.

    Stage Flow: 1 → 2 → 3 → 4 (linear, same as ROOT_CAUSE)

    Key Difference: Mitigation is available as a TOOL during early stages.
    - Stage 1: Verify symptom + apply mitigation if correlation strong
    - Stage 2: Continue formulating hypotheses (service now stable)
    - Stage 3: Validate hypothesis for root cause
    - Stage 4: Apply permanent solution

    Use When: ONGOING + HIGH/CRITICAL urgency
    Benefit: Stops bleeding quickly while still pursuing full RCA
    """

    ROOT_CAUSE = "root_cause"
    """
    Traditional RCA path.

    Stage Flow: 1 → 2 → 3 → 4
    - Stage 1: Verify symptom (where/when)
    - Stage 2: Formulate hypotheses (why)
    - Stage 3: Validate hypothesis (why really)
    - Stage 4: Apply solution (how)

    Use When: HISTORICAL + LOW/MEDIUM urgency
    Benefit: Thorough investigation without pressure
    """

    USER_CHOICE = "user_choice"
    """Ambiguous case - let user decide between paths"""

class PathSelection(BaseModel):
    """
    Path selection details.

    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state × urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during verification.
    System calls determine_investigation_path() to select path.
    LLM does NOT choose the path directly!
    """

    path: InvestigationPath = Field(
        description="Selected path (system-determined from matrix)"
    )

    auto_selected: bool = Field(
        description="True if system auto-selected based on matrix"
    )

    rationale: str = Field(
        description="Why this path was selected (system-generated)"
    )

    alternate_path: Optional[InvestigationPath] = Field(
        default=None,
        description="Alternative path user could choose (if applicable)"
    )

    selected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    selected_by: str = Field(default="system")

    # Decision inputs (from LLM during verification)
    temporal_state: Optional[TemporalState] = None
    urgency_level: Optional[UrgencyLevel] = None
```

### 1.5 Complete Case Model

```python
class Case(BaseModel):
    """Complete case model with opportunistic investigation architecture"""

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
    status: CaseStatus = Field(default=CaseStatus.INQUIRY)

    status_history: List[CaseStatusTransition] = Field(
        default_factory=list,
        description="Audit trail of status changes"
    )

    closure_reason: Optional[str] = Field(
        default=None,
        description="resolved | abandoned | escalated | inquiry_only | duplicate | other"
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
    # Investigation Path
    # ============================================================
    path_selection: Optional[PathSelection] = None

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

    # ============================================================
    # Cross-Cutting State
    # ============================================================
    working_conclusion: Optional[WorkingConclusion] = None
    root_cause_conclusion: Optional[RootCauseConclusion] = None

    # ============================================================
    # Special States
    # ============================================================
    degraded_mode: Optional[DegradedMode] = None
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
        description="When case reached terminal state (RESOLVED or CLOSED)"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """Investigation stage (only when INVESTIGATING)"""
        if self.status != CaseStatus.INVESTIGATING:
            return None
        return self.progress.current_stage

    @property
    def is_stuck(self) -> bool:
        """Detect if investigation is blocked"""
        return self.turns_without_progress >= 3

    @property
    def is_terminal(self) -> bool:
        """Check if case is in terminal state"""
        return self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        """Time from creation to terminal state"""
        if self.closed_at:
            return self.closed_at - self.created_at
        return None

class CaseStatusTransition(BaseModel):
    """Record of status change"""
    from_status: CaseStatus
    to_status: CaseStatus
    triggered_at: datetime
    triggered_by: str
    reason: str
```

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
        See USER-CONFIRMATION-DESIGN-PRINCIPLE.md for full pattern documentation.
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
    quick_suggestions: List[str] = Field(
        default_factory=list,
        description="Quick fixes or guidance provided during inquiry"
    )

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
        description="Initial severity assessment: CRITICAL | HIGH | MEDIUM | LOW"
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
        description="Permanent solution (for ROOT_CAUSE path)"
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

### 1.9 WorkingConclusion

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

### 1.10 RootCauseConclusion

```python
class RootCauseConclusion(BaseModel):
    """
    Final determination of root cause.
    More authoritative than WorkingConclusion.
    Created when root_cause_identified milestone completes.
    """

    root_cause: str = Field(
        description="Definitive statement of root cause"
    )

    likelihood: float = Field(
        ge=0.0,
        le=1.0,
        description="Numeric confidence score (0.0-1.0)"
    )

    @property
    def confidence_level(self) -> ConfidenceLevel:
        """Categorical confidence level derived from likelihood."""
        return ConfidenceLevel.from_score(self.likelihood)

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

### 1.11 EscalationState

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

### 1.12 DocumentationData

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

    post_mortem_id: Optional[str] = Field(
        default=None,
        description="Link to post-mortem document if created"
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
    """Type of generated document"""
    INCIDENT_REPORT = "incident_report"
    POST_MORTEM = "post_mortem"
    RUNBOOK = "runbook"
    CHAT_SUMMARY = "chat_summary"
    TIMELINE = "timeline"
    EVIDENCE_BUNDLE = "evidence_bundle"
    OTHER = "other"
```

---

## 2. Evidence Model

### 2.1 Purpose-Driven Categories

```python
class EvidenceCategory(str, Enum):
    """Evidence classification by investigation purpose"""

    UNCLASSIFIED = "unclassified"
    """
    Raw user-submitted data awaiting LLM classification.
    NOT evidence yet — stored with ID so LLM can reference it.
    Does NOT advance any milestones.
    """

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Validates: Symptom, scope, timeline, changes
    Advances: symptom_verified, scope_assessed, timeline_established, changes_identified
    """

    CAUSAL_EVIDENCE = "causal_evidence"
    """
    Validates: Root cause hypothesis
    Advances: root_cause_identified
    """

    RESOLUTION_EVIDENCE = "resolution_evidence"
    """
    Validates: Solution effectiveness
    Supports: User-Agent Handshake for solution_verified
    Note: Does NOT directly advance solution_verified.
    The agent uses this evidence to propose resolution via ProposedTransition.
    """

    OTHER = "other"
    """
    Evidence that doesn't fit above categories.
    May be useful but doesn't directly advance standard milestones.
    """
```

### 2.2 Evidence Schema

```python
class Evidence(BaseModel):
    """
    Evidence with purpose-driven categorization.

    Category Determination (with LLM override):
    1. LLM may suggest category_override based on contextual analysis
    2. System applies default rules if no override provided
    3. LLM override is respected when provided (LLM has context awareness)

    Default system rules (when no override):
    - Which milestones are incomplete (if symptom not verified → SYMPTOM_EVIDENCE)
    - Hypothesis linkage (if tests_hypothesis_id set → CAUSAL_EVIDENCE)
    - Solution state (if solution proposed → RESOLUTION_EVIDENCE)

    LLM provides: summary, analysis, tests_hypothesis_id, stance, stance_confidence, category_override
    System infers: category (respecting override), advances_milestones
    """

    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:12]}")

    # ============================================================
    # Purpose Classification
    # ============================================================
    category: EvidenceCategory = Field(
        description="Final category (system-inferred or LLM-overridden)"
    )

    category_override: Optional[EvidenceCategory] = Field(
        default=None,
        description="""
        LLM-suggested category override.

        Use when LLM's contextual analysis identifies that evidence serves
        a different purpose than system rules would infer. For example:
        - Error log that CONTAINS causal config values → CAUSAL_EVIDENCE
        - Metrics that SHOW symptom but REVEAL cause → CAUSAL_EVIDENCE

        When set, system respects this override over default rules.
        """
    )

    primary_purpose: str = Field(
        description="What this evidence validates (milestone or hypothesis)"
    )

    # Content
    summary: str = Field(max_length=500)
    content_ref: str
    analysis: Optional[str] = None

    # Source information
    source_type: EvidenceSourceType
    form: EvidenceForm

    # Hypothesis linkage (for CAUSAL_EVIDENCE)
    tests_hypothesis_id: Optional[str] = None
    stance: Optional[EvidenceStance] = None
    stance_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="""
        Confidence in the stance assessment (0.0-1.0).

        Use this for granularity instead of STRONGLY_SUPPORTS vs SUPPORTS.
        - 0.9+ : Very strong support/refutation
        - 0.7-0.9: Moderate support/refutation
        - 0.5-0.7: Weak support/refutation
        - <0.5: Consider using NEUTRAL stance instead
        """
    )

    # Milestone advancement
    advances_milestones: List[str] = Field(default_factory=list)

    # Metadata
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    collected_by: str
    collected_at_turn: int
    fulfills_request_id: Optional[str] = None

class EvidenceSourceType(str, Enum):
    LOG_FILE = "log_file"
    METRICS_DATA = "metrics_data"
    CONFIG_FILE = "config_file"
    CODE_REVIEW = "code_review"
    SCREENSHOT = "screenshot"
    COMMAND_OUTPUT = "command_output"
    DATABASE_QUERY = "database_query"
    TRACE_DATA = "trace_data"
    API_RESPONSE = "api_response"
    USER_REPORT = "user_report"
    MONITORING_ALERT = "monitoring_alert"
    OTHER = "other"

class EvidenceForm(str, Enum):
    DOCUMENT = "document"
    USER_INPUT = "user_input"

class EvidenceStance(str, Enum):
    """
    Simplified 3-state stance for LLM consistency.

    Use stance_confidence (0.0-1.0) for granularity instead of
    STRONGLY_SUPPORTS vs SUPPORTS distinctions, which LLMs score inconsistently.
    """
    SUPPORTS = "supports"
    """Evidence supports the hypothesis"""

    REFUTES = "refutes"
    """Evidence contradicts the hypothesis"""

    NEUTRAL = "neutral"
    """Evidence neither supports nor refutes"""
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
    status: HypothesisStatus
    likelihood: float = Field(ge=0.0, le=1.0)

    # Evidence requirements
    evidence_requirements: List[EvidenceRequirement]
    supporting_evidence: List[str] = Field(default_factory=list)
    refuting_evidence: List[str] = Field(default_factory=list)

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
    DATA = "data"
    HARDWARE = "hardware"
    EXTERNAL = "external"
    HUMAN = "human"
    OTHER = "other"  # Doesn't fit above categories

class HypothesisStatus(str, Enum):
    CAPTURED = "captured"
    ACTIVE = "active"
    VALIDATED = "validated"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    RETIRED = "retired"

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

**Level 1: Hypothesis Category Anchoring**

```python
def detect_category_anchoring(case: Case) -> Optional[str]:
    """Detect if agent stuck testing same hypothesis category"""

    category_counts = {}
    for h in case.hypotheses.values():
        if h.status in [HypothesisStatus.REFUTED, HypothesisStatus.INCONCLUSIVE]:
            category_counts[h.category] = category_counts.get(h.category, 0) + 1

    # Anchoring if 4+ hypotheses in same category
    for category, count in category_counts.items():
        if count >= 4:
            return f"Tested {count} '{category.value}' hypotheses without validation. Try different category."

    return None
```

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

## 4. Degraded Mode

```python
class DegradedMode(BaseModel):
    """
    Investigation is blocked or struggling.
    Agent needs to offer fallback options.
    """

    mode_type: DegradedModeType

    entered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    reason: str = Field(
        description="Why investigation entered degraded mode"
    )

    attempted_actions: List[str] = Field(
        default_factory=list,
        description="What agent tried before degrading"
    )

    fallback_offered: Optional[str] = Field(
        default=None,
        description="Fallback option presented to user"
    )

    user_choice: Optional[str] = Field(
        default=None,
        description="How user responded to fallback"
    )

    exited_at: Optional[datetime] = Field(
        default=None,
        description="When degraded mode was exited"
    )

    exit_reason: Optional[str] = Field(
        default=None,
        description="How investigation recovered from degraded mode"
    )

    @property
    def is_active(self) -> bool:
        """Check if still in degraded mode"""
        return self.exited_at is None

class DegradedModeType(str, Enum):
    NO_PROGRESS = "no_progress"
    LIMITED_DATA = "limited_data"
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
    EXTERNAL_DEPENDENCY = "external_dependency"
    OTHER = "other"  # Unexpected degradation reason

def should_enter_degraded_mode(case: Case) -> Optional[DegradedModeType]:
    """Determine if should enter degraded mode"""

    if case.turns_without_progress >= 3:
        return DegradedModeType.NO_PROGRESS

    if len(case.hypotheses) > 0:
        all_inconclusive = all(
            h.status == HypothesisStatus.INCONCLUSIVE
            for h in case.hypotheses.values()
        )
        if all_inconclusive:
            return DegradedModeType.HYPOTHESIS_DEADLOCK

    return None
```
