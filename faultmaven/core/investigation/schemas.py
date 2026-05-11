"""Investigation Schemas for Structured Output

This module defines the Pydantic models used for LLM structured output generation.
It implements the "Form-Filler" pattern where the LLM returns a structured state update
along with its natural language response.

Design Reference:
- docs/architecture/investigation-engine/agent-behavioral-rules.md
- docs/architecture/investigation-engine/prompt-templates.md
- Investigation Architecture v2.0

Key Features:
- Stage-specific schemas (Dynamic Views) to minimize token usage
- Strict validation fields
- Integration with Case domain models
- Optional[List[T]] pattern for all list fields to handle LLM returning null

IMPORTANT: List Field Pattern (Fix for Turn 3 Pydantic Validation Errors)
===========================================================================
All list fields use the pattern: Optional[List[T]] = Field(default_factory=list)

This handles LLM behavior where empty lists are often returned as null instead of [].
The pattern provides:
- Accept null: Optional[List[T]] allows None values
- Accept []: Direct empty list accepted
- Accept ["item"]: Direct list with items accepted
- Default to []: default_factory=list ensures None becomes []

Without this pattern, LLM returning null causes Pydantic validation errors:
  "Input should be a valid array [type=list_type, input_value=None]"

Applied to 23 list fields across all schemas (see git blame for specific changes).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from faultmaven.modules.agent.domain.models.agentic import QueryIntent  # noqa: F401
from faultmaven.modules.case.contracts import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    HypothesisStatus,
    InvestigationStage,
    SolutionType,
    TurnOutcome,
)

# =============================================================================
# Unified Ingestion Pipeline (v4.1)
# =============================================================================


@dataclass
class Attachment:
    """A file or pasted data submitted with a turn."""

    content: bytes
    filename: str
    content_type: str
    source_metadata: Optional[Dict[str, Any]] = None


@dataclass
class TurnPayload:
    """Universal turn payload — canonical input to the investigation pipeline.

    Every user turn is represented as an optional query + optional attachments.
    At least one must be provided. If attachments are present, they are preprocessed
    through Tier 0+1 before the LLM sees them. If no query is provided, an implicit
    system query is injected.
    """

    query: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)
    intent: Optional["QueryIntent"] = None

    @property
    def has_attachments(self) -> bool:
        return len(self.attachments) > 0

    @property
    def has_query(self) -> bool:
        return self.query is not None and self.query.strip() != ""


# =============================================================================
# Shared Components
# =============================================================================


class ReasoningConclusion(BaseModel):
    """Single reasoning step in the internal analysis."""

    observation: str = Field(description="What was observed in the evidence")
    inference: str = Field(description="What this implies about the problem")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in this inference"
    )


class InternalReasoning(BaseModel):
    """
    Internal reasoning that must be completed BEFORE state_updates.
    Forces LLM to justify decisions with evidence trail.

    Reference: Prompt Engineering Guide Section 13
    """

    evidence_analyzed: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs that were considered in this turn",
    )
    conclusions: Optional[List[ReasoningConclusion]] = Field(
        default_factory=list,
        description="Step-by-step reasoning from evidence to conclusions",
    )
    milestone_justifications: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "MANDATORY: For EVERY milestone set to True in milestones, provide a justification here. "
            "Format: {milestone_name: 'justification citing specific evidence IDs'}. "
            "Example: {'symptom_verified': 'Connection errors confirmed per ev_abc123'}. "
            "DO NOT leave empty {} when completing milestones - validation will reject."
        ),
    )
    uncertainties: Optional[List[str]] = Field(
        default_factory=list,
        description="What remains unclear or uncertain after this analysis",
    )


class ProblemConfirmation(BaseModel):
    """Agent's initial understanding of the problem."""

    problem_type: Literal["error", "slowness", "unavailability", "data_issue", "other"]
    severity_guess: Literal["critical", "high", "medium", "low", "unknown"]
    preliminary_guidance: Optional[str] = None


class PreliminaryUrgency(BaseModel):
    """Early urgency signal based on BUSINESS IMPACT."""

    level: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    is_ongoing: bool
    is_incident_report: bool = Field(
        default=False,
        description=(
            "True ONLY when the user is actively reporting a current problem "
            "affecting their systems. False for informational/how-to questions, "
            "even if the topic involves failures or outages."
        ),
    )
    impact_assessment: str
    mitigation_hint: Optional[str] = None


class KnowledgeMatch(BaseModel):
    """Knowledge base match for potential instant resolution."""

    match_type: Literal["past_case", "runbook", "documentation"]
    match_likelihood: float = Field(ge=0.0, le=1.0)
    match_summary: str
    suggested_solution: Optional[str] = None


