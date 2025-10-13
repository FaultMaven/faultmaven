"""Investigation State Models for OODA Framework

This module defines the comprehensive investigation state models for FaultMaven's
OODA (Observe-Orient-Decide-Act) framework with 7 investigation phases.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
- docs/architecture/evidence-collection-and-tracking-design.md
- docs/architecture/case-lifecycle-management.md

Key Components:
- InvestigationState: Root state with 5 layers (Metadata, Lifecycle, OODA, Evidence, Memory)
- Investigation Phases: 7 phases (0-6) from Intake to Document
- OODA Engine: Observe-Orient-Decide-Act cycle management
- Hierarchical Memory: Hot/Warm/Cold memory tiers for token optimization
- Engagement Modes: Consultant vs Lead Investigator
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Enums
# =============================================================================


class InvestigationPhase(int, Enum):
    """Investigation phases (0-indexed) in OODA framework

    7 phases from problem confirmation through documentation:
    - Phase 0: Intake (problem confirmation, Consultant mode)
    - Phase 1: Blast Radius (scope assessment, OODA begins)
    - Phase 2: Timeline (temporal context)
    - Phase 3: Hypothesis (theory generation)
    - Phase 4: Validation (hypothesis testing, full OODA)
    - Phase 5: Solution (fix application and verification)
    - Phase 6: Document (artifact generation)
    """
    INTAKE = 0          # Problem confirmation and consent
    BLAST_RADIUS = 1    # Impact scope assessment
    TIMELINE = 2        # Temporal context establishment
    HYPOTHESIS = 3      # Root cause theory generation
    VALIDATION = 4      # Systematic hypothesis testing
    SOLUTION = 5        # Fix implementation and verification
    DOCUMENT = 6        # Knowledge capture and artifacts


class OODAStep(str, Enum):
    """OODA framework steps for tactical execution

    4 steps defining HOW to investigate within each phase:
    - Observe: Gather information and evidence
    - Orient: Analyze and contextualize data
    - Decide: Choose action or hypothesis
    - Act: Execute test or apply solution
    """
    OBSERVE = "observe"    # 📊 Gather data and evidence
    ORIENT = "orient"      # 🧭 Analyze and contextualize
    DECIDE = "decide"      # 💡 Choose action/hypothesis
    ACT = "act"            # ⚡ Execute test/solution


class EngagementMode(str, Enum):
    """Agent engagement modes based on investigation state

    - Consultant: Expert colleague providing guidance (Phase 0)
    - Lead Investigator: War room lead driving resolution (Phases 1-6)
    """
    CONSULTANT = "consultant"              # Reactive, follows user lead
    LEAD_INVESTIGATOR = "lead_investigator"  # Proactive, guides methodology


class InvestigationStrategy(str, Enum):
    """Investigation approach - speed vs depth

    Selected when Lead Investigator Mode activated:
    - Active Incident: Speed priority, mitigation first
    - Post-Mortem: Thoroughness priority, complete RCA
    """
    ACTIVE_INCIDENT = "active_incident"  # Service down, prioritize mitigation
    POST_MORTEM = "post_mortem"          # Historical analysis, thoroughness


class HypothesisStatus(str, Enum):
    """Hypothesis lifecycle states"""
    PENDING = "pending"        # Not yet tested
    TESTING = "testing"        # Currently under evaluation
    VALIDATED = "validated"    # Confirmed by evidence
    REFUTED = "refuted"        # Disproved by evidence
    RETIRED = "retired"        # Abandoned (low confidence/anchoring)


# =============================================================================
# Phase-to-OODA Mapping
# =============================================================================


class PhaseOODAMapping(BaseModel):
    """Defines which OODA steps are active per investigation phase"""

    phase: InvestigationPhase
    active_steps: List[OODAStep] = Field(default_factory=list)
    intensity: str = Field(..., description="light, medium, full")
    expected_iterations: Tuple[int, int] = Field(..., description="(min, max) iterations")
    primary_goal: str

    @classmethod
    def get_mapping(cls, phase: InvestigationPhase) -> "PhaseOODAMapping":
        """Get OODA configuration for a specific phase"""
        mappings = {
            InvestigationPhase.INTAKE: cls(
                phase=InvestigationPhase.INTAKE,
                active_steps=[],  # No OODA in Phase 0
                intensity="none",
                expected_iterations=(0, 0),
                primary_goal="Problem confirmation and consent"
            ),
            InvestigationPhase.BLAST_RADIUS: cls(
                phase=InvestigationPhase.BLAST_RADIUS,
                active_steps=[OODAStep.OBSERVE, OODAStep.ORIENT],
                intensity="light",
                expected_iterations=(1, 2),
                primary_goal="Scope impact assessment"
            ),
            InvestigationPhase.TIMELINE: cls(
                phase=InvestigationPhase.TIMELINE,
                active_steps=[OODAStep.OBSERVE, OODAStep.ORIENT],
                intensity="light",
                expected_iterations=(1, 2),
                primary_goal="Establish temporal context"
            ),
            InvestigationPhase.HYPOTHESIS: cls(
                phase=InvestigationPhase.HYPOTHESIS,
                active_steps=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE],
                intensity="medium",
                expected_iterations=(2, 3),
                primary_goal="Formulate root cause theories"
            ),
            InvestigationPhase.VALIDATION: cls(
                phase=InvestigationPhase.VALIDATION,
                active_steps=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE, OODAStep.ACT],
                intensity="full",
                expected_iterations=(3, 6),
                primary_goal="Systematic hypothesis testing"
            ),
            InvestigationPhase.SOLUTION: cls(
                phase=InvestigationPhase.SOLUTION,
                active_steps=[OODAStep.DECIDE, OODAStep.ACT, OODAStep.ORIENT],
                intensity="medium",
                expected_iterations=(2, 4),
                primary_goal="Implement and verify solution"
            ),
            InvestigationPhase.DOCUMENT: cls(
                phase=InvestigationPhase.DOCUMENT,
                active_steps=[OODAStep.ORIENT],  # Synthesis mode only
                intensity="light",
                expected_iterations=(1, 1),
                primary_goal="Generate artifacts and capture learnings"
            ),
        }
        return mappings[phase]


# =============================================================================
# Core Investigation State Models
# =============================================================================


class ProblemConfirmation(BaseModel):
    """Phase 0 problem triage structure (informal, pre-investigation)

    Created in Consultant Mode before Lead Investigator activation.
    Distinct from AnomalyFrame (Phase 1 formal definition).

    Workflow:
    1. Agent synthesizes from user conversation
    2. Creates ProblemConfirmation
    3. User consents → Mode switch to Lead Investigator
    4. Phase 1 creates AnomalyFrame (formal)
    """
    problem_statement: str = Field(..., description="User's problem description")
    affected_components: List[str] = Field(default_factory=list, description="Approximate components")
    severity: str = Field(..., description="Initial severity assessment")
    impact: str = Field(..., description="Who/what is affected")
    investigation_approach: str = Field(..., description="Proposed strategy")
    estimated_evidence_needed: List[str] = Field(
        default_factory=list,
        description="Expected evidence categories"
    )


class AnomalyFrame(BaseModel):
    """Formal problem definition created in Phase 1 (Blast Radius)

    Created via OODA Orient step after evidence gathering.
    More structured than ProblemConfirmation, revisable as investigation progresses.
    """
    statement: str = Field(..., description="Formal problem statement")
    affected_components: List[str] = Field(default_factory=list)
    affected_scope: str = Field(..., description="Impact radius")
    started_at: datetime = Field(..., description="When issue started")
    severity: str = Field(..., description="low, medium, high, critical")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in framing")

    # Revision tracking
    revision_count: int = Field(default=0, description="Number of times revised")
    framed_at_turn: int = Field(..., description="Turn when initially framed")
    revised_at_turns: List[int] = Field(default_factory=list, description="Turns when revised")


class Hypothesis(BaseModel):
    """Root cause hypothesis with confidence management and anchoring prevention"""

    hypothesis_id: str = Field(default_factory=lambda: str(uuid4()))
    statement: str = Field(..., description="Hypothesis statement")
    category: str = Field(..., description="infrastructure, code, config, etc.")

    # Confidence management
    likelihood: float = Field(..., ge=0.0, le=1.0, description="Current confidence")
    initial_likelihood: float = Field(..., ge=0.0, le=1.0, description="Original confidence")
    confidence_trajectory: List[Tuple[int, float]] = Field(
        default_factory=list,
        description="(turn, confidence) history"
    )

    # Status tracking
    status: HypothesisStatus = Field(default=HypothesisStatus.PENDING)
    created_at_turn: int = Field(...)
    last_updated_turn: int = Field(...)

    # Evidence linkage
    supporting_evidence: List[str] = Field(default_factory=list, description="Evidence IDs")
    refuting_evidence: List[str] = Field(default_factory=list, description="Evidence IDs")

    # Anchoring prevention
    iterations_without_progress: int = Field(default=0)
    last_progress_at_turn: Optional[int] = None
    retirement_reason: Optional[str] = None

    def apply_confidence_decay(self, current_turn: int) -> float:
        """Decay confidence if no progress made

        Args:
            current_turn: Current conversation turn

        Returns:
            Updated likelihood after decay
        """
        turns_since_progress = current_turn - (self.last_progress_at_turn or self.created_at_turn)

        if turns_since_progress >= 2:
            decay_factor = 0.85 ** self.iterations_without_progress
            self.likelihood = self.initial_likelihood * decay_factor

            if self.likelihood < 0.3:
                self.status = HypothesisStatus.RETIRED
                self.retirement_reason = "Confidence decayed below threshold"

        return self.likelihood


class HypothesisTest(BaseModel):
    """Record of hypothesis testing during Phase 4 (Validation)"""

    test_id: str = Field(default_factory=lambda: str(uuid4()))
    hypothesis_id: str = Field(..., description="Hypothesis being tested")
    test_description: str = Field(..., description="What was tested")
    evidence_required: List[str] = Field(default_factory=list, description="Evidence request IDs")
    evidence_obtained: List[str] = Field(default_factory=list, description="Evidence provided IDs")

    result: str = Field(..., description="supports, refutes, inconclusive")
    confidence_change: float = Field(..., description="Change in hypothesis likelihood")

    executed_at_turn: int = Field(...)
    ooda_iteration: int = Field(..., description="Which OODA iteration")


class OODAIteration(BaseModel):
    """One complete OODA cycle (Observe→Orient→Decide→Act)"""

    iteration_id: str = Field(default_factory=lambda: str(uuid4()))
    iteration_number: int = Field(..., description="Iteration count in current phase")
    phase: InvestigationPhase = Field(..., description="Which investigation phase")

    # Timing
    started_at_turn: int = Field(...)
    completed_at_turn: Optional[int] = None
    duration_turns: int = Field(default=0)

    # Steps executed
    steps_completed: List[OODAStep] = Field(default_factory=list)
    steps_skipped: List[OODAStep] = Field(default_factory=list)

    # State changes during this iteration
    anomaly_refined: bool = Field(default=False, description="AnomalyFrame updated")
    new_evidence_collected: int = Field(default=0)
    hypotheses_generated: int = Field(default=0)
    hypotheses_tested: int = Field(default=0)
    hypotheses_retired: int = Field(default=0)

    # Progress tracking
    confidence_delta: float = Field(default=0.0, description="Change in max hypothesis confidence")
    new_insights: List[str] = Field(default_factory=list)
    made_progress: bool = Field(default=False)
    stall_reason: Optional[str] = None


class PhaseTransition(BaseModel):
    """Record of phase advancement"""

    from_phase: InvestigationPhase
    to_phase: InvestigationPhase
    transition_reason: str = Field(..., description="Why phase advanced")
    occurred_at_turn: int = Field(...)
    completion_criteria_met: List[str] = Field(default_factory=list)
    skipped_phases: List[InvestigationPhase] = Field(
        default_factory=list,
        description="Phases skipped (e.g., critical incident)"
    )


# =============================================================================
# Memory Management Models
# =============================================================================


class MemorySnapshot(BaseModel):
    """Compressed memory snapshot for warm/cold tiers"""

    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    iteration_range: Tuple[int, int] = Field(..., description="(start, end) iteration numbers")
    summary: str = Field(..., description="LLM-generated summary")
    key_facts: List[str] = Field(default_factory=list)
    confidence_changes: Dict[str, float] = Field(default_factory=dict)
    evidence_collected: List[str] = Field(default_factory=list, description="Evidence IDs")
    decisions_made: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HierarchicalMemory(BaseModel):
    """Token-optimized memory system with hot/warm/cold tiers

    Token Budget: ~1,600 tokens total (vs 4,500+ unmanaged)
    - Hot Memory: ~500 tokens (last 2 iterations, full fidelity)
    - Warm Memory: ~300 tokens (iterations 3-5, summarized)
    - Cold Memory: ~100 tokens (older, key facts only)
    - Persistent Insights: ~100 tokens (always accessible)

    Compression: Triggered every 3 turns (64% reduction)
    """

    # Hot memory: Full detail, last 2 iterations
    hot_memory: List[OODAIteration] = Field(
        default_factory=list,
        description="Last 2 OODA iterations (full fidelity)"
    )

    # Warm memory: Summarized, iterations 3-5
    warm_snapshots: List[MemorySnapshot] = Field(
        default_factory=list,
        description="Iterations 3-5 (summarized, max 3)"
    )

    # Cold memory: Key facts, older iterations
    cold_snapshots: List[MemorySnapshot] = Field(
        default_factory=list,
        description="Older iterations (key facts only, max 5)"
    )

    # Persistent: Always accessible insights
    persistent_insights: List[str] = Field(
        default_factory=list,
        description="Key learnings maintained across all turns"
    )

    last_compression_turn: int = Field(default=0)

    def should_compress(self, current_turn: int) -> bool:
        """Check if compression should be triggered

        Args:
            current_turn: Current conversation turn

        Returns:
            True if compression needed (every 3 turns)
        """
        return current_turn % 3 == 0 and current_turn > self.last_compression_turn


# =============================================================================
# Investigation State Layers
# =============================================================================


class InvestigationMetadata(BaseModel):
    """Metadata Layer - Identity and timestamps"""

    investigation_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(..., description="Session this investigation belongs to")
    user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    current_turn: int = Field(default=1, description="Current conversation turn")
    engagement_mode: EngagementMode = Field(default=EngagementMode.CONSULTANT)


class InvestigationLifecycle(BaseModel):
    """Lifecycle Layer - Phase progression and status"""

    current_phase: InvestigationPhase = Field(default=InvestigationPhase.INTAKE)
    phase_name: str = Field(default="intake", description="intake, blast_radius, etc.")
    entry_phase: InvestigationPhase = Field(
        default=InvestigationPhase.INTAKE,
        description="Where investigation started"
    )

    # Case status (from case-lifecycle-management.md)
    case_status: str = Field(default="intake", description="Case lifecycle status")
    urgency_level: str = Field(default="medium", description="low, medium, high, critical")
    investigation_strategy: Optional[InvestigationStrategy] = None

    # Phase history
    phase_history: List[PhaseTransition] = Field(default_factory=list)
    turns_in_current_phase: int = Field(default=0)
    phase_complete: bool = Field(default=False)

    # Document phase tracking
    artifacts_offered: bool = Field(
        default=False,
        description="Whether documentation artifacts have been offered to user"
    )


class OODAEngineState(BaseModel):
    """OODA Engine Layer - Tactical execution state"""

    ooda_active: bool = Field(default=False, description="OODA framework active (Phases 1-6)")
    current_step: Optional[OODAStep] = None
    current_iteration: int = Field(default=0, description="OODA iteration count in current phase")

    # Core investigation objects
    anomaly_frame: Optional[AnomalyFrame] = None  # Created in Phase 1
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    tests_performed: List[HypothesisTest] = Field(default_factory=list)

    # Iteration tracking
    iterations: List[OODAIteration] = Field(default_factory=list)

    # Anchoring detection
    anchoring_detected: bool = Field(default=False)
    forced_alternatives_at_turn: List[int] = Field(default_factory=list)
    confidence_trajectory: List[float] = Field(
        default_factory=list,
        description="Max hypothesis confidence per turn"
    )


class EvidenceLayer(BaseModel):
    """Evidence Layer - Evidence collection and tracking

    Note: Evidence schemas defined in evidence.py
    This layer references those models
    """

    evidence_requests: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs (see evidence.py)"
    )
    evidence_provided: List[str] = Field(
        default_factory=list,
        description="Evidence provided IDs (see evidence.py)"
    )
    evidence_coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    critical_evidence_blocked: List[str] = Field(
        default_factory=list,
        description="Critical evidence requests blocked by user"
    )


class MemoryLayer(BaseModel):
    """Memory Layer - Hierarchical memory management"""

    hierarchical_memory: HierarchicalMemory = Field(default_factory=HierarchicalMemory)
    token_budget_used: int = Field(default=0, description="Approximate tokens in state")
    compression_count: int = Field(default=0, description="Times compression triggered")


# =============================================================================
# Root Investigation State
# =============================================================================


class InvestigationState(BaseModel):
    """Root investigation state with 5 hierarchical layers

    Design: investigation-phases-and-ooda-integration.md

    Layers:
    1. Metadata Layer - Identity and timestamps
    2. Lifecycle Layer - Phase progression
    3. OODA Engine Layer - Tactical execution
    4. Evidence Layer - Evidence tracking
    5. Memory Layer - Token-optimized memory

    Token Budget: ~1,600 tokens (vs 4,500+ unmanaged, 64% reduction)
    """

    # Layer 1: Metadata
    metadata: InvestigationMetadata = Field(default_factory=InvestigationMetadata)

    # Layer 2: Lifecycle
    lifecycle: InvestigationLifecycle = Field(default_factory=InvestigationLifecycle)

    # Layer 3: OODA Engine
    ooda_engine: OODAEngineState = Field(default_factory=OODAEngineState)

    # Layer 4: Evidence (references evidence.py models)
    evidence: EvidenceLayer = Field(default_factory=EvidenceLayer)

    # Layer 5: Memory
    memory: MemoryLayer = Field(default_factory=MemoryLayer)

    # Problem confirmation (Phase 0)
    problem_confirmation: Optional[ProblemConfirmation] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() + 'Z'
        }
