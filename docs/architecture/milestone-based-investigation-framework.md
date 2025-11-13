# FaultMaven Investigation Architecture Specification v2.0

## Executive Summary

This document defines FaultMaven's investigation architecture using a **milestone-based approach** where the agent completes tasks opportunistically based on data availability rather than following rigid sequential phases.

**Core Principles**:
- Investigation progress tracked via **milestone completions**, not phase transitions
- Agent can complete **multiple milestones in one turn** when sufficient data is available
- **Case status** (CONSULTING/INVESTIGATING/RESOLVED/CLOSED) provides user-facing lifecycle state
- **Investigation stages** (Understanding/Diagnosing/Resolving) provide optional progress detail
- Hypothesis testing is **optional exploration**, not a required workflow step

---

## Table of Contents

1. [Architectural Philosophy](#1-architectural-philosophy)
2. [Core Data Models](#2-core-data-models)
3. [Investigation Lifecycle](#3-investigation-lifecycle)
4. [Path Selection & Routing](#4-path-selection--routing)
5. [Evidence Model](#5-evidence-model)
6. [Hypothesis Workflow](#6-hypothesis-workflow)
7. [Turn Progress Tracking](#7-turn-progress-tracking)
8. [Degraded Mode](#8-degraded-mode)
9. [UI/UX Design](#9-uiux-design)
10. [Complete Examples](#10-complete-examples)
11. [Prompt Engineering Guide](#11-prompt-engineering-guide)

---

## 1. Architectural Philosophy

### 1.1 Core Concept

Investigation is **data-driven and opportunistic**, not phase-constrained.

**The Agent**:
- Checks what data is available
- Completes all tasks for which sufficient data exists
- Records which milestones were completed
- Proceeds naturally without artificial barriers

**Example**:
```
User uploads comprehensive log file containing:
  - Error messages (symptom data)
  - Timestamps (timeline data)
  - Stack trace (root cause data)
  
Agent in ONE turn:
  ✅ Verifies symptom
  ✅ Establishes timeline
  ✅ Identifies root cause
  ✅ Proposes solution
  
No sequential phase transitions required.
```

### 1.2 Key Design Decisions

**1. Milestones Track Completion, Not Position**

```python
# Milestone-based approach: Check data availability and completion status
if has_diagnostic_data(case) and not case.progress.root_cause_identified:
    identify_root_cause()
    case.progress.root_cause_identified = True
```

**Key Insight**: Instead of tracking "what phase am I in?", the system checks "what data is available?" and "what's been completed?" This enables opportunistic task completion.

**2. Status is User-Facing Lifecycle State**

Case status answers: **"Is my problem fixed?"**
- CONSULTING: Exploring
- INVESTIGATING: Working on it
- RESOLVED: Fixed (closed WITH solution)
- CLOSED: Done (closed WITHOUT solution)

**3. Stages are Optional Progress Detail**

Investigation stage answers: **"What's the agent doing right now?"**
- Understanding: Verifying the problem
- Diagnosing: Finding root cause
- Resolving: Applying solution

**4. Hypotheses are Optional Exploration Paths**

Agent may:
- Identify root cause directly from evidence (no hypotheses needed)
- OR generate hypotheses for systematic exploration (when cause unclear)

---

## 2. Core Data Models

### 2.1 CaseStatus

```python
class CaseStatus(str, Enum):
    """
    Case lifecycle status (4 states).
    Two terminal states: RESOLVED (with solution) and CLOSED (without solution).
    """
    
    CONSULTING = "consulting"
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
    Investigation abandoned, escalated, or consulting-only.
    
    closure_reason = "abandoned" | "escalated" | "consulting_only" | "duplicate" | "other"
    """
```

**Key Points**:
- **RESOLVED** and **CLOSED** are both terminal (no further state)
- **RESOLVED** = Problem fixed (has solution)
- **CLOSED** = Problem not fixed (no solution, or consulting-only)
- Agent doesn't care about cases after they reach terminal state

### 2.2 InvestigationProgress

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
    
    # NOTE: temporal_state moved to ProblemVerification (where it logically belongs)
    # It's determined during verification, not a milestone itself
    
    # ============================================================
    # Investigation Milestones
    # ============================================================
    root_cause_identified: bool = Field(
        default=False,
        description="Root cause determined"
    )
    
    root_cause_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in root cause (0.0-1.0)"
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
        description="Solution effectiveness verified"
    )

    # ============================================================
    # Path-Specific Tracking
    # ============================================================
    mitigation_applied: bool = Field(
        default=False,
        description="""
        MITIGATION_FIRST path: Quick mitigation applied (stage 1 → 4 complete).

        Used to track progress in MITIGATION_FIRST path (1-4-2-3-4):
        - Stage 1: Symptom verified
        - Stage 4: Quick mitigation applied (mitigation_applied = True)
        - Stage 2: Return to hypothesis formulation for RCA
        - Stage 3: Hypothesis validation
        - Stage 4: Permanent solution applied (solution_applied = True)

        When True: Agent should return to stage 2 for full RCA
        When False: Either ROOT_CAUSE path, or MITIGATION_FIRST hasn't applied mitigation yet

        Note: Different from solution_applied - mitigation is quick correlation-based fix,
        solution is comprehensive permanent fix after RCA.
        """
    )
    
    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> InvestigationStage:
        """
        Compute investigation stage from milestones.
        For optional progress detail, not workflow control.
        """
        if (self.solution_proposed or 
            self.solution_applied or 
            self.solution_verified):
            return InvestigationStage.RESOLVING
        
        if self.symptom_verified and not self.root_cause_identified:
            return InvestigationStage.DIAGNOSING
        
        return InvestigationStage.UNDERSTANDING
    
    @property
    def verification_complete(self) -> bool:
        """All verification milestones completed"""
        return (
            self.symptom_verified and
            self.scope_assessed and
            self.timeline_established and
            self.changes_identified
        )
    
    @property
    def completion_percentage(self) -> float:
        """Overall progress for UI display"""
        milestones = [
            self.symptom_verified,
            self.scope_assessed,
            self.timeline_established,
            self.changes_identified,
            self.root_cause_identified,
            self.solution_proposed,
            self.solution_applied,
            self.solution_verified,
            self.mitigation_applied,  # 9th milestone for MITIGATION_FIRST path
        ]
        return sum(milestones) / len(milestones)

class InvestigationStage(str, Enum):
    """
    Investigation stage within INVESTIGATING phase (4 stages).
    Computed from milestones for optional progress detail.

    Stage Progression (Path-Dependent):
    - MITIGATION_FIRST: 1 → 4 → 2 → 3 → 4 (quick mitigation, then return for RCA)
    - ROOT_CAUSE: 1 → 2 → 3 → 4 (traditional RCA)
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

### 2.3 TurnProgress

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

### 2.4 InvestigationPath

```python
class InvestigationPath(str, Enum):
    """
    Investigation routing based on temporal state and urgency (4-stage workflow).

    Two paths based on urgency:
    - MITIGATION_FIRST: 1-4-2-3-4 (quick mitigation, then RCA)
    - ROOT_CAUSE: 1-2-3-4 (traditional RCA)
    """
    MITIGATION_FIRST = "mitigation_first"
    """
    Mitigation-first path (updated from "mitigation only").

    Stage Flow: 1 → 4 → 2 → 3 → 4
    - Stage 1: Verify symptom (where/when)
    - Stage 4: Apply quick mitigation (correlation-based fix)
    - Stage 2: Formulate hypotheses (why)
    - Stage 3: Validate hypothesis (why really)
    - Stage 4: Apply permanent solution (how)

    Use When: ONGOING + HIGH/CRITICAL urgency
    Key Change: No longer "mitigation only" - returns to RCA after initial mitigation
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

### 2.5 Complete Case Model

```python
class Case(BaseModel):
    """Complete case model with milestone-based architecture"""
    
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
    status: CaseStatus = Field(default=CaseStatus.CONSULTING)
    
    status_history: List[CaseStatusTransition] = Field(
        default_factory=list,
        description="Audit trail of status changes"
    )
    
    closure_reason: Optional[str] = Field(
        default=None,
        description="resolved | abandoned | escalated | consulting_only | duplicate | other"
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
    consulting: ConsultingData = Field(default_factory=ConsultingData)
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

### 2.6 ProblemVerification

```python
class ProblemVerification(BaseModel):
    """
    Consolidated problem verification data.
    Created when investigation starts (CONSULTING → INVESTIGATING).
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

### 2.7 ConsultingData

```python
class ConsultingData(BaseModel):
    """
    Pre-investigation consulting phase data.
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
        description="Quick fixes or guidance provided during consulting"
    )
    
    decided_to_investigate: bool = Field(
        default=False,
        description="Whether user committed to formal investigation"
    )
    
    decision_made_at: Optional[datetime] = Field(
        default=None,
        description="When user decided to investigate (if decided)"
    )
    
    consultation_turns: int = Field(
        default=0,
        description="Number of turns spent in consulting phase"
    )

class ProblemConfirmation(BaseModel):
    """Agent's initial problem understanding during consulting"""
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

### 2.8 Solution

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

### 2.10 WorkingConclusion

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
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this conclusion (0.0-1.0)"
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

### 2.11 RootCauseConclusion

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
    
    confidence_level: ConfidenceLevel
    
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Numeric confidence score (0.0-1.0)"
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

### 2.12 EscalationState

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

### 2.13 DocumentationData

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

## 3. Investigation Lifecycle

### 3.1 Status Transition Map

```
┌──────────────┐
│  CONSULTING  │
│              │
│ Exploring    │
└──────┬───────┘
       │
       ├─────(User decides to investigate)────────┐
       │                                          │
       │                                          ▼
       │                              ┌────────────────────┐
       │                              │   INVESTIGATING    │
       │                              │                    │
       │                              │ Verification       │
       │                              │ Investigation      │
       │                              │ Resolution         │
       │                              └─────────┬──────────┘
       │                                        │
       │                              ┌─────────┴──────────┐
       │                              │                    │
       │                   (solution_verified)    (no solution,
       │                              │            abandoned/escalated)
       │                              │                    │
       │                              ▼                    ▼
       │                      ┌──────────────┐    ┌──────────────┐
       │                      │   RESOLVED   │    │    CLOSED    │
       │                      │              │    │              │
       │                      │ TERMINAL     │    │  TERMINAL    │
       │                      │ With solution│    │ No solution  │
       │                      └──────────────┘    └──────────────┘
       │                                                  ▲
       └──(no investigation needed)──────────────────────┘
          (consulting-only)
```

### 3.2 Status Transitions

#### CONSULTING → INVESTIGATING

**Trigger**: User commits to formal investigation AND confirms problem statement

```python
def can_start_investigation(case: Case) -> bool:
    """
    Requires:
    1. Problem confirmation (agent understands problem type)
    2. Problem statement formalized and confirmed by user
    3. User decided to investigate
    """
    return (
        case.status == CaseStatus.CONSULTING and
        case.consulting.problem_confirmation is not None and
        case.consulting.proposed_problem_statement is not None and
        case.consulting.problem_statement_confirmed == True and
        case.consulting.decided_to_investigate == True
    )

if can_start_investigation(case):
    # Create ProblemVerification with confirmed statement
    case.problem_verification = ProblemVerification(
        symptom_statement=case.consulting.proposed_problem_statement
        # LLM will fill other fields during investigation
    )
    
    transition_status(case, CaseStatus.INVESTIGATING, "system", 
                     "User confirmed problem and decided to investigate")
```

#### INVESTIGATING → RESOLVED (Terminal)

**Trigger**: Solution verified

```python
def can_mark_resolved(case: Case) -> bool:
    return (
        case.status == CaseStatus.INVESTIGATING and
        case.progress.solution_verified == True
    )

if can_mark_resolved(case):
    case.status = CaseStatus.RESOLVED
    case.resolved_at = datetime.now(timezone.utc)
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "resolved"
    # TERMINAL - no further transitions
```

#### INVESTIGATING → CLOSED (Terminal)

**Trigger**: Investigation abandoned without solution

```python
def force_close_investigation(case: Case, user_id: str, reason: str):
    """User abandons investigation without solution"""
    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = reason  # "abandoned" | "escalated" | "other"
    # TERMINAL - no further transitions
```

#### CONSULTING → CLOSED (Terminal)

**Trigger**: Consulting-only, no investigation needed

```python
def close_from_consulting(case: Case, user_id: str):
    """Close after consulting without formal investigation"""
    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "consulting_only"
    # TERMINAL - no further transitions
```

### 3.3 Valid Transitions Summary

```python
VALID_TRANSITIONS = {
    CaseStatus.CONSULTING: [
        CaseStatus.INVESTIGATING,  # Start investigation
        CaseStatus.CLOSED           # Consulting-only, no investigation
    ],
    CaseStatus.INVESTIGATING: [
        CaseStatus.RESOLVED,        # Solution verified (terminal)
        CaseStatus.CLOSED           # Abandoned (terminal)
    ],
    CaseStatus.RESOLVED: [],        # TERMINAL - no transitions
    CaseStatus.CLOSED: []           # TERMINAL - no transitions
}
```

### 3.4 Milestone Progression

**During INVESTIGATING status, milestones complete opportunistically**:

```python
async def process_turn(case: Case, user_message: str):
    """Process one turn and update milestones"""
    
    # Only process if not terminal
    if case.is_terminal:
        return "Case is closed."
    
    # Capture state before
    progress_before = case.progress.dict()
    
    # Agent analyzes available data and completes tasks
    agent_response = await agent.process(case, user_message)
    
    # Capture state after
    progress_after = case.progress.dict()
    
    # Detect completed milestones
    milestones_completed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]
    
    # Record turn
    record_turn(case, milestones_completed)
    
    # Check for automatic status transitions to terminal states
    check_status_transitions(case)
    
    return agent_response
```

### 3.5 Manual Status Change Requests

**Purpose**: Allow users to manually request status transitions for practical scenarios (urgent issues, external resolutions, etc.)

**Core Principle**: Manual status changes follow the same confirmation pattern as natural progression - **all status changes require explicit user confirmation**.

---

#### 3.5.1 UI Component: Status Dropdown

**Location**: Case header (collapsed view)

**Behavior**:
- Shows current status with dropdown indicator
- Displays only **forward transitions** (status changes are irreversible)
- Terminal states (RESOLVED, CLOSED) have dropdown disabled

**Available Options by Status**:

| Current Status | Dropdown Options |
|---------------|------------------|
| CONSULTING    | Investigating, Closed |
| INVESTIGATING | Resolved, Closed |
| RESOLVED      | *(disabled - terminal state)* |
| CLOSED        | *(disabled - terminal state)* |

**API Support**: No direct API - uses existing query submission endpoint

---

#### 3.5.2 Request Flow

**Step 1: User Initiates Request**

User selects new status from dropdown → Frontend shows confirmation modal:

```
⚠️ Request Status Change

This will ask the agent to transition the case to [NEW_STATUS].

Are you sure you want to proceed?

[Cancel]  [Continue]
```

**API Call**: None yet - just frontend modal

---

**Step 2: Submit Request via Chat**

User confirms modal → Frontend sends system-generated message:

```typescript
POST /api/v1/cases/{case_id}/queries
Body: {
  "message": "[User requested to change case status to Investigating]"
}
```

**API Endpoint**: `POST /api/v1/cases/{case_id}/queries`
- **Purpose**: Submit user messages (including system-generated ones)
- **Auth**: Requires Bearer token + X-Session-Id
- **Returns**: AgentResponse with agent's confirmation message

---

**Step 3: Agent Validates and Asks for Confirmation**

Agent receives request message and responds with:
1. **Context validation** - Ensures prerequisites are met or asks for missing information
2. **Confirmation question** - Presents specific question with Yes/No buttons
3. **Tip text** - Indicates user can type qualified answer

**Example Agent Response** (CONSULTING → INVESTIGATING):

```
"You've requested to move to investigation.

Based on our conversation, the problem is:
'Database queries timing out in production, affecting 30% of requests'

Is this what you want me to investigate?

[✅ Yes]  [❌ No]

💡 Tip: Click a button or type to clarify"
```

**If missing information**, agent asks questions first:

```
"You've requested to move to investigation.

Before we can investigate, I need to understand the problem.
What issue are you experiencing?"

(No buttons yet - waiting for user to provide context)
```

---

**Step 4: User Confirms (3 Options)**

**Option A: Click [✅ Yes]**
- Frontend sends system-generated message: `"Yes"`
- Agent immediately transitions status
- Agent responds with acknowledgment

**Option B: Click [❌ No]**
- Frontend sends system-generated message: `"No"`
- Agent cancels request, stays in current status
- Agent asks what user wants to do next

**Option C: Type qualified answer**
- User types: "Not 30%, more like 50%, and started 3 hours ago"
- Agent refines understanding
- Agent presents confirmation question again with updated context

**API Call for all options**:
```typescript
POST /api/v1/cases/{case_id}/queries
Body: {
  "message": "Yes"  // or "No" or user's typed message
}
```

---

**Step 5: Agent Executes Transition**

If user confirmed (Option A or refined via Option C), agent:

1. **Sets status** to new value
2. **Initializes required state** (e.g., creates `ProblemVerification` for INVESTIGATING)
3. **Records transition** in `status_history`
4. **Responds with acknowledgment** and next steps

**Example response** (CONSULTING → INVESTIGATING):

```
"Understood. Transitioning to formal investigation now.

Based on our discussion, the problem is:
'Database queries timing out in production, affecting 50% of requests
since 3 hours ago'

Let me start by verifying the scope and impact. What services are affected?"
```

**Backend updates**:
- `case.status = CaseStatus.INVESTIGATING`
- `case.problem_verification = ProblemVerification(symptom_statement=...)`
- `case.status_history.append(CaseStatusTransition(...))`

---

#### 3.5.3 Confirmation UI Pattern

**Visual Design** (in chat conversation):

```
┌─────────────────────────────────────────────────┐
│ Agent:                                   2:45 PM│
│                                                 │
│ You've requested to move to investigation.      │
│                                                 │
│ Based on our conversation, the problem is:      │
│ "Database queries timing out in production,     │
│ affecting 30% of requests"                      │
│                                                 │
│ Is this what you want me to investigate?        │
│                                                 │
│ ┌─────────┐  ┌─────────┐                       │
│ │ ✅ Yes  │  │ ❌ No   │                       │
│ └─────────┘  └─────────┘                       │
│                                                 │
│ 💡 Tip: Click a button or type to clarify      │
└─────────────────────────────────────────────────┘
```

**Buttons are rendered by frontend** when agent message contains:
- Confirmation question pattern
- Binary choice indicators

**Button clicks generate system messages**:
- `[✅ Yes]` → Sends `"Yes"` via POST `/queries`
- `[❌ No]` → Sends `"No"` via POST `/queries`

---

#### 3.5.4 Status-Specific Confirmation Examples

**CONSULTING → INVESTIGATING**

```python
# Agent validation
if not case.consulting.proposed_problem_statement:
    # Missing problem - ask first
    return "Before we can investigate, what problem are we trying to solve?"
else:
    # Present confirmation
    return f"""You've requested to move to investigation.

    The problem is: {case.consulting.proposed_problem_statement}

    Is this what you want me to investigate?

    [✅ Yes]  [❌ No]"""
```

**INVESTIGATING → RESOLVED**

```python
# Agent asks for resolution details
return f"""You've requested to mark this as resolved.

Problem: {case.problem_verification.symptom_statement}
Root cause: {case.root_cause_conclusion.root_cause if exists else "Not identified"}

What did you do to resolve this issue?

(Agent waits for user to explain, then presents confirmation)
"""
```

**INVESTIGATING → CLOSED**

```python
# Agent confirms closure without resolution
return f"""You've requested to close this case without resolution.

Problem: {case.problem_verification.symptom_statement}
Current findings: {case.working_conclusion.summary if exists else "Limited data"}

Should I close the case and archive our findings?

[✅ Yes]  [❌ No]"""
```

---

#### 3.5.5 API Summary

All manual status changes use **existing endpoints** - no new APIs required:

| Action | Endpoint | Method | Body |
|--------|----------|--------|------|
| Submit status change request | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "[User requested to change case status to Investigating]"}` |
| User clicks Yes button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "Yes"}` |
| User clicks No button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "No"}` |
| User types qualified answer | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "<user's typed message>"}` |

**All messages appear in conversation history** - full audit trail maintained.

---

#### 3.5.6 Design Rationale

**Why dropdown menu instead of pure chat?**
- **Discoverability**: Users see available status transitions
- **Clarity**: Visual indicator of current status + forward-only options
- **Efficiency**: One click vs composing message
- **Removes ambiguity**: "Let's investigate" could mean many things

**Why agent confirmation instead of direct status change?**
- **Consistency**: Same pattern as natural progression (all status changes require confirmation)
- **Safety**: Agent can validate prerequisites and catch mistakes
- **Context**: Agent ensures mutual understanding before transition
- **Audit**: Full conversation record of why status changed

**Why buttons + typed fallback?**
- **Efficiency**: Most cases are simple yes/no
- **Flexibility**: User can elaborate when needed
- **Natural**: Matches existing confirmation pattern in natural progression

---

## 4. Path Selection & Routing

### 4.1 Path Selection Matrix

Based on **temporal_state × urgency_level**:

| Temporal State | Urgency | Path | Rationale |
|----------------|---------|------|-----------|
| **Ongoing** | CRITICAL | MITIGATION_FIRST (auto) | Production broken NOW - stop impact, RCA later |
| **Ongoing** | HIGH | MITIGATION_FIRST (auto) | Significant active impact - stop bleeding first |
| **Ongoing** | MEDIUM | USER_CHOICE | User decides: quick mitigation or thorough RCA |
| **Ongoing** | LOW | USER_CHOICE | Minor issue, user decides approach |
| **Historical** | CRITICAL | USER_CHOICE | Clarify why critical if past issue |
| **Historical** | HIGH | USER_CHOICE | High urgency for past issue? |
| **Historical** | MEDIUM | ROOT_CAUSE (auto) | Standard post-mortem - find root cause |
| **Historical** | LOW | ROOT_CAUSE (auto) | Thorough investigation - permanent solution |

### 4.2 Path Selection Logic

```python
def determine_investigation_path(
    problem_verification: ProblemVerification
) -> PathSelection:
    """Determine investigation path after verification complete"""
    
    temporal = problem_verification.temporal_state
    urgency = problem_verification.urgency_level
    
    # AUTO: Ongoing + High Urgency → MITIGATION_FIRST (then RCA)
    if temporal == TemporalState.ONGOING and urgency in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=f"Ongoing {urgency.value} issue requires immediate mitigation, RCA after impact stopped",
            alternate_path=InvestigationPath.ROOT_CAUSE
        )

    # AUTO: Historical + Low Urgency → ROOT_CAUSE (permanent solution)
    if temporal == TemporalState.HISTORICAL and urgency in [UrgencyLevel.LOW, UrgencyLevel.MEDIUM]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=f"Historical {urgency.value} issue allows thorough investigation with permanent solution",
            alternate_path=InvestigationPath.MITIGATION_FIRST
        )

    # USER CHOICE: Ambiguous cases - let user decide between paths
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale=f"Ambiguous case ({temporal.value} + {urgency.value}): User chooses (a) mitigation first or (b) RCA",
        alternate_path=None
    )
```

### 4.3 Path Impact on Investigation

**Path (a): MITIGATION_FIRST (1-4-2-3-4)**:

Key Change: **No longer "mitigation only" - returns to full RCA after mitigation**

- **Stage 1: Symptom Verification**
  - Verify where and when problem is happening
  - Assess urgency and temporal state
  - Path selection: MITIGATION_FIRST chosen due to ONGOING + HIGH/CRITICAL urgency
  - Next: Skip directly to stage 4

- **Stage 4: Quick Mitigation (First Visit)**
  - Apply correlation-based temporary fix
  - Stop immediate impact and restore service
  - Mark `mitigation_applied = True`
  - Next: Return to stage 2 for RCA

- **Stage 2: Hypothesis Formulation**
  - Generate theories about root cause
  - Now that service is restored, can take time for thorough analysis
  - May use systematic exploration when cause unclear
  - Next: Stage 3

- **Stage 3: Hypothesis Validation**
  - Test hypotheses with diagnostic evidence
  - Identify root cause with confidence
  - Mark `root_cause_identified = True`
  - Next: Stage 4 (second visit)

- **Stage 4: Permanent Solution (Second Visit)**
  - Apply evidence-based permanent fix
  - Address root cause to prevent recurrence
  - Verify effectiveness
  - Case transitions to RESOLVED

**Milestones**: `symptom_verified` → `mitigation_applied` → `root_cause_identified` → `solution_applied` → `solution_verified`

**Path (b): ROOT_CAUSE (1-2-3-4)**:

Traditional RCA path - thorough investigation from start

- **Stage 1: Symptom Verification**
  - Verify where and when (historical problem or low urgency)
  - Path selection: ROOT_CAUSE chosen
  - Next: Stage 2

- **Stage 2: Hypothesis Formulation**
  - Generate theories systematically
  - Next: Stage 3

- **Stage 3: Hypothesis Validation**
  - Test hypotheses, identify root cause
  - Mark `root_cause_identified = True`
  - Next: Stage 4

- **Stage 4: Solution**
  - Apply permanent solution based on root cause
  - Verify effectiveness
  - Case transitions to RESOLVED

**Milestones**: `symptom_verified` → `root_cause_identified` → `solution_applied` → `solution_verified`

**Key Differences**:
- **MITIGATION_FIRST**: Stage 4 visited TWICE (quick mitigation, then permanent fix), stages 2-3 done AFTER mitigation
- **ROOT_CAUSE**: Stage 4 visited ONCE (permanent fix), stages 2-3-4 done sequentially
- **MITIGATION_FIRST**: Uses `mitigation_applied` field to track the return path
- **ROOT_CAUSE**: Traditional linear progression through all 4 stages

---

## 5. Evidence Model

### 5.1 Purpose-Driven Categories

```python
class EvidenceCategory(str, Enum):
    """Evidence classification by investigation purpose"""
    
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
    Advances: solution_verified
    """
    
    OTHER = "other"
    """
    Evidence that doesn't fit above categories.
    May be useful but doesn't directly advance standard milestones.
    """
```

### 5.2 Evidence Schema

```python
class Evidence(BaseModel):
    """
    Evidence with purpose-driven categorization.
    
    NOTE: Evidence.category is SYSTEM-INFERRED, not LLM-specified!
    System categorizes based on:
    - Which milestones are incomplete (if symptom not verified → SYMPTOM_EVIDENCE)
    - Hypothesis linkage (if tests_hypothesis_id set → CAUSAL_EVIDENCE)
    - Solution state (if solution proposed → RESOLUTION_EVIDENCE)
    
    LLM provides: summary, analysis, tests_hypothesis_id, stance
    System infers: category, advances_milestones
    """
    
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:12]}")
    
    # ============================================================
    # Purpose Classification (SYSTEM-INFERRED)
    # ============================================================
    category: EvidenceCategory = Field(
        description="System-inferred category based on investigation context"
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
    STRONGLY_SUPPORTS = "strongly_supports"
    SUPPORTS = "supports"
    NEUTRAL = "neutral"
    CONTRADICTS = "contradicts"
    STRONGLY_CONTRADICTS = "strongly_contradicts"
```

---

## 6. Hypothesis Workflow

### 6.1 Hypothesis Schema

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

### 6.2 Anchoring Detection

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

## 7. Turn Progress Tracking

```python
async def record_turn(
    case: Case,
    user_message: str,
    agent_response: str
) -> TurnProgress:
    """Record turn and detect progress"""
    
    # Capture state before
    progress_before = case.progress.dict()
    evidence_count_before = len(case.evidence)
    
    # Process turn (agent work happens here)
    
    # Capture state after
    progress_after = case.progress.dict()
    evidence_count_after = len(case.evidence)
    
    # Detect milestones completed
    milestones_completed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]
    
    # Detect evidence added
    evidence_added = []
    if evidence_count_after > evidence_count_before:
        new_evidence = case.evidence[evidence_count_before:]
        evidence_added = [e.evidence_id for e in new_evidence]
    
    # Determine if progress made
    progress_made = (
        len(milestones_completed) > 0 or
        len(evidence_added) > 0 or
        any(h.status == HypothesisStatus.VALIDATED for h in case.hypotheses.values())
    )
    
    # Create turn record
    turn = TurnProgress(
        turn_number=case.current_turn,
        milestones_completed=milestones_completed,
        evidence_added=evidence_added,
        progress_made=progress_made,
        outcome=determine_turn_outcome(case, progress_made)
    )
    
    case.turn_history.append(turn)
    case.current_turn += 1
    
    # Track turns without progress
    if progress_made:
        case.turns_without_progress = 0
    else:
        case.turns_without_progress += 1
    
    # Escalate if stuck
    if case.turns_without_progress >= 3:
        enter_degraded_mode(case, DegradedModeType.NO_PROGRESS)
    
    return turn
```

---

## 8. Degraded Mode

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

---

## 9. UI/UX Design

### 9.1 Primary Display: Case Status

```python
def render_case_header(case: Case) -> str:
    """Primary UI shows STATUS"""
    
    status_display = {
        CaseStatus.CONSULTING: {
            "label": "💬 Exploring",
            "description": "Discussing the problem",
            "color": "blue"
        },
        CaseStatus.INVESTIGATING: {
            "label": "🔍 Investigating",
            "description": "Working on finding and fixing the issue",
            "color": "yellow"
        },
        CaseStatus.RESOLVED: {
            "label": "✅ Resolved",
            "description": "Problem fixed (closed with solution)",
            "color": "green"
        },
        CaseStatus.CLOSED: {
            "label": "📦 Closed",
            "description": f"Closed without solution ({case.closure_reason})",
            "color": "gray"
        }
    }
    
    info = status_display[case.status]
    
    # Stage as secondary detail (only for INVESTIGATING)
    stage_detail = ""
    if case.status == CaseStatus.INVESTIGATING and case.current_stage:
        stage_labels = {
            InvestigationStage.UNDERSTANDING: "Understanding the problem",
            InvestigationStage.DIAGNOSING: "Diagnosing the cause",
            InvestigationStage.RESOLVING: "Applying solution",
        }
        stage_detail = f"\n  {stage_labels[case.current_stage]} ({case.progress.completion_percentage:.0f}%)"
    
    return f"""
┌─────────────────────────────────────────────┐
│ Status: {info['label']}                     │
│ {info['description']}                       │
{stage_detail}
│                                             │
│ Turn {case.current_turn} | {format_time_ago(case.updated_at)}
└─────────────────────────────────────────────┘
"""
```

---

## 10. Complete Examples

### 10.1 One-Turn Resolution

```python
user_message = """
My API is timing out. Attached error.log showing NullPointerException
at line 42 starting at 14:23 UTC. We deployed v2.1.3 at 14:20 UTC.
"""

def agent_turn_1(case: Case):
    # Complete MULTIPLE milestones in one turn
    case.progress.symptom_verified = True
    case.progress.scope_assessed = True
    case.progress.timeline_established = True
    case.progress.changes_identified = True
    case.progress.root_cause_identified = True
    case.progress.root_cause_confidence = 0.95
    case.progress.solution_proposed = True
    
    return """
**Investigation Complete** (1 turn)

✅ Symptom: NullPointerException causing API timeouts
✅ Timeline: Started 14:23 UTC (3 min after v2.1.3 deploy)
✅ Root cause: Missing null check at line 42 in UserService.java

**Recommended Solutions**:
1. IMMEDIATE: Rollback to v2.1.2
2. LONG-TERM: Add null check at line 42

Would you like to proceed with rollback?
"""
```

### 10.2 Status Lifecycle Example

```python
# Turn 1: Consulting
case.status = CaseStatus.CONSULTING

# Turn 3: User decides to investigate
case.status = CaseStatus.INVESTIGATING  # Automatic transition

# Turns 4-15: Investigation
case.progress.symptom_verified = True
case.progress.root_cause_identified = True
case.progress.solution_applied = True

# Turn 17: Solution verified
case.progress.solution_verified = True
case.status = CaseStatus.RESOLVED  # Automatic transition to TERMINAL
case.resolved_at = datetime.now()
case.closed_at = datetime.now()
case.closure_reason = "resolved"

# Case is now TERMINAL - no further processing
```

### 10.3 Closed Without Solution

```python
# User abandons investigation
user: "Need to escalate to senior engineer. Close this case."

case.status = CaseStatus.CLOSED  # Transition to TERMINAL
case.closed_at = datetime.now()
case.closure_reason = "escalated"

# Case is now TERMINAL - no further processing
```

---

## 11. Prompt Engineering Guide

### 11.1 Core Concept: LLM as Form-Filler + Conversationalist

**Each turn, LLM performs two tasks**:

1. **Fill out investigation state form** (structured data update)
2. **Respond to user naturally** (conversation)

```python
async def process_turn(case: Case, user_message: str):
    """
    Agent asks LLM to:
    1. Analyze user message + current case state
    2. Update structured investigation state
    3. Respond naturally to user
    """
    
    # Build context
    context = {
        "current_state": case.dict(),
        "user_message": user_message,
        "turn_number": case.current_turn
    }
    
    # LLM processes
    llm_response = await llm.generate(
        prompt=build_prompt(case, user_message),
        response_format=InvestigationResponse  # Structured output
    )
    
    # LLM returns
    return {
        "agent_response": "I've analyzed the logs...",  # Natural language
        "state_updates": {                               # Structured updates
            "milestones": {"symptom_verified": True, ...},
            "evidence_to_add": [...],
            "working_conclusion": {...}
        }
    }
```

---

### 11.2 Three Prompt Templates

**Template selection based on case.status**:

| Case Status | Template | Schema | Complexity | Frequency |
|-------------|----------|--------|-----------|-----------|
| **CONSULTING** | #1 | ConsultingResponse | Low | ~10% |
| **INVESTIGATING** | #2 | InvestigationResponse | High | ~85% |
| **RESOLVED/CLOSED** | #3 | TerminalResponse | Low | ~5% |

---

### 11.3 Template 1: CONSULTING

**Purpose**: Explore problem, formalize understanding, get user commitment

**LLM Output Schema**:
```python
class ConsultingResponse(BaseModel):
    agent_response: str
    state_updates: ConsultingStateUpdate

class ConsultingStateUpdate(BaseModel):
    """
    What LLM can update during CONSULTING status.
    
    Conversation context (turn_history) is provided in prompt - no need to accumulate raw user input.
    """
    
    # Problem understanding
    problem_confirmation: Optional[ProblemConfirmation] = None
    
    # Problem statement formalization (ITERATIVE REFINEMENT pattern)
    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="""
        Clear, specific, actionable problem statement for user confirmation.
        
        ITERATIVE REFINEMENT workflow:
        1. CREATE when you have enough information to formalize problem
        2. UPDATE if user provides corrections ("Not quite - it's 30%, not 10%")
        3. Becomes immutable once user confirms without reservation
        
        Base formulation on conversation history (provided in prompt context).
        Refine iteratively until user confirms.
        
        Example evolution:
        Turn 3: "API experiencing slowness" (first formalization)
        Turn 4: User corrects → UPDATE to "...with 30% failure rate"
        Turn 5: User adds detail → UPDATE to "...started 2h ago"
        Turn 6: User confirms → Immutable
        
        Confirmation: Button (✅ Confirm) or text ("Yes, exactly!")
        """,
        max_length=1000
    )
    
    quick_suggestions: List[str] = []
```

**Prompt Structure**:
```
You are FaultMaven, an SRE troubleshooting copilot.

STATUS: CONSULTING (Pre-Investigation)
Turn: {case.current_turn}

CONVERSATION HISTORY (last 5-10 turns):
{recent_conversation_context}

{if case.consulting.proposed_problem_statement}
YOUR PROPOSED PROBLEM STATEMENT:
"{case.consulting.proposed_problem_statement}"

Confirmation Status: {case.consulting.problem_statement_confirmed ? "✅ Confirmed" : "⏳ Awaiting user confirmation"}

{if not case.consulting.problem_statement_confirmed}
NOTE: User has NOT confirmed yet. They may:
- Agree completely → System sets confirmed = True
- Suggest revisions → UPDATE proposed_problem_statement based on their feedback
- Ignore → Keep asking for confirmation
{endif}
{endif}

CURRENT USER MESSAGE:
{user_message}

YOUR TASK:

1. Answer user's question thoroughly

2. Problem Detection & Formalization Workflow:
   
   Step 0: DETECT PROBLEM SIGNALS (Check Every Turn)
   → Problem signals: errors, failures, slowness, outages, user asks "Help me fix..."
   → No problem signals: general questions, informational queries, configuration help
   → IF NO PROBLEM SIGNAL: Just answer question, done. Don't create proposed_problem_statement.
   → IF PROBLEM SIGNAL DETECTED: Proceed to Step A (formalization)
   
   Step A: When you have enough information to formalize
   → Fill out: problem_confirmation (problem_type, severity_guess)
   → CREATE: proposed_problem_statement (clear, specific, actionable)
   → In response: "Let me confirm: <proposed_problem_statement>. Is that accurate?"
   
   Step B: User provides corrections/refinements (REVISION LOOP)
   → UPDATE: proposed_problem_statement based on user feedback
   → In response: "Thanks for clarifying! Let me refine: <updated_statement>. Is that better?"
   → ITERATE until user confirms without reservation
   
   Example revision loop:
   Turn 3: You create "API experiencing slowness"
           User: "Not quite - 30% of requests take >5s"
   Turn 4: You UPDATE to "API experiencing slowness with 30% failure rate"
           User: "Yes, and started 2h ago"
   Turn 5: You UPDATE to "...started approximately 2 hours ago"
           User: "Yes, exactly!" → System confirms
   
   Step C: User confirms without reservation
   → User says: "Yes", "correct", "exactly", or clicks ✅ Confirm button
   → System detects and sets: problem_statement_confirmed = True
   → proposed_problem_statement becomes IMMUTABLE
   → Ask: "Would you like me to investigate this formally?"
   
   Step D: User decides to investigate
   → User says: "Yes, investigate" or clicks ✅ Yes, Investigate button
   → System detects and sets: decided_to_investigate = True
   → System will transition to INVESTIGATING status

3. Provide quick_suggestions if you have any

OUTPUT FORMAT:
{
  "agent_response": "<your natural response>",
  "state_updates": {
    "problem_confirmation": {...} or null,
    "proposed_problem_statement": "<statement>" or null,  // Can update until confirmed
    "quick_suggestions": [...]
  }
}

CRITICAL PRINCIPLES:
- Iterative refinement: Revise proposed_problem_statement until user confirms without reservation
- NO raw user input: Use conversation context to formalize problem (don't copy user text)
- Button confirmations: User clicks ✅ Confirm or ❌ Revise (discrete choices)
```

---

### 11.4 Template 2: INVESTIGATING ⭐ **PRIMARY TEMPLATE**

**Purpose**: Complete milestones opportunistically, drive investigation to resolution

**LLM Output Schema**:
```python
class InvestigationResponse(BaseModel):
    agent_response: str
    state_updates: InvestigationStateUpdate

class InvestigationStateUpdate(BaseModel):
    """
    THE BIG FORM - LLM fills this every turn during INVESTIGATING.
    
    IMPORTANT: Same schema for all stages (Understanding/Diagnosing/Resolving).
    Prompt instructions adapt to emphasize relevant sections.
    """
    
    # Milestones (LLM sets to True when completed)
    milestones: MilestoneUpdates
    
    # Verification data (if verification not complete)
    verification_updates: Optional[ProblemVerificationUpdate] = None
    
    # Evidence (ALWAYS available - user provides freely)
    evidence_to_add: List[EvidenceToAdd] = []
    
    # Hypotheses (optional - only if root cause unclear)
    hypotheses_to_add: List[HypothesisToAdd] = []
    hypotheses_to_update: Dict[str, HypothesisUpdate] = {}
    
    # Hypothesis-Evidence Links (when evaluating submitted evidence)
    hypothesis_evidence_links: List[HypothesisEvidenceLinkToAdd] = []
    
    # Solutions (when ready to fix)
    solutions_to_add: List[SolutionToAdd] = []
    
    # Working conclusion (update frequently)
    working_conclusion: Optional[WorkingConclusion] = None
    
    # Root cause conclusion (when root_cause_identified)
    root_cause_conclusion: Optional[RootCauseConclusion] = None

class MilestoneUpdates(BaseModel):
    """Milestones LLM can set to True (never False - milestones only advance forward)"""
    symptom_verified: Optional[bool] = None      # Set to True if can verify
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_confidence: Optional[float] = None
    root_cause_method: Optional[str] = None
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None      # User action - LLM reports
    solution_verified: Optional[bool] = None

class EvidenceToAdd(BaseModel):
    """Evidence object LLM creates when user submits data"""
    # LLM provides
    summary: str  # "Connection pool at 95% capacity"
    analysis: Optional[str]  # Detailed analysis
    
    # System infers
    # - category (from hypothesis_evidence_links created)
    # - advances_milestones (from milestone completion)
    # - evidence_id, timestamps, etc.

class HypothesisEvidenceLinkToAdd(BaseModel):
    """
    Relationship between hypothesis and evidence (created when LLM evaluates evidence).
    ONE evidence → MANY hypotheses (different stance per hypothesis).
    """
    hypothesis_id: str  # Which hypothesis
    evidence_id: str    # Which evidence (from just-submitted evidence)
    stance: EvidenceStance  # STRONGLY_SUPPORTS | SUPPORTS | NEUTRAL | CONTRADICTS | STRONGLY_CONTRADICTS | IRRELEVANT
    reasoning: str      # "Pool at 95% confirms exhaustion theory"
    completeness: float # How well this evidence tests THIS hypothesis (0.0-1.0)
```

**Prompt Structure (Adaptive)**:
```python
def build_investigating_prompt(case: Case, user_message: str) -> str:
    """
    Single template, but instructions adapt to current stage.
    """
    
    prompt = f"""
You are FaultMaven, an SRE troubleshooting copilot.

STATUS: INVESTIGATING
Turn: {case.current_turn}
Investigation Path: {case.path_selection.path if case.path_selection else "Not yet selected"}

═══════════════════════════════════════════════════════════
WHAT YOU ALREADY KNOW (Don't re-verify!)
═══════════════════════════════════════════════════════════

PROBLEM:
{case.problem_verification.symptom_statement if case.problem_verification else "Not yet verified"}

MILESTONES:
"""
    
    # Show milestone status
    for milestone, completed in {
        "symptom_verified": case.progress.symptom_verified,
        "scope_assessed": case.progress.scope_assessed,
        "timeline_established": case.progress.timeline_established,
        "changes_identified": case.progress.changes_identified,
        "root_cause_identified": case.progress.root_cause_identified,
        "solution_proposed": case.progress.solution_proposed,
        "solution_applied": case.progress.solution_applied,
        "solution_verified": case.progress.solution_verified,
    }.items():
        status = "✅" if completed else "⏳"
        prompt += f"{status} {milestone}\n"
    
    # Show current data
    prompt += f"""
EVIDENCE: {len(case.evidence)} pieces collected
HYPOTHESES: {len(case.hypotheses)} generated ({len([h for h in case.hypotheses.values() if h.status == HypothesisStatus.ACTIVE])} active)
SOLUTIONS: {len(case.solutions)} proposed
"""
    
    # Show recent conversation
    if case.turn_history:
        recent = case.turn_history[-3:]
        prompt += f"\nRECENT CONVERSATION:\n"
        for turn in recent:
            prompt += f"Turn {turn.turn_number}: {turn.outcome.value}\n"
    
    prompt += f"""
═══════════════════════════════════════════════════════════
USER'S MESSAGE
═══════════════════════════════════════════════════════════
{user_message}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════
"""
    
    # ADAPTIVE INSTRUCTIONS based on stage
    stage = case.progress.current_stage
    
    if stage == InvestigationStage.UNDERSTANDING:
        prompt += """
FOCUS: VERIFICATION (Understanding the Problem)

Priority Actions:
1. Verify symptom with concrete evidence
2. Assess scope (who/what affected, blast radius)
3. Establish timeline (when started, when noticed)
4. Identify recent changes (deployments, configs, scaling)
5. Determine temporal_state (ONGOING vs HISTORICAL)
6. Assess urgency_level (CRITICAL/HIGH/MEDIUM/LOW)

Fill Out:
- verification_updates: Complete ProblemVerification fields
- milestones: Set verification milestones to True when verified
- evidence_to_add: Add evidence for data user provided

IMPORTANT: If user provides comprehensive data, you can jump ahead!
- If logs show root cause → Set root_cause_identified = True
- Don't artificially constrain yourself to verification only
"""
    
    elif stage == InvestigationStage.DIAGNOSING:
        prompt += """
✅ VERIFICATION COMPLETE

FOCUS: ROOT CAUSE IDENTIFICATION (Finding Why)

Verification Data Available:
- Symptom: {case.problem_verification.symptom_statement}
- Temporal State: {case.problem_verification.temporal_state}
- Urgency: {case.problem_verification.urgency_level}
- Path: {case.path_selection.path if case.path_selection else "Determining..."}

Options:

A) Direct Identification (if root cause obvious from evidence):
   → Set: root_cause_identified = True
   → Fill: root_cause_conclusion
   → Specify: root_cause_method = "direct_analysis"

B) Hypothesis Testing (if root cause unclear):
   → Generate: hypotheses_to_add
   → When user provides evidence: Evaluate against ALL hypotheses (hypothesis_evidence_links)
   → Update hypothesis.status based on evidence: TESTING → VALIDATED/REFUTED

IMPORTANT: Don't generate hypotheses if root cause is obvious!
"""
    
    elif stage == InvestigationStage.RESOLVING:
        prompt += """
✅ VERIFICATION COMPLETE
✅ ROOT CAUSE IDENTIFIED

FOCUS: SOLUTION (Fixing the Problem)

Root Cause: {case.root_cause_conclusion.root_cause}
Confidence: {case.root_cause_conclusion.confidence_level}

Actions:

1. Propose Solution:
   - MITIGATION path: Quick fix (immediate_action)
   - ROOT_CAUSE path: Comprehensive fix (longterm_fix + immediate_action)
   → Fill: solutions_to_add

2. Guide Implementation:
   - Provide: implementation_steps
   - Provide: commands (specific commands to run)
   - Warn: risks (potential side effects)

3. Track Progress:
   - solution_proposed: Set to True when you propose
   - solution_applied: Set to True when user confirms applied
   - solution_verified: Set to True when you verify it worked

4. Verify Effectiveness:
   - Request: verification evidence (metrics, error rates, etc.)
   - Analyze: Did solution fix the problem?
   - Set: solution_verified if confirmed
"""
    
    # GENERAL INSTRUCTIONS (apply to all stages)
    prompt += """
═══════════════════════════════════════════════════════════
GENERAL INSTRUCTIONS
═══════════════════════════════════════════════════════════

Evidence Handling:

**Create Evidence from objective data only:**
✅ Uploaded files, pasted command output, error messages, stack traces
❌ User saying "I saw X", "I think Y", "Page seems slow"
→ If user describes → Request actual data: "Please provide: [command/file]"

**Three types (system decides, not you):**
1. SYMPTOM - Shows problem exists (error logs, metrics, stack traces)
2. CAUSAL - Tests why problem exists (diagnostic logs, code, config)
3. RESOLUTION - Shows fix worked (logs/metrics after fix)

**Hypothesis evaluation:**
• Symptom evidence → No evaluation (just shows problem exists)
• Causal evidence → Evaluate against ALL hypotheses (tests theories)
• Resolution evidence → No evaluation (just shows fix worked)

When evaluating causal evidence:
- For EACH hypothesis, determine:
  * stance: STRONGLY_SUPPORTS | SUPPORTS | NEUTRAL | CONTRADICTS | STRONGLY_CONTRADICTS | IRRELEVANT
  * reasoning: Why this evidence has this stance for THIS hypothesis
  * completeness: How well this evidence tests THIS hypothesis (0.0-1.0)
- ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses!

**Request format:**
❌ "When did this start?" (forces user to guess)
✅ "Command: journalctl --since='24h' | grep ERROR" (objective data)

**Examples:**
User: "I saw errors" → Request: "Please provide error logs"
User: [Uploads error.log] → Create Evidence (SYMPTOM, no eval)
User: [Uploads session.log showing why] → Create Evidence (CAUSAL, eval vs hypotheses)
User: [Uploads logs after fix] → Create Evidence (RESOLUTION, no eval)

Working Conclusion:
- ALWAYS update with current best understanding
- Include: statement, confidence (0.0-1.0), reasoning
- Reference: supporting_evidence_ids

Milestones:
- Only set to True if you have EVIDENCE (don't guess!)
- You can complete MULTIPLE milestones in ONE turn
- Never set to False (milestones only advance forward)
"""
    
    # Degraded mode handling
    if case.turns_without_progress >= 2:
        prompt += f"""
⚠️ WARNING: No progress for {case.turns_without_progress} turns!

Stall Analysis:
- Did user provide requested data? → Process it
- Did user answer your question? → Use their answer
- Did user ignore your request? → Try DIFFERENT approach
- Is user disengaged? → Offer fallback

Fallback Options (if stuck for 3+ turns):
1. Proceed with best guess (mark confidence as PROBABLE, not VERIFIED)
2. Offer to escalate to human expert
3. Offer to close investigation
4. Try completely different hypothesis category

Fill out: degraded_mode if investigation is truly stuck
"""
    
    prompt += """
═══════════════════════════════════════════════════════════
OUTCOME CLASSIFICATION
═══════════════════════════════════════════════════════════

Choose outcome (what happened THIS turn):

✅ LLM Selects:
- milestone_completed: You completed milestone(s)
- data_provided: User gave you data
- data_requested: You asked user for data
- data_not_provided: You asked for data, user didn't provide
- hypothesis_tested: You validated/refuted hypothesis
- case_resolved: Solution verified, investigation complete
- conversation: Normal Q&A

❌ DON'T Select:
- "blocked": System determines this from patterns (not your call!)

If user didn't provide requested data: Use "data_not_provided"
System will detect blocking patterns automatically (3+ turns → degraded mode)

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON matching InvestigationResponse schema.

ONLY include fields that CHANGE this turn!
- Use null for unchanged fields
- Don't repeat static data
- Be realistic - only fill what user data supports

KEY PRINCIPLE: Be opportunistic! Complete everything you CAN this turn.
"""
    
    return prompt
```

---

### 11.5 Template 3: TERMINAL

**Purpose**: Answer questions, generate documentation

**LLM Output Schema**:
```python
class TerminalResponse(BaseModel):
    agent_response: str
    state_updates: TerminalStateUpdate

class TerminalStateUpdate(BaseModel):
    """Limited updates for terminal cases"""
    documentation_updates: Optional[DocumentationUpdate] = None

class DocumentationUpdate(BaseModel):
    lessons_learned: List[str] = []
    what_went_well: List[str] = []
    what_could_improve: List[str] = []
    preventive_measures: List[str] = []
    monitoring_recommendations: List[str] = []
    documents_to_generate: List[DocumentType] = []
```

**Prompt Structure**:
```
You are FaultMaven.

STATUS: {case.status.upper()} (Terminal - Case Closed)

CASE SUMMARY:
- Problem: {case.problem_verification.symptom_statement if case.problem_verification else "Not investigated"}
- Root Cause: {case.root_cause_conclusion.root_cause if case.root_cause_conclusion else "Not identified"}
- Solution: {case.solutions[0].title if case.solutions else "None"}
- Closure Reason: {case.closure_reason}
- Closed: {format_time_ago(case.closed_at)}

USER MESSAGE:
{user_message}

YOUR TASK:

1. Answer user's question about this closed case
   - Explain what happened
   - Summarize findings
   - Provide documentation if requested

2. Documentation (if user requests):
   - Fill out: documentation_updates
   - Generate: documents (incident report, post-mortem, runbook, etc.)

LIMITATION: You CANNOT update investigation state (case is terminal)
- No milestone changes
- No new evidence
- No new hypotheses
- No new solutions

You can only:
- Answer questions
- Generate documentation
- Provide summaries

OUTPUT FORMAT: JSON matching TerminalResponse schema
```

---

### 11.6 LLM vs System Responsibilities

**Clear Boundary: What LLM Can/Cannot Determine**

#### **LLM DETERMINES (Observable from Turn)**:

| Category | Examples | Why LLM Can Do This |
|----------|----------|---------------------|
| **Content Analysis** | summary, analysis, mechanism | LLM reads and understands content |
| **Milestone Completion** | symptom_verified, root_cause_identified | LLM has evidence to verify |
| **Hypothesis Operations** | generate, status (VALIDATED/REFUTED), likelihood | LLM tests against evidence |
| **Solution Proposals** | immediate_action, implementation_steps, risks | LLM formulates solutions |
| **Confidence Assessment** | root_cause_confidence, verification_confidence | LLM evaluates certainty |
| **Temporal Classification** | temporal_state (ONGOING/HISTORICAL), urgency_level | LLM infers from description |
| **Evidence Linking** | tests_hypothesis_id, stance (SUPPORTS/REFUTES) | LLM knows relationships |
| **Turn Outcomes** | milestone_completed, data_requested, data_not_provided | LLM observes what happened |

#### **SYSTEM DETERMINES (Calculated/Inferred)**:

| Category | Examples | Why LLM Cannot Do This |
|----------|----------|------------------------|
| **Identifiers** | case_id, evidence_id, hypothesis_id | Auto-generated |
| **Timestamps** | created_at, updated_at, collected_at | System clock |
| **Counts** | current_turn, turns_without_progress | Incremented by system |
| **Progress Detection** | progress_made, milestones_completed | Compare before/after state |
| **Path Selection** | path (MITIGATION/ROOT_CAUSE) | Deterministic from matrix |
| **Evidence Category** | category (SYMPTOM/CAUSAL/RESOLUTION) | Inferred from context |
| **Milestone Advancement** | advances_milestones | Calculated from evidence |
| **Degraded Mode** | Should enter? When? | Pattern analysis (3+ turns) |
| **Status Transitions** | INVESTIGATING→RESOLVED | Rule-based triggers |
| **File Metadata** | content_ref (S3 URI), source_type, form | From upload system |

#### **USER ACTIONS (LLM Reports, Doesn't Decide)**:

| Action | LLM Role | System Role |
|--------|----------|-------------|
| **decided_to_investigate** | Reports user's decision | Sets field to True |
| **solution_applied** | Reports user applied solution | Sets milestone |
| **Closes case** | Understands user wants to close | Transitions status |

---

### 11.7 Handling Realistic Expectations

**Principle**: Only ask LLM to determine fields it can realistically infer from available data.

#### **Pattern: Conditional Field Requests**

```python
# In prompt
"""
VERIFICATION SECTION (fill if you have data for each field):

symptom_statement: 
  ✓ Can fill if: User described the problem clearly
  ✗ Cannot fill if: User only said "something wrong" (too vague)
  → If too vague: ASK for specifics in your response

temporal_state:
  ✓ Can fill if: User said "happening now" OR "yesterday"
  ✗ Cannot fill if: User didn't mention timing
  → If unknown: ASK "Is this currently happening or did it happen in the past?"

urgency_level:
  ✓ Can fill if: You know severity + impact + temporal state
  ✗ Cannot fill if: Missing impact data
  → If unknown: ASK about user impact

affected_services:
  ✓ Can fill if: User mentioned specific services/components
  ✗ Cannot fill if: User didn't specify
  → Use empty list, ASK in your response if needed

RULE: If you can't determine a field, use null/empty and ASK user.
Don't guess! Better to request data than make assumptions.
"""
```

#### **Example: Handling Insufficient Data**

```python
# Turn with partial data
User: "API is slow"

LLM Response:
{
  "agent_response": "I understand the API is slow. Let me gather more details:
                     - Which endpoint is slow?
                     - When did this start?
                     - Is it happening right now or did it occur in the past?
                     - How many users are affected?",
  
  "state_updates": {
    "milestones": {
      # Don't set symptom_verified = True yet (insufficient data!)
      "symptom_verified": null
    },
    "verification_updates": {
      # Only fill what we know
      "symptom_statement": "API slowness reported",  # Generic
      # Leave scope, timeline empty - need more data
    },
    "working_conclusion": {
      "statement": "User reports API slowness, specifics unknown",
      "confidence": 0.3,  # Low - insufficient data
      "reasoning": "User description only, no concrete evidence yet"
    }
  }
}
```

---

### 11.8 Problem Statement Confirmation Workflow

**Critical**: Get user to confirm clear problem statement before formal investigation.

#### **Consultation Flow**:

```python
# Turn 1: Initial problem description
User: "Our API has been acting weird lately"

Agent LLM fills:
{
  "agent_response": "I see. Can you describe what 'acting weird' means? 
                     Are you seeing errors, slowness, or something else?",
  "state_updates": {
    # No problem_confirmation yet - too vague
  }
}

# Turn 2: User provides details
User: "It's timing out sometimes, like 10% of requests fail"

Agent LLM fills:
{
  "agent_response": "Got it. Let me confirm my understanding:
                     
                     **Problem**: API intermittently timing out (10% request failure rate)
                     
                     Is this accurate? Any corrections?",
  
  "state_updates": {
    "problem_confirmation": {
      "problem_type": "error",
      "severity_guess": "high"
    },
    "proposed_problem_statement": "API intermittently timing out (10% request failure rate)"
    # Awaiting user confirmation
  }
}

# Turn 3: User confirms
User: "Yes, that's right. Please investigate."

Agent LLM fills:
{
  "agent_response": "Perfect. I'll start formal investigation. First, I need to verify...",
  
  "state_updates": {
    # User confirmed statement AND decided to investigate
    # (detected from user message: "Yes" + "investigate")
  }
}

# System processes:
case.consulting.problem_statement_confirmed = True
case.consulting.decided_to_investigate = True

# System triggers transition:
transition_to_investigating(case)

# Creates ProblemVerification with confirmed statement:
case.problem_verification = ProblemVerification(
    symptom_statement=case.consulting.proposed_problem_statement  # Use confirmed statement
)
```

**Why This Matters**:
- ✅ Ensures alignment (agent and user agree on problem)
- ✅ Prevents investigating wrong problem
- ✅ Creates clean, specific symptom_statement
- ✅ Better user experience (explicit confirmation loop)

---

### 11.10 System Post-Processing

**After LLM returns structured output, system performs**:

```python
async def post_process_llm_output(
    case: Case,
    llm_output: InvestigationResponse
) -> None:
    """
    System applies LLM updates and infers additional fields.
    """
    
    # 1. Apply milestone updates
    apply_milestone_updates(case.progress, llm_output.state_updates.milestones)
    
    # 2. Process evidence
    for evidence_data in llm_output.state_updates.evidence_to_add:
        # LLM provided: summary, analysis, tests_hypothesis_id, stance
        # SYSTEM INFERS:
        category = infer_evidence_category(evidence_data, case)
        advances_milestones = determine_milestone_advancement(evidence_data, case)
        
        evidence = Evidence(
            **evidence_data.dict(),
            category=category,                    # ← System-inferred
            advances_milestones=advances_milestones,  # ← System-inferred
            evidence_id=generate_id(),            # ← System-generated
            collected_at=datetime.now(),          # ← System-generated
            collected_by=case.user_id,            # ← System-generated
            collected_at_turn=case.current_turn,  # ← System-generated
            content_ref=upload_to_s3(),           # ← System-managed
            source_type=detect_source_type(),     # ← System-detected
            form=detect_form()                    # ← System-detected
        )
        
        case.evidence.append(evidence)
    
    # 3. Create hypothesis-evidence links (many-to-many evaluation)
    for link_data in llm_output.state_updates.hypothesis_evidence_links:
        hypothesis = case.hypotheses.get(link_data.hypothesis_id)
        if hypothesis:
            # OPTIMIZATION: Skip creating links with no investigative value
            # Don't create link if:
            # 1. IRRELEVANT - evidence doesn't relate to hypothesis
            # 2. NEUTRAL with low completeness (<0.3) - evidence doesn't meaningfully test hypothesis
            if link_data.stance == EvidenceStance.IRRELEVANT:
                continue  # No relationship to store
            
            if link_data.stance == EvidenceStance.NEUTRAL and link_data.completeness < 0.3:
                continue  # Doesn't meaningfully test hypothesis
            
            # Create link only if it provides investigative value
            hypothesis.evidence_links[link_data.evidence_id] = HypothesisEvidenceLink(
                hypothesis_id=link_data.hypothesis_id,
                evidence_id=link_data.evidence_id,
                stance=link_data.stance,
                reasoning=link_data.reasoning,
                completeness=link_data.completeness,
                analyzed_at=datetime.now(timezone.utc)
            )
    
    # 4. Determine path selection (if verification just completed)
    if (case.progress.verification_complete and 
        case.path_selection is None and
        llm_output.state_updates.verification_updates):
        
        # SYSTEM DETERMINES path (not LLM!)
        path_selection = determine_investigation_path(case.problem_verification)
        case.path_selection = path_selection
    
    # 5. Detect progress
    progress_made = detect_progress(case, llm_output)
    
    # 6. Update turns_without_progress
    if progress_made:
        case.turns_without_progress = 0
    else:
        case.turns_without_progress += 1
    
    # 7. Check degraded mode trigger
    if case.turns_without_progress >= 3 and case.degraded_mode is None:
        enter_degraded_mode(case, DegradedModeType.NO_PROGRESS)
    
    # 8. Check status transitions
    check_automatic_status_transitions(case)
```

#### **Evidence Categorization Logic**:

```python
def infer_evidence_category(evidence_data: EvidenceToAdd, case: Case) -> EvidenceCategory:
    """
    SYSTEM infers category from investigation context.
    LLM doesn't specify this!
    """
    
    # If testing specific hypothesis → CAUSAL
    if evidence_data.tests_hypothesis_id is not None:
        return EvidenceCategory.CAUSAL_EVIDENCE
    
    # If verification incomplete → SYMPTOM
    if not case.progress.verification_complete:
        return EvidenceCategory.SYMPTOM_EVIDENCE
    
    # If solution already proposed → RESOLUTION
    if case.progress.solution_proposed:
        return EvidenceCategory.RESOLUTION_EVIDENCE
    
    # Default: OTHER
    return EvidenceCategory.OTHER
```

#### **Path Selection Logic**:

```python
def determine_investigation_path(pv: ProblemVerification) -> PathSelection:
    """
    SYSTEM determines path from matrix.
    LLM provides inputs (temporal_state, urgency_level) only.
    """
    
    temporal = pv.temporal_state
    urgency = pv.urgency_level
    
    # Deterministic rules
    if temporal == TemporalState.ONGOING and urgency in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
        return PathSelection(
            path=InvestigationPath.MITIGATION,
            auto_selected=True,
            rationale=f"Ongoing {urgency.value} issue requires immediate mitigation",
            alternate_path=InvestigationPath.ROOT_CAUSE,
            temporal_state=temporal,
            urgency_level=urgency
        )
    
    # ... other matrix rules
    
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale="Ambiguous case - user should choose path"
    )
```

---

### 11.11 Key Prompt Engineering Principles

#### **1. Incremental Updates Only**

```python
# DON'T: Send entire case state every turn
# DO: Send only what changed

{
  "state_updates": {
    "milestones": {
      "symptom_verified": True,     # Changed!
      "scope_assessed": null,        # No change
      "timeline_established": null   # No change
    },
    "evidence_to_add": [...],        # Only NEW evidence
    "working_conclusion": {...}      # Updated conclusion
  }
}
```

#### **2. Show What's Already Known**

```
VERIFIED FACTS (Don't re-verify):
✅ Symptom: API timeout errors
✅ Timeline: Started 14:23 UTC
✅ Root Cause: NullPointerException at line 42

Don't ask about these again!
```

#### **3. Adaptive Instructions**

```python
# Stage-specific emphasis
if stage == UNDERSTANDING:
    "FOCUS on verification"
elif stage == DIAGNOSING:
    "FOCUS on root cause"
elif stage == RESOLVING:
    "FOCUS on solution"

# But always: "You CAN jump ahead if data allows!"
```

#### **4. Fallback When Stuck**

```python
if turns_without_progress >= 3:
    """
    STUCK! Offer fallbacks:
    1. Proceed with best guess (lower confidence)
    2. Escalate
    3. Close
    
    Fill out: degraded_mode
    """
```

---

### 11.12 Complete Example: One Turn with All Responsibilities

**Turn N Processing**:

```python
# INPUT
user_message = "Here's the error log [upload: error.log]"
case = Case(status=INVESTIGATING, current_turn=5, ...)

# STEP 1: Agent builds prompt
prompt = build_investigating_prompt(case, user_message)
# Includes:
# - Current milestones (what's done)
# - Pending requests (what's asked)
# - Recent turns (conversation context)
# - Stage-specific instructions
# - Request follow-up guidance

# STEP 2: LLM processes
llm_output = await llm.generate(prompt, schema=InvestigationResponse)
# LLM returns:
{
  "agent_response": "Great! The error log shows NullPointerException at line 42...",
  "state_updates": {
    "milestones": {
      "symptom_verified": True,
      "timeline_established": True,
      "root_cause_identified": True
    },
    "evidence_to_add": [{
      "summary": "NullPointerException at UserService.java:42",
      "analysis": "Missing null check causes crash on null user object",
      # LLM does NOT specify category - system will infer
    }],
    "root_cause_conclusion": {
      "root_cause": "Missing null check at line 42",
      "mechanism": "When user object is null, code crashes",
      "confidence_score": 0.95
    },
    "solutions_to_add": [{
      "title": "Rollback to v2.1.2",
      "solution_type": "rollback",
      "immediate_action": "kubectl rollout undo deployment/api"
    }],
    "outcome": "milestone_completed"  # LLM observes: milestones completed
  }
}

# STEP 3: System post-processes
# System infers:
evidence.category = EvidenceCategory.SYMPTOM_EVIDENCE  # ← No symptom verified yet
evidence.advances_milestones = ["symptom_verified", "root_cause_identified"]
evidence.evidence_id = "ev_a1b2c3d4e5f6"
evidence.collected_at = datetime.now()
# ... all system-managed fields

# System detects:
progress_made = True  # 3 milestones completed
turns_without_progress = 0  # Reset

# System records:
turn = TurnProgress(
  turn_number=5,
  milestones_completed=["symptom_verified", "timeline_established", "root_cause_identified"],
  evidence_added=["ev_a1b2c3d4e5f6"],
  progress_made=True,
  outcome="milestone_completed"  # From LLM
)

# STEP 4: Check transitions
# None yet - solution not applied/verified

# OUTPUT
return llm_output.agent_response  # Natural language to user
```

---

### 11.13 Prompt Template Summary

| Template | Status | LLM Provides | System Infers | Key Feature |
|----------|--------|--------------|---------------|-------------|
| **CONSULTING** | CONSULTING | problem_type, severity_guess, proposed_problem_statement | None (simple) | Problem statement confirmation |
| **INVESTIGATING** | INVESTIGATING | Milestones, evidence analysis, hypotheses, solutions, conclusions | Evidence category, milestone advancement, path selection, progress detection | Adaptive instructions by stage |
| **TERMINAL** | RESOLVED/CLOSED | Documentation updates only | None | Read-only access |

**Total Templates**: 3

**Template #2 (INVESTIGATING)** handles 3 stages with adaptive instructions:
- Same schema (full InvestigationStateUpdate)
- Instructions emphasize relevant sections
- LLM can fill any section if data allows (enables one-turn resolution)

---

**Document Version**: 2.0  
**Last Updated**: 2025-11-04  
**Status**: Production Specification  
**Authors**: System Architecture Team