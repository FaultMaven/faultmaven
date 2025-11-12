# FaultMaven Case Model Design v2.0

## Executive Summary

This document provides complete data structure definitions for FaultMaven's milestone-based investigation system. All models align with the Investigation Architecture Specification v2.0.

**Purpose**: Define every model, field, relationship, and validation rule needed to implement the investigation system.

**Scope**: Data structures only. For workflow behavior and investigation logic, see Investigation Architecture Specification.

---

## Table of Contents

1. [Core Case Model](#1-core-case-model)
2. [Status & Lifecycle Models](#2-status--lifecycle-models)
3. [Investigation Progress Models](#3-investigation-progress-models)
4. [Problem Context Models](#4-problem-context-models)
5. [Evidence Models](#5-evidence-models)
6. [Hypothesis Models](#6-hypothesis-models)
7. [Solution Models](#7-solution-models)
8. [Turn Tracking Models](#8-turn-tracking-models)
9. [Path Selection Models](#9-path-selection-models)
10. [Conclusion Models](#10-conclusion-models)
11. [Special State Models](#11-special-state-models)
12. [Documentation Models](#12-documentation-models)
13. [Validation Rules](#13-validation-rules)
14. [Database Schema](#14-database-schema)
15. [Implementation Guide](#15-implementation-guide)

---

## 1. Core Case Model

### 1.1 Case

```python
class Case(BaseModel):
    """
    Root case entity.
    Represents one complete troubleshooting investigation.
    """
    
    # ============================================================
    # Core Identity
    # ============================================================
    case_id: str = Field(
        default_factory=lambda: f"case_{uuid4().hex[:12]}",
        description="Unique case identifier",
        min_length=17,
        max_length=17,
        regex=r"^case_[a-f0-9]{12}$"
    )
    
    user_id: str = Field(
        description="User who created the case",
        min_length=1,
        max_length=255
    )
    
    organization_id: str = Field(
        description="Organization this case belongs to",
        min_length=1,
        max_length=255
    )
    
    title: str = Field(
        description="Short case title for list views and headers (e.g., 'API Performance Issue')",
        min_length=1,
        max_length=200
    )
    
    description: str = Field(
        default="",
        description="""
        Confirmed problem description - canonical, user-facing, displayed prominently in UI.
        
        Lifecycle:
        1. Empty initially during CONSULTING (while agent formalizes problem)
        2. Set when user confirms proposed_problem_statement and decides to investigate
        3. Immutable after status becomes INVESTIGATING (provides stable reference)
        4. Used for UI display, search, and documentation
        
        Example: "API experiencing slowness with 30% of requests taking >5s response time 
                  across all US regions, started 2 hours ago coinciding with v2.1.3 deployment"
        """,
        max_length=2000
    )
    
    # ============================================================
    # Status (PRIMARY - User-Facing Lifecycle)
    # ============================================================
    status: CaseStatus = Field(
        default=CaseStatus.CONSULTING,
        description="Current lifecycle status"
    )
    
    status_history: List[CaseStatusTransition] = Field(
        default_factory=list,
        description="Complete history of status changes"
    )
    
    closure_reason: Optional[str] = Field(
        default=None,
        description="Why case was closed: resolved | abandoned | escalated | consulting_only | duplicate | other",
        max_length=100
    )
    
    # ============================================================
    # Investigation Progress (SECONDARY - Internal Detail)
    # ============================================================
    progress: InvestigationProgress = Field(
        default_factory=InvestigationProgress,
        description="Milestone-based progress tracking"
    )
    
    # ============================================================
    # Turn Tracking
    # ============================================================
    current_turn: int = Field(
        default=0,
        ge=0,
        description="Current turn number (increments with each user-agent exchange)"
    )
    
    turns_without_progress: int = Field(
        default=0,
        ge=0,
        description="Consecutive turns with no milestone advancement (for stuck detection)"
    )
    
    turn_history: List[TurnProgress] = Field(
        default_factory=list,
        description="Complete history of all turns"
    )
    
    # ============================================================
    # Investigation Path & Strategy
    # ============================================================
    path_selection: Optional[PathSelection] = Field(
        default=None,
        description="Selected investigation path (MITIGATION vs ROOT_CAUSE)"
    )
    
    investigation_strategy: InvestigationStrategy = Field(
        default=InvestigationStrategy.POST_MORTEM,
        description="Investigation approach: ACTIVE_INCIDENT (speed) vs POST_MORTEM (thoroughness)"
    )
    
    # ============================================================
    # Problem Context
    # ============================================================
    consulting: ConsultingData = Field(
        default_factory=ConsultingData,
        description="Pre-investigation CONSULTING status data"
    )
    
    # NOTE: decided_to_investigate is in ConsultingData, not here (avoid duplication)
    
    problem_verification: Optional[ProblemVerification] = Field(
        default=None,
        description="Consolidated verification data (symptom, scope, timeline, changes)"
    )
    
    # ============================================================
    # Investigation Data
    # ============================================================
    uploaded_files: List[UploadedFile] = Field(
        default_factory=list,
        description="""
        All files uploaded to this case (raw file metadata).

        Files can be uploaded at ANY phase (CONSULTING or INVESTIGATING).
        Evidence is DERIVED from uploaded files after analysis during INVESTIGATING phase.

        Difference from evidence:
        - uploaded_files: Raw file metadata (file_id, filename, size, upload time)
        - evidence: Investigation data linked to hypotheses (only in INVESTIGATING phase)
        """
    )

    evidence: List[Evidence] = Field(
        default_factory=list,
        description="All evidence collected during investigation"
    )

    hypotheses: Dict[str, Hypothesis] = Field(
        default_factory=dict,
        description="Generated hypotheses (key = hypothesis_id)"
    )

    solutions: List[Solution] = Field(
        default_factory=list,
        description="Proposed and applied solutions"
    )
    
    # ============================================================
    # Cross-Cutting State
    # ============================================================
    working_conclusion: Optional[WorkingConclusion] = Field(
        default=None,
        description="Agent's current best understanding (updated iteratively)"
    )
    
    root_cause_conclusion: Optional[RootCauseConclusion] = Field(
        default=None,
        description="Final root cause determination"
    )
    
    # ============================================================
    # Special States
    # ============================================================
    degraded_mode: Optional[DegradedMode] = Field(
        default=None,
        description="Investigation is stuck or blocked"
    )
    
    escalation_state: Optional[EscalationState] = Field(
        default=None,
        description="Escalated to human expert"
    )
    
    # ============================================================
    # Documentation
    # ============================================================
    documentation: DocumentationData = Field(
        default_factory=DocumentationData,
        description="Generated documentation and lessons learned"
    )
    
    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When case was created"
    )
    
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last modification timestamp"
    )
    
    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Most recent user/agent interaction (for 'updated Xm ago' display)"
    )
    
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
        """
        Computed investigation stage (only when INVESTIGATING).
        Returns: UNDERSTANDING | DIAGNOSING | RESOLVING | None
        """
        if self.status != CaseStatus.INVESTIGATING:
            return None
        return self.progress.current_stage
    
    @property
    def is_stuck(self) -> bool:
        """
        Detect if investigation is blocked.
        Returns True if 3+ consecutive turns without progress.
        """
        return self.turns_without_progress >= 3
    
    @property
    def is_terminal(self) -> bool:
        """
        Check if case is in terminal state.
        Terminal states: RESOLVED, CLOSED (no further transitions).
        """
        return self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
    
    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        """
        Time from case creation to terminal state.
        Returns None if case not yet closed.
        """
        if self.closed_at:
            return self.closed_at - self.created_at
        return None
    
    @property
    def evidence_count_by_category(self) -> Dict[str, int]:
        """Count evidence by category for analytics"""
        counts = {}
        for ev in self.evidence:
            cat = ev.category.value
            counts[cat] = counts.get(cat, 0) + 1
        return counts
    
    @property
    def active_hypotheses(self) -> List[Hypothesis]:
        """Get hypotheses currently being tested"""
        return [
            h for h in self.hypotheses.values()
            if h.status == HypothesisStatus.ACTIVE
        ]
    
    @property
    def validated_hypotheses(self) -> List[Hypothesis]:
        """Get validated hypotheses (found root cause)"""
        return [
            h for h in self.hypotheses.values()
            if h.status == HypothesisStatus.VALIDATED
        ]
    
    @property
    def warnings(self) -> List[Dict[str, Any]]:
        """
        Get active warnings for UI display.
        
        Returns list of warning dictionaries with type, severity, message.
        Used by frontend to display alert banners.
        """
        warnings = []
        
        # Warning: Investigation stuck
        if self.is_stuck:
            warnings.append({
                "type": "stuck",
                "severity": "warning",
                "message": f"No progress for {self.turns_without_progress} consecutive turns",
                "action": "Consider providing more data, escalating, or closing case"
            })
        
        # Error: Degraded mode active
        if self.degraded_mode and self.degraded_mode.is_active:
            warnings.append({
                "type": "degraded_mode",
                "severity": "error",
                "message": f"Investigation blocked: {self.degraded_mode.reason}",
                "mode_type": self.degraded_mode.mode_type.value,
                "action": self.degraded_mode.fallback_offered or "Escalate or close case"
            })
        
        # Info: Escalation active
        if self.escalation_state and self.escalation_state.is_active:
            warnings.append({
                "type": "escalation",
                "severity": "info",
                "message": f"Escalated to {self.escalation_state.escalated_to or 'expert'}",
                "escalated_at": self.escalation_state.escalated_at.isoformat()
            })
        
        # Warning: Terminal state but no documentation
        if self.is_terminal and len(self.documentation.documents_generated) == 0:
            warnings.append({
                "type": "no_documentation",
                "severity": "info",
                "message": "Case closed but no documentation generated",
                "action": "Generate post-mortem or runbook"
            })
        
        return warnings
    
    # ============================================================
    # Validation
    # ============================================================
    @validator('title')
    def title_not_empty(cls, v):
        """Ensure title is not just whitespace"""
        if not v or not v.strip():
            raise ValueError("Title cannot be empty")
        return v.strip()
    
    @validator('description')
    def description_valid(cls, v):
        """Ensure description is meaningful if not empty"""
        if v and not v.strip():
            raise ValueError("Description cannot be only whitespace")
        return v.strip() if v else ""
    
    @root_validator
    def description_required_when_investigating(cls, values):
        """Ensure description is set before transitioning to INVESTIGATING"""
        status = values.get('status')
        description = values.get('description', '').strip()
        
        # INVESTIGATING requires confirmed problem description
        if status == CaseStatus.INVESTIGATING and not description:
            raise ValueError(
                "description must be set (from confirmed proposed_problem_statement) "
                "before transitioning to INVESTIGATING status"
            )
        
        return values
    
    @validator('closure_reason')
    def valid_closure_reason(cls, v):
        """Validate closure reason is from allowed set"""
        if v is not None:
            allowed = ["resolved", "abandoned", "escalated", "consulting_only", "duplicate", "other"]
            if v not in allowed:
                raise ValueError(f"closure_reason must be one of: {allowed}")
        return v
    
    @validator('status_history')
    def status_history_ordered(cls, v):
        """Ensure status history is chronologically ordered"""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].triggered_at > v[i+1].triggered_at:
                    raise ValueError("Status history must be chronologically ordered")
        return v
    
    @validator('turn_history')
    def turn_history_sequential(cls, v):
        """Ensure turn numbers are sequential"""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].turn_number + 1 != v[i+1].turn_number:
                    raise ValueError("Turn numbers must be sequential")
        return v
    
    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        validate_assignment = True  # Validate on field assignment
        use_enum_values = False     # Keep enum instances
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            timedelta: lambda v: v.total_seconds()
        }
```

---

## 2. Status & Lifecycle Models

### 2.1 CaseStatus

```python
class CaseStatus(str, Enum):
    """
    Case lifecycle status.
    
    Lifecycle Flow:
      CONSULTING → INVESTIGATING → RESOLVED (terminal)
                                 → CLOSED (terminal)
               ↘ CLOSED (terminal)
    
    Terminal States: RESOLVED, CLOSED (no further transitions)
    """
    
    CONSULTING = "consulting"
    """
    Pre-investigation exploration.
    
    Characteristics:
    - User asking questions
    - Agent providing quick guidance
    - No formal investigation commitment
    - May transition to INVESTIGATING or CLOSED
    
    Typical Duration: Minutes to hours
    """
    
    INVESTIGATING = "investigating"
    """
    Active formal investigation.
    
    Characteristics:
    - Working through milestones
    - Gathering evidence
    - Testing hypotheses
    - Applying solutions
    - May transition to RESOLVED or CLOSED
    
    Typical Duration: Hours to days
    """
    
    RESOLVED = "resolved"
    """
    TERMINAL STATE: Case closed WITH solution.
    
    Characteristics:
    - Problem was fixed
    - Solution verified
    - closure_reason = "resolved"
    - No further transitions allowed
    
    State: Terminal (permanent)
    """
    
    CLOSED = "closed"
    """
    TERMINAL STATE: Case closed WITHOUT solution.
    
    Characteristics:
    - Investigation abandoned/escalated
    - OR consulting-only (no investigation)
    - closure_reason = "abandoned" | "escalated" | "consulting_only" | "duplicate" | "other"
    - No further transitions allowed
    
    State: Terminal (permanent)
    """
    
    @property
    def is_terminal(self) -> bool:
        """Check if this status is terminal"""
        return self in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
    
    @property
    def is_active(self) -> bool:
        """Check if case is active (not terminal)"""
        return self in [CaseStatus.CONSULTING, CaseStatus.INVESTIGATING]
```

### 2.2 CaseStatusTransition

```python
class CaseStatusTransition(BaseModel):
    """
    Record of one status change.
    Provides audit trail for case lifecycle.
    """
    
    from_status: CaseStatus = Field(
        description="Status before transition"
    )
    
    to_status: CaseStatus = Field(
        description="Status after transition"
    )
    
    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When transition occurred"
    )
    
    triggered_by: str = Field(
        description="Who triggered: user_id or 'system' for automatic transitions"
    )
    
    reason: str = Field(
        description="Human-readable reason for transition",
        max_length=500
    )
    
    # ============================================================
    # Validation
    # ============================================================
    @validator('to_status')
    def validate_transition(cls, v, values):
        """Ensure transition is valid"""
        from_status = values.get('from_status')
        if from_status:
            if not is_valid_transition(from_status, v):
                raise ValueError(f"Invalid transition: {from_status} → {v}")
        return v
    
    class Config:
        frozen = True  # Immutable once created

def is_valid_transition(from_status: CaseStatus, to_status: CaseStatus) -> bool:
    """
    Validate status transition.
    
    Valid Transitions:
    - CONSULTING → INVESTIGATING
    - CONSULTING → CLOSED
    - INVESTIGATING → RESOLVED
    - INVESTIGATING → CLOSED
    
    Invalid:
    - RESOLVED → * (terminal)
    - CLOSED → * (terminal)
    - INVESTIGATING → CONSULTING (no backward)
    """
    valid_transitions = {
        CaseStatus.CONSULTING: [CaseStatus.INVESTIGATING, CaseStatus.CLOSED],
        CaseStatus.INVESTIGATING: [CaseStatus.RESOLVED, CaseStatus.CLOSED],
        CaseStatus.RESOLVED: [],  # Terminal
        CaseStatus.CLOSED: []     # Terminal
    }
    
    return to_status in valid_transitions.get(from_status, [])
```

### 2.3 InvestigationStrategy

```python
class InvestigationStrategy(str, Enum):
    """
    Investigation approach mode.
    Affects decision thresholds, workflow behavior, and agent prompts.
    """
    
    ACTIVE_INCIDENT = "active_incident"
    """
    Service is down NOW. Priority: Speed over completeness.
    
    Characteristics:
    - Accept hypothesis with TESTING status for quick mitigation
    - Skip to solution phase even without complete root cause analysis
    - Escalate after 3 failed attempts
    - Evidence threshold: SUPPORTS is sufficient (not STRONGLY_SUPPORTS)
    - Time pressure: Minutes matter
    
    Use when:
    - temporal_state = ONGOING
    - urgency_level = CRITICAL or HIGH
    - User needs immediate restoration
    """
    
    POST_MORTEM = "post_mortem"
    """
    Historical analysis. Priority: Thorough understanding.
    
    Characteristics:
    - Require VALIDATED hypothesis before root cause conclusion
    - Complete all milestones systematically
    - Escalate after hypothesis space exhausted (not time-based)
    - Evidence threshold: STRONGLY_SUPPORTS required
    - Time pressure: Days acceptable
    
    Use when:
    - temporal_state = HISTORICAL or INTERMITTENT (resolved)
    - No immediate service impact
    - Learning/prevention goal
    """
```

---

## 3. Investigation Progress Models

### 3.1 InvestigationProgress

```python
class InvestigationProgress(BaseModel):
    """
    Milestone-based progress tracking.
    
    Philosophy: Track what's completed, not what phase we're in.
    Agent completes milestones opportunistically based on data availability.
    """
    
    # ============================================================
    # Verification Milestones
    # ============================================================
    symptom_verified: bool = Field(
        default=False,
        description="Symptom confirmed with concrete evidence (logs, metrics, user reports)"
    )
    
    scope_assessed: bool = Field(
        default=False,
        description="Scope determined: affected users/services/regions, blast radius"
    )
    
    timeline_established: bool = Field(
        default=False,
        description="Timeline determined: when problem started, when noticed, duration"
    )
    
    changes_identified: bool = Field(
        default=False,
        description="Recent changes identified: deployments, configs, scaling events"
    )
    
    # NOTE: temporal_state moved to ProblemVerification (where it logically belongs)
    # It's determined during verification, not a milestone itself
    
    # ============================================================
    # Investigation Milestones
    # ============================================================
    root_cause_identified: bool = Field(
        default=False,
        description="Root cause determined (directly or via hypothesis validation)"
    )
    
    root_cause_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in root cause identification (0.0 = unknown, 1.0 = certain)"
    )
    
    root_cause_method: Optional[str] = Field(
        default=None,
        description="How root cause was identified: direct_analysis | hypothesis_validation | correlation | other"
    )
    
    # ============================================================
    # Resolution Milestones
    # ============================================================
    solution_proposed: bool = Field(
        default=False,
        description="Solution or mitigation has been proposed"
    )

    solution_applied: bool = Field(
        default=False,
        description="Solution has been applied by user"
    )

    solution_verified: bool = Field(
        default=False,
        description="Solution effectiveness verified (error rate decreased, metrics improved)"
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

        When True: Agent should return to stage 2 (hypothesis formulation) for full RCA
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
        Compute investigation stage from completed milestones.
        For UI display, NOT workflow control.

        Maps milestones to stages:
        - SYMPTOM_VERIFICATION (stage 1): Verification in progress
        - HYPOTHESIS_FORMULATION (stage 2): Hypotheses being generated
        - HYPOTHESIS_VALIDATION (stage 3): Testing hypotheses for root cause
        - SOLUTION (stage 4): Solution work (proposal, application, verification)

        Note: This is a simplified mapping. The actual stage may differ based on path:
        - MITIGATION_FIRST path may be in stage 4 (mitigation) before stage 2 (RCA)
        - Tracking this requires additional path context beyond just milestones
        """
        # SOLUTION (Stage 4): Any solution work
        if (self.solution_proposed or
            self.solution_applied or
            self.solution_verified):
            return InvestigationStage.SOLUTION

        # HYPOTHESIS_VALIDATION (Stage 3): Root cause identified or being validated
        # (If root_cause_identified=True, we're past validation, but haven't proposed solution yet)
        if self.root_cause_identified:
            return InvestigationStage.HYPOTHESIS_VALIDATION

        # HYPOTHESIS_FORMULATION (Stage 2): Symptom verified, working on "why"
        # (This assumes hypotheses are being formulated; in reality, this might be stage 3)
        if self.symptom_verified:
            return InvestigationStage.HYPOTHESIS_FORMULATION

        # SYMPTOM_VERIFICATION (Stage 1): Initial verification
        return InvestigationStage.SYMPTOM_VERIFICATION
    
    @property
    def verification_complete(self) -> bool:
        """Check if all verification milestones completed"""
        return (
            self.symptom_verified and
            self.scope_assessed and
            self.timeline_established and
            self.changes_identified
        )
    
    @property
    def investigation_complete(self) -> bool:
        """Check if investigation milestones completed"""
        return self.root_cause_identified
    
    @property
    def resolution_complete(self) -> bool:
        """Check if resolution milestones completed"""
        return (
            self.solution_proposed and
            self.solution_applied and
            self.solution_verified
        )
    
    @property
    def completion_percentage(self) -> float:
        """
        Overall progress percentage for UI display.
        Returns: 0.0 to 1.0
        """
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
        completed = sum(milestones)
        total = len(milestones)
        return completed / total if total > 0 else 0.0
    
    @property
    def completed_milestones(self) -> List[str]:
        """Get list of completed milestone names"""
        milestone_map = {
            'symptom_verified': self.symptom_verified,
            'scope_assessed': self.scope_assessed,
            'timeline_established': self.timeline_established,
            'changes_identified': self.changes_identified,
            'root_cause_identified': self.root_cause_identified,
            'solution_proposed': self.solution_proposed,
            'solution_applied': self.solution_applied,
            'solution_verified': self.solution_verified,
            'mitigation_applied': self.mitigation_applied,
        }
        return [name for name, completed in milestone_map.items() if completed]

    @property
    def pending_milestones(self) -> List[str]:
        """Get list of pending milestone names"""
        milestone_map = {
            'symptom_verified': self.symptom_verified,
            'scope_assessed': self.scope_assessed,
            'timeline_established': self.timeline_established,
            'changes_identified': self.changes_identified,
            'root_cause_identified': self.root_cause_identified,
            'solution_proposed': self.solution_proposed,
            'solution_applied': self.solution_applied,
            'solution_verified': self.solution_verified,
            'mitigation_applied': self.mitigation_applied,
        }
        return [name for name, completed in milestone_map.items() if not completed]
    
    # ============================================================
    # Validation
    # ============================================================
    @validator('root_cause_method')
    def valid_root_cause_method(cls, v):
        """Validate root cause method"""
        if v is not None:
            allowed = ["direct_analysis", "hypothesis_validation", "correlation", "other"]
            if v not in allowed:
                raise ValueError(f"root_cause_method must be one of: {allowed}")
        return v
    
    @root_validator
    def root_cause_consistency(cls, values):
        """Ensure root cause fields are consistent"""
        identified = values.get('root_cause_identified', False)
        confidence = values.get('root_cause_confidence', 0.0)
        method = values.get('root_cause_method')
        
        if identified:
            if confidence == 0.0:
                raise ValueError("root_cause_confidence must be > 0 when root_cause_identified=True")
            if method is None:
                raise ValueError("root_cause_method must be set when root_cause_identified=True")
        
        return values
    
    @root_validator
    def solution_ordering(cls, values):
        """Ensure solutions are applied in order"""
        proposed = values.get('solution_proposed', False)
        applied = values.get('solution_applied', False)
        verified = values.get('solution_verified', False)
        
        if applied and not proposed:
            raise ValueError("Cannot apply solution without proposing first")
        
        if verified and not applied:
            raise ValueError("Cannot verify solution without applying first")
        
        return values
```

### 3.2 InvestigationStage

```python
class InvestigationStage(str, Enum):
    """
    Investigation stage within INVESTIGATING phase (4 stages).

    Purpose: User-facing progress label computed from completed milestones.
    NOT used for workflow control - milestones drive advancement opportunistically.
    Only relevant when case status = INVESTIGATING.

    Stage Progression (Path-Dependent):
    - MITIGATION_FIRST: 1 → 4 → 2 → 3 → 4 (quick mitigation, then return for RCA)
    - ROOT_CAUSE: 1 → 2 → 3 → 4 (traditional RCA)
    """

    SYMPTOM_VERIFICATION = "symptom_verification"
    """
    Stage 1: Symptom verification (where and when).

    Focus: Understanding what's happening and when it started
    Milestones: symptom_verified, scope_assessed, timeline_established, changes_identified

    Agent Actions:
    - Confirming symptom with evidence (logs, metrics, user reports)
    - Assessing scope and impact (affected users/services/regions)
    - Establishing timeline (when started, when noticed, duration)
    - Identifying recent changes (deployments, configs, scaling events)
    - Determining temporal state (ONGOING vs HISTORICAL)
    - Assessing urgency level (CRITICAL/HIGH/MEDIUM/LOW)

    Path Selection: Urgency + temporal state determines MITIGATION_FIRST vs ROOT_CAUSE path
    """

    HYPOTHESIS_FORMULATION = "hypothesis_formulation"
    """
    Stage 2: Hypotheses formulation (why).

    Focus: Generating theories about what caused the problem
    Prerequisites: Symptom verified (stage 1 complete)

    Agent Actions:
    - Analyzing evidence patterns and correlations
    - Generating hypotheses (opportunistic from strong clues, or systematic when unclear)
    - Categorizing hypotheses (CODE/CONFIG/ENVIRONMENT/NETWORK/DATA/etc)
    - Prioritizing hypotheses by likelihood

    Hypotheses are Optional: Agent may identify root cause directly from evidence without hypotheses.
    When root cause is unclear, hypotheses enable systematic exploration.

    Note: In MITIGATION_FIRST path, this stage occurs AFTER initial mitigation (stage 4)
    """

    HYPOTHESIS_VALIDATION = "hypothesis_validation"
    """
    Stage 3: Hypothesis validation (why really).

    Focus: Testing theories to identify root cause with confidence
    Prerequisites: Hypotheses generated (stage 2 complete)
    Milestone: root_cause_identified

    Agent Actions:
    - Requesting diagnostic data to test specific hypotheses
    - Analyzing evidence against ALL active hypotheses
    - Evaluating evidence stance (STRONGLY_SUPPORTS/SUPPORTS/CONTRADICTS/STRONGLY_CONTRADICTS)
    - Validating or refuting hypotheses based on evidence
    - Increasing/decreasing hypothesis likelihood based on evidence

    Outcome: Root cause identified with confidence level (VERIFIED/CONFIDENT/PROBABLE/SPECULATION)

    Note: In MITIGATION_FIRST path, this provides comprehensive RCA after initial mitigation
    """

    SOLUTION = "solution"
    """
    Stage 4: Solution (how).

    Focus: Applying fix to resolve the problem
    Prerequisites (Path-Dependent):
    - MITIGATION_FIRST path: Symptom verified (stage 1) - correlation-based quick fix
    - ROOT_CAUSE path: Root cause identified (stage 3) - evidence-based permanent fix
    Milestones: solution_proposed, solution_applied, solution_verified, mitigation_applied

    Agent Actions:
    - Proposing solutions (quick mitigation or permanent fix based on path)
    - Providing implementation steps and commands
    - Guiding user through application
    - Verifying effectiveness with before/after evidence

    Path-Specific Behavior:
    - MITIGATION_FIRST: After applying quick mitigation, returns to stage 2 for full RCA
    - ROOT_CAUSE: After permanent solution verified, case transitions to RESOLVED

    Solution Types: ROLLBACK, CONFIG_CHANGE, RESTART, SCALING, CODE_FIX, WORKAROUND, etc.
    """
```

### 3.3 TemporalState

```python
class TemporalState(str, Enum):
    """
    Problem temporal classification.
    Used for investigation path routing.
    """
    
    ONGOING = "ongoing"
    """
    Problem is currently happening.
    
    Characteristics:
    - Active user impact
    - Real-time symptoms
    - Urgency to mitigate
    
    Routing: Likely MITIGATION path if high urgency
    """
    
    HISTORICAL = "historical"
    """
    Problem occurred in the past.
    
    Characteristics:
    - No current impact
    - Post-mortem investigation
    - Can take time for thorough RCA
    
    Routing: Likely ROOT_CAUSE path
    """
```

---

## 4. Problem Context Models

### 4.1 ConsultingData

```python
class ConsultingData(BaseModel):
    """
    Pre-investigation CONSULTING status data.
    Captures early problem exploration before formal investigation commitment.
    """
    
    # NOTE: initial_description REMOVED (v2.0)
    # Reason: Violates LLM/System-only principle (accumulated raw user input)
    # Instead: System provides conversation history in prompt context,
    #          LLM creates proposed_problem_statement directly (formalized version)
    
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
        Agent's formalized problem statement (clear, specific, actionable) - ITERATIVE REFINEMENT pattern.
        
        UI Display:
        - When None: Display "To be defined" or blank (no problem detected yet)
        - When set: Display the statement text
        
        Lifecycle:
        1. LLM creates initial formalization from conversation context
        2. LLM can UPDATE iteratively based on user corrections/refinements
        3. Becomes IMMUTABLE once problem_statement_confirmed = True
        4. Copied to case.description when investigation starts
        
        Refinement workflow (user confirms without reservation):
        Turn 3: LLM creates "API experiencing slowness"
                Agent asks: "Is that accurate?"
        Turn 4: User: "Not quite - 30% failure rate" 
                LLM revises: "API experiencing slowness with 30% failure rate"
                Agent asks: "Is that better?"
        Turn 5: User: "Yes, and started 2h ago"
                LLM refines: "...started approximately 2 hours ago"
                Agent asks: "Is that accurate now?"
        Turn 6: User: "Yes, exactly!" (confirms without reservation)
                System: problem_statement_confirmed = True
                Field becomes IMMUTABLE
        
        Confirmation methods:
        - Button click: ✅ Confirm (recommended - unambiguous)
        - Text: "Yes", "correct", "that's right", etc. (system detects)
        
        Pattern: Iterative Refinement - refine until user confirms without reservation
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
        description="When user decided to investigate (or not)"
    )
    
    consultation_turns: int = Field(
        default=0,
        ge=0,
        description="Number of turns spent in CONSULTING status"
    )
```

### 4.2 ProblemConfirmation

```python
class ProblemConfirmation(BaseModel):
    """
    Agent's initial problem understanding during consulting.
    """
    
    problem_type: str = Field(
        description="Classified problem type: error | slowness | unavailability | data_issue | other",
        max_length=100
    )
    
    severity_guess: str = Field(
        description="Initial severity assessment: critical | high | medium | low | unknown",
        max_length=50
    )
    
    preliminary_guidance: str = Field(
        description="Initial guidance or suggestions",
        max_length=2000
    )
    
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this confirmation was created"
    )
    
    @validator('problem_type')
    def valid_problem_type(cls, v):
        """Validate problem type"""
        allowed = ["error", "slowness", "unavailability", "data_issue", "other"]
        if v not in allowed:
            raise ValueError(f"problem_type must be one of: {allowed}")
        return v
    
    @validator('severity_guess')
    def valid_severity(cls, v):
        """Validate severity"""
        allowed = ["critical", "high", "medium", "low", "unknown"]
        if v not in allowed:
            raise ValueError(f"severity_guess must be one of: {allowed}")
        return v
```

### 4.3 ProblemVerification

```python
class ProblemVerification(BaseModel):
    """
    Consolidated problem verification data.
    Replaces old AnomalyFrame + Timeline split.
    
    Contains all data gathered during verification phase:
    - Symptom details
    - Scope assessment
    - Timeline
    - Recent changes
    - Correlations
    """
    
    # ============================================================
    # Symptom
    # ============================================================
    symptom_statement: str = Field(
        description="Clear statement of the problem symptom",
        min_length=1,
        max_length=1000
    )
    
    symptom_indicators: List[str] = Field(
        default_factory=list,
        description="Specific metrics/observations confirming symptom (e.g., 'Error rate: 15%', 'P99 latency: 5s')"
    )
    
    # ============================================================
    # Scope
    # ============================================================
    affected_services: List[str] = Field(
        default_factory=list,
        description="Services/components affected"
    )
    
    affected_users: Optional[str] = Field(
        default=None,
        description="User impact description: 'all users' | '10% of users' | 'premium tier' | etc.",
        max_length=200
    )
    
    affected_regions: List[str] = Field(
        default_factory=list,
        description="Geographic regions affected"
    )
    
    severity: str = Field(
        description="Assessed severity: CRITICAL | HIGH | MEDIUM | LOW",
        max_length=50
    )
    
    user_impact: Optional[str] = Field(
        default=None,
        description="Description of user-facing impact",
        max_length=1000
    )
    
    # ============================================================
    # Timeline
    # ============================================================
    started_at: Optional[datetime] = Field(
        default=None,
        description="When problem began (best estimate)"
    )
    
    noticed_at: Optional[datetime] = Field(
        default=None,
        description="When problem was noticed/reported"
    )
    
    resolved_naturally_at: Optional[datetime] = Field(
        default=None,
        description="If problem resolved on its own, when?"
    )
    
    duration: Optional[timedelta] = Field(
        default=None,
        description="How long problem lasted (for historical problems)"
    )
    
    temporal_state: Optional[TemporalState] = Field(
        default=None,
        description="ONGOING | HISTORICAL"
    )
    
    # ============================================================
    # Changes
    # ============================================================
    recent_changes: List[Change] = Field(
        default_factory=list,
        description="Recent changes that may be relevant (deployments, configs, etc.)"
    )
    
    correlations: List[Correlation] = Field(
        default_factory=list,
        description="Identified correlations between changes and symptom",
        max_items=10  # Limit to top 10
    )
    
    correlation_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in change-symptom correlation (0.0 = no correlation, 1.0 = certain)"
    )
    
    # ============================================================
    # Urgency Assessment
    # ============================================================
    urgency_level: UrgencyLevel = Field(
        default=UrgencyLevel.UNKNOWN,
        description="Urgency classification for path routing"
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
        description="Overall confidence in verification accuracy"
    )
    
    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def is_complete(self) -> bool:
        """Check if verification has all required data"""
        return (
            bool(self.symptom_statement) and
            bool(self.severity) and
            self.temporal_state is not None and
            self.urgency_level != UrgencyLevel.UNKNOWN
        )
    
    @property
    def time_to_detection(self) -> Optional[timedelta]:
        """Time between problem start and detection"""
        if self.started_at and self.noticed_at:
            return self.noticed_at - self.started_at
        return None
    
    # ============================================================
    # Validation
    # ============================================================
    @validator('severity')
    def valid_severity(cls, v):
        """Validate severity"""
        allowed = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        if v.upper() not in allowed:
            raise ValueError(f"severity must be one of: {allowed}")
        return v.upper()
    
    @root_validator
    def timeline_consistency(cls, values):
        """Ensure timeline fields are consistent"""
        started = values.get('started_at')
        noticed = values.get('noticed_at')
        resolved = values.get('resolved_naturally_at')
        
        if started and noticed and started > noticed:
            raise ValueError("started_at cannot be after noticed_at")
        
        if started and resolved and started > resolved:
            raise ValueError("started_at cannot be after resolved_naturally_at")
        
        if noticed and resolved and noticed > resolved:
            raise ValueError("noticed_at cannot be after resolved_naturally_at")
        
        return values
```

### 4.4 Change

```python
class Change(BaseModel):
    """
    Recent change that may be relevant to the problem.
    """
    
    description: str = Field(
        description="What changed",
        min_length=1,
        max_length=500
    )
    
    occurred_at: datetime = Field(
        description="When the change occurred"
    )
    
    change_type: str = Field(
        description="Type of change: deployment | config | scaling | code | infrastructure | data | other",
        max_length=50
    )
    
    changed_by: Optional[str] = Field(
        default=None,
        description="Who made the change (user, system, team)",
        max_length=200
    )
    
    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional structured details (version numbers, config values, etc.)"
    )
    
    @validator('change_type')
    def valid_change_type(cls, v):
        """Validate change type"""
        allowed = ["deployment", "config", "scaling", "code", "infrastructure", "data", "other"]
        if v not in allowed:
            raise ValueError(f"change_type must be one of: {allowed}")
        return v
```

### 4.5 Correlation

```python
class Correlation(BaseModel):
    """
    Correlation between a change and the symptom.
    """
    
    change_description: str = Field(
        description="Description of the change",
        max_length=500
    )
    
    timing_description: str = Field(
        description="Temporal relationship: '2 minutes before', 'immediately after', 'coincides with', etc.",
        max_length=200
    )
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this correlation (0.0 = weak, 1.0 = strong)"
    )
    
    correlation_type: str = Field(
        description="Type: temporal | causal | coincidental | other",
        max_length=50
    )
    
    evidence: Optional[str] = Field(
        default=None,
        description="Evidence supporting this correlation",
        max_length=1000
    )
    
    @validator('correlation_type')
    def valid_correlation_type(cls, v):
        """Validate correlation type"""
        allowed = ["temporal", "causal", "coincidental", "other"]
        if v not in allowed:
            raise ValueError(f"correlation_type must be one of: {allowed}")
        return v
```

### 4.6 UrgencyLevel

```python
class UrgencyLevel(str, Enum):
    """
    Urgency classification for path routing.
    
    Used with TemporalState to determine investigation path:
    - ONGOING + HIGH/CRITICAL → MITIGATION
    - HISTORICAL + LOW/MEDIUM → ROOT_CAUSE
    - Other combinations → USER_CHOICE
    """
    
    CRITICAL = "critical"
    """
    Severe production impact.
    Examples: Total outage, data loss, security breach
    """
    
    HIGH = "high"
    """
    Significant impact but not total failure.
    Examples: Degraded performance, partial outage, many users affected
    """
    
    MEDIUM = "medium"
    """
    Moderate impact.
    Examples: Minor performance issues, small user subset affected
    """
    
    LOW = "low"
    """
    Minimal impact.
    Examples: Edge case bugs, cosmetic issues, very few users
    """
    
    UNKNOWN = "unknown"
    """
    Urgency not yet assessed.
    """
```

---

## 5. Evidence Models

### 5.1 UploadedFile

```python
class UploadedFile(BaseModel):
    """
    Raw file metadata for files uploaded to a case.

    Key Distinction:
    - UploadedFile: Raw file metadata, exists in ANY case phase (CONSULTING or INVESTIGATING)
    - Evidence: Investigation-linked data derived from files, ONLY exists in INVESTIGATING phase

    Files uploaded during CONSULTING are tracked here but do NOT become evidence until
    the case transitions to INVESTIGATING and hypotheses are formulated.
    """

    file_id: str = Field(
        default_factory=lambda: f"file_{uuid4().hex[:12]}",
        description="Unique file identifier (same as data_id in data service)",
        pattern=r"^(file_|data_)[a-f0-9]{12,16}$"  # Accept both file_ and data_ prefixes
    )

    filename: str = Field(
        description="Original filename",
        min_length=1,
        max_length=255
    )

    size_bytes: int = Field(
        ge=0,
        description="File size in bytes"
    )

    data_type: str = Field(
        description="Detected data type from preprocessing (log, metric, config, code, text, image, etc.)",
        max_length=50
    )

    uploaded_at_turn: int = Field(
        ge=0,
        description="Turn number when file was uploaded"
    )

    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Upload timestamp"
    )

    source_type: str = Field(
        default="file_upload",
        description="file_upload | paste | screenshot | page_injection | agent_generated",
        max_length=50
    )

    preprocessing_summary: Optional[str] = Field(
        default=None,
        description="Brief summary from preprocessing pipeline (<500 chars)",
        max_length=500
    )

    content_ref: str = Field(
        description="Reference to stored file content (S3 URI or data_id)",
        max_length=1000
    )
```

### 5.2 Evidence

```python
class Evidence(BaseModel):
    """
    Evidence collected during investigation.
    Categorized by purpose to drive milestone advancement.
    
    NOTE: Evidence.category is SYSTEM-INFERRED, not LLM-specified!
    System categorizes based on:
    - Which milestones are incomplete (if symptom not verified → SYMPTOM_EVIDENCE)
    - Hypothesis evaluation results (if creates hypothesis_evidence links → CAUSAL_EVIDENCE)
    - Solution state (if solution proposed → RESOLUTION_EVIDENCE)
    
    LLM provides: summary, analysis
    LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
    System infers: category, advances_milestones
    """
    
    evidence_id: str = Field(
        default_factory=lambda: f"ev_{uuid4().hex[:12]}",
        description="Unique evidence identifier",
        regex=r"^ev_[a-f0-9]{12}$"
    )
    
    # ============================================================
    # Purpose Classification (SYSTEM-INFERRED)
    # ============================================================
    category: EvidenceCategory = Field(
        description="System-inferred category: SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER"
    )
    
    primary_purpose: str = Field(
        description="What this evidence validates (milestone name or hypothesis ID)",
        max_length=100
    )
    
    # ============================================================
    # Content (Three-Tier Storage)
    # ============================================================
    summary: str = Field(
        description="Brief summary of evidence content (<500 chars) for UI display and quick scanning",
        min_length=1,
        max_length=500
    )
    
    preprocessed_content: str = Field(
        description="""
        Extracted relevant diagnostic information from preprocessing pipeline.
        
        This is what the agent uses for hypothesis evaluation and evidence analysis.
        Contains only the high-signal portions extracted from raw files.
        
        Examples:
        - Logs: Crime scene extraction (±200 lines around errors)
        - Metrics: Anomaly detection results with statistical analysis
        - Config: Parsed configuration with secrets redacted
        - Code: AST-extracted functions and classes
        - Text: LLM-generated summary
        - Images: Vision model description
        
        Size: Typically 5-50KB (compressed from larger raw files).
        Compression ratios: 200:1 for logs, 167:1 for metrics, 50:1 for code.
        
        This field is REQUIRED for all evidence. Raw files remain in S3 for audit/deep dive.
        """
    )
    
    content_ref: str = Field(
        description="S3 URI to original raw file (1-10MB) for audit, compliance, and deep dive analysis",
        max_length=1000
    )
    
    content_size_bytes: int = Field(
        ge=0,
        description="Size of original raw file in bytes"
    )
    
    preprocessing_method: str = Field(
        description="""
        Preprocessing method used to extract preprocessed_content from raw file.
        Examples: crime_scene_extraction, anomaly_detection, parse_and_sanitize, 
        ast_extraction, vision_analysis, single_shot_summary, map_reduce_summary
        """
    )
    
    compression_ratio: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Ratio of preprocessed to raw content size (e.g., 0.005 = 200:1 compression)"
    )
    
    analysis: Optional[str] = Field(
        default=None,
        description="Agent's analysis of this evidence and its significance to the investigation",
        max_length=2000
    )
    
    # ============================================================
    # Source Information
    # ============================================================
    source_type: EvidenceSourceType = Field(
        description="Type of evidence source"
    )
    
    form: EvidenceForm = Field(
        description="How evidence was provided: DOCUMENT (uploaded) or USER_INPUT (typed)"
    )
    
    # ============================================================
    # Milestone Advancement
    # ============================================================
    advances_milestones: List[str] = Field(
        default_factory=list,
        description="Which milestones this evidence helped complete"
    )
    
    # ============================================================
    # Metadata
    # ============================================================
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When evidence was collected"
    )
    
    collected_by: str = Field(
        description="Who collected: user_id or 'system' for automated collection"
    )
    
    collected_at_turn: int = Field(
        ge=0,
        description="Turn number when evidence was collected"
    )
    
```

### 5.3 EvidenceCategory

```python
class EvidenceCategory(str, Enum):
    """
    Evidence classification by investigation purpose.
    Determines which milestones the evidence can advance.
    """
    
    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Purpose: Verify symptom and establish context.
    
    Validates:
    - Symptom is real
    - Scope of impact
    - Timeline
    - Recent changes
    
    Advances Milestones:
    - symptom_verified
    - scope_assessed
    - timeline_established
    - changes_identified
    
    Examples:
    - Error logs
    - Metrics dashboards
    - User impact reports
    - Deployment logs
    """
    
    CAUSAL_EVIDENCE = "causal_evidence"
    """
    Purpose: Test hypothesis about root cause.
    
    Validates:
    - Specific theory about what caused the problem
    - Hypothesis-driven diagnostic data
    
    Advances Milestones:
    - root_cause_identified (if hypothesis validated)
    
    Examples:
    - Connection pool metrics (for "pool exhausted" hypothesis)
    - Memory dumps (for "memory leak" hypothesis)
    - Network traces (for "latency" hypothesis)
    - Config files (for "misconfigured" hypothesis)
    """
    
    RESOLUTION_EVIDENCE = "resolution_evidence"
    """
    Purpose: Verify solution effectiveness.
    
    Validates:
    - Solution was applied
    - Problem resolved after fix
    
    Advances Milestones:
    - solution_verified
    
    Examples:
    - Error rate after rollback (before/after comparison)
    - Latency metrics after optimization
    - Resource usage after scaling
    - Success rate after config change
    """
    
    OTHER = "other"
    """
    Evidence that doesn't fit standard categories.
    May be useful contextually but doesn't directly advance milestones.
    
    Examples:
    - Background documentation
    - Architecture diagrams
    - Historical incident notes
    """
```

### 5.4 EvidenceSourceType

```python
class EvidenceSourceType(str, Enum):
    """Type of evidence source"""
    
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
```

### 5.5 EvidenceForm

```python
class EvidenceForm(str, Enum):
    """How evidence was provided by user"""
    
    DOCUMENT = "document"
    """Uploaded file (log, screenshot, config, etc.)"""
    
    USER_INPUT = "user_input"
    """Typed text answer or description"""
```

### 5.6 EvidenceStance

```python
class EvidenceStance(str, Enum):
    """
    How evidence relates to a hypothesis.
    Evaluated by LLM after evidence submission against ALL active hypotheses.
    One evidence can have different stances for different hypotheses.
    """
    
    STRONGLY_SUPPORTS = "strongly_supports"
    """Evidence strongly confirms hypothesis (→ VALIDATED)"""
    
    SUPPORTS = "supports"
    """Evidence somewhat supports hypothesis (increase confidence)"""
    
    NEUTRAL = "neutral"
    """Evidence neither supports nor contradicts"""
    
    CONTRADICTS = "contradicts"
    """Evidence somewhat contradicts hypothesis (decrease confidence)"""
    
    STRONGLY_CONTRADICTS = "strongly_contradicts"
    """Evidence strongly refutes hypothesis (→ REFUTED)"""
    
    IRRELEVANT = "irrelevant"
    """Evidence is not related to this hypothesis (no link created in hypothesis_evidence table)"""
```


---

## 6. Hypothesis Models

### 6.1 Hypothesis

```python
class Hypothesis(BaseModel):
    """
    Hypothesis for systematic root cause exploration.
    
    Philosophy: Hypotheses are OPTIONAL. Agent may:
    - Identify root cause directly from evidence (no hypotheses)
    - OR generate hypotheses for systematic testing (when unclear)
    """
    
    hypothesis_id: str = Field(
        default_factory=lambda: f"hyp_{uuid4().hex[:12]}",
        description="Unique hypothesis identifier",
        regex=r"^hyp_[a-f0-9]{12}$"
    )
    
    statement: str = Field(
        description="Hypothesis statement (what we think caused the problem)",
        min_length=1,
        max_length=500
    )
    
    category: HypothesisCategory = Field(
        description="Hypothesis category (for anchoring detection)"
    )
    
    status: HypothesisStatus = Field(
        default=HypothesisStatus.CAPTURED,
        description="Current hypothesis status"
    )
    
    likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Estimated likelihood this hypothesis is correct (0.0-1.0)"
    )
    
    # ============================================================
    # Evidence Relationships (Many-to-Many)
    # ============================================================
    evidence_links: Dict[str, HypothesisEvidenceLink] = Field(
        default_factory=dict,
        description="""
        Maps evidence_id to relationship details.
        
        ONE evidence can:
        - STRONGLY_SUPPORTS hypothesis A
        - REFUTES hypothesis B  
        - Be IRRELEVANT to hypothesis C
        
        Backed by hypothesis_evidence junction table in database.
        LLM evaluates each evidence against ALL active hypotheses after submission.
        """
    )
    
    # ============================================================
    # Metadata
    # ============================================================
    generated_at_turn: int = Field(
        ge=0,
        description="Turn number when hypothesis was generated"
    )
    
    generation_mode: HypothesisGenerationMode = Field(
        description="How hypothesis was generated"
    )
    
    rationale: str = Field(
        description="Why this hypothesis was generated",
        max_length=1000
    )
    
    # ============================================================
    # Testing History
    # ============================================================
    tested_at: Optional[datetime] = Field(
        default=None,
        description="When hypothesis testing began"
    )
    
    concluded_at: Optional[datetime] = Field(
        default=None,
        description="When hypothesis was validated/refuted/retired"
    )
    
    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def supporting_evidence(self) -> List[str]:
        """Get evidence IDs that support this hypothesis"""
        return [
            evidence_id for evidence_id, link in self.evidence_links.items()
            if link.stance in [EvidenceStance.STRONGLY_SUPPORTS, EvidenceStance.SUPPORTS]
        ]
    
    @property
    def refuting_evidence(self) -> List[str]:
        """Get evidence IDs that refute this hypothesis"""
        return [
            evidence_id for evidence_id, link in self.evidence_links.items()
            if link.stance in [EvidenceStance.CONTRADICTS, EvidenceStance.STRONGLY_CONTRADICTS]
        ]
    
    @property
    def evidence_score(self) -> float:
        """
        Evidence balance score.
        Returns: -1.0 (all refuting) to 1.0 (all supporting)
        """
        total_support = len(self.supporting_evidence)
        total_refute = len(self.refuting_evidence)
        total = total_support + total_refute
        
        if total == 0:
            return 0.0
        
        return (total_support - total_refute) / total
```

### 6.2 HypothesisEvidenceLink

```python
class HypothesisEvidenceLink(BaseModel):
    """
    Many-to-many relationship between hypothesis and evidence.
    
    ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses:
    - Evidence "Pool at 95%" → STRONGLY_SUPPORTS "pool exhausted" hypothesis
    - Evidence "Pool at 95%" → REFUTES "network latency" hypothesis
    - Evidence "Pool at 95%" → IRRELEVANT to "memory leak" hypothesis
    
    Stored in hypothesis_evidence junction table.
    LLM evaluates evidence against ALL active hypotheses after submission.
    """
    
    hypothesis_id: str = Field(
        description="Hypothesis being evaluated"
    )
    
    evidence_id: str = Field(
        description="Evidence being evaluated"
    )
    
    stance: EvidenceStance = Field(
        description="How this evidence relates to THIS hypothesis (including IRRELEVANT)"
    )
    
    reasoning: str = Field(
        description="LLM's explanation of the relationship",
        max_length=1000
    )
    
    completeness: float = Field(
        ge=0.0,
        le=1.0,
        description="How well this evidence tests THIS hypothesis (0.0 = doesn't test, 1.0 = fully tests)"
    )
    
    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this relationship was established"
    )
```

### 6.3 HypothesisCategory

```python
class HypothesisCategory(str, Enum):
    """
    Hypothesis categories for anchoring detection.
    
    If agent tests 4+ hypotheses in same category without validation,
    it's "anchored" and should try different category.
    """
    
    CODE = "code"
    """Code bugs, logic errors, null pointers, etc."""
    
    CONFIG = "config"
    """Configuration issues, misconfigurations, wrong settings"""
    
    ENVIRONMENT = "environment"
    """Environment issues, resource exhaustion, system limits"""
    
    NETWORK = "network"
    """Network issues, connectivity, latency, DNS"""
    
    DATA = "data"
    """Data issues, database problems, data corruption"""
    
    HARDWARE = "hardware"
    """Hardware failures, disk issues, CPU/memory"""
    
    EXTERNAL = "external"
    """External dependencies, third-party services"""
    
    HUMAN = "human"
    """Human errors, operational mistakes"""
    
    OTHER = "other"
    """Doesn't fit above categories"""
```

### 6.3 HypothesisStatus

```python
class HypothesisStatus(str, Enum):
    """Hypothesis lifecycle status"""
    
    CAPTURED = "captured"
    """
    Generated but not yet actively testing.
    Hypothesis is in the queue.
    """
    
    ACTIVE = "active"
    """
    Currently being tested.
    Evidence is being gathered.
    """
    
    VALIDATED = "validated"
    """
    Evidence strongly supports hypothesis.
    Root cause identified.
    """
    
    REFUTED = "refuted"
    """
    Evidence contradicts hypothesis.
    Not the root cause.
    """
    
    INCONCLUSIVE = "inconclusive"
    """
    Evidence is ambiguous.
    Cannot determine if hypothesis is correct.
    """
    
    RETIRED = "retired"
    """
    No longer relevant.
    Investigation moved in different direction.
    """
```

### 6.4 HypothesisGenerationMode

```python
class HypothesisGenerationMode(str, Enum):
    """How hypothesis was generated"""
    
    OPPORTUNISTIC = "opportunistic"
    """
    Generated from strong correlation or obvious clue.
    Example: Deploy immediately preceded errors → hypothesis: "Bug in new deploy"
    """
    
    SYSTEMATIC = "systematic"
    """
    Generated methodically when root cause unclear.
    Example: Generic slowness → generate hypotheses for common causes
    """
    
    FORCED_ALTERNATIVE = "forced_alternative"
    """
    User requested alternative hypotheses.
    Example: User: "What else could it be?"
    """
```


## 7. Solution Models

### 7.1 Solution

```python
class Solution(BaseModel):
    """
    Proposed or applied solution/mitigation.
    """
    
    solution_id: str = Field(
        default_factory=lambda: f"sol_{uuid4().hex[:12]}",
        description="Unique solution identifier",
        regex=r"^sol_[a-f0-9]{12}$"
    )
    
    # ============================================================
    # Solution Type
    # ============================================================
    solution_type: SolutionType = Field(
        description="Type of solution"
    )
    
    # ============================================================
    # Solution Details
    # ============================================================
    title: str = Field(
        description="Short solution title",
        min_length=1,
        max_length=200
    )
    
    immediate_action: Optional[str] = Field(
        default=None,
        description="Quick fix or mitigation (temporary)",
        max_length=2000
    )
    
    longterm_fix: Optional[str] = Field(
        default=None,
        description="Permanent solution (comprehensive)",
        max_length=2000
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
        description="Specific commands to execute"
    )
    
    risks: List[str] = Field(
        default_factory=list,
        description="Risks or side effects of this solution"
    )
    
    # ============================================================
    # Lifecycle
    # ============================================================
    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When solution was proposed"
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
    
    # ============================================================
    # Verification
    # ============================================================
    verification_method: Optional[str] = Field(
        default=None,
        description="How effectiveness was verified",
        max_length=500
    )
    
    verification_evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID proving solution worked"
    )
    
    effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well solution worked (0.0 = failed, 1.0 = perfect)"
    )
    
    # ============================================================
    # Validation
    # ============================================================
    @root_validator
    def solution_content_required(cls, values):
        """Ensure solution has actionable content"""
        immediate = values.get('immediate_action')
        longterm = values.get('longterm_fix')
        steps = values.get('implementation_steps', [])
        commands = values.get('commands', [])
        
        if not any([immediate, longterm, steps, commands]):
            raise ValueError("Solution must have at least one of: immediate_action, longterm_fix, implementation_steps, or commands")
        
        return values
    
    @root_validator
    def verification_consistency(cls, values):
        """Ensure verification fields are consistent"""
        verified_at = values.get('verified_at')
        effectiveness = values.get('effectiveness')
        
        if verified_at and effectiveness is None:
            raise ValueError("verified_at requires effectiveness score")
        
        if effectiveness is not None and not verified_at:
            raise ValueError("effectiveness requires verified_at")
        
        return values
```

### 7.2 SolutionType

```python
class SolutionType(str, Enum):
    """Type of solution/mitigation"""
    
    ROLLBACK = "rollback"
    """Revert to previous version/state"""
    
    CONFIG_CHANGE = "config_change"
    """Modify configuration settings"""
    
    RESTART = "restart"
    """Restart service/component"""
    
    SCALING = "scaling"
    """Scale resources (increase/decrease)"""
    
    CODE_FIX = "code_fix"
    """Fix code bug (requires deployment)"""
    
    WORKAROUND = "workaround"
    """Temporary workaround (not root fix)"""
    
    INFRASTRUCTURE = "infrastructure"
    """Infrastructure changes (servers, networking, etc.)"""
    
    DATA_FIX = "data_fix"
    """Fix data corruption or inconsistency"""
    
    OTHER = "other"
    """Doesn't fit above categories"""
```

---

## 8. Turn Tracking Models

### 8.1 TurnProgress

```python
class TurnProgress(BaseModel):
    """
    Record of what happened in one turn.
    Turn = one user message + one agent response.
    """
    
    turn_number: int = Field(
        ge=0,
        description="Sequential turn number"
    )
    
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When turn occurred"
    )
    
    # ============================================================
    # What Advanced This Turn
    # ============================================================
    milestones_completed: List[str] = Field(
        default_factory=list,
        description="Milestone names completed this turn (e.g., 'symptom_verified')"
    )
    
    evidence_added: List[str] = Field(
        default_factory=list,
        description="Evidence IDs added this turn"
    )
    
    hypotheses_generated: List[str] = Field(
        default_factory=list,
        description="Hypothesis IDs generated this turn"
    )
    
    hypotheses_validated: List[str] = Field(
        default_factory=list,
        description="Hypothesis IDs validated this turn"
    )
    
    solutions_proposed: List[str] = Field(
        default_factory=list,
        description="Solution IDs proposed this turn"
    )
    
    # ============================================================
    # Progress Assessment
    # ============================================================
    progress_made: bool = Field(
        description="Did investigation advance this turn?"
    )
    
    actions_taken: List[str] = Field(
        default_factory=list,
        description="Agent actions: 'verified_symptom', 'requested_logs', 'generated_hypothesis', etc."
    )
    
    # ============================================================
    # Outcome
    # ============================================================
    outcome: TurnOutcome = Field(
        description="Turn outcome classification"
    )
    
    # NOTE: No blocked_reason field - outcome is observable only, not for workflow control
    
    # ============================================================
    # User Interaction
    # ============================================================
    user_message_summary: Optional[str] = Field(
        default=None,
        description="Summary of user's message",
        max_length=500
    )
    
    agent_response_summary: Optional[str] = Field(
        default=None,
        description="Summary of agent's response",
        max_length=500
    )
    
    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def advancement_count(self) -> int:
        """Total items advanced this turn"""
        return (
            len(self.milestones_completed) +
            len(self.evidence_added) +
            len(self.hypotheses_validated) +
            len(self.solutions_proposed)
        )
    
    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        frozen = True  # Immutable once created
```

### 8.2 TurnOutcome

```python
class TurnOutcome(str, Enum):
    """
    Turn outcome classification.
    
    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress, degraded_mode).
    Outcomes are for analytics and prompt context, not control flow.
    """
    
    MILESTONE_COMPLETED = "milestone_completed"
    """
    One or more milestones completed.
    Investigation advanced.
    """
    
    DATA_PROVIDED = "data_provided"
    """
    User provided data/evidence this turn.
    """
    
    DATA_REQUESTED = "data_requested"
    """
    Agent requested data from user.
    Awaiting user response.
    """
    
    DATA_NOT_PROVIDED = "data_not_provided"
    """
    Agent requested data, user didn't provide.
    LLM uses this when user didn't address request.
    System tracks pattern - if 3+ consecutive, triggers degraded mode.
    """
    
    HYPOTHESIS_TESTED = "hypothesis_tested"
    """
    Hypothesis was tested (validated/refuted).
    """
    
    CASE_RESOLVED = "case_resolved"
    """
    Solution verified.
    Case can transition to RESOLVED status (terminal).
    """
    
    CONVERSATION = "conversation"
    """
    Normal Q&A, no data requests or milestones.
    """
    
    OTHER = "other"
    """
    Doesn't fit standard outcomes.
    """
    
    # NOTE: No "BLOCKED" outcome!
    # Investigation stalls naturally via turns_without_progress metric.
    # Degraded mode triggers at 3 turns without progress (system-managed, not LLM-determined).

```

---

## 9. Path Selection Models

### 9.1 InvestigationPath

```python
class InvestigationPath(str, Enum):
    """
    Investigation routing strategy.
    
    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state × urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during verification.
    System calls determine_investigation_path() to select path deterministically.
    """
    
    MITIGATION = "mitigation"
    """
    Fast action path.
    
    Characteristics:
    - Skip deep root cause analysis
    - Apply correlation-based fix quickly
    - Defer comprehensive investigation
    
    Use When: Ongoing + High Urgency
    Next Milestone: solution_proposed
    """
    
    ROOT_CAUSE = "root_cause"
    """
    Thorough investigation path.
    
    Characteristics:
    - Deep root cause analysis
    - May generate and test hypotheses
    - Comprehensive solution
    
    Use When: Historical + Low Urgency
    Next Milestone: root_cause_identified
    """
    
    USER_CHOICE = "user_choice"
    """
    Ambiguous case - let user decide.
    
    Characteristics:
    - Unclear which path is better
    - Present options to user
    - User makes strategic decision
    
    Use When: Ambiguous temporal_state × urgency combinations
    """
```

### 9.2 PathSelection

```python
class PathSelection(BaseModel):
    """
    Path selection details.
    Records how investigation path was chosen.
    
    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state × urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during verification.
    System calls determine_investigation_path() to select path deterministically.
    LLM does NOT choose the path directly!
    """
    
    path: InvestigationPath = Field(
        description="Selected investigation path (system-determined from matrix)"
    )
    
    auto_selected: bool = Field(
        description="True if system auto-selected, False if user chose"
    )
    
    rationale: str = Field(
        description="Why this path was selected",
        max_length=500
    )
    
    alternate_path: Optional[InvestigationPath] = Field(
        default=None,
        description="Alternative path user could have chosen (if auto-selected)"
    )
    
    selected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When path was selected"
    )
    
    selected_by: str = Field(
        default="system",
        description="Who selected: 'system' for auto, or user_id for manual"
    )
    
    # ============================================================
    # Decision Inputs
    # ============================================================
    temporal_state: Optional[TemporalState] = Field(
        default=None,
        description="Temporal state used in decision"
    )
    
    urgency_level: Optional[UrgencyLevel] = Field(
        default=None,
        description="Urgency level used in decision"
    )
    
    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        frozen = True  # Immutable once created
```

---

## 10. Conclusion Models

### 10.1 WorkingConclusion

```python
class WorkingConclusion(BaseModel):
    """
    Agent's current best understanding of the problem.
    Updated iteratively as investigation progresses.
    
    Less authoritative than RootCauseConclusion.
    """
    
    statement: str = Field(
        description="Current conclusion statement",
        min_length=1,
        max_length=1000
    )
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this conclusion (0.0-1.0)"
    )
    
    reasoning: str = Field(
        description="Why agent believes this conclusion",
        max_length=2000
    )
    
    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description="Evidence IDs supporting this conclusion"
    )
    
    caveats: List[str] = Field(
        default_factory=list,
        description="Limitations or uncertainties"
    )
    
    # ============================================================
    # Metadata
    # ============================================================
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this conclusion was formed/updated"
    )
    
    supersedes_conclusion_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of previous conclusion this replaces"
    )
```

### 10.2 RootCauseConclusion

```python
class RootCauseConclusion(BaseModel):
    """
    Final determination of root cause.
    More authoritative than WorkingConclusion.
    """
    
    root_cause: str = Field(
        description="Definitive statement of root cause",
        min_length=1,
        max_length=1000
    )
    
    confidence_level: ConfidenceLevel = Field(
        description="Categorical confidence level"
    )
    
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Numeric confidence score (0.0-1.0)"
    )
    
    mechanism: str = Field(
        description="How this root cause led to the symptom",
        max_length=2000
    )
    
    # ============================================================
    # Evidence Basis
    # ============================================================
    evidence_basis: List[str] = Field(
        default_factory=list,
        description="Evidence IDs supporting this conclusion"
    )
    
    validated_hypothesis_id: Optional[str] = Field(
        default=None,
        description="If identified via hypothesis validation, the hypothesis ID"
    )
    
    # ============================================================
    # Contributing Factors
    # ============================================================
    contributing_factors: List[str] = Field(
        default_factory=list,
        description="Secondary factors that made the problem worse or more likely"
    )
    
    # ============================================================
    # Metadata
    # ============================================================
    determined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When root cause was determined"
    )
    
    determined_by: str = Field(
        default="agent",
        description="Who determined: 'agent' or user_id"
    )
    
    # ============================================================
    # Validation
    # ============================================================
    @root_validator
    def confidence_consistency(cls, values):
        """Ensure confidence_level matches confidence_score"""
        level = values.get('confidence_level')
        score = values.get('confidence_score')
        
        if level and score is not None:
            expected_level = ConfidenceLevel.from_score(score)
            if level != expected_level:
                raise ValueError(f"confidence_level {level} doesn't match score {score} (expected {expected_level})")
        
        return values
```

### 10.3 ConfidenceLevel

```python
class ConfidenceLevel(str, Enum):
    """
    Categorical confidence levels.
    Maps to numeric confidence scores.
    """
    
    SPECULATION = "speculation"
    """
    Low confidence guess.
    Score: < 0.5
    """
    
    PROBABLE = "probable"
    """
    Likely but not certain.
    Score: 0.5 - 0.69
    """
    
    CONFIDENT = "confident"
    """
    High confidence.
    Score: 0.7 - 0.89
    """
    
    VERIFIED = "verified"
    """
    Evidence-backed certainty.
    Score: ≥ 0.9
    """
    
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

---

## 11. Special State Models

### 11.1 DegradedMode

```python
class DegradedMode(BaseModel):
    """
    Investigation is blocked or struggling.
    Agent offers fallback options.
    """
    
    mode_type: DegradedModeType = Field(
        description="Why investigation degraded"
    )
    
    entered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When degraded mode was entered"
    )
    
    reason: str = Field(
        description="Detailed explanation of why investigation degraded",
        max_length=1000
    )
    
    attempted_actions: List[str] = Field(
        default_factory=list,
        description="What agent tried before degrading"
    )
    
    # ============================================================
    # Fallback
    # ============================================================
    fallback_offered: Optional[str] = Field(
        default=None,
        description="Fallback option presented to user",
        max_length=1000
    )
    
    user_choice: Optional[str] = Field(
        default=None,
        description="How user responded: 'accept_fallback' | 'provide_more_data' | 'escalate' | 'abandon'",
        max_length=100
    )
    
    # ============================================================
    # Exit
    # ============================================================
    exited_at: Optional[datetime] = Field(
        default=None,
        description="When degraded mode was exited (if recovered)"
    )
    
    exit_reason: Optional[str] = Field(
        default=None,
        description="How investigation recovered from degraded mode",
        max_length=500
    )
    
    @property
    def is_active(self) -> bool:
        """Check if still in degraded mode"""
        return self.exited_at is None
```

### 11.2 DegradedModeType

```python
class DegradedModeType(str, Enum):
    """Reason for entering degraded mode"""
    
    NO_PROGRESS = "no_progress"
    """
    3+ consecutive turns without milestone advancement.
    Investigation is stuck (covers insufficient data, user non-engagement, data access issues).
    """
    
    LIMITED_DATA = "limited_data"
    """
    Cannot obtain required evidence.
    Insufficient data to proceed.
    """
    
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
    """
    All hypotheses are inconclusive.
    Cannot determine root cause.
    """
    
    EXTERNAL_DEPENDENCY = "external_dependency"
    """
    Waiting on external team/person.
    Outside agent's control.
    """
    
    OTHER = "other"
    """
    Doesn't fit standard degradation reasons.
    """
```

### 11.3 EscalationState

```python
class EscalationState(BaseModel):
    """
    Investigation escalated to human expert.
    Tracks escalation lifecycle.
    """
    
    escalation_type: EscalationType = Field(
        description="Why escalation was needed"
    )
    
    reason: str = Field(
        description="Detailed explanation of escalation reason",
        max_length=1000
    )
    
    escalated_to: Optional[str] = Field(
        default=None,
        description="Team or person escalated to",
        max_length=200
    )
    
    escalated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When escalation occurred"
    )
    
    # ============================================================
    # Context Transfer
    # ============================================================
    context_summary: str = Field(
        description="Summary of investigation so far for escalation recipient",
        max_length=5000
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
        description="How escalation was resolved",
        max_length=2000
    )
    
    resolved_at: Optional[datetime] = Field(
        default=None,
        description="When escalation was resolved"
    )
    
    @property
    def is_active(self) -> bool:
        """Check if escalation is still active"""
        return self.resolved_at is None
```

### 11.4 EscalationType

```python
class EscalationType(str, Enum):
    """Reason for escalation"""
    
    EXPERTISE_REQUIRED = "expertise_required"
    """
    Requires specialized domain expertise.
    Beyond agent's knowledge.
    """
    
    PERMISSIONS_REQUIRED = "permissions_required"
    """
    User lacks permissions for needed actions.
    Requires higher privileges.
    """
    
    NO_PROGRESS = "no_progress"
    """
    Investigation is stuck despite best efforts.
    Human insight needed.
    """
    
    USER_REQUEST = "user_request"
    """
    User explicitly requested escalation.
    """
    
    CRITICAL_SEVERITY = "critical_severity"
    """
    Problem too critical for agent-only investigation.
    Human oversight required.
    """
    
    OTHER = "other"
    """
    Doesn't fit standard escalation reasons.
    """
```

---

## 12. Documentation Models

### 12.1 DocumentationData

```python
class DocumentationData(BaseModel):
    """
    Documentation generated when case closes.
    Captures lessons learned and artifacts.
    """
    
    documents_generated: List[GeneratedDocument] = Field(
        default_factory=list,
        description="All documents generated for this case"
    )
    
    runbook_entry: Optional[str] = Field(
        default=None,
        description="Runbook entry created from this case",
        max_length=5000
    )
    
    post_mortem_id: Optional[str] = Field(
        default=None,
        description="Link to post-mortem doc if created"
    )
    
    # ============================================================
    # Lessons Learned
    # ============================================================
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
    
    # ============================================================
    # Prevention
    # ============================================================
    preventive_measures: List[str] = Field(
        default_factory=list,
        description="How to prevent recurrence"
    )
    
    monitoring_recommendations: List[str] = Field(
        default_factory=list,
        description="Monitoring/alerts to add"
    )
    
    # ============================================================
    # Metadata
    # ============================================================
    generated_at: Optional[datetime] = Field(
        default=None,
        description="When documentation was generated"
    )
    
    generated_by: str = Field(
        default="agent",
        description="Who generated: 'agent' or user_id"
    )
```

### 12.2 GeneratedDocument

```python
class GeneratedDocument(BaseModel):
    """A generated document artifact"""
    
    document_id: str = Field(
        default_factory=lambda: f"doc_{uuid4().hex[:12]}",
        description="Unique document identifier"
    )
    
    document_type: DocumentType = Field(
        description="Type of document"
    )
    
    title: str = Field(
        description="Document title",
        min_length=1,
        max_length=200
    )
    
    content_ref: str = Field(
        description="Reference to document content (S3 URI, file path, etc.)",
        max_length=1000
    )
    
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When document was generated"
    )
    
    format: str = Field(
        description="Document format: markdown | pdf | html | json | other",
        max_length=50
    )
    
    size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Document size in bytes"
    )
    
    @validator('format')
    def valid_format(cls, v):
        """Validate format"""
        allowed = ["markdown", "pdf", "html", "json", "txt", "other"]
        if v not in allowed:
            raise ValueError(f"format must be one of: {allowed}")
        return v
```

### 12.3 DocumentType

```python
class DocumentType(str, Enum):
    """Type of generated document"""
    
    INCIDENT_REPORT = "incident_report"
    """Formal incident report"""
    
    POST_MORTEM = "post_mortem"
    """Post-mortem analysis"""
    
    RUNBOOK = "runbook"
    """Runbook entry for future reference"""
    
    CHAT_SUMMARY = "chat_summary"
    """Summary of investigation conversation"""
    
    TIMELINE = "timeline"
    """Timeline visualization of events"""
    
    EVIDENCE_BUNDLE = "evidence_bundle"
    """Compiled evidence package"""
    
    OTHER = "other"
    """Doesn't fit standard document types"""
```

---

## 13. Validation Rules

### 13.1 Status Transition Validation

```python
def validate_status_transition(
    case: Case,
    new_status: CaseStatus
) -> Tuple[bool, Optional[str]]:
    """
    Validate if status transition is allowed.
    
    Returns:
        (is_valid, error_message)
    """
    
    current = case.status
    
    # Terminal states cannot transition
    if current in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
        return False, f"Cannot transition from terminal state {current}"
    
    # Check valid transitions map
    valid_next = {
        CaseStatus.CONSULTING: [CaseStatus.INVESTIGATING, CaseStatus.CLOSED],
        CaseStatus.INVESTIGATING: [CaseStatus.RESOLVED, CaseStatus.CLOSED],
    }
    
    if new_status not in valid_next.get(current, []):
        return False, f"Invalid transition: {current} → {new_status}"
    
    # INVESTIGATING requires:
    # 1. User decided to investigate
    # 2. Problem statement confirmed
    # 3. Description set (from confirmed statement)
    if new_status == CaseStatus.INVESTIGATING:
        if not case.consulting.decided_to_investigate:
            return False, "Cannot start investigation without user commitment"
        
        if not case.consulting.problem_statement_confirmed:
            return False, "Cannot start investigation without confirmed problem statement"
        
        if not case.description or not case.description.strip():
            return False, "Cannot start investigation without problem description (set from confirmed proposed_problem_statement)"
    
    # RESOLVED requires solution_verified=True
    if new_status == CaseStatus.RESOLVED:
        if not case.progress.solution_verified:
            return False, "Cannot mark RESOLVED without solution verification"
    
    return True, None
```

### 13.2 Milestone Completion Validation

```python
def validate_milestone_completion(
    milestone_name: str,
    case: Case
) -> Tuple[bool, Optional[str]]:
    """
    Validate if milestone can be completed given current case state.
    
    Returns:
        (is_valid, error_message)
    """
    
    # Can only complete milestones during INVESTIGATING
    if case.status != CaseStatus.INVESTIGATING:
        return False, f"Cannot complete milestones in {case.status} status"
    
    # Solution milestones require root_cause_identified (unless MITIGATION path)
    solution_milestones = ['solution_proposed', 'solution_applied', 'solution_verified']
    if milestone_name in solution_milestones:
        if case.path_selection and case.path_selection.path == InvestigationPath.MITIGATION:
            # MITIGATION path can skip root cause
            pass
        elif not case.progress.root_cause_identified:
            return False, f"{milestone_name} requires root_cause_identified (unless MITIGATION path)"
    
    # solution_applied requires solution_proposed
    if milestone_name == 'solution_applied':
        if not case.progress.solution_proposed:
            return False, "solution_applied requires solution_proposed first"
    
    # solution_verified requires solution_applied
    if milestone_name == 'solution_verified':
        if not case.progress.solution_applied:
            return False, "solution_verified requires solution_applied first"
    
    return True, None
```

### 13.3 Evidence Categorization Validation

```python
def validate_evidence_category(
    evidence: Evidence,
    case: Case
) -> Tuple[bool, Optional[str]]:
    """
    Validate evidence category assignment.
    
    Returns:
        (is_valid, error_message)
    """
    
    # NOTE: Evidence category is system-inferred AFTER hypothesis evaluation
    # No validation needed here - category assigned based on hypothesis_evidence links
    
    # RESOLUTION_EVIDENCE requires solution exists
    if evidence.category == EvidenceCategory.RESOLUTION_EVIDENCE:
        if len(case.solutions) == 0:
            return False, "RESOLUTION_EVIDENCE requires at least one solution"
    
    return True, None
```

---

## 14. Database Schema

### 14.1 Primary Tables

```sql
-- Cases table
CREATE TABLE cases (
    case_id VARCHAR(17) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    organization_id VARCHAR(255) NOT NULL,
    title VARCHAR(500) NOT NULL,
    status VARCHAR(20) NOT NULL,
    closure_reason VARCHAR(100),
    current_turn INTEGER DEFAULT 0,
    turns_without_progress INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    closed_at TIMESTAMP,
    
    -- NOTE: decided_to_investigate is in consulting JSONB field, not top-level
    
    -- Indexes
    INDEX idx_user_status (user_id, status),
    INDEX idx_org_status (organization_id, status),
    INDEX idx_created_at (created_at),
    INDEX idx_updated_at (updated_at),
    
    -- Constraints
    CHECK (status IN ('consulting', 'investigating', 'resolved', 'closed')),
    CHECK (current_turn >= 0),
    CHECK (turns_without_progress >= 0)
);

-- Investigation progress (embedded in case, but can be separate table for querying)
CREATE TABLE investigation_progress (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id),
    symptom_verified BOOLEAN DEFAULT FALSE,
    scope_assessed BOOLEAN DEFAULT FALSE,
    timeline_established BOOLEAN DEFAULT FALSE,
    changes_identified BOOLEAN DEFAULT FALSE,
    temporal_state VARCHAR(20),
    root_cause_identified BOOLEAN DEFAULT FALSE,
    root_cause_confidence FLOAT DEFAULT 0.0,
    root_cause_method VARCHAR(50),
    solution_proposed BOOLEAN DEFAULT FALSE,
    solution_applied BOOLEAN DEFAULT FALSE,
    solution_verified BOOLEAN DEFAULT FALSE,
    
    -- Indexes
    INDEX idx_verification_complete (symptom_verified, scope_assessed, timeline_established, changes_identified),
    INDEX idx_rca_complete (root_cause_identified),
    INDEX idx_solution_complete (solution_verified),
    
    CHECK (root_cause_confidence >= 0.0 AND root_cause_confidence <= 1.0)
);

-- Status transitions
CREATE TABLE case_status_transitions (
    transition_id SERIAL PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id),
    from_status VARCHAR(20) NOT NULL,
    to_status VARCHAR(20) NOT NULL,
    triggered_at TIMESTAMP NOT NULL,
    triggered_by VARCHAR(255) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    
    INDEX idx_case_transitions (case_id, triggered_at),
    INDEX idx_transition_type (from_status, to_status)
);

-- Uploaded Files (raw file metadata)
CREATE TABLE uploaded_files (
    file_id VARCHAR(20) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    size_bytes INTEGER NOT NULL,
    data_type VARCHAR(50) NOT NULL,
    uploaded_at_turn INTEGER NOT NULL,
    uploaded_at TIMESTAMP NOT NULL,
    source_type VARCHAR(50) NOT NULL DEFAULT 'file_upload',
    preprocessing_summary VARCHAR(500),
    content_ref VARCHAR(1000) NOT NULL,

    INDEX idx_case_uploaded_files (case_id, uploaded_at),
    INDEX idx_source_type (source_type),

    CHECK (size_bytes >= 0),
    CHECK (uploaded_at_turn >= 0),
    CHECK (source_type IN ('file_upload', 'paste', 'screenshot', 'page_injection', 'agent_generated'))
);

-- Evidence (investigation-linked data derived from uploaded files)
CREATE TABLE evidence (
    evidence_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id),
    category VARCHAR(30) NOT NULL,
    primary_purpose VARCHAR(100) NOT NULL,
    summary VARCHAR(500) NOT NULL,
    content_ref VARCHAR(1000) NOT NULL,
    analysis TEXT,
    source_type VARCHAR(50) NOT NULL,
    form VARCHAR(20) NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    collected_by VARCHAR(255) NOT NULL,
    collected_at_turn INTEGER NOT NULL,
    
    INDEX idx_case_evidence (case_id, collected_at),
    INDEX idx_category (category),
    
    CHECK (category IN ('symptom_evidence', 'causal_evidence', 'resolution_evidence', 'other')),
    CHECK (form IN ('document', 'user_input'))
);

-- Hypotheses
CREATE TABLE hypotheses (
    hypothesis_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id),
    statement VARCHAR(500) NOT NULL,
    category VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    likelihood FLOAT NOT NULL,
    generated_at_turn INTEGER NOT NULL,
    generation_mode VARCHAR(30) NOT NULL,
    rationale VARCHAR(1000) NOT NULL,
    tested_at TIMESTAMP,
    concluded_at TIMESTAMP,
    
    INDEX idx_case_hypotheses (case_id),
    INDEX idx_status (status),
    INDEX idx_category (category),
    
    CHECK (likelihood >= 0.0 AND likelihood <= 1.0),
    CHECK (category IN ('code', 'config', 'environment', 'network', 'data', 'hardware', 'external', 'human', 'other')),
    CHECK (status IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired'))
);

-- Solutions
CREATE TABLE solutions (
    solution_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id),
    solution_type VARCHAR(30) NOT NULL,
    title VARCHAR(200) NOT NULL,
    immediate_action TEXT,
    longterm_fix TEXT,
    proposed_at TIMESTAMP NOT NULL,
    proposed_by VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP,
    applied_by VARCHAR(255),
    verified_at TIMESTAMP,
    verification_method VARCHAR(500),
    verification_evidence_id VARCHAR(15),
    effectiveness FLOAT,
    
    INDEX idx_case_solutions (case_id, proposed_at),
    INDEX idx_solution_type (solution_type),
    
    CHECK (effectiveness IS NULL OR (effectiveness >= 0.0 AND effectiveness <= 1.0))
);

-- Turn history
CREATE TABLE turn_history (
    turn_id SERIAL PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id),
    turn_number INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    progress_made BOOLEAN NOT NULL,
    outcome VARCHAR(30) NOT NULL,
    
    INDEX idx_case_turns (case_id, turn_number),
    INDEX idx_timestamp (timestamp),
    
    CHECK (turn_number >= 0),
    UNIQUE (case_id, turn_number)
);
```

### 14.2 JSON/JSONB Fields

For PostgreSQL, several complex nested structures can be stored as JSONB:

```sql
ALTER TABLE cases 
    ADD COLUMN consulting JSONB,
    ADD COLUMN problem_verification JSONB,
    ADD COLUMN path_selection JSONB,
    ADD COLUMN working_conclusion JSONB,
    ADD COLUMN root_cause_conclusion JSONB,
    ADD COLUMN degraded_mode JSONB,
    ADD COLUMN escalation_state JSONB,
    ADD COLUMN documentation JSONB;

-- Indexes on JSONB fields
CREATE INDEX idx_temporal_state ON cases ((problem_verification->>'temporal_state'));
CREATE INDEX idx_urgency_level ON cases ((problem_verification->>'urgency_level'));
CREATE INDEX idx_degraded_active ON cases ((degraded_mode->>'exited_at')) WHERE degraded_mode IS NOT NULL;
```

### 14.3 Queries

```sql
-- Find stuck investigations
SELECT case_id, title, turns_without_progress, updated_at
FROM cases
WHERE status = 'investigating' 
  AND turns_without_progress >= 3
ORDER BY updated_at DESC;

-- Find cases by milestone completion
SELECT c.case_id, c.title, ip.*
FROM cases c
JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE ip.symptom_verified = TRUE
  AND ip.root_cause_identified = FALSE
  AND c.status = 'investigating';

-- Find cases with validated hypotheses
SELECT c.case_id, c.title, h.statement, h.likelihood
FROM cases c
JOIN hypotheses h ON c.case_id = h.case_id
WHERE h.status = 'validated';

-- Average time to resolution by organization
SELECT 
    organization_id,
    AVG(EXTRACT(EPOCH FROM (closed_at - created_at))) / 3600 as avg_hours_to_close,
    COUNT(*) as total_cases
FROM cases
WHERE status IN ('resolved', 'closed')
GROUP BY organization_id;

-- Evidence collection patterns
SELECT 
    category,
    COUNT(*) as evidence_count,
    AVG(collected_at_turn) as avg_turn_collected
FROM evidence
GROUP BY category;
```

---

## 15. Implementation Guide

### 15.1 Creating a New Case

```python
def create_case(
    user_id: str,
    organization_id: str,
    title: Optional[str] = None
) -> Case:
    """
    Create a new case in CONSULTING status.
    
    Note: Problem description comes from conversation, not creation parameter.
    LLM will formalize problem into proposed_problem_statement across multiple turns
    based on conversation history provided in prompts.
    
    Args:
        user_id: User creating the case
        organization_id: Organization case belongs to
        title: Optional case title (auto-generated if not provided)
    """
    
    case = Case(
        user_id=user_id,
        organization_id=organization_id,
        title=title or generate_default_title(),  # System-generated default: "Case-MMDD-N"
        status=CaseStatus.CONSULTING,
        consulting=ConsultingData()  # Starts empty - LLM fills via conversation
    )
    
    # Record initial status
    case.status_history.append(
        CaseStatusTransition(
            from_status=CaseStatus.CONSULTING,  # First transition uses same status
            to_status=CaseStatus.CONSULTING,
            triggered_at=case.created_at,
            triggered_by="system",
            reason="Case created"
        )
    )
    
    return case
```

### 15.2 Starting Investigation

```python
def start_investigation(case: Case, user_id: str) -> None:
    """Transition from CONSULTING to INVESTIGATING"""
    
    # Validate can start
    is_valid, error = validate_status_transition(case, CaseStatus.INVESTIGATING)
    if not is_valid:
        raise ValueError(error)
    
    # Ensure problem statement was confirmed
    if not case.consulting.problem_statement_confirmed:
        raise ValueError("Cannot start investigation without confirmed problem statement")
    
    if not case.consulting.proposed_problem_statement:
        raise ValueError("No proposed_problem_statement to confirm")
    
    # SET DESCRIPTION (CRITICAL!)
    # This is the canonical problem description that will be displayed in UI
    case.description = case.consulting.proposed_problem_statement
    
    # Mark decision made
    case.consulting.decided_to_investigate = True
    case.consulting.decision_made_at = datetime.now(timezone.utc)
    
    # Create ProblemVerification with confirmed description
    case.problem_verification = ProblemVerification(
        symptom_statement=case.description  # Use confirmed description
    )
    
    # Transition status
    transition_status(
        case=case,
        to_status=CaseStatus.INVESTIGATING,
        triggered_by=user_id,
        reason="User confirmed problem statement and decided to investigate formally"
    )
```

### 15.3 Recording Turn Progress

```python
def record_turn(
    case: Case,
    user_message: str,
    agent_response: str
) -> TurnProgress:
    """Record what happened in one turn"""
    
    # Capture state before agent processing
    progress_before = case.progress.dict()
    evidence_count_before = len(case.evidence)
    
    # Agent processes turn (updates case state)
    # ... agent work happens here ...
    
    # Capture state after
    progress_after = case.progress.dict()
    
    # Detect milestones completed
    milestones_completed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]
    
    # Detect evidence added
    evidence_added = [
        e.evidence_id for e in case.evidence[evidence_count_before:]
    ]
    
    # Determine progress
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
    
    # Update case
    case.turn_history.append(turn)
    case.current_turn += 1
    case.updated_at = datetime.now(timezone.utc)
    
    # Track progress streak
    if progress_made:
        case.turns_without_progress = 0
    else:
        case.turns_without_progress += 1
    
    # Check for degraded mode
    if case.turns_without_progress >= 3:
        enter_degraded_mode(case, DegradedModeType.NO_PROGRESS)
    
    return turn
```

### 15.4 Adding Evidence

```python
def add_evidence(
    case: Case,
    category: EvidenceCategory,
    summary: str,
    content_ref: str,
    source_type: EvidenceSourceType,
    form: EvidenceForm,
    collected_by: str
) -> Evidence:
    """Add evidence and process milestone advancement"""
    
    evidence = Evidence(
        category=category,
        primary_purpose=determine_primary_purpose(category, case),
        summary=summary,
        content_ref=content_ref,
        source_type=source_type,
        form=form,
        collected_by=collected_by,
        collected_at_turn=case.current_turn
    )
    
    # Validate
    is_valid, error = validate_evidence_category(evidence, case)
    if not is_valid:
        raise ValueError(error)
    
    # Process milestone advancement
    milestones_advanced = process_evidence_for_milestones(evidence, case)
    evidence.advances_milestones = milestones_advanced
    
    # Add to case
    case.evidence.append(evidence)
    
    return evidence

def process_evidence_for_milestones(
    evidence: Evidence,
    case: Case
) -> List[str]:
    """Determine which milestones this evidence advances"""
    
    milestones = []
    
    if evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE:
        if contains_symptom_indicators(evidence) and not case.progress.symptom_verified:
            case.progress.symptom_verified = True
            milestones.append('symptom_verified')
        
        if contains_timeline_data(evidence) and not case.progress.timeline_established:
            case.progress.timeline_established = True
            milestones.append('timeline_established')
        
        # ... other verification milestones ...
    
    elif evidence.category == EvidenceCategory.CAUSAL_EVIDENCE:
        # Check if any hypothesis was validated by this evidence
        for hypothesis in case.hypotheses.values():
            link = hypothesis.evidence_links.get(evidence.evidence_id)
            if link and link.stance == EvidenceStance.STRONGLY_SUPPORTS:
                hypothesis.status = HypothesisStatus.VALIDATED
                case.progress.root_cause_identified = True
                case.progress.root_cause_confidence = hypothesis.likelihood
                milestones.append('root_cause_identified')
                break
    
    elif evidence.category == EvidenceCategory.RESOLUTION_EVIDENCE:
        if indicates_solution_success(evidence):
            case.progress.solution_verified = True
            milestones.append('solution_verified')
    
    return milestones
```

### 15.5 Closing Case

```python
def close_case(
    case: Case,
    user_id: str,
    closure_reason: str
) -> None:
    """Close case (either RESOLVED or CLOSED status)"""
    
    # Determine terminal status
    if closure_reason == "resolved":
        new_status = CaseStatus.RESOLVED
        
        # Validate solution was verified
        if not case.progress.solution_verified:
            raise ValueError("Cannot mark RESOLVED without solution verification")
    else:
        new_status = CaseStatus.CLOSED
    
    # Validate transition
    is_valid, error = validate_status_transition(case, new_status)
    if not is_valid:
        raise ValueError(error)
    
    # Set closure details
    case.closure_reason = closure_reason
    case.closed_at = datetime.now(timezone.utc)
    
    if new_status == CaseStatus.RESOLVED:
        case.resolved_at = case.closed_at
    
    # Transition status
    transition_status(
        case=case,
        to_status=new_status,
        triggered_by=user_id,
        reason=f"Case closed: {closure_reason}"
    )
    
    # Generate documentation (optional)
    if case.progress.solution_verified:
        generate_documentation(case)
```

---

**Document Version**: 2.0  
**Last Updated**: 2025-11-03  
**Status**: Production Specification  
**Alignment**: Investigation Architecture Specification v2.0