class KnowledgeResolution(BaseModel):
    """Records instant resolution via KB match."""

    match_id: str
    match_type: Literal["past_case", "runbook", "documentation"]
    solution_applied: str
    user_confirmation: str


class MilestoneUpdates(BaseModel):
    """Milestones the LLM can set to True (never False).

    Two categories:

    1. PROGRESS INDICATORS — provide context and analytics, do not drive
       stage transitions.
    2. STAGE-GATE MILESTONES — drive stage transitions when set. The LLM is
       the compliance detector: it sets these in structured output when it
       recognizes that the user has complied with a ProposedAction
       (Framework doc §4.1).

    NOTE: solution_verified is NOT settable here. It requires the User-Agent
    Handshake pattern via ProposedTransition.
    """

    # Progress indicators (LLM-settable, non-stage-driving)
    symptom_verified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    # solution_proposed removed (3F) — set programmatically at ProposedAction creation
    root_cause_method: Optional[
        Literal[
            "direct_analysis",
            "hypothesis_validation",
            "single_shot_validation",
            "correlation",
            "user_provided",
            "other",
        ]
    ] = Field(
        None,
        description="How root cause was identified: direct_analysis | hypothesis_validation | single_shot_validation | correlation | user_provided | other",
    )

    # Stage-gate milestones (LLM-settable, drive stage transitions)
    # The LLM sets these when it detects user compliance with a pending
    # ProposedAction — the user's action is the trigger, the LLM recognizes it.
    mitigation_accepted: Optional[bool] = None
    mitigation_verified: Optional[bool] = None
    solution_accepted: Optional[bool] = None


class ProblemVerificationUpdate(BaseModel):
    """Updates to problem verification data."""

    symptom_correction: Optional[str] = None
    scope_impact: Optional[str] = None
    timeline_start: Optional[str] = None
    timeline_duration: Optional[str] = None
    changes_list: Optional[List[str]] = Field(default_factory=list)
    rca_infeasible: Optional[bool] = None
    rca_infeasible_rationale: Optional[str] = None


class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case.

    Milestone Attribution (Option 2.5 - Three-Tier Logic):

    Tier 1: MilestoneUpdates drives milestone state (turn-level, LLM specifies)
    Tier 2: System infers advances_milestones from category (automatic, handles 90%)
    Tier 3: LLM overrides via advances_milestones field (optional, handles 10%)

    The advances_milestones field is OPTIONAL. If omitted, the system will automatically
    infer milestone attribution based on the evidence category and milestones completed
    this turn using the CATEGORY_MILESTONE_MAP.

    Only provide advances_milestones explicitly when:
    - The automatic inference would be wrong (rare edge case)
    - You need to specify a subset of eligible milestones
    - The evidence contributed to a milestone outside normal category mapping

    Design Reference:
    - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
    - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md
    """

    summary: str
    extract: Optional[str] = Field(
        default=None,
        description=(
            "Optional verbatim quote: a short snippet (one or a few lines) "
            "drawn directly from a file, command output, or conversation, "
            "to ground the finding in concrete evidence. Leave empty when "
            "the summary is self-contained. Do NOT put a filename or "
            "'file:NAME' here — file-backed evidence is created automatically "
            "from uploaded attachments by the preprocessing pipeline; "
            "evidence_to_add is for derived findings the agent draws from "
            "those uploads, not for re-creating file references. Persisted "
            "as Evidence.extract on the resulting row."
        ),
    )
    category: EvidenceCategory
    source_type: EvidenceSourceType
    likelihood: float = Field(0.8, ge=0.0, le=1.0)

    @field_validator("summary", mode="before")
    @classmethod
    def truncate_summary(cls, v):
        # Domain Evidence.content has no length cap, but the summary is the
        # short-form description used for UI. Verbose providers (e.g. DeepSeek
        # on logs-zookeeper) overshoot 500; truncate softly so the turn does
        # not 500. Same graceful-degrade pattern as the binary-decode
        # placeholder. See ISS-057.
        if isinstance(v, str) and len(v) > 500:
            return v[:489] + " [...trunc]"
        return v

    advances_milestones: Optional[List[str]] = Field(
        default=None,
        description=(
            "OPTIONAL: Override system-inferred milestone attribution. "
            "If omitted, system infers from category + milestones completed this turn. "
            "Only specify when automatic inference is incorrect (rare edge case)."
        ),
    )

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v):
        """
        Post-010: strict category validation. Unrecognized categories
        fail loudly (no CONTEXTUAL_EVIDENCE fallback — it's no longer a
        valid value). The LLM is instructed to use exactly one of the
        four post-010 categories (symptom/causal/mitigation/solution).
        """
        if isinstance(v, str):
            return EvidenceCategory(v)
        return v

    @field_validator("likelihood")
    @classmethod
    def validate_likelihood(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class HypothesisToAdd(BaseModel):
    """New hypothesis to track."""

    statement: str
    category: HypothesisCategory
    likelihood: float = Field(ge=0.0, le=1.0)
    rationale: str


class HypothesisUpdate(BaseModel):
    """Update to an existing hypothesis.

    Pair integrity: when ``status`` is set to ``REFUTED``, ``refutation_reason``
    MUST also be provided (max 200 chars). The orchestration layer rejects
    updates that carry one without the other. If there is no disproof
    evidence, use ``status=RETIRED`` instead (no reason required).
    """

    likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    status: Optional[HypothesisStatus] = None
    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Required when setting status=REFUTED. Cite the specific evidence "
            "or reasoning that disproves the hypothesis. Not used for other "
            "statuses. status=REFUTED and refutation_reason travel together."
        ),
    )

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        """
        Validate hypothesis status against HypothesisStatus enum.

        Maps known LLM aliases (e.g., "TESTING" -> ACTIVE) and rejects
        unrecognized values by returning None (no status update).
        """
        if v is None:
            return None
        if isinstance(v, HypothesisStatus):
            return v
        if isinstance(v, str):
            # Map known LLM aliases to valid enum values
            aliases = {"TESTING": "active", "testing": "active"}
            mapped = aliases.get(v, v.lower())
            try:
                return HypothesisStatus(mapped)
            except ValueError:
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    f"LLM returned unrecognized hypothesis status '{v}', "
                    f"ignoring status update",
                    extra={
                        "status_attempted": v,
                        "alert_team": "llm_integration",
                        "severity": "warning",
                        "metric": "hypothesis.status_fallback",
                    },
                )
                return None
        return v


class HypothesisEvidenceLinkToAdd(BaseModel):
    """Link evidence to a hypothesis."""

    hypothesis_id_ref: str = Field(
        description="Hypothesis ID or 'new_index_N' if created this turn"
    )
    evidence_id_ref: str = Field(
        description="Evidence ID or 'new_index_N' if created this turn"
    )
    stance: EvidenceStance
    reasoning: str
    stance_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0)",
    )


class JournalEntryOutput(BaseModel):
    """A journal entry produced by the LLM for the investigation journal."""

    entry_type: Literal[
        "finding", "decision", "user_context", "ruled_out", "blocker", "milestone"
    ] = Field(description="Type of journal entry")
    content: str = Field(
        description="The distilled insight (max 200 chars)",
    )

    @field_validator("content")
    @classmethod
    def truncate_content(cls, v: str) -> str:
        """Truncate instead of rejecting — LLMs often exceed the 200 char guideline."""
        if len(v) > 200:
            return v[:197] + "..."
        return v

    evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID this entry relates to, if any",
    )
    hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID this entry relates to, if any",
    )


class WorkingConclusionUpdate(BaseModel):
    """Current working theory of the case.

    All fields are optional to allow empty working_conclusion during early INQUIRY phase
    when the agent doesn't yet have enough information to form a theory.
    """

    summary: Optional[str] = None
    likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    next_steps: Optional[List[str]] = Field(default_factory=list)
    blockers: Optional[List[str]] = Field(default_factory=list)


class BlockerType(str, Enum):
    """Type of blocker preventing investigation progress."""

    DATA_CORRUPTED = "data_corrupted"
    DATA_MISSING = "data_missing"
    DATA_INCOMPLETE = "data_incomplete"
    DATA_ACCESS_DENIED = "data_access_denied"
    TOOL_UNAVAILABLE = "tool_unavailable"
    EXTERNAL_DEPENDENCY = "external_dependency"


class EvidenceQualityIssue(BaseModel):
    """
    Quality issue with evidence that may block or limit investigation.

    Reference: Prompt Engineering Guide Section 14 (lines 3352-3376)
    """

    evidence_id: str = Field(description="Evidence ID with quality issue")
    issue_type: str = Field(
        description="corrupted | incomplete | missing_metadata | ambiguous | etc."
    )
    severity: Literal["blocking", "limiting", "minor"] = Field(
        description="blocking=cannot proceed, limiting=reduced confidence, minor=note only"
    )
    description: str = Field(description="What's wrong with this evidence")
    workaround: Optional[str] = Field(
        None, description="How to work around this issue if possible"
    )


class MissingCriticalData(BaseModel):
    """
    Proactive detection of critical data blockers.
    Flags data quality issues to the agent via system feedback so it can
    communicate limitations and suggest alternatives in its response.

    Reference: Prompt Engineering Guide Section 14 (lines 3316-3351)
    """

    blocker_type: BlockerType = Field(description="Type of data blocker")
    description: str = Field(description="Specific description of what's missing/wrong")
    what_was_expected: str = Field(description="What data was expected")
    what_was_found: str = Field(description="What was actually found")
    impact: str = Field(description="How this blocks investigation progress")
    suggested_alternatives: Optional[List[str]] = Field(
        default_factory=list,
        description="Alternative data sources or approaches user could try",
    )


class ProposedTransition(BaseModel):
    """
    Agent signals a state transition. Engine handles everything else.

    Terminal transitions (→ RESOLVED or → CLOSED) require user confirmation;
    this signal sets pending_transition. Engine derives closure_reason from
    case state programmatically — the LLM does not author it. The engine
    builds the confirmation prompt summary from existing helpers.
    """

    to_status: str = Field(description="Target status: 'resolved' or 'closed'")
    evidence_ids: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs supporting this transition proposal",
    )


class RootCauseConclusionUpdate(BaseModel):
    """Final root cause conclusion."""

    root_cause: str
    mechanism: str
    evidence_ids: List[str] = Field(default_factory=list)
    likelihood: float = Field(default=0.7, ge=0.0, le=1.0)


class SolutionToAdd(BaseModel):
    """Proposed solution."""

    description: str
    solution_type: SolutionType
    estimated_impact: str
    risks: str
    commands: Optional[List[str]] = Field(
        default_factory=list,
        description="Specific commands for the user to execute",
    )


# =============================================================================
# Template Schemas
# =============================================================================


class SuggestedFollowUp(BaseModel):
    """A follow-up suggestion for the user, classified by intended user action."""

    label: str = Field(
        description="Short card title (3-8 words, e.g., 'Search KB for incidents')"
    )
    action_type: Literal["COOPERATIVE", "EVIDENCE", "FREE_SPEECH"] = Field(
        default="COOPERATIVE",
        description=(
            "COOPERATIVE = click submits query or copies command; "
            "EVIDENCE = informational, tells user what data to provide; "
            "FREE_SPEECH = informational, asks user a question with framework hints"
        ),
    )
    payload: str = Field(
        description=(
            "COOPERATIVE: pre-composed query text or shell command; "
            "EVIDENCE: description of what data to provide and why; "
            "FREE_SPEECH: the question text shown to the user"
        )
    )
    body: Optional[str] = Field(
        default=None,
        description="Reasoning text shown on card (why the user should take this action)",
    )

    # COOPERATIVE fields
    cooperative_action: Optional[Literal["query_submit", "command_copy"]] = Field(
        default=None,
        description="query_submit = auto-submit as user message; command_copy = copy to clipboard",
    )

    # Shell command prefixes for auto-detection of command_copy
    _COMMAND_PREFIXES: ClassVar[tuple] = (
        "kubectl",
        "redis-cli",
        "docker",
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "grep",
        "awk",
        "sed",
        "cat",
        "tail",
        "head",
        "journalctl",
        "systemctl",
        "service",
        "sudo",
        "apt",
        "yum",
        "dnf",
        "pip",
        "npm",
        "yarn",
        "helm",
        "terraform",
        "aws",
        "gcloud",
        "az",
        "psql",
        "mysql",
        "mongo",
        "etcdctl",
        "crictl",
        "ctr",
        "nslookup",
        "dig",
        "ping",
        "traceroute",
        "netstat",
        "ss",
        "iptables",
        "top",
        "htop",
        "free",
        "df",
        "du",
        "lsof",
        "strace",
        "tcpdump",
    )

    @model_validator(mode="after")
    def _infer_cooperative_action(self) -> "SuggestedFollowUp":
        """Auto-detect command_copy when payload looks like a shell command.

        When action_type is COOPERATIVE and cooperative_action is not set,
        check if the payload starts with a known CLI tool prefix. If so,
        set cooperative_action to "command_copy" so the frontend copies
        to clipboard instead of submitting as a chat message.
        """
        if self.action_type == "COOPERATIVE" and not self.cooperative_action:
            payload_first_word = self.payload.strip().split()[0] if self.payload else ""
            if payload_first_word in self._COMMAND_PREFIXES:
                self.cooperative_action = "command_copy"
            else:
                self.cooperative_action = "query_submit"
        return self

    # FREE_SPEECH fields
    hints: Optional[List[str]] = Field(
        default=None,
        description="Short framework tags guiding what aspects the user should address (e.g., 'symptoms', 'timeline', 'affected services')",
    )

    # Optional intent metadata — when present, the frontend sends this as
    # the QueryIntent alongside the payload. This bridges COOPERATIVE
    # suggestions with the deterministic intent routing system, so clicks
    # on transition confirmations flow through IntentType.CONFIRMATION
    # instead of plain-text pattern matching.
    intent: Optional[Dict[str, Any]] = Field(
        default=None,
        description="QueryIntent metadata to send with payload (e.g., {type: 'confirmation', confirmation_value: true})",
    )


class BaseInteractionResponse(BaseModel):
    """Base class for all agent responses."""

    agent_response: str = Field(description="Natural language response to the user.")
    suggested_follow_ups: Optional[List[SuggestedFollowUp]] = Field(
        default=None,
        description="2-4 contextual follow-up actions the user can take. Each should be specific to the current investigation state.",
    )


class InquiryResponse(BaseInteractionResponse):
    """Response schema for INQUIRY status."""

    class InquiryStateUpdate(BaseModel):
        problem_confirmation: Optional[ProblemConfirmation] = None
        proposed_problem_statement: Optional[str] = None
        preliminary_urgency: Optional[PreliminaryUrgency] = None
        knowledge_match: Optional[KnowledgeMatch] = None
        knowledge_resolution: Optional[KnowledgeResolution] = None
        user_confirmed_investigation: bool = Field(
            default=False,
            description=(
                "Set to True when the user confirms the problem statement. "
                "Explicit: 'Yes', 'Correct', 'Let's investigate', 'That's right'. "
                "Implicit (after problem statement was presented): user asks diagnostic "
                "questions or expresses urgency about the problem. "
                "Set False when: same turn you first present the problem statement, "
                "user explicitly disagrees, user submits new data with a question "
                "(answer the question first), or message is unrelated."
            ),
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose INQUIRY → CLOSED for user confirmation when the user asks "
                "to close/cancel without investigating. Use to_status='closed'. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )

        # Post-010 (strict evidence model): evidence_to_add is intentionally
        # absent from INQUIRY. Evidence presupposes a confirmed claim; during
        # INQUIRY the claim is still being formed. Uploaded files persist in
        # the ``uploaded_files`` table; the LLM evaluates them and creates
        # evidence rows via ``evidence_to_add`` once the case transitions to
        # INVESTIGATING. See
        # docs/architecture/investigation-engine/evidence-driven-investigation-framework.md
        # §5 for the canonical rule.

    state_updates: InquiryStateUpdate


class TerminalResponse(BaseInteractionResponse):
    """Response schema for RESOLVED/CLOSED status."""

    class TerminalStateUpdate(BaseModel):
        final_summary_update: Optional[str] = None
        documentation_links: Optional[List[str]] = Field(default_factory=list)

    state_updates: TerminalStateUpdate


# =============================================================================
# Investigation Schemas (Dynamic Views)
# =============================================================================


class InvestigationResponse_Diagnosis(BaseInteractionResponse):
    """Schema for DIAGNOSIS stage — covers symptom verification, hypothesis work, and solution proposal."""

    class DiagnosisStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: TurnOutcome

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: DiagnosisStateUpdate


class InvestigationResponse_Mitigation(BaseInteractionResponse):
    """Schema for MITIGATION stage — applying and verifying temporary fix."""

    class MitigationStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: TurnOutcome

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: MitigationStateUpdate


class InvestigationResponse_Treatment(BaseInteractionResponse):
    """Schema for TREATMENT stage — verifying fix, extended diagnosis if fix fails."""

    class TreatmentStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: TurnOutcome

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: TreatmentStateUpdate


class InvestigationResponse_General(BaseInteractionResponse):
    """Fallback 'Full' schema if stage is ambiguous or degraded."""

    class GeneralStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: TurnOutcome

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: GeneralStateUpdate


def get_schema_for_stage(stage: Optional[InvestigationStage]) -> Any:
    """Factory to get the appropriate Pydantic model for the current stage."""
    if stage == InvestigationStage.DIAGNOSIS:
        return InvestigationResponse_Diagnosis
    elif stage == InvestigationStage.MITIGATION:
        return InvestigationResponse_Mitigation
    elif stage == InvestigationStage.TREATMENT:
        return InvestigationResponse_Treatment
    else:
        return InvestigationResponse_General
