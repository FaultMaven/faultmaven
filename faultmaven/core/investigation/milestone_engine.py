"""Data-Driven and Opportunistic Investigation Engine

This module implements the data-driven investigation system that replaces
legacy process-based frameworks. Instead of rigid phase orchestration, this engine
completes milestones opportunistically based on data availability.

Key Design Principles:
- Process-Agnostic: No rigid phase transitions - milestones complete when data is available
- Opportunistic: Multiple milestones can complete in one turn
- Data-Driven Context: Status-based prompt generation based on available data
- Progress tracked via InvestigationProgress

Design Reference:
- docs/architecture/milestone-based-investigation-framework.md


Architecture:
- Process turn → Generate status-based prompt → Invoke LLM → Process response
- Update milestones based on LLM state_updates
- Track turn progress for analytics
- Automatic status transitions (INVESTIGATING → RESOLVED)
"""

import asyncio
import difflib
import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

# Module initialization
logger = logging.getLogger(__name__)

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.llm_error_handler import ErrorAction, LLMErrorHandler
from faultmaven.core.investigation.prompts.templates import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
)
from faultmaven.core.investigation.stagnation_detector import (
    StagnationBreaker,
    StagnationDetector,
    StagnationType,
)
from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationSeverity,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    ProgressMetrics,
    calculate_progress_metrics,
    generate_working_conclusion,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputMode,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisStatus,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationPath,
    InvestigationProgress,
    InvestigationStage,
    KnowledgeMatch,
    KnowledgeResolution,
    ProblemVerification,
    ProposedAction,
    Solution,
    SolutionType,
    TemporalState,
    TurnOutcome,
    TurnProgress,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    determine_investigation_path,
)
from faultmaven.modules.knowledge.contracts import IKnowledgeService

# =============================================================================
# Evidence Category - Milestone Mapping (Option 2.5: System-Inferred Attribution)
# =============================================================================
#
# This mapping defines which milestones each evidence category can potentially advance.
# Used for automatic milestone attribution via the _infer_milestones() function.
#
# Design Reference:
# - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
# - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md
#
# Derived from MILESTONE_EVIDENCE_EXPECTATIONS in evidence_processor.py
#
# Three-Tier Logic:
#   Tier 1: MilestoneUpdates drives state (turn-level, LLM specifies)
#   Tier 2: System infers advances_milestones from this map (handles 90% of cases)
#   Tier 3: LLM can override with explicit specification (handles 10% edge cases)

CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified",  # Confirms problem exists
        "scope_assessed",  # Identifies impact scope
        "timeline_established",  # Provides temporal data
        "changes_identified",  # Shows what changed (deployment logs, config diffs)
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        "changes_identified",  # Identifies which change caused the problem
        "root_cause_identified",  # Demonstrates root cause
        "solution_proposed",  # Justifies proposed solution
    ],
    EvidenceCategory.MITIGATION_EVIDENCE: [
        # Mitigation evidence verifies temp fix effectiveness
        # mitigation_verified is a stage-gate milestone (set by compliance detection)
    ],
    EvidenceCategory.SOLUTION_EVIDENCE: [
        # Solution evidence verifies permanent fix effectiveness
        # solution_verified is a stage-gate milestone (set via User-Agent Handshake)
    ],
    EvidenceCategory.CONTEXTUAL_EVIDENCE: [
        # Contextual evidence provides baseline/environmental info
        # It informs investigation but doesn't directly advance milestones
    ],
}


def _determine_action_type(
    case: Case, solution_type: SolutionType
) -> InvestigationActionType:
    """
    Determine whether a proposed solution is a MITIGATION or SOLUTION action.

    Used when creating ProposedAction from SolutionToAdd. The action_type
    determines which stage-gate milestone is set by compliance detection:
    - MITIGATION → mitigation_accepted → enters MITIGATION stage
    - SOLUTION → solution_accepted → enters TREATMENT stage

    Logic:
    1. WORKAROUND solution_type → always MITIGATION (explicitly temporary)
    2. MITIGATION_FIRST path + no mitigation accepted yet → MITIGATION
    3. Otherwise → SOLUTION
    """
    if solution_type == SolutionType.WORKAROUND:
        return InvestigationActionType.MITIGATION

    if (
        case.path_selection
        and case.path_selection.path == InvestigationPath.MITIGATION_FIRST
        and not case.progress.mitigation_accepted
    ):
        return InvestigationActionType.MITIGATION

    return InvestigationActionType.SOLUTION


def _apply_stage_gate_side_effects(
    case: Case,
    completed_gates: set[str],
    user_message: str,
    metadata: dict[str, Any],
) -> None:
    """Apply side effects when stage-gate milestones are completed.

    When the LLM sets a stage-gate milestone, we:
    1. Mark the corresponding pending ProposedAction as "accepted"
    2. Create an ActionAttempt audit record
    3. Handle mitigation flag reset for re-entry (3B)

    This replaces the old compliance_detector.py logic — the LLM now
    detects compliance per Framework §4.1.
    """
    # Find the most recent pending action
    pending_action = None
    for action in reversed(case.proposed_actions):
        if action.status == "pending":
            pending_action = action
            break

    if pending_action:
        pending_action.status = "accepted"
        # Create audit trail
        attempt = ActionAttempt(
            action_id=pending_action.action_id,
            user_message=user_message[:10000],
            submitted_at=datetime.now(UTC),
            compliance_detected=True,
            compliance_confidence=1.0,  # LLM-detected = full confidence
        )
        case.action_attempts.append(attempt)
        logger.info(
            f"Stage-gate milestone(s) {completed_gates} set by LLM for case "
            f"{case.case_id} (action {pending_action.action_id}, "
            f"type={pending_action.action_type.value})"
        )

    # 3B: Mitigation flag reset for re-entry
    # When mitigation_verified is set, both mitigation flags reset so the
    # mitigation path can be re-entered if a future mitigation is needed.
    if "mitigation_verified" in completed_gates:
        case.progress.mitigation_accepted = False
        case.progress.mitigation_verified = False
        logger.info(
            f"Reset mitigation flags for case {case.case_id} (return to DIAGNOSIS)"
        )

    metadata["compliance_detected"] = True
    metadata["progress_made"] = True


def _infer_milestones(
    category: EvidenceCategory, milestones_completed_this_turn: list[str]
) -> list[str]:
    """
    Infer which milestones this evidence likely advanced.

    This implements Tier 2 of the three-tier milestone attribution logic:
    - Tier 1: MilestoneUpdates drives milestone state (turn-level, LLM specifies)
    - Tier 2: System infers advances_milestones from category (THIS FUNCTION - handles 90%)
    - Tier 3: LLM overrides when explicit (optional, handles 10% edge cases)

    Design Reference:
    - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
    - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md

    Args:
        category: The evidence category (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL)
        milestones_completed_this_turn: Milestones completed this turn from MilestoneUpdates

    Returns:
        List of milestone names this evidence contributed to

    Logic:
        1. Get eligible milestones for this category from CATEGORY_MILESTONE_MAP
        2. Intersect with milestones completed this turn (from MilestoneUpdates)
        3. Result = milestones this evidence can claim credit for

    Example:
        category = SYMPTOM_EVIDENCE
        milestones_completed_this_turn = ["symptom_verified", "scope_assessed"]
        eligible = ["symptom_verified", "scope_assessed", "timeline_established", "changes_identified"]
        result = ["symptom_verified", "scope_assessed"]

    Key Insight:
        With one-file-per-turn constraint (UI limitation), inference is UNAMBIGUOUS.
        There's only one evidence record per turn, so all eligible milestones completed
        that turn get attributed to it. No guessing needed.

    Note:
        - CONTEXTUAL_EVIDENCE returns [] (doesn't directly advance milestones)
        - If category not in map, returns [] (safe fallback)
        - LLM can override by explicitly setting advances_milestones in EvidenceToAdd
    """
    # Get eligible milestones for this category
    eligible_milestones = CATEGORY_MILESTONE_MAP.get(category, [])

    # Intersect with milestones completed this turn
    # This is the "system inference" - we know this evidence contributed to these milestones
    inferred = [m for m in milestones_completed_this_turn if m in eligible_milestones]

    logger.debug(
        f"_infer_milestones: category={category.value}, "
        f"milestones_completed_this_turn={milestones_completed_this_turn}, "
        f"eligible={eligible_milestones}, "
        f"inferred={inferred}"
    )

    return inferred


# =============================================================================
# Content Sanitization
# =============================================================================


def _truncate_content_ref(
    content_ref: str | None, max_length: int = 4950
) -> str | None:
    """
    Defensively truncate content_ref to prevent ValidationError from max_length constraint.

    The Evidence domain model enforces max_length=5000 for content_ref. This helper
    ensures content never exceeds the limit, preventing crashes while preserving
    data quality.

    Args:
        content_ref: The content reference string (log excerpt, file ref, etc.)
        max_length: Maximum allowed length (default 4950 to leave room for suffix)

    Returns:
        Truncated string with "..." suffix if truncation occurred, or None if input is None

    Design Decision:
        - Truncate at 4950 chars (leaving 50-char buffer below 5000 limit)
        - Add "..." suffix to indicate truncation
        - Log warning when truncation occurs for observability
    """
    if content_ref is None:
        return None

    if len(content_ref) <= max_length:
        return content_ref

    # Truncation needed
    truncated = content_ref[:max_length] + "..."
    logger.warning(
        f"content_ref truncated from {len(content_ref)} to {len(truncated)} chars "
        f"(max_length={max_length})"
    )
    return truncated


# =============================================================================
# Reasoning Validation
# =============================================================================


def validate_reasoning_first(
    response_obj: BaseInteractionResponse, case: Case
) -> tuple[bool, list[str]]:
    """
    Validate that milestone completions are justified with internal reasoning.

    This function enforces the "Reasoning-First" pattern where the LLM must provide
    justifications for milestone completions BEFORE setting state updates. This prevents
    the LLM from arbitrarily completing milestones without evidence-based reasoning.

    EXCEPTION: Validation is skipped during terminal state transitions to allow graceful
    case closure without forcing justifications. This handles the scenario where:
    - User confirms a pending transition via the User-Agent Handshake
    - Case is transitioning to RESOLVED or CLOSED

    Reference: Prompt Engineering Guide Section 13 (lines 3236-3281)

    Args:
        response_obj: LLM's structured response (InquiryResponse, InvestigationResponse_*, or TerminalResponse)
        case: Current case state

    Returns:
        (is_valid, error_messages): Tuple of validation result and list of error messages

    Skip Conditions (validation bypassed):
        1. Response is InquiryResponse or TerminalResponse (no investigation milestones)
        2. Case is already in terminal state (RESOLVED or CLOSED)
        3. Case has a pending_transition (user confirmation in progress)
    """
    errors = []

    # Debug logging for Turn 2 issue
    logger.debug(
        f"validate_reasoning_first: response_type={type(response_obj).__name__}, "
        f"case_status={case.status.value}, "
        f"is_InquiryResponse={isinstance(response_obj, InquiryResponse)}, "
        f"is_TerminalResponse={isinstance(response_obj, TerminalResponse)}"
    )

    # Only validate investigation responses (not INQUIRY or TERMINAL)
    if isinstance(response_obj, (InquiryResponse, TerminalResponse)):
        logger.debug("Skipping reasoning validation (INQUIRY or TERMINAL response)")
        return True, []

    # Skip validation if case is already in terminal state
    if case.is_terminal:
        logger.debug("Skipping reasoning validation (case already in terminal state)")
        return True, []

    # Check if response has internal_reasoning field
    internal_reasoning = getattr(response_obj, "internal_reasoning", None)
    milestones = getattr(response_obj.state_updates, "milestones", None)

    if not milestones:
        # No milestones being completed, no validation needed
        return True, []

    # Get list of milestone fields being completed (set to True)
    completed_milestones = []
    milestone_dict = milestones.model_dump(exclude_none=True)
    for milestone_name, value in milestone_dict.items():
        if isinstance(value, bool) and value is True:
            completed_milestones.append(milestone_name)

    if not completed_milestones:
        # No milestones actually completed, no validation needed
        return True, []

    # ===== TERMINAL TRANSITION EXCEPTION =====
    # Skip validation if case has a pending transition (User-Agent Handshake in progress).
    # The user has already confirmed the transition, so we allow graceful closure
    # without forcing the LLM to justify additional milestones.
    if case.status == CaseStatus.INVESTIGATING:
        has_pending = hasattr(case, "pending_transition") and case.pending_transition
        already_solution_verified = case.progress.solution_verified

        if has_pending or already_solution_verified:
            logger.debug(
                f"Skipping reasoning validation (terminal transition in progress: "
                f"pending={has_pending}, solution_verified={already_solution_verified})"
            )
            return True, []

    # If milestones are being completed, internal_reasoning is REQUIRED
    if not internal_reasoning:
        errors.append(
            f"Milestones {completed_milestones} completed without internal_reasoning. "
            "You MUST provide internal_reasoning with justifications when completing milestones."
        )
        return False, errors

    # Check 1: All completed milestones must have justifications
    for milestone in completed_milestones:
        if milestone not in internal_reasoning.milestone_justifications:
            errors.append(
                f"Milestone '{milestone}' completed without justification. "
                f"You MUST add an entry to internal_reasoning.milestone_justifications. "
                f"Required format: milestone_justifications: {{{milestone}: 'justification citing specific evidence IDs'}}. "
                f"Example: {{{milestone}: 'Confirmed via ev_abc123 (logs) showing X and ev_def456 (metrics) showing Y'}}. "
                "DO NOT leave milestone_justifications as empty {{}}."
            )

    # Check 1.5: Warn if trying to complete milestones with no actionable evidence.
    # Contextual evidence (raw uploads) cannot justify milestones — only
    # LLM-classified evidence (symptom, causal, mitigation, solution) counts.
    from faultmaven.modules.case.contracts import EvidenceCategory

    evidence_being_added = (
        getattr(response_obj.state_updates, "evidence_to_add", []) or []
    )
    non_contextual_evidence = [
        e for e in case.evidence if e.category != EvidenceCategory.CONTEXTUAL_EVIDENCE
    ]
    non_contextual_being_added = [
        e
        for e in evidence_being_added
        if e.category != EvidenceCategory.CONTEXTUAL_EVIDENCE
    ]
    has_actionable_evidence = bool(non_contextual_evidence) or bool(
        non_contextual_being_added
    )

    if internal_reasoning.milestone_justifications and not has_actionable_evidence:
        errors.append(
            "Cannot complete milestones when no actionable evidence has been collected. "
            "Contextual evidence alone cannot justify milestones. "
            "You must first analyze and classify evidence before completing milestones."
        )

    # Check 2: REMOVED - Category-based validation no longer requires evidence_analyzed
    # evidence_analyzed is now OPTIONAL and only used for historical turn references
    # Milestone validation is done via evidence categories in evidence_processor.py

    # Check 3: Validate turn references if provided (optional)
    # If evidence_analyzed contains turn references (e.g., "turn_2"), validate format
    for ref in internal_reasoning.evidence_analyzed:
        if isinstance(ref, str) and ref.startswith("turn_"):
            try:
                turn_num = int(ref.split("_")[1])
                if turn_num < 1 or turn_num > case.current_turn:
                    errors.append(
                        f"Invalid turn reference '{ref}': turn number must be between 1 and current turn ({case.current_turn})"
                    )
            except (IndexError, ValueError):
                errors.append(
                    f"Invalid turn reference format: '{ref}'. Expected format: 'turn_N' where N is a number"
                )

    return len(errors) == 0, errors


def _post_process_llm_response(
    updates: Any,
    user_message: str,
    case: Case,
) -> Any:
    """
    Post-process LLM response — currently a no-op pass-through.

    Previously this function ran regex-based pattern detection on the user
    message to create fallback evidence when the LLM didn't produce any.
    That approach was removed because:

    1. It second-guessed the LLM with crude regexes. When the LLM
       deliberately chose NOT to classify a message as data (e.g., an SSH
       banner with incidental "memory" / "8%" text), the fallback overrode
       that judgment and created bogus SYMPTOM_EVIDENCE records.

    2. It conflated "user pasted data into the text box" with "user
       submitted external data for analysis". A user who pastes terminal
       output as a conversational message should get a conversational
       response — or a clarifying question — not silent evidence creation.

    3. When attachments existed, it duplicated the attachment pipeline's
       evidence with a lower-quality regex-derived record.

    The LLM already sees every user message and can:
    - Create evidence via ``evidence_to_add`` when it recognizes data.
    - Ask for clarification when the message is ambiguous.
    - Treat non-data messages as conversation.

    If the LLM consistently fails to recognise a specific class of data,
    the fix belongs in the prompt or LLM schema, not in a post-hoc regex
    layer that cannot understand context.

    Args:
        updates: Parsed LLM response (InquiryResponse or InvestigationResponse_*)
        user_message: Original user message (retained for future use / logging)
        case: Current case state

    Returns:
        The updates object, unmodified.
    """
    evidence_to_add = getattr(updates, "evidence_to_add", []) or []
    logger.debug(
        f"Post-processing LLM response: "
        f"evidence_to_add_count={len(evidence_to_add)}"
    )
    return updates


# =============================================================================
# Resolution Summary Helpers
# =============================================================================


def _get_root_cause_summary(case) -> str:
    """Extract a brief root cause description from the case for confirmation prompts."""
    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        cause = case.root_cause_conclusion.root_cause
        return cause[:200] + "..." if len(cause) > 200 else cause
    if case.working_conclusion and getattr(case.working_conclusion, "statement", None):
        stmt = case.working_conclusion.statement
        return stmt[:200] + "..." if len(stmt) > 200 else stmt
    return "Not yet identified"


def _get_solution_summary(case) -> str:
    """Extract a brief solution description from the case for confirmation prompts."""
    if case.solutions:
        sol = case.solutions[-1]  # Most recent solution
        title = getattr(sol, "title", None)
        if title:
            return title
        longterm = getattr(sol, "longterm_fix", None)
        if longterm:
            return longterm[:200] + "..." if len(longterm) > 200 else longterm
        immediate = getattr(sol, "immediate_action", None)
        if immediate:
            return immediate[:200] + "..." if len(immediate) > 200 else immediate
    return "Not yet documented"


def _investigation_confirmation_suggestions() -> list:
    """Generate COOPERATIVE follow-up suggestions for investigation confirmation.

    Used when the dropdown triggers INQUIRY → INVESTIGATING and a problem
    statement already exists. One positive (confirm) and one mild negative (refine).
    """
    return [
        {
            "label": "Yes, let's investigate",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Yes, that's correct. Let's investigate.",
            "body": "Confirm the problem statement and start the investigation.",
        },
        {
            "label": "Not quite, let me clarify",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not quite — let me clarify the problem before we investigate.",
            "body": "Refine the problem statement before starting the investigation.",
        },
    ]


def _build_resolution_confirmation(case) -> str:
    """Build the resolution confirmation prompt with optional enrichment hints.

    Shows what we have on record (root cause + solution) and suggests
    additional details that would improve the resolution documentation
    and any runbook generated from it. Makes clear these are optional.
    """
    parts = [
        "Here's what I have on record:\n",
        f"- **Root cause**: {_get_root_cause_summary(case)}",
        f"- **Solution**: {_get_solution_summary(case)}",
    ]

    # Check what enrichment data is missing — these improve docs but don't block resolution
    enrichment_hints = []

    evidence_count = len(case.evidence) if case.evidence else 0
    if evidence_count == 0:
        enrichment_hints.append("diagnostic evidence (logs, metrics, error messages)")

    has_verification = False
    if case.solutions:
        has_verification = any(
            getattr(s, "verification_method", None) for s in case.solutions
        )
    if not has_verification:
        enrichment_hints.append("how you verified the fix worked")

    has_commands = False
    if case.solutions:
        has_commands = any(
            getattr(s, "commands", None) or getattr(s, "implementation_steps", None)
            for s in case.solutions
        )
    if not has_commands:
        enrichment_hints.append("specific commands or steps you used")

    if enrichment_hints:
        parts.append(
            "\nThis is enough to resolve. If you'd like to improve the documentation "
            "(and any runbook generated from it), you can also share:"
        )
        for hint in enrichment_hints:
            parts.append(f"- {hint}")
        parts.append("\nConfirm to resolve now, or share more details first.")
    else:
        parts.append(
            "\nIs this correct? Once you confirm, I'll mark the case as resolved."
        )

    return "\n".join(parts)


def _resolution_confirmation_suggestions() -> list:
    """Generate COOPERATIVE follow-up suggestions for resolution confirmation.

    Mirrors the INQUIRY confirmation pattern: one positive (confirm resolution)
    and one mild negative (continue investigating).
    """
    return [
        {
            "label": "Yes, mark as resolved",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Yes, the issue is resolved. Please mark this case as resolved.",
            "body": "Confirm resolution and close the investigation.",
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not yet — I'd like to continue investigating before resolving.",
            "body": "Decline resolution and continue refining the root cause or exploring alternative solutions.",
        },
    ]


def _close_confirmation_suggestions() -> list:
    """Generate COOPERATIVE follow-up suggestions for close (abandon) confirmation.

    Mirrors the INQUIRY and RESOLVED confirmation patterns: one positive
    (confirm close) and one mild negative (continue investigating).
    """
    return [
        {
            "label": "Yes, close this case",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Yes, close this case without resolution.",
            "body": "Confirm closing the case. A summary will be generated.",
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not yet — I'd like to continue investigating.",
            "body": "Keep the investigation open and continue working toward a solution.",
        },
    ]


def _runbook_suggestion() -> list:
    """Generate COOPERATIVE suggestion for runbook generation.

    Always offered at resolution time. Evaluation (readiness, deduplication)
    happens when the user accepts — not at suggestion time.
    """
    return [
        {
            "label": "Generate runbook from this case",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Generate a runbook from this resolved case",
            "body": "Create a reusable troubleshooting runbook from the root cause and solution.",
        },
    ]


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


class MilestoneEngine:
    """
    Data-Driven and Opportunistic Investigation Engine.

    Replaces legacy process-based engines with a simpler, more flexible approach where
    the agent completes milestones opportunistically based on available data.

    Responsibilities:
    - Generate prompts based on case status (INQUIRY, INVESTIGATING, RESOLVED)
    - Invoke LLM with appropriate schema
    - Process LLM responses and update case state
    - Track milestone completion and turn progress
    - Automatic status transitions when milestones complete

    Key Design Principles:
    - No phase orchestration - milestones complete when data is available
    - Status-based prompts instead of phase-based
    - Multiple milestones can complete in single turn
    - Repository abstraction for persistence (no direct DB access)
    """

    def __init__(
        self,
        llm_provider: ILLMProvider,
        repository: Any,  # Case repository abstraction (duck typing)
        investigation_tools: Any,
        evidence_service: Any,
        knowledge_service: IKnowledgeService | None = None,
        trace_enabled: bool = True,
        checkpoint_service: Any | None = None,
        da_provider: Any | None = None,
        da_model: str | None = None,
        sanitizer: Any | None = None,
        redis_client: Any | None = None,
        report_service: Any | None = None,
    ):
        """Initialize milestone engine.

        Args:
            llm_provider: LLM provider implementation (ILLMProvider interface)
            repository: Case repository with save/get methods
            investigation_tools: AgentToolRegistry with investigation tools
                (search_file, deep_analysis, etc.). Required — DA turns use
                these for evidence searching during generation.
            evidence_service: Evidence service for tool context. Required for
                building tool execution context.
            knowledge_service: Optional knowledge service for KB searches
            trace_enabled: Enable observability tracing
            checkpoint_service: Optional CheckpointService for state snapshots
            da_provider: Dedicated provider for DA (directed analysis) turns
                (configured via DA_PROVIDER in .env).
                When None, falls back to llm_provider.
            da_model: Model to use with da_provider. When None,
                the provider's default model is used.
            sanitizer: DataSanitizer for case-scoped PII redaction.
                When None, PII redaction at the engine level is disabled.
            redis_client: Async Redis client for persisting redaction
                registries across turns. When None, registries are
                in-memory only (consistent within turn).
            report_service: Optional ReportGenerationService for auto-generating
                reports on terminal transitions. Fire-and-forget — failure
                does not block the transition.
        """
        self.llm_provider = llm_provider
        self.repository = repository
        self.knowledge_service = knowledge_service
        self.trace_enabled = trace_enabled
        self.checkpoint_service = checkpoint_service
        self.investigation_tools = investigation_tools
        self.evidence_service = evidence_service
        self.da_provider = da_provider
        self.da_model = da_model
        self.sanitizer = sanitizer
        self.redis_client = redis_client
        self.report_service = report_service
        self.hypothesis_manager = create_hypothesis_manager()
        self.state_validator = StateValidator()
        self.stagnation_detector = StagnationDetector()
        self.stagnation_breaker = StagnationBreaker()
        self.llm_error_handler = LLMErrorHandler()

        # G10: Per-case asyncio locks to prevent concurrent process_turn
        # calls on the same case from interleaving and corrupting state
        self._case_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        logger.info("MilestoneEngine initialized with structured output engine")

    async def _auto_generate_report(self, case: "Case") -> None:
        """Fire-and-forget auto-generation of terminal summary.

        Generates RESOLUTION_SUMMARY for RESOLVED cases and CLOSURE_SUMMARY
        for CLOSED cases. Called after case is saved in terminal state.
        Failure is logged but does not propagate — the transition is
        already complete.
        """
        if not getattr(case, "_pending_summary", False):
            logger.debug(
                f"Auto-summary skipped for case {case.case_id}: "
                f"guardrail determined insufficient substance"
            )
            return
        if not self.report_service:
            logger.debug("No report service available — skipping auto-summary")
            return

        try:
            from faultmaven.modules.case.domain.owned_models.report import ReportType

            if case.status == CaseStatus.RESOLVED:
                report_type = ReportType.RESOLUTION_SUMMARY
            elif case.status == CaseStatus.CLOSED:
                report_type = ReportType.CLOSURE_SUMMARY
            else:
                logger.warning(
                    f"Unexpected status {case.status} for auto-summary on case {case.case_id}"
                )
                return

            await self.report_service.generate_reports(case, [report_type])
            logger.info(
                f"Auto-generated {report_type.value} for case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
        except Exception as e:
            logger.warning(
                f"Auto-summary generation failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )

    _REPORT_REGEN_PATTERNS = (
        "regenerate",
        "re-generate",
        "redo the report",
        "redo the summary",
        "new report",
        "new summary",
        "update the report",
        "update the summary",
        "better report",
        "better summary",
        "generate a report",
        "generate a summary",
        "generate report",
        "generate summary",
    )

    _RUNBOOK_CREATION_PATTERNS = (
        "generate a runbook",
        "generate runbook",
        "create a runbook",
        "create runbook",
        "yes, generate",
        "yes, create",
    )

    async def _process_terminal_turn(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle turns on terminal cases: Q&A, report regeneration, runbook creation.

        Terminal cases are immutable — no evidence, milestones, or state changes.
        Three scenarios:
          1. User requests report regeneration → regenerate summary.
          2. User accepts runbook suggestion → evaluate, create draft, direct to Dashboard.
          3. User asks questions about the case → answer via TERMINAL_TEMPLATE.
        """
        msg_lower = user_message.lower()

        # Scenario 1: Report regeneration
        if any(p in msg_lower for p in self._REPORT_REGEN_PATTERNS):
            return await self._handle_report_regeneration(case, metadata)

        # Scenario 2: Runbook creation (RESOLVED cases only)
        if case.status == CaseStatus.RESOLVED and any(
            p in msg_lower for p in self._RUNBOOK_CREATION_PATTERNS
        ):
            return await self._handle_runbook_creation(case, metadata)

        # Scenario 3: Q&A
        return await self._process_terminal_qa(case, user_message, metadata)

    async def _handle_report_regeneration(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Regenerate the terminal summary report for a closed case."""
        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.status == CaseStatus.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution Summary"
        else:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure Summary"

        if not self.report_service:
            return {
                "agent_response": (
                    "Report generation is not available at the moment. "
                    "Please try again later."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        try:
            await self.report_service.generate_reports(case, [report_type])
            agent_response = (
                f"The {report_label} has been regenerated. "
                f"You can view it in the Dashboard."
            )
            logger.info(
                f"Regenerated {report_type.value} for terminal case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
        except Exception as e:
            logger.warning(
                f"Report regeneration failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                f"Failed to regenerate the {report_label}. " f"Please try again later."
            )

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [],
            "case_updated": case,
            "metadata": metadata,
        }

    async def _handle_runbook_creation(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate readiness + dedup, then create runbook draft (fire-and-forget).

        Flow:
        1. Check content readiness (assess_runbook_readiness)
        2. Check deduplication (evaluate_runbook_suggestion)
        3. If eligible: call ConversionService.convert_from_case() in background
        4. Return immediately with a message directing user to Dashboard Drafts
        """
        from faultmaven.core.investigation.terminal_transitions import (
            evaluate_runbook_suggestion,
            RunbookSuggestion,
        )

        # Step 1+2: Evaluate readiness and deduplication
        runbook_kb = None
        if self.knowledge_service and hasattr(self.knowledge_service, "runbook_kb"):
            runbook_kb = self.knowledge_service.runbook_kb

        suggestion = await evaluate_runbook_suggestion(case, runbook_kb)

        if suggestion.verdict == RunbookSuggestion.NOT_READY:
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        if suggestion.verdict == RunbookSuggestion.EXISTING_COVERS:
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Step 3: Create the runbook draft
        conversion_service = getattr(self, "conversion_service", None)
        if not conversion_service:
            logger.warning(
                f"Runbook creation requested for case {case.case_id} but "
                f"conversion_service is not available"
            )
            return {
                "agent_response": (
                    "Runbook generation is not available at the moment. "
                    "You can create one from the Dashboard instead."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Fire-and-forget: kick off conversion in background
        try:
            from faultmaven.modules.knowledge.domain.models.conversion import (
                CaseConversionRequest,
            )

            request = CaseConversionRequest.from_case(case, scope="global")
            # Don't await the full pipeline — fire and forget
            import asyncio

            asyncio.create_task(
                self._run_runbook_conversion(conversion_service, request, case.user_id)
            )

            agent_response = (
                "Creating your runbook draft from this case. "
                "You'll find it in the Dashboard under **Knowledge > Drafts** once it's ready."
            )
            logger.info(
                f"Runbook creation initiated for case {case.case_id}",
                extra={"case_id": case.case_id},
            )
        except Exception as e:
            logger.warning(
                f"Failed to initiate runbook creation for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                "Failed to start runbook generation. "
                "You can try again or create one from the Dashboard."
            )

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [],
            "case_updated": case,
            "metadata": metadata,
        }

    async def _run_runbook_conversion(
        self,
        conversion_service,
        request,
        user_id: str,
    ) -> None:
        """Background task for runbook conversion. Logs success/failure."""
        try:
            result = await conversion_service.convert_from_case(
                request=request,
                user_id=user_id,
            )
            if result.drafts:
                draft = result.drafts[0]
                logger.info(
                    f"Runbook draft created: {draft.runbook_id} "
                    f"(title='{draft.title}', quality={getattr(draft, 'quality_score', 'N/A')})",
                    extra={
                        "case_id": request.case_id,
                        "runbook_id": draft.runbook_id,
                    },
                )
            else:
                logger.warning(
                    f"Runbook conversion completed but no drafts produced "
                    f"for case {request.case_id}",
                    extra={"case_id": request.case_id},
                )
        except Exception as e:
            logger.error(
                f"Background runbook creation failed for case {request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )

    async def _process_terminal_qa(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Process a Q&A turn on a terminal case via the LLM.

        Uses TERMINAL_TEMPLATE and TerminalResponse schema. No state mutations.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.security.case_redaction import (
            CaseRedactionContext,
        )

        redaction_settings = get_settings()
        redaction_ctx = CaseRedactionContext(
            case_id=case.case_id,
            sanitizer=self.sanitizer,
            redis_client=self.redis_client,
            enabled=self._should_redact(),
            ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
        )
        await redaction_ctx.load()

        prompt = get_prompt_for_case(case, user_message)

        # Pass tools with auto tool_choice — LLM decides whether to invoke
        # kb_qa, web_search, etc. based on the user's question.
        tools_kwargs: dict[str, Any] = {}
        if self.investigation_tools:
            tools_kwargs["investigation_tools"] = self._build_da_tool_schemas()
            tools_kwargs["tool_context"] = self._build_tool_context(
                case, intent_data=None
            )
            tools_kwargs["force_tool_use"] = False

        response_obj = await self._generate_structured_output(
            prompt,
            TerminalResponse,
            **tools_kwargs,
            redaction_ctx=redaction_ctx,
            case=case,
        )

        await redaction_ctx.save()

        # Extract follow-up suggestions
        follow_ups = []
        if (
            hasattr(response_obj, "suggested_follow_ups")
            and response_obj.suggested_follow_ups
        ):
            for f in response_obj.suggested_follow_ups:
                suggestion = {
                    "label": f.label,
                    "action_type": f.action_type,
                    "payload": f.payload,
                }
                if f.body:
                    suggestion["body"] = f.body
                if f.cooperative_action:
                    suggestion["cooperative_action"] = f.cooperative_action
                if f.hints:
                    suggestion["hints"] = f.hints
                follow_ups.append(suggestion)

        return {
            "agent_response": response_obj.agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    def _should_redact(self) -> bool:
        """Determine whether PII redaction should be applied at the engine level.

        Checks SANITIZE_PII setting. Returns False when no sanitizer is
        configured (redaction disabled at DI level).
        """
        if not self.sanitizer:
            return False

        from faultmaven.config.settings import get_settings

        return get_settings().protection.sanitize_pii

    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process a single conversation turn with optional structured intent.

        This is the main entry point for the milestone engine. It:
        1. Routes based on intent_type (if provided) or processes normally
        2. Generates status-appropriate prompt
        3. Invokes LLM with structured output
        4. Processes response and updates case state
        5. Records turn progress
        6. Checks for automatic status transitions

        Args:
            case: Current case
            user_message: User's message this turn
            attachments: Optional file attachments
            intent_type: Optional structured intent type (status_transition, confirmation, etc.)
            intent_data: Optional intent-specific data

        Returns:
            {
                "agent_response": str,        # Natural language response to user
                "case_updated": Case,         # Updated case object
                "metadata": {
                    "turn_number": int,
                    "milestones_completed": List[str],
                    "progress_made": bool,
                    "status_transitioned": bool,
                    "outcome": TurnOutcome
                }
            }

        Raises:
            MilestoneEngineError: If processing fails
        """
        # G10: Acquire per-case lock to prevent concurrent turns from
        # interleaving reads/writes on the same case state
        async with self._case_locks[case.case_id]:
            return await self._process_turn_impl(
                case,
                user_message,
                attachments,
                intent_type,
                intent_data,
            )

    async def _process_turn_impl(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inner implementation of process_turn, called under per-case lock."""
        # Add intent information to logger for tracing
        # Note: current_turn has already been incremented by investigation_service before this point
        intent_info = f" [intent={intent_type}]" if intent_type else ""
        logger.info(
            f"Processing turn {case.current_turn} for case {case.case_id} "
            f"(status: {case.status}){intent_info}"
        )

        # Debug logging for Turn 2 issue
        # Note: current_turn already incremented before this point
        if case.status == CaseStatus.INQUIRY:
            logger.info(
                f"Turn {case.current_turn} starting: status={case.status.value}, "
                f"confirmed={case.inquiry.problem_statement_confirmed}, "
                f"decided_to_investigate={case.inquiry.decided_to_investigate}"
            )
        else:
            logger.info(
                f"Turn {case.current_turn} starting: status={case.status.value}, "
                f"stage={case.current_stage}"
            )

        try:
            # Initialize metadata early so it can be used throughout the function
            metadata = {
                "milestones_completed": [],
                "evidence_added": [],
                "hypotheses_generated": [],
                "hypotheses_validated": [],
                "solutions_proposed": [],
                "progress_made": False,
                "status_transitioned": False,
                "outcome": TurnOutcome.CONVERSATION,
            }

            # 0a. Terminal case handling — Q&A and report regeneration only
            if case.is_terminal:
                return await self._process_terminal_turn(case, user_message, metadata)

            # 0b. Pending transition confirmation — short-circuit before LLM
            # When a pending transition exists (User-Agent Handshake), check if
            # the user is confirming or declining BEFORE calling the LLM. This
            # avoids unnecessary LLM calls and prevents schema validation errors
            # from blocking the confirmation.
            if hasattr(case, "pending_transition") and case.pending_transition:
                if not case.pending_transition.get("needs_info"):
                    # Standard confirmation (not awaiting info)
                    if self._user_confirms_transition(user_message):
                        from faultmaven.core.investigation.terminal_transitions import (
                            confirm_pending_transition,
                        )

                        if self.checkpoint_service:
                            to_status = case.pending_transition.get(
                                "to_status", "unknown"
                            )
                            await self.checkpoint_service.create_checkpoint(
                                case,
                                trigger="pre_case_action",
                                metadata={
                                    "from_status": case.status.value,
                                    "to_status": to_status,
                                },
                            )

                        confirm_pending_transition(case, case.user_id)

                        agent_response = (
                            "Case resolved."
                            if case.status == CaseStatus.RESOLVED
                            else "Case closed."
                        )
                        self._record_deterministic_turn(
                            case, user_message or "", agent_response
                        )
                        await self.repository.save(case)

                        # Auto-generate report (fire-and-forget)
                        await self._auto_generate_report(case)

                        follow_ups = (
                            _runbook_suggestion()
                            if case.status == CaseStatus.RESOLVED
                            else []
                        )

                        return {
                            "agent_response": agent_response,
                            "suggested_follow_ups": follow_ups,
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": True,
                                "status_transitioned": True,
                            },
                        }
                    elif self._user_declines_transition(user_message):
                        from faultmaven.core.investigation.terminal_transitions import (
                            cancel_pending_transition,
                        )

                        cancel_pending_transition(case)
                        await self.repository.save(case)
                        # Fall through to normal LLM processing

            # 0c. Detect explicit user intent to close/resolve case
            # This handles cases where user explicitly says "close this case" or "mark as resolved"
            # without relying on LLM to set solution_verified=True
            #
            # CRITICAL DISTINCTION:
            # - CLOSED (without solution): User abandons investigation without finding solution
            # - RESOLVED (with solution): User confirms problem is fixed/resolved
            #
            # TWO COMPLEMENTARY PATHS (Intent-Based Routing Design):
            # 1. EXPLICIT INTENT (frontend buttons/actions) → Skip pattern matching, use intent_data
            # 2. NATURAL LANGUAGE (user types in chat) → Pattern matching fallback (below)
            #
            # Pattern matching order matters: Check abandonment FIRST, then resolution.
            # This prevents "close as unresolved" from matching resolution patterns.
            #
            # BUG FIX (2026-02-08): User said "Close this case as unresolved" but system went to RESOLVED
            # ROOT CAUSE: Patterns were too specific ("close as unresolved" exact match)
            # FIX: Use key phrases that work with variations:
            #   - "as unresolved" matches: "close as unresolved", "close this case as unresolved"
            #   - "without solution" matches: "close without solution", "close this without solution"
            # This handles natural language variations while maintaining correct intent detection.
            # ============================================================
            # USER INTENT DETECTION - EXPLICIT STATUS TRANSITION (Frontend Buttons)
            # ============================================================
            # BUG FIX (2026-02-09): Status dropdown transitions not working
            # ROOT CAUSE: intent_type="status_transition" skipped pattern matching but had no handler
            # FIX: Add explicit handler before pattern matching section
            if intent_type == "status_transition" and intent_data:
                to_status_str = intent_data.get("to_status")
                from_status_str = intent_data.get("from_status")

                if not to_status_str:
                    raise ValueError(
                        "to_status is required for status_transition intent"
                    )

                logger.info(
                    f"Explicit status_transition intent: {from_status_str} → {to_status_str} "
                    f"for case {case.case_id}"
                )

                # Import terminal transition functions
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_closure_readiness,
                    propose_transition,
                )

                # Handle each status transition
                if to_status_str == "closed":
                    if case.status not in (
                        CaseStatus.INQUIRY,
                        CaseStatus.INVESTIGATING,
                    ):
                        raise ValueError(
                            f"Cannot transition to CLOSED from {case.status.value}"
                        )

                    # Use closure readiness for a meaningful summary
                    closure = assess_closure_readiness(case)
                    closure_reason = (
                        "inquiry_only"
                        if case.status == CaseStatus.INQUIRY
                        else "abandoned"
                    )

                    propose_transition(
                        case=case,
                        to_status="closed",
                        reason=f"User requested closure via dropdown ({closure_reason})",
                        summary=closure.message,
                        closure_reason=closure_reason,
                    )

                    logger.info(
                        f"Proposed CLOSED transition for case {case.case_id} via dropdown "
                        f"(pending user confirmation)"
                    )

                    # Save and return with closure summary
                    self._record_deterministic_turn(
                        case, user_message or "", closure.message
                    )
                    await self.repository.save(case)
                    return {
                        "agent_response": closure.message,
                        "suggested_follow_ups": [],
                        "case_updated": case,
                        "metadata": {
                            "turn_number": case.current_turn,
                            "milestones_completed": [],
                            "progress_made": False,
                        },
                    }

                elif to_status_str == "resolved":
                    if case.status != CaseStatus.INVESTIGATING:
                        raise ValueError(
                            f"Cannot transition to RESOLVED from {case.status.value}"
                        )

                    from faultmaven.modules.case.domain.services.case_action_manager import (
                        CaseActionManager,
                    )

                    if not user_message or not user_message.strip():
                        user_message = (
                            CaseActionManager.get_agent_message(
                                CaseStatus.INVESTIGATING, CaseStatus.RESOLVED
                            )
                            or "The issue is resolved."
                        )

                    # If a pending transition to resolved already exists, this
                    # dropdown click is a confirmation of the existing proposal.
                    # Execute the transition (User-Agent Handshake: confirm step).
                    if (
                        hasattr(case, "pending_transition")
                        and case.pending_transition
                        and case.pending_transition.get("to_status") == "resolved"
                    ):
                        from faultmaven.core.investigation.terminal_transitions import (
                            confirm_pending_transition,
                        )

                        confirm_pending_transition(case, case.user_id)
                        metadata["status_transitioned"] = True

                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: confirmed existing pending "
                            f"transition for case {case.case_id}"
                        )

                        _resp = "Case resolved. The issue has been marked as resolved."
                        self._record_deterministic_turn(case, user_message or "", _resp)
                        await self.repository.save(case)

                        # Auto-generate incident report (fire-and-forget)
                        await self._auto_generate_report(case)

                        return {
                            "agent_response": _resp,
                            "suggested_follow_ups": _runbook_suggestion(),
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": ["solution_verified"],
                                "progress_made": True,
                            },
                        }

                    # No pending transition — check resolution readiness before proposing.
                    from faultmaven.core.investigation.terminal_transitions import (
                        assess_resolution_readiness,
                        propose_transition,
                    )

                    readiness = assess_resolution_readiness(case)

                    if readiness.verdict in (
                        readiness.SUGGEST_CLOSE,
                        readiness.NEEDS_INFO,
                    ):
                        # Not ready — tell user what's missing, remember their intent
                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: case {case.case_id} "
                            f"verdict={readiness.verdict} (missing: {readiness.missing}). "
                            f"Remembering resolve intent."
                        )
                        propose_transition(
                            case=case,
                            to_status="resolved",
                            reason=f"User indicated resolution via dropdown ({readiness.verdict})",
                            summary=readiness.message,
                        )
                        case.pending_transition["needs_info"] = True
                        self._record_deterministic_turn(
                            case, user_message or "", readiness.message
                        )
                        await self.repository.save(case)
                        return {
                            "agent_response": readiness.message,
                            "suggested_follow_ups": [],
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": False,
                            },
                        }

                    # READY — propose transition via User-Agent Handshake
                    propose_transition(
                        case=case,
                        to_status="resolved",
                        reason="User indicated resolution via status dropdown",
                        summary="Case meets resolution criteria. Awaiting user confirmation.",
                    )
                    metadata["transition_proposed_this_turn"] = True

                    logger.info(
                        f"INVESTIGATING->RESOLVED dropdown: proposed transition for "
                        f"case {case.case_id} (pending user confirmation)"
                    )

                    # Return immediately with confirmation prompt.
                    _resp = (
                        "You've indicated this issue is resolved.\n\n"
                        + _build_resolution_confirmation(case)
                    )
                    self._record_deterministic_turn(case, user_message or "", _resp)
                    await self.repository.save(case)
                    return {
                        "agent_response": _resp,
                        "suggested_follow_ups": [],
                        "case_updated": case,
                        "metadata": {
                            "turn_number": case.current_turn,
                            "milestones_completed": [],
                            "progress_made": True,
                        },
                    }

                elif to_status_str == "investigating":
                    if case.status != CaseStatus.INQUIRY:
                        raise ValueError(
                            f"Cannot transition to INVESTIGATING from {case.status.value}"
                        )

                    # Inject a pre-composed message and let the normal INQUIRY
                    # LLM flow handle the problem statement + transition.
                    # The frontend expects the case to transition in this turn,
                    # so we fall through to the LLM pipeline which can set
                    # user_confirmed_investigation=True and trigger the transition
                    # via _check_automatic_transitions.
                    from faultmaven.modules.case.domain.services.case_action_manager import (
                        CaseActionManager,
                    )

                    if not user_message or not user_message.strip():
                        user_message = (
                            CaseActionManager.get_agent_message(
                                CaseStatus.INQUIRY, CaseStatus.INVESTIGATING
                            )
                            or "I want to start a formal investigation to find the root cause."
                        )

                    logger.info(
                        f"INQUIRY->INVESTIGATING dropdown: routing through normal INQUIRY flow "
                        f"for case {case.case_id}"
                    )
                    # Fall through to normal LLM processing (no transition executed here)

                else:
                    raise ValueError(f"Unknown to_status: {to_status_str}")

            elif intent_type == "confirmation":
                logger.info(
                    f"Explicit confirmation intent for case {case.case_id} "
                    f"(has_pending_statement={bool(case.inquiry.proposed_problem_statement)})"
                )

                if case.status != CaseStatus.INQUIRY:
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but status is {case.status.value}"
                    )
                elif not case.inquiry.proposed_problem_statement:
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but no proposed problem statement exists"
                    )
                else:
                    # Update inquiry state
                    case.inquiry.problem_statement_confirmed = True
                    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
                    case.inquiry.decided_to_investigate = True
                    case.inquiry.decision_made_at = datetime.now(UTC)

                    # Transition to INVESTIGATING
                    await self._transition_to_investigating(case)
                    case.action_history.append(
                        CaseAction(
                            from_status=CaseStatus.INQUIRY,
                            to_status=CaseStatus.INVESTIGATING,
                            triggered_at=datetime.now(UTC),
                            triggered_by=case.user_id,
                            reason="User confirmed proposed problem statement",
                        )
                    )

                    logger.info(
                        f"Case {case.case_id} transitioned to INVESTIGATING via confirmation intent"
                    )

                    # Continue to normal LLM flow for investigation kickoff message

            # ============================================================
            # USER INTENT DETECTION - PATTERN MATCHING (Natural Language)
            # ============================================================
            # Two complementary paths:
            # 1. Explicit intent (frontend buttons) → Handled above with intent_data
            # 2. Natural language (user types) → Pattern matching below
            #
            # SKIP PATTERN MATCHING if explicit intent provided by frontend
            # UNLESS intent is "conversation" (which is default for user typing)
            if user_message and (not intent_type or intent_type == "conversation"):
                user_msg_lower = user_message.lower().strip()

                # ========================================
                # INQUIRY STATUS - CLOSE FROM INQUIRY
                # ========================================
                if case.status == CaseStatus.INQUIRY:
                    # User wants to close from INQUIRY without starting investigation
                    close_inquiry_patterns = [
                        "close this",
                        "close the case",
                        "don't need",
                        "don't want",
                        "no need",
                        "not needed",
                        "cancel this",
                        "never mind",
                        "nevermind",
                    ]

                    if any(
                        pattern in user_msg_lower for pattern in close_inquiry_patterns
                    ):
                        logger.info(
                            f"Detected user intent to CLOSE from INQUIRY for case {case.case_id}: '{user_message[:50]}...'"
                        )
                        from faultmaven.core.investigation.terminal_transitions import (
                            assess_closure_readiness,
                            propose_transition,
                        )

                        closure = assess_closure_readiness(case)
                        propose_transition(
                            case=case,
                            to_status="closed",
                            reason="User expressed close intent from INQUIRY via NLP",
                            summary=closure.message,
                            closure_reason="inquiry_only",
                        )

                        logger.info(
                            f"Proposed CLOSED transition for case {case.case_id} (inquiry_only) based on user intent"
                        )

                # ========================================
                # INVESTIGATING STATUS - ABANDONMENT / RESOLUTION
                # ========================================
                elif case.status == CaseStatus.INVESTIGATING:
                    # Pattern matching for ABANDONMENT intent (CLOSED without solution)
                    # These patterns indicate user wants to close WITHOUT confirming solution.
                    # Use multi-word phrases to avoid false positives from contextual mentions.
                    # e.g., "abandon" alone would match "I don't want to abandon this".
                    abandonment_patterns = [
                        "as unresolved",  # "close as unresolved"
                        "without solution",  # "close without solution"
                        "without resolving",  # "close without resolving"
                        "abandon this case",
                        "abandon the case",
                        "abandon this investigation",
                        "give up on this",
                        "i give up",
                        "let's give up",
                        "can't solve this",
                        "cannot solve this",
                        "unable to resolve this",
                        "escalate this case",
                        "escalate this to",
                    ]

                    # Negation patterns that indicate the user does NOT want to abandon
                    abandonment_negations = [
                        "don't abandon",
                        "do not abandon",
                        "don't want to abandon",
                        "not abandon",
                        "don't give up",
                        "do not give up",
                        "don't want to give up",
                        "shouldn't escalate",
                    ]

                    # Pattern matching for RESOLUTION intent (RESOLVED with solution)
                    # These patterns indicate user confirms problem is fixed.
                    # Use specific phrases that clearly indicate solution/resolution.
                    resolve_patterns = [
                        "mark as resolved",
                        "mark this resolved",
                        "case is resolved",
                        "case resolved",
                        "this is resolved",
                        "problem is solved",
                        "problem solved",
                        "issue is fixed",
                        "issue fixed",
                        "solution worked",
                        "solution works",
                        "it's fixed now",
                        "that fixed it",
                        "the fix worked",
                    ]

                    # Ambiguous patterns that need context from abandonment keywords
                    # "close this case" could mean either CLOSED or RESOLVED
                    # Check if combined with abandonment indicators
                    close_patterns = [
                        "close this case",
                        "close the case",
                    ]

                    # CHECK ABANDONMENT FIRST (highest priority)
                    # Guard: skip if message contains negation ("don't abandon", etc.)
                    has_abandonment = any(
                        pattern in user_msg_lower for pattern in abandonment_patterns
                    )
                    has_negation = any(
                        neg in user_msg_lower for neg in abandonment_negations
                    )
                    if has_abandonment and not has_negation:
                        logger.info(
                            f"Detected explicit user intent to ABANDON case {case.case_id} (CLOSED): '{user_message[:50]}...'"
                        )
                        from faultmaven.core.investigation.terminal_transitions import (
                            assess_closure_readiness,
                            propose_transition,
                        )

                        closure = assess_closure_readiness(case)
                        propose_transition(
                            case=case,
                            to_status="closed",
                            reason="User expressed abandonment intent via NLP",
                            summary=closure.message,
                            closure_reason="abandoned",
                        )

                        logger.info(
                            f"Proposed CLOSED transition for case {case.case_id} (abandoned) based on user intent"
                        )

                    # CHECK RESOLUTION (medium priority)
                    # User-Agent Handshake: NLP-detected intent to resolve.
                    # Instead of directly setting solution_verified, propose the
                    # transition. The LLM will include the proposal in its response
                    # and the user confirms on the next turn.
                    elif any(pattern in user_msg_lower for pattern in resolve_patterns):
                        logger.info(
                            f"Detected NLP intent to RESOLVE case {case.case_id}: '{user_message[:50]}...'"
                        )
                        from faultmaven.core.investigation.terminal_transitions import (
                            assess_resolution_readiness,
                            propose_transition,
                        )

                        readiness = assess_resolution_readiness(case)

                        if readiness.verdict in (
                            readiness.SUGGEST_CLOSE,
                            readiness.NEEDS_INFO,
                        ):
                            logger.info(
                                f"NLP resolve for case {case.case_id}: "
                                f"verdict={readiness.verdict} (missing: {readiness.missing}). "
                                f"Remembering resolve intent."
                            )
                            propose_transition(
                                case=case,
                                to_status="resolved",
                                reason=f"User indicated resolution via NLP ({readiness.verdict})",
                                summary=readiness.message,
                            )
                            case.pending_transition["needs_info"] = True
                            self._record_deterministic_turn(
                                case, user_message, readiness.message
                            )
                            await self.repository.save(case)
                            return {
                                "agent_response": readiness.message,
                                "suggested_follow_ups": [],
                                "case_updated": case,
                                "metadata": {
                                    "turn_number": case.current_turn,
                                    "milestones_completed": [],
                                    "progress_made": False,
                                },
                            }

                        # READY — propose transition
                        propose_transition(
                            case=case,
                            to_status="resolved",
                            reason="User indicated the problem is resolved",
                            summary="Case meets resolution criteria. Awaiting user confirmation.",
                        )

                        logger.info(
                            f"Proposed RESOLVED transition for case {case.case_id} (pending user confirmation)"
                        )
                        metadata["transition_proposed_this_turn"] = True

                        _resp = (
                            "It sounds like the issue is resolved.\n\n"
                            + _build_resolution_confirmation(case)
                        )
                        self._record_deterministic_turn(case, user_message, _resp)
                        await self.repository.save(case)
                        return {
                            "agent_response": _resp,
                            "suggested_follow_ups": [],
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": True,
                            },
                        }

                    # CHECK AMBIGUOUS CLOSE PATTERNS (lowest priority)
                    # "close this case" is ambiguous — could mean CLOSED (abandoned) or RESOLVED.
                    # Propose resolution and let the LLM ask user to clarify.
                    elif any(pattern in user_msg_lower for pattern in close_patterns):
                        logger.info(
                            f"Detected ambiguous close intent for case {case.case_id}: '{user_message[:50]}...'"
                        )

                        # Do NOT set pending_transition here — we don't know
                        # whether the user wants RESOLVED or CLOSED yet.
                        # Just ask for clarification. Their next message will
                        # route through the resolve_patterns or abandonment_patterns.

                        # Return immediately with clarification request.
                        _resp = (
                            "You'd like to close this case. Before I do, I need to know:\n\n"
                            "- **Resolved** — The problem is fixed. I'll document the solution.\n"
                            "- **Closed** — The investigation is ending without a solution "
                            "(abandoned, escalated, or mitigation was sufficient).\n\n"
                            "Which would you like?"
                        )
                        self._record_deterministic_turn(case, user_message, _resp)
                        await self.repository.save(case)
                        return {
                            "agent_response": _resp,
                            "suggested_follow_ups": [],
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": False,
                            },
                        }

            # 1. Gather Context & Build Prompt
            # Phase 3: Inquiry KB Search (Fast-track)
            kb_results = None
            if case.status == CaseStatus.INQUIRY and self.knowledge_service:
                try:
                    kb_results = await self.knowledge_service.search(user_message, k=3)
                    logger.info(
                        f"KB Search found {len(kb_results) if kb_results else 0} results"
                    )
                except Exception as e:
                    logger.warning(
                        f"KB Search failed (non-critical, continuing without KB): {e}",
                        exc_info=True,
                        extra={"case_id": case.case_id, "turn": case.current_turn},
                    )

            # Initialize case-scoped PII redaction context.
            # Created fresh each turn — the assembled prompt contains raw
            # structural indices from ALL evidence files, so a single
            # sanitize() call builds a collision-free registry. Redis
            # load() provides cross-turn numbering consistency (same IP
            # keeps the same placeholder across turns) but is not required
            # for correctness.
            from faultmaven.config.settings import get_settings
            from faultmaven.infrastructure.security.case_redaction import (
                CaseRedactionContext,
            )

            redaction_settings = get_settings()
            redaction_ctx = CaseRedactionContext(
                case_id=case.case_id,
                sanitizer=self.sanitizer,
                redis_client=self.redis_client,
                enabled=self._should_redact(),
                ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
            )
            await redaction_ctx.load()

            # Build prompt using the adaptive template system
            # Gap #6: Pass provider info for dynamic token budget calculation
            provider_name = getattr(self.llm_provider, "provider_name", None)
            model_name = (
                getattr(self.llm_provider.config, "default_model", None)
                if hasattr(self.llm_provider, "config")
                else None
            )

            # Classify query for processing mode (structural index role tagging)
            from faultmaven.modules.agent.domain.services.query_classifier import (
                classify_query,
            )

            classification = classify_query(
                user_message, has_attachments=bool(case.evidence)
            )

            prompt = get_prompt_for_case(
                case,
                user_message,
                kb_results,
                provider_name=provider_name,
                model_name=model_name,
                processing_mode=classification.mode.value,
            )

            # Determine schema based on status/stage
            if case.status == CaseStatus.INQUIRY:
                schema_model = InquiryResponse
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"status={case.status.value}, schema=InquiryResponse"
                )
            elif case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
                schema_model = TerminalResponse
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"status={case.status.value}, schema=TerminalResponse"
                )
            else:
                schema_model = get_schema_for_stage(
                    case.current_stage or InvestigationStage.DIAGNOSIS
                )
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"status={case.status.value}, stage={case.current_stage}, "
                    f"schema={schema_model.__name__}"
                )

            # 2. Invoke LLM with structured output
            # Tool availability: all turns get tools when tools are registered.
            # The LLM decides which tool to invoke based on the user's question.
            #
            # tool_choice varies by query mode:
            # - directed_analysis + evidence: "required" — LLM must search evidence
            # - all other turns: "auto" — LLM decides whether to use tools
            query_mode = (intent_data or {}).get("query_mode")
            if self.investigation_tools:
                force_tools = query_mode == "directed_analysis" and bool(case.evidence)
                da_tools = self._build_da_tool_schemas()
                da_context = self._build_tool_context(case, intent_data)
                response_obj = await self._generate_structured_output(
                    prompt,
                    schema_model,
                    investigation_tools=da_tools,
                    tool_context=da_context,
                    force_tool_use=force_tools,
                    redaction_ctx=redaction_ctx,
                    case=case,
                )
            else:
                response_obj = await self._generate_structured_output(
                    prompt,
                    schema_model,
                    redaction_ctx=redaction_ctx,
                    case=case,
                )

            # Debug: Log what type was actually returned
            logger.info(
                f"Turn {case.current_turn} response type: {type(response_obj).__name__}"
            )

            # 3. Validate reasoning BEFORE applying state (design: error-handling §3.2)
            # This prevents duplicate state mutations if self-correction retry is needed.
            #
            # EXCEPTION: Knowledge queries skip diagnostic reasoning validation
            # entirely. These are general knowledge questions (e.g., "What is Opik?")
            # that don't reference case evidence and cannot satisfy the
            # OBSERVATION/ANALYSIS format requirement. Validating them would
            # always trigger a false-positive retry.
            is_knowledge_turn = query_mode == "knowledge_query"

            from faultmaven.core.investigation.diagnostic_reasoning_validator import (
                validate_diagnostic_reasoning,
            )

            if is_knowledge_turn:
                is_valid_reasoning = True
                violations = []
                logger.info("Knowledge query: skipping diagnostic reasoning validation")
            else:
                is_valid_reasoning, violations = validate_diagnostic_reasoning(
                    case=case,
                    agent_response=response_obj.agent_response,
                    contains_suggestion=None,  # Auto-detect
                )

            # For DA turns (tool-augmented), "Missing causal reasoning" is a
            # false positive on factual lookups (e.g., "What usernames did X try?").
            # Downgrade it from retry-triggering to warning-only so we don't waste
            # a 5-7s retry that can never satisfy the check. Other violations
            # (checklist, generic advice, missing observation) still trigger retry.
            _CAUSAL_VIOLATION = "Missing causal reasoning"
            is_da_turn = query_mode == "directed_analysis"
            if not is_valid_reasoning and is_da_turn:
                causal_violations = [v for v in violations if _CAUSAL_VIOLATION in v]
                retry_violations_list = [
                    v for v in violations if _CAUSAL_VIOLATION not in v
                ]

                if causal_violations and not retry_violations_list:
                    # ONLY causal reasoning is missing — this is likely a factual lookup.
                    # Skip retry, log as warning, feed back to next turn.
                    logger.info(
                        f"DA turn: downgrading causal reasoning violation to warning "
                        f"(no retry). Violations: {causal_violations}"
                    )
                    metadata["diagnostic_reasoning_violations"] = causal_violations
                    is_valid_reasoning = True  # Skip retry block
                    violations = []
                elif causal_violations and retry_violations_list:
                    # Other violations exist too — retry for those, but remove causal
                    # from the retry prompt since it can't be satisfied for factual lookups.
                    violations = retry_violations_list
                    logger.info(
                        f"DA turn: removed causal reasoning from retry violations "
                        f"(remaining: {retry_violations_list})"
                    )

            if not is_valid_reasoning:
                logger.warning(
                    f"Diagnostic reasoning validation failed: {violations}. "
                    "Attempting self-correction retry."
                )

                # Self-correction: retry once with violation feedback.
                # Include the original response so the retry LLM has the search
                # data found during the DA tool loop (which isn't re-run here).
                try:
                    correction_feedback = (
                        "\n\n[SYSTEM CORRECTION REQUIRED]\n"
                        "Your previous response failed diagnostic reasoning validation. "
                        "You MUST fix these issues:\n"
                        + "\n".join(f"- {v}" for v in violations)
                        + "\n\nHere is your previous response to rewrite:\n"
                        + response_obj.agent_response
                        + "\n\nRewrite the agent_response to address ALL violations above. "
                        "Structure it as: OBSERVATION (cite specific data from at least 2 "
                        "categories — timestamps like HH:MM, error messages, IPs/usernames, "
                        "or metrics/counts — directly from the search results above) then "
                        "ANALYSIS (explain WHY using causal language like 'because', "
                        "'therefore', 'this indicates'). "
                        "Keep all state_updates from your previous response unchanged."
                    )
                    corrected_prompt = prompt + correction_feedback
                    corrected_response = await self._generate_structured_output(
                        corrected_prompt,
                        schema_model,
                        redaction_ctx=redaction_ctx,
                        case=case,
                    )

                    # Re-validate the corrected response
                    is_valid_retry, retry_violations = validate_diagnostic_reasoning(
                        case=case,
                        agent_response=corrected_response.agent_response,
                        contains_suggestion=None,
                    )

                    if is_valid_retry:
                        logger.info(
                            "Self-correction succeeded: retried response passes validation."
                        )
                        response_obj = corrected_response
                        violations = []
                    else:
                        logger.warning(
                            f"Self-correction retry also failed: {retry_violations}. "
                            "Proceeding with retried response; violations fed back for next turn."
                        )
                        # Use the retried response (may be partially improved)
                        response_obj = corrected_response
                        violations = retry_violations
                except Exception as e:
                    logger.warning(
                        f"Self-correction retry failed: {e}. "
                        "Proceeding with original response.",
                        exc_info=True,
                        extra={"case_id": case.case_id, "turn": case.current_turn},
                    )
                    # Keep original response_obj as-is

                # Add remaining violations to metadata for observability (G9+G11 wires them)
                if violations:
                    metadata["diagnostic_reasoning_violations"] = violations

            # 4. Apply state from the final accepted response (exactly once)
            case_updated, response_metadata = await self._process_response_structured(
                case, user_message, response_obj, attachments
            )
            # Merge response metadata with early metadata (which may have transition_proposed_this_turn)
            metadata.update(response_metadata)

            # 4a. Stage-gate compliance is now handled via LLM milestone output
            # (Framework §4.1). The LLM sets stage-gate milestones in its
            # structured response; side effects are applied in
            # _apply_investigation_updates → _apply_stage_gate_side_effects.

            # Phase 1: No-Op Detection
            progress_made = self._check_if_progress_made(metadata)
            metadata["progress_made"] = progress_made
            # Outcome is already set by _process_response_structured (default) or applied updates (LLM choice)

            # 4. Check for automatic status transitions
            case_updated = await self._check_automatic_transitions(
                case_updated, metadata, user_message
            )

            # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
            # This happens after transitions but before recording the turn
            self._perform_hypothesis_housekeeping(case_updated, metadata)

            # Step 5.5: Calculate progress metrics
            progress_metrics = calculate_progress_metrics(
                case=case_updated, current_turn=case_updated.current_turn
            )
            metadata["momentum"] = progress_metrics.investigation_momentum
            metadata["blocked_reasons"] = progress_metrics.blocked_reasons
            metadata["next_steps"] = progress_metrics.next_steps

            # Step 5.6: Generate working conclusion EVERY turn during INVESTIGATING
            # Gap #7: Working Conclusion Every Turn
            # Reference: Prompt Engineering Guide Section 11.7
            # Why: Provides consistent context tracking, prevents "lost context" issues
            if case_updated.status == CaseStatus.INVESTIGATING:
                working_conclusion = generate_working_conclusion(
                    case=case_updated, current_turn=case_updated.current_turn
                )
                case_updated.working_conclusion = working_conclusion
                logger.debug(
                    f"Working conclusion updated: likelihood={working_conclusion.likelihood:.2f}"
                )

            # Step 5.7: Validate state consistency
            is_valid, validation_issues = self.state_validator.is_valid(case_updated)
            validation_repairs: list[str] = []
            if validation_issues:
                # Log validation issues and collect repairs
                for issue in validation_issues:
                    if issue.severity == ValidationSeverity.ERROR:
                        logger.warning(
                            f"State validation error: {issue.code} - {issue.message}"
                        )
                        if issue.suggested_fix:
                            validation_repairs.append(
                                f"{issue.code}: {issue.suggested_fix}"
                            )
                    elif issue.severity == ValidationSeverity.WARNING:
                        logger.debug(
                            f"State validation warning: {issue.code} - {issue.message}"
                        )
                metadata["validation_issues"] = [
                    {"code": i.code, "message": i.message, "severity": i.severity.value}
                    for i in validation_issues
                ]

            # Step 5.8: Update progress tracking (before stagnation check)
            if metadata.get("progress_made", False):
                case_updated.turns_without_progress = 0
            else:
                case_updated.turns_without_progress += 1

            # Step 5.9: Check for stagnation (before recording turn)
            stagnation_type = self.stagnation_detector.detect_stagnation(case_updated)
            stagnation_str: str | None = None
            if stagnation_type:
                stagnation_str = stagnation_type.value
                # Get breakout action and apply it
                breakout_action = self.stagnation_breaker.break_stagnation(
                    case_updated, stagnation_type
                )
                metadata["stagnation_type"] = stagnation_type.value
                metadata["breakout_action"] = breakout_action.action
                metadata["breakout_prompt_injection"] = breakout_action.prompt_injection
                # G1: Store breakout prompt as system_feedback so it reaches
                # the LLM in the next turn via build_investigation_context()
                if breakout_action.prompt_injection:
                    current_feedback = metadata.get("system_feedback", "") or ""
                    # Gentle reminders don't need an alarming prefix
                    if breakout_action.action == "gentle_reminder":
                        breakout_msg = breakout_action.prompt_injection
                    else:
                        breakout_msg = (
                            f"STAGNATION RECOVERY: {breakout_action.prompt_injection}"
                        )
                    metadata["system_feedback"] = (
                        f"{current_feedback}\n{breakout_msg}".strip()
                    )
                log_level = (
                    logging.DEBUG
                    if breakout_action.action == "gentle_reminder"
                    else logging.INFO
                )
                logger.log(
                    log_level,
                    f"Stagnation detected: {stagnation_type.value}. "
                    f"Action: {breakout_action.action}",
                )

            # Step 5.9b: Wire validation errors into system_feedback (G9 + G11)
            # Diagnostic reasoning violations and reasoning validation errors
            # must propagate to the next turn so the LLM can self-correct.
            feedback_parts = []
            if metadata.get("diagnostic_reasoning_violations"):
                violations = metadata["diagnostic_reasoning_violations"]
                feedback_parts.append(
                    f"DIAGNOSTIC REASONING ISSUES: {'; '.join(violations)}. "
                    "Provide case-specific reasoning with evidence references."
                )
            if metadata.get("reasoning_validation_errors"):
                errors = metadata["reasoning_validation_errors"]
                feedback_parts.append(
                    f"REASONING VALIDATION: {'; '.join(errors)}. "
                    "Provide internal_reasoning with milestone_justifications."
                )
            if feedback_parts:
                current_feedback = metadata.get("system_feedback", "") or ""
                new_feedback = "\n".join(feedback_parts)
                metadata["system_feedback"] = (
                    f"{current_feedback}\n{new_feedback}".strip()
                )

            # Step 6: Record turn progress
            turn_record = self._create_turn_record(
                turn_number=case_updated.current_turn,
                milestones_completed=metadata.get("milestones_completed", []),
                evidence_added=metadata.get("evidence_added", []),
                hypotheses_generated=metadata.get("hypotheses_generated", []),
                hypotheses_validated=metadata.get("hypotheses_validated", []),
                solutions_proposed=metadata.get("solutions_proposed", []),
                progress_made=metadata.get("progress_made", False),
                outcome=metadata.get("outcome", TurnOutcome.CONVERSATION),
                user_message=user_message,
                agent_response=response_obj.agent_response,
                system_feedback=metadata.get("system_feedback"),
                momentum=progress_metrics.investigation_momentum,
                blocked_reasons=progress_metrics.blocked_reasons,
                next_steps=progress_metrics.next_steps,
                stagnation_detected=stagnation_str,
                validation_repairs=validation_repairs,
            )
            case_updated.turn_history.append(turn_record)

            # Step 7: Save case (only if changes made, but turn history always updates)
            case_updated.updated_at = datetime.now(UTC)
            case_updated.last_activity_at = datetime.now(UTC)
            await self.repository.save(case_updated)

            # Step 7b: Auto-generate incident report on terminal transition (fire-and-forget)
            if metadata.get("status_transitioned") and case_updated.status in (
                CaseStatus.RESOLVED,
                CaseStatus.CLOSED,
            ):
                await self._auto_generate_report(case_updated)

            logger.info(
                f"Turn {case_updated.current_turn} processed successfully. "
                f"Status: {case_updated.status}, "
                f"Progress made: {metadata.get('progress_made', False)}"
            )

            # Extract follow-up suggestions from LLM response
            follow_ups = []
            if (
                hasattr(response_obj, "suggested_follow_ups")
                and response_obj.suggested_follow_ups
            ):
                for f in response_obj.suggested_follow_ups:
                    suggestion = {
                        "label": f.label,
                        "action_type": f.action_type,
                        "payload": f.payload,
                    }
                    # Optional fields — include only if present
                    if f.body:
                        suggestion["body"] = f.body
                    if f.cooperative_action:
                        suggestion["cooperative_action"] = f.cooperative_action
                    if f.hints:
                        suggestion["hints"] = f.hints
                    follow_ups.append(suggestion)

            # Persist redaction registry for cross-turn consistency
            await redaction_ctx.save()

            agent_response_text = response_obj.agent_response

            # Override LLM response with deterministic confirmation when
            # user provided missing info after a SUGGEST_CLOSE/NEEDS_INFO verdict.
            # Shows what we have + enrichment hints, with confirmation suggestions.
            if metadata.get("resolution_ready_for_confirmation"):
                agent_response_text = (
                    "Thanks for the additional details.\n\n"
                    + _build_resolution_confirmation(case_updated)
                )
                follow_ups = _resolution_confirmation_suggestions()

            # Offer runbook suggestion when case just transitioned to RESOLVED.
            # Evaluation (readiness + dedup) happens when user accepts,
            # inside _process_terminal_turn → _handle_runbook_creation.
            if (
                metadata.get("status_transitioned")
                and case_updated.status == CaseStatus.RESOLVED
            ):
                follow_ups = _runbook_suggestion()

            return {
                "agent_response": agent_response_text,
                "suggested_follow_ups": follow_ups,
                "case_updated": case_updated,
                "redaction_ctx": redaction_ctx,
                "metadata": {
                    "turn_number": case_updated.current_turn,
                    "milestones_completed": metadata.get("milestones_completed", []),
                    "progress_made": metadata.get("progress_made", False),
                    "status_transitioned": metadata.get("status_transitioned", False),
                    "outcome": metadata.get("outcome", TurnOutcome.CONVERSATION),
                    "momentum": metadata.get("momentum"),
                    "next_steps": metadata.get("next_steps", []),
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }

        except Exception as e:
            # Use LLMErrorHandler's classification instead of duplicating patterns
            is_external = self.llm_error_handler.is_retryable_error(e)

            if is_external:
                logger.warning(
                    f"External service error for case {case.case_id}: {str(e)[:200]}",
                    extra={"case_id": case.case_id, "turn": case.current_turn},
                )
            else:
                logger.error(
                    f"Error processing turn for case {case.case_id}: {e}",
                    exc_info=True,
                    extra={"case_id": case.case_id, "turn": case.current_turn},
                )

            raise MilestoneEngineError(f"Turn processing failed: {e}") from e

    # =========================================================================
    # Prompt Generation
    # =========================================================================

    # Constants for tool-augmented generation
    MAX_TOOL_ITERATIONS = 4
    TOOL_RESULT_MAX_CHARS = 8000
    MAX_DEEP_ANALYSIS = 1

    async def _tool_augmented_generate(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict],
        tool_context: Any,
        max_tokens: int = 8000,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        force_tool_use: bool = False,
    ) -> BaseInteractionResponse:
        """Run a bounded tool-calling loop with investigation tools.

        The LLM gets real investigation tools (search_file, deep_analysis,
        kb_qa, web_search) alongside the response schema tool.

        Algorithm:
        1. Build schema tool from Pydantic model (reuses existing converter)
        2. Combine: all_tools = investigation_tools + schema_tools
        3. Loop with tool_choice per force_tool_use:
           - force_tool_use=True (DA turns): "required" — LLM must call a tool
           - force_tool_use=False (other turns): "auto" — LLM may respond directly
        4. When LLM calls schema tool → parse and return structured output
        5. After max iterations → force schema with only schema tools available

        Vectorization (v5.2):
        - Proactive: starts background vectorization for large evidence files
          at loop entry. Runs concurrently with tool calls.
        - Reactive: tracks per-evidence DA failure signals (empty searches,
          timeouts, low confidence). Triggers vectorization as fallback.

        Args:
            prompt: Full investigation prompt
            schema_model: Pydantic model class for structured output
            investigation_tools: OpenAI-format tool defs for search/analysis
            tool_context: ToolContext for tool execution
            max_tokens: Max tokens for LLM calls
            case: Case object for evidence access and DA count persistence

        Returns:
            Instantiated Pydantic model (BaseInteractionResponse)
        """
        from faultmaven.utils.schema_converter import pydantic_to_openai_tools

        # Use dedicated DA provider (DA_PROVIDER from .env) if available,
        # otherwise fall back to the default router
        provider = self.da_provider or self.llm_provider
        provider_name = getattr(provider, "provider_name", type(provider).__name__)
        model_info = f", model: {self.da_model}" if self.da_model else ""
        logger.info(
            f"Tool-augmented generate using provider: {provider_name}{model_info}"
        )

        # Build schema tool (same pattern used by single-shot path)
        schema_tools = pydantic_to_openai_tools(schema_model)
        schema_tool_name = schema_tools[0]["function"]["name"]

        # Combine investigation tools + schema tool
        all_tools = investigation_tools + schema_tools

        # Build tool name list for the DA system instruction
        tool_names = [t["function"]["name"] for t in investigation_tools]

        # Initialize conversation with DA system instruction + user prompt
        da_system_instruction = self._build_da_system_instruction(
            tool_names,
            schema_tool_name,
        )
        messages = [
            {"role": "system", "content": da_system_instruction},
            {"role": "user", "content": prompt},
        ]
        deep_analysis_count = 0

        # Per-evidence DA failure tracking for auto-vectorization (v5.2)
        # Same pattern as deep_analysis_count above — mechanical counters
        # that trigger system actions when thresholds are met.
        da_empty_search_counts: dict[str, int] = {}  # evidence_id → consecutive empties
        da_vectorized: set[str] = set()  # evidence IDs already vectorized

        # Proactive vectorization: start background tasks for large evidence
        # files before the tool loop begins. Runs concurrently so semantic
        # search is available by the time the agent needs it.
        proactive_tasks: dict[str, asyncio.Task] = {}
        if case:
            proactive_tasks = await self._start_proactive_vectorization(
                case, tool_context
            )

        force_schema_next = False

        for iteration in range(self.MAX_TOOL_ITERATIONS + 1):
            is_final = iteration == self.MAX_TOOL_ITERATIONS

            # Tool availability per iteration:
            # - Iteration 0..N-1: all tools (investigation + schema)
            # - Final iteration / force_schema: schema tools ONLY
            if is_final or force_schema_next:
                tools_for_call = schema_tools
            else:
                tools_for_call = all_tools

            # DA turns: "required" — LLM must search evidence before answering
            # Other turns: "auto" — LLM decides whether to use tools
            # Final/force-schema iterations always use "required" (schema tool only)
            if is_final or force_schema_next:
                choice = "required"
            elif force_tool_use:
                choice = "required"
            else:
                choice = "auto"

            logger.info(
                f"Tool loop iteration {iteration}/{self.MAX_TOOL_ITERATIONS} "
                f"(is_final={is_final}, force_schema={force_schema_next}, tool_choice={choice})"
            )

            # Pass da_model when using dedicated provider
            generate_kwargs = dict(
                prompt="",
                messages=messages,
                tools=tools_for_call,
                tool_choice=choice,
                max_tokens=max_tokens,
                temperature=0.2,
                case_id=case.case_id if case is not None else None,
            )
            if self.da_model and self.da_provider:
                generate_kwargs["model"] = self.da_model

            try:
                response = await provider.generate(**generate_kwargs)
            except Exception as e:
                # On the first iteration, a generate failure with tools is likely
                # a model/provider incompatibility (e.g., DeepSeek on Fireworks
                # doesn't support OpenAI-compatible tool calling). Raise a
                # specific exception so the caller can fall back to the non-tool
                # structured output path.
                if iteration == 0:
                    from faultmaven.exceptions import ToolCallingUnsupportedError

                    logger.warning(
                        "Tool loop: first generate with tools failed "
                        "(provider=%s, model=%s): %s. "
                        "Raising ToolCallingUnsupportedError for fallback.",
                        provider_name,
                        model_info,
                        e,
                    )
                    raise ToolCallingUnsupportedError(
                        message=(
                            f"Tool calling failed on first attempt: {e}. "
                            f"Model may not support function calling."
                        ),
                        provider=provider_name,
                        model=self.da_model,
                    ) from e
                # On later iterations the model already succeeded with tools,
                # so a failure is a transient issue — propagate as-is.
                raise

            # Check for tool calls in response
            if not hasattr(response, "tool_calls") or not response.tool_calls:
                # No tool calls — shouldn't happen with tool_choice=auto/required
                # but handle gracefully by appending to messages and forcing schema
                if is_final or force_schema_next:
                    raise MilestoneEngineError(
                        "Tool loop: LLM returned no tool calls on forced final iteration"
                    )
                logger.warning(
                    "Tool loop: LLM returned no tool calls at iteration %d, "
                    "will force schema on next iteration",
                    iteration,
                )

                # Append the plain text response so the LLM knows what it said
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content
                        or "I should use a tool to proceed.",
                    }
                )

                force_schema_next = True
                continue

            # Reset the flag on successful tool usage
            force_schema_next = False

            # Check if LLM called the schema tool (termination signal)
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                if func_name == schema_tool_name:
                    logger.info(
                        "Tool loop: schema tool called at iteration %d, "
                        "parsing structured output",
                        iteration,
                    )
                    return self._parse_schema_tool_call(tc, schema_model)

            # Build assistant message with tool calls
            assistant_msg = self._build_assistant_message(response)
            messages.append(assistant_msg)

            # Execute each investigation tool call
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                args_str = tc.function.get("arguments", "{}")
                logger.info(
                    "Tool loop iter %d: LLM called tool=%s args=%s",
                    iteration,
                    func_name,
                    (
                        args_str[:200]
                        if isinstance(args_str, str)
                        else str(args_str)[:200]
                    ),
                )

                try:
                    args = (
                        json.loads(args_str) if isinstance(args_str, str) else args_str
                    )
                except (json.JSONDecodeError, TypeError):
                    args = {}

                # Enforce deep_analysis limit
                if (
                    func_name == "deep_analysis"
                    and deep_analysis_count >= self.MAX_DEEP_ANALYSIS
                ):
                    result_text = (
                        "deep_analysis is limited to 1 call per turn. "
                        "Use search_file for additional searches."
                    )
                else:
                    tool_result = await self.investigation_tools.execute_tool(
                        func_name,
                        args,
                        tool_context,
                    )
                    if func_name == "deep_analysis":
                        deep_analysis_count += 1

                    result_text = self._format_tool_result(
                        tool_result, tool_name=func_name
                    )

                    # --- Per-evidence DA failure tracking (v5.2) ---
                    # Track search_file empty results and check vectorization
                    # triggers. Same pattern as deep_analysis_count above.
                    evidence_id = args.get("evidence_id", "")
                    if evidence_id and func_name in ("search_file", "deep_analysis"):
                        result_text = await self._track_da_result(
                            func_name=func_name,
                            evidence_id=evidence_id,
                            tool_result=tool_result,
                            result_text=result_text,
                            case=case,
                            tool_context=tool_context,
                            da_empty_search_counts=da_empty_search_counts,
                            da_vectorized=da_vectorized,
                            proactive_tasks=proactive_tasks,
                        )

                # Redact PII in tool results before sending to LLM.
                # Tool results contain raw file content (search_file,
                # deep_analysis) which bypasses prompt-level redaction.
                if redaction_ctx:
                    result_text = redaction_ctx.sanitize(result_text)

                # Truncate long results
                if len(result_text) > self.TOOL_RESULT_MAX_CHARS:
                    result_text = (
                        result_text[: self.TOOL_RESULT_MAX_CHARS] + "\n[truncated]"
                    )

                # Append tool result message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": result_text,
                    }
                )

        # Should not reach here (final iteration forces schema)
        raise MilestoneEngineError(
            "Tool loop exhausted without producing structured output"
        )

    # ================================================================
    # Vectorization tracking (v5.2) — mechanical safety nets
    # Same pattern as deep_analysis_count / MAX_DEEP_ANALYSIS above.
    # ================================================================

    async def _start_proactive_vectorization(
        self,
        case: Any,
        tool_context: Any,
    ) -> dict[str, asyncio.Task]:
        """Start background vectorization for qualifying DA-mode evidence.

        Runs concurrently with the tool loop so case_evidence_search
        is available by the time the agent needs it. Only vectorizes
        files above the size threshold that haven't been vectorized yet.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        settings = get_settings()
        min_size = settings.agent.vectorization_min_size_bytes
        tasks: dict[str, asyncio.Task] = {}

        for ev in getattr(case, "evidence", []):
            size = getattr(ev, "content_size_bytes", 0) or 0
            if (
                size >= min_size
                and size <= VECTORIZATION_MAX_SIZE_BYTES
                and not getattr(ev, "vectorized", False)
            ):
                tasks[ev.evidence_id] = asyncio.create_task(
                    self._vectorize_evidence(ev.evidence_id, tool_context)
                )
                logger.info(
                    "proactive_vectorization_started",
                    extra={
                        "evidence_id": ev.evidence_id,
                        "content_size_bytes": size,
                    },
                )
        return tasks

    async def _vectorize_evidence(
        self,
        evidence_id: str,
        tool_context: Any,
    ) -> bool:
        """Vectorize a single evidence file via the registered tool."""
        try:
            result = await asyncio.wait_for(
                self.investigation_tools.execute_tool(
                    "vectorize_file",
                    {"evidence_id": evidence_id},
                    tool_context,
                ),
                timeout=60,
            )
            if not result.success:
                logger.warning(
                    "vectorize_file returned failure for %s: %s",
                    evidence_id,
                    result.error,
                )
            else:
                logger.info(
                    "vectorize_file succeeded for %s",
                    evidence_id,
                )
            return result.success
        except Exception as e:
            logger.warning(
                "Vectorization failed for %s: %s",
                evidence_id,
                e,
                exc_info=True,
            )
            return False

    async def _track_da_result(
        self,
        func_name: str,
        evidence_id: str,
        tool_result: Any,
        result_text: str,
        case: Any | None,
        tool_context: Any,
        da_empty_search_counts: dict[str, int],
        da_vectorized: set[str],
        proactive_tasks: dict[str, asyncio.Task],
    ) -> str:
        """Track DA failure signals and trigger vectorization when needed.

        Returns result_text, potentially with [SYSTEM] messages appended.
        """
        # Check if proactive vectorization completed for this evidence
        if evidence_id not in da_vectorized and evidence_id in proactive_tasks:
            task = proactive_tasks[evidence_id]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(
                        "Proactive vectorization task failed for %s: %s",
                        evidence_id,
                        exc,
                    )
                elif task.result():
                    da_vectorized.add(evidence_id)
                    result_text += (
                        "\n\n[SYSTEM] This file has been automatically "
                        "indexed for semantic search. Use "
                        "case_evidence_search to find content by "
                        "meaning rather than keywords."
                    )
                    logger.info(
                        "proactive_vectorization_completed",
                        extra={"evidence_id": evidence_id},
                    )

        # Track search_file empty results
        if func_name == "search_file" and tool_result.success:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict) and data.get("results_count", 0) == 0:
                    da_empty_search_counts[evidence_id] = (
                        da_empty_search_counts.get(evidence_id, 0) + 1
                    )
                else:
                    da_empty_search_counts[evidence_id] = 0
            except (json.JSONDecodeError, TypeError):
                pass

            # Advisory after 3 consecutive empty searches
            count = da_empty_search_counts.get(evidence_id, 0)
            if count >= 3:
                result_text += (
                    f"\n\n[SYSTEM] Last {count} search_file calls on this "
                    "file returned zero results. Consider using "
                    "deep_analysis with a different query approach."
                )

        # Track deep_analysis confidence + persist cross-turn counter
        if func_name == "deep_analysis" and tool_result.success and case:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict):
                    confidence = float(data.get("confidence", 1.0))
                    # Persist da_invocation_count for cross-turn tracking
                    for ev in getattr(case, "evidence", []):
                        if ev.evidence_id == evidence_id:
                            ev.da_invocation_count = (
                                getattr(ev, "da_invocation_count", 0) + 1
                            )
                            break
                    try:
                        await self.repository.save(case)
                    except Exception as e:
                        logger.debug("Failed to persist da_invocation_count: %s", e)

                    # Low confidence trigger
                    if confidence < 0.2 and evidence_id not in da_vectorized:
                        result_text = await self._reactive_vectorize(
                            evidence_id,
                            tool_context,
                            da_vectorized,
                            result_text,
                            "low_confidence",
                        )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Track timeouts
        if (
            not tool_result.success
            and "timed out" in (getattr(tool_result, "error", "") or "").lower()
        ):
            if evidence_id not in da_vectorized:
                result_text = await self._reactive_vectorize(
                    evidence_id,
                    tool_context,
                    da_vectorized,
                    result_text,
                    "tool_timeout",
                )

        # Reactive vectorization on repeated empty searches
        empty_count = da_empty_search_counts.get(evidence_id, 0)
        if empty_count >= 3 and evidence_id not in da_vectorized:
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                da_vectorized,
                result_text,
                "repeated_empty_searches",
            )

        return result_text

    async def _reactive_vectorize(
        self,
        evidence_id: str,
        tool_context: Any,
        da_vectorized: set[str],
        result_text: str,
        trigger: str,
    ) -> str:
        """Attempt reactive vectorization for a qualifying evidence file."""
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        # Get evidence size for size gate check
        ev_size = 0
        try:
            ev = await tool_context.evidence_service.get_evidence(
                evidence_id,
                tool_context.organization_id,
            )
            ev_size = (
                getattr(ev, "file_size", 0) or getattr(ev, "content_size_bytes", 0) or 0
            )
        except Exception:
            return result_text

        settings = get_settings()
        if ev_size < settings.agent.vectorization_min_size_bytes:
            return result_text
        if ev_size > VECTORIZATION_MAX_SIZE_BYTES:
            return result_text

        success = await self._vectorize_evidence(evidence_id, tool_context)
        if success:
            da_vectorized.add(evidence_id)
            result_text += (
                "\n\n[SYSTEM] This file has been automatically "
                "indexed for semantic search. Use "
                "case_evidence_search to find content by "
                "meaning rather than keywords."
            )
            logger.info(
                "reactive_vectorization_triggered",
                extra={
                    "evidence_id": evidence_id,
                    "trigger": trigger,
                    "content_size_bytes": ev_size,
                },
            )
        return result_text

    def _build_da_tool_schemas(self) -> list[dict]:
        """Build OpenAI-format tool definitions for DA investigation tools."""
        if not self.investigation_tools:
            return []

        tools = []
        for agent_tool in self.investigation_tools.get_all_tools():
            schema = agent_tool.get_schema()
            tools.append(
                {
                    "type": "function",
                    "function": schema,
                }
            )
        return tools

    @staticmethod
    def _build_da_system_instruction(
        tool_names: list[str],
        schema_tool_name: str,
    ) -> str:
        """Build the system instruction that tells the LLM how to use DA tools.

        Adapts to whichever investigation tools are actually registered.
        Without this, the LLM sees tool definitions but has no guidance on
        when or why to call them, leading to non-deterministic tool usage.
        """
        has_search = "search_file" in tool_names
        has_da = "deep_analysis" in tool_names
        has_web = "web_search" in tool_names
        has_kb = "kb_qa" in tool_names

        # Build tool guidance based on what's actually available
        search_mode_guidance = (
            "search_file modes:\n"
            "- keyword (DEFAULT): Splits query into tokens and finds lines "
            "containing all of them. Use for IPs, hostnames, error codes, "
            "service names, usernames. Just pass the raw value as query — "
            'e.g., query="173.234.31.186" or query="timeout connection".\n'
            "- regex: Only when keyword mode cannot express the pattern "
            "(e.g., timestamp ranges, capture groups). Regex is error-prone "
            "— prefer keyword mode unless you specifically need pattern matching."
        )

        # Core evidence tools
        tool_lines = []
        if has_search:
            tool_lines.append(
                "- search_file: keyword/regex search against raw evidence files. "
                "Use for exact matches — IPs, timestamps, error codes, service names."
            )
        if has_da:
            tool_lines.append(
                "- deep_analysis: LLM-interpreted analysis of specific evidence sections. "
                "Use for analytical questions keyword search cannot answer. "
                "Limited to 1 call per turn."
            )
        if has_kb:
            tool_lines.append(
                "- kb_qa: Search the knowledge base for runbooks, best practices, "
                "and documented solutions. Returns results from all accessible "
                "sources (global, personal, team) automatically."
            )
        if has_web:
            tool_lines.append(
                "- web_search: Search trusted technical websites (Stack Overflow, "
                "official docs) for error messages and solutions."
            )

        if tool_lines:
            # Build priority guidance
            priority_parts = []
            if has_search or has_da:
                evidence_tools = ", ".join(
                    t for t in ["search_file", "deep_analysis"] if t in tool_names
                )
                priority_parts.append(
                    f"1. Start with case evidence ({evidence_tools}) — "
                    "ground your analysis in THIS case's data first."
                )
            if has_kb:
                priority_parts.append(
                    "2. Check knowledge base (kb_qa) for documented solutions "
                    "when evidence alone doesn't explain the issue."
                )
            if has_web:
                priority_parts.append(
                    "3. Use web_search as a last resort when evidence and KB "
                    "have no answers — e.g., unfamiliar error messages or "
                    "technology-specific issues."
                )

            tool_guidance = (
                f"You have {len(tool_lines)} investigation tools:\n"
                + "\n".join(tool_lines)
                + "\n\nTool priority:\n"
                + "\n".join(priority_parts)
            )
            if has_search:
                tool_guidance += f"\n\n{search_mode_guidance}"
        else:
            tool_guidance = (
                "No investigation tools are available for this turn. "
                "Base your analysis on the evidence context provided."
            )

        return (
            "You have investigation tools available to search and analyze "
            "the raw evidence files attached to this case.\n\n"
            f"{tool_guidance}\n\n"
            "QUESTION ROUTING — Decide which type of question the user is asking:\n\n"
            "TYPE A — CASE QUESTION (about THIS case's evidence):\n"
            "Questions about specific data in the submitted files — IPs, errors, "
            "timestamps, patterns, configurations, or anything that requires "
            "examining the evidence. Examples: 'What IPs failed auth?', "
            "'What happened at 14:00?', 'Is there a pattern in the errors?'\n"
            f"→ You MUST search the evidence ({', '.join(t for t in ['search_file', 'deep_analysis'] if t in tool_names)}) before "
            "responding. The structural indexes are summaries — they lack the "
            "specific values needed for grounded analysis. After searching, call "
            f"{schema_tool_name} to produce your structured response.\n\n"
            "TYPE B — KNOWLEDGE QUESTION (general technical knowledge):\n"
            "Questions about technologies, concepts, best practices, or setup "
            "procedures that are NOT answerable from case evidence. Examples: "
            "'What is Opik?', 'How to set up Redis clustering?', "
            "'Common causes of OOM kills?'\n"
            "→ Answer from your own knowledge. Optionally use web_search or "
            "kb_qa for supplementary detail. Connect your answer to the "
            f"case context when relevant, then call {schema_tool_name}.\n\n"
            "TYPE C — HYBRID (needs both evidence AND knowledge):\n"
            "Questions that bridge case data and external knowledge. Examples: "
            "'Is our Redis config following best practices?', "
            "'Are these SSH settings secure?'\n"
            "→ Search evidence first to understand the current state, then use "
            "your knowledge, web_search, or KB tools for the reference baseline.\n\n"
            "DEFAULT: When uncertain, treat it as Type A (case question) — "
            "evidence search is always safe. Only skip evidence search when "
            "the question clearly cannot be answered from log files, configs, "
            "or other submitted data.\n\n"
            "IMPORTANT — Search for the specific entity, not the event type:\n"
            "When the user asks about a specific IP, hostname, username, error "
            "code, or timestamp, search for THAT value directly — e.g., "
            'query="173.234.31.186", not query="Failed password". Searching '
            "for event types returns results for ALL entities and buries the "
            "relevant lines.\n\n"
            "IMPORTANT — PII tokens vs raw data:\n"
            "The <evidence_collected> summaries use PII placeholders "
            "(e.g., <IP_ADDRESS_1>). The raw files contain ORIGINAL values. "
            "When calling search_file, use ORIGINAL values from the user's "
            "message, NOT PII tokens.\n\n"
            "EVIDENCE vs KNOWLEDGE — These are fundamentally different data types:\n"
            "- EVIDENCE is case-specific data submitted by the user: log files, "
            "metrics, configs, pasted text, screenshots, user statements about "
            "their environment. Only user-submitted data goes in evidence_to_add.\n"
            "- KNOWLEDGE is pre-built reference material from kb_qa, web_search, "
            "or your own training data. Knowledge informs your analysis but is "
            "NEVER recorded as evidence. Do NOT create evidence_to_add entries "
            "from kb_qa results, web_search results, or your own knowledge.\n\n"
            "RESPONSE FORMAT — Your agent_response MUST follow this structure:\n"
            "1. OBSERVATION: For case questions, cite specific data found — "
            "exact values from timestamps, error messages, IPs/hostnames, or "
            "metrics. For knowledge questions, state the relevant facts.\n"
            "2. ANALYSIS: Explain the significance. For case questions, use "
            "causal language connecting findings to your diagnosis. For knowledge "
            "questions, relate the information to the user's investigation context "
            "when possible."
        )

    def _build_tool_context(self, case: Any, intent_data: dict | None = None) -> Any:
        """Build ToolContext for tool execution during DA turns."""
        from faultmaven.modules.agent.tools.base import ToolContext

        user_id = (intent_data or {}).get("user_id", "system")
        organization_id = getattr(case, "organization_id", "")

        # Extract current investigation stage for tool context enrichment
        metadata: dict[str, Any] = {}
        progress = getattr(case, "investigation_progress", None)
        if progress:
            current_stage = getattr(progress, "current_stage", None)
            if current_stage:
                stage_value = (
                    current_stage.value
                    if hasattr(current_stage, "value")
                    else str(current_stage)
                )
                metadata["stage"] = stage_value.upper()

        return ToolContext(
            session_id=case.case_id,
            case_id=case.case_id,
            organization_id=organization_id,
            user_id=user_id,
            evidence_service=self.evidence_service,
            metadata=metadata,
        )

    def _parse_schema_tool_call(
        self,
        tool_call: Any,
        schema_model: Any,
    ) -> BaseInteractionResponse:
        """Parse a schema tool call response into a Pydantic model.

        Applies the same JSON cleanup (nested parsing + enum fixing) as the
        single-shot path in _generate_structured_output.
        """
        args = tool_call.function.get("arguments", "{}")
        if isinstance(args, dict):
            content = json.dumps(args)
        else:
            content = args

        # Parse JSON (strict=False allows control chars in LLM-generated strings)
        content_obj = json.loads(content, strict=False)

        # Recursively parse nested JSON strings
        content_obj = self._parse_nested_json(content_obj)

        # Fix hallucinated enum values
        schema_dict = schema_model.model_json_schema()
        content_obj = self._fix_enum_violations(
            content_obj,
            schema_dict,
            root_defs=schema_dict.get("$defs"),
        )

        # Validate with Pydantic
        content = json.dumps(content_obj)
        return schema_model.model_validate_json(content)

    @staticmethod
    def _build_assistant_message(response: Any) -> dict:
        """Convert LLMResponse to OpenAI-format assistant message."""
        tool_calls_list = []
        for tc in response.tool_calls or []:
            tool_calls_list.append(
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": tc.function,
                }
            )

        msg = {
            "role": "assistant",
            "content": response.content or "",
        }
        if tool_calls_list:
            msg["tool_calls"] = tool_calls_list
        return msg

    @staticmethod
    def _format_tool_result(result: Any, tool_name: str = "") -> str:
        """Format a ToolResult into a string for the LLM."""
        if not result.success:
            return f"Error: {result.error or 'Unknown error'}"

        if result.data is None:
            return "Success (no data returned)"

        # KB results: wrap with relay instruction and source citation guidance.
        # Note: _arun returns a pre-formatted string (via KBConfig.format_response),
        # not a dict. The string includes "Sources: ..." at the end.
        if tool_name == "kb_qa" and result.data:
            content = (
                result.data if isinstance(result.data, str) else json.dumps(result.data)
            )
            logger.info(f"kb_qa result: {len(content)} chars")
            return (
                "KNOWLEDGE BASE RESULT — Include the detailed content below "
                "in your response to the user. Do NOT summarize it into a "
                "single sentence. Preserve the key details, diagnostic steps, "
                "and resolution procedures.\n\n"
                f"{content}\n\n"
                "SOURCE CITATION: At the very end of your response, add a "
                "compact source line in italic markdown using this exact format:\n"
                "*Sources: [title1], [title2]*\n"
                "Use only the primary source title(s) from the content above. "
                "Keep it to one short line. Do NOT write a verbose paragraph "
                "about where the information came from."
            )

        if isinstance(result.data, str):
            return result.data
        return json.dumps(result.data)

    @staticmethod
    def _parse_nested_json(obj):
        """Recursively parse JSON strings in a dict/list structure."""
        if isinstance(obj, dict):
            return {k: MilestoneEngine._parse_nested_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [MilestoneEngine._parse_nested_json(item) for item in obj]
        elif isinstance(obj, str):
            try:
                parsed = json.loads(obj)
                return MilestoneEngine._parse_nested_json(parsed)
            except (json.JSONDecodeError, TypeError):
                return obj
        else:
            return obj

    @staticmethod
    def _fix_enum_violations(obj, schema_dict, root_defs=None):
        """Recursively fix enum violations in the response object."""

        if not isinstance(obj, dict):
            return obj

        properties = schema_dict.get("properties", {})
        if root_defs is None:
            root_defs = schema_dict.get("$defs", {})
        local_defs = schema_dict.get("$defs", {})
        all_defs = {**local_defs, **root_defs}

        fixed_obj = {}
        for key, value in obj.items():
            if key not in properties:
                fixed_obj[key] = value
                continue

            prop_schema = properties[key]

            if "enum" in prop_schema and isinstance(value, str):
                valid_values = prop_schema["enum"]
                if value not in valid_values:
                    closest_match = difflib.get_close_matches(
                        value, valid_values, n=1, cutoff=0.6
                    )
                    if closest_match:
                        corrected = closest_match[0]
                        logger.warning(
                            f"Auto-correcting hallucinated enum value: "
                            f"'{value}' -> '{corrected}' for field '{key}'"
                        )
                        fixed_obj[key] = corrected
                    else:
                        fallback = valid_values[0]
                        logger.warning(
                            f"No close match for hallucinated enum '{value}', "
                            f"using fallback '{fallback}' for field '{key}'"
                        )
                        fixed_obj[key] = fallback
                else:
                    fixed_obj[key] = value

            elif isinstance(value, dict):
                nested_schema = None
                if "$ref" in prop_schema:
                    ref_name = prop_schema["$ref"].split("/")[-1]
                    nested_schema = all_defs.get(ref_name, {})
                elif "anyOf" in prop_schema:
                    for option in prop_schema["anyOf"]:
                        if "$ref" in option:
                            ref_name = option["$ref"].split("/")[-1]
                            nested_schema = all_defs.get(ref_name, {})
                            break
                        elif option.get("type") != "null":
                            nested_schema = option
                            break
                elif "properties" in prop_schema:
                    nested_schema = prop_schema

                if nested_schema:
                    fixed_obj[key] = MilestoneEngine._fix_enum_violations(
                        value, nested_schema, root_defs
                    )
                else:
                    fixed_obj[key] = value

            elif isinstance(value, list):
                fixed_list = []
                item_schema = None
                if "items" in prop_schema:
                    if "$ref" in prop_schema["items"]:
                        ref_name = prop_schema["items"]["$ref"].split("/")[-1]
                        item_schema = all_defs.get(ref_name, {})
                    else:
                        item_schema = prop_schema["items"]
                elif "anyOf" in prop_schema:
                    for option in prop_schema["anyOf"]:
                        if option.get("type") == "array" and "items" in option:
                            if "$ref" in option["items"]:
                                ref_name = option["items"]["$ref"].split("/")[-1]
                                item_schema = all_defs.get(ref_name, {})
                            else:
                                item_schema = option["items"]
                            break

                for item in value:
                    if isinstance(item, dict) and item_schema:
                        fixed_list.append(
                            MilestoneEngine._fix_enum_violations(
                                item, item_schema, root_defs
                            )
                        )
                    else:
                        fixed_list.append(item)
                fixed_obj[key] = fixed_list

            else:
                fixed_obj[key] = value

        return fixed_obj

    async def _generate_structured_output(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict] | None = None,
        tool_context: Any | None = None,
        force_tool_use: bool = False,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
    ) -> BaseInteractionResponse:
        """
        Generate structured output from LLM using provider-agnostic capability system.

        This method automatically detects the provider's structured output capabilities
        and adjusts the prompt and response format accordingly:
        - STRICT mode: Uses json_schema with strict:true (OpenAI GPT-4o, Groq gpt-oss)
        - BEST_EFFORT mode: Uses json_object with schema in prompt (most models)
        - FUNCTION_CALLING mode: Uses tool calling pattern (Anthropic Claude)
        - NONE mode: Schema only in prompt, no API support (legacy models)

        When investigation_tools and tool_context are provided, routes through
        _tool_augmented_generate for a bounded tool-calling loop.

        Args:
            prompt: User prompt
            schema_model: Pydantic model class for expected output
            investigation_tools: OpenAI-format tool defs for investigation tools
            tool_context: ToolContext for tool execution
            force_tool_use: If True, tool_choice="required" (DA turns).
                If False, tool_choice="auto" (LLM decides).
            redaction_ctx: Case-scoped redaction context for PII sanitization

        Returns:
            Instantiated Pydantic model
        """
        # Apply case-scoped PII redaction to the prompt before any LLM call.
        # This covers both the tool-augmented (DA) and single-shot paths.
        if redaction_ctx:
            prompt = redaction_ctx.sanitize(prompt)
        # Branch to tool-augmented generation for DA turns with tools.
        # Two layers of protection:
        # 1. Pre-check: skip known-incompatible providers (avoids wasted API call)
        # 2. Runtime fallback: if tool calling fails on first attempt, catch
        #    ToolCallingUnsupportedError and fall through to non-tool path
        if investigation_tools and tool_context:
            from faultmaven.exceptions import ToolCallingUnsupportedError

            # Layer 1: Pre-check for known-incompatible providers/models
            provider = self.da_provider or self.llm_provider
            model = self.da_model if self.da_provider else None
            if hasattr(
                provider, "supports_tool_calling"
            ) and not provider.supports_tool_calling(model):
                logger.warning(
                    "Provider %s (model: %s) does not support tool calling. "
                    "Falling back to non-tool structured output path.",
                    getattr(provider, "provider_name", type(provider).__name__),
                    model or "default",
                )
            else:
                # Layer 2: Runtime fallback for unknown incompatibilities
                try:
                    return await self._tool_augmented_generate(
                        prompt,
                        schema_model,
                        investigation_tools,
                        tool_context,
                        force_tool_use=force_tool_use,
                        redaction_ctx=redaction_ctx,
                        case=case,
                    )
                except ToolCallingUnsupportedError as e:
                    logger.warning(
                        "Tool calling failed at runtime: %s. "
                        "Falling back to non-tool structured output path.",
                        e,
                    )

        # Get provider-specific structured output strategy
        schema = schema_model.model_json_schema()
        strategy = self.llm_provider.get_structured_output_strategy(schema)

        # Conditionally include schema in prompt based on provider capability
        if strategy.include_schema_in_prompt:
            # Provider requires schema in prompt text (json_object or prompt_only modes)
            from faultmaven.core.investigation.prompts.templates import (
                SCHEMA_INSTRUCTIONS,
            )

            schema_json = json.dumps(schema, indent=2)
            json_instruction = (
                f"\n\n{SCHEMA_INSTRUCTIONS}\n"
                "You MUST respond with valid JSON matching this exact schema:\n\n"
                f"```json\n{schema_json}\n```\n\n"
                "IMPORTANT:\n"
                "- Use the exact field names shown in the schema\n"
                "- Do not add extra fields not in the schema\n"
                "- Do not include any text before or after the JSON\n"
                "- Ensure all required fields are present\n"
            )
            final_prompt = f"{prompt}{json_instruction}"
        else:
            # Provider supports strict json_schema - no need for schema in prompt
            final_prompt = prompt

        # Track max_tokens across retries (will be increased if truncation detected)
        max_tokens_state = {"value": 8000}  # Increased from 4000 base

        # Define the LLM operation for retry
        async def llm_operation():
            # Build generate parameters based on strategy mode
            # CRITICAL FIX: Increased from 4000 to 8000 tokens to prevent JSON truncation
            # Investigation schemas (especially _Verification) can be large, and Turn 2+
            # requires substantial context. 4000 tokens was insufficient, causing:
            # "EOF while parsing a value at line 4 column 24" errors
            current_max_tokens = max_tokens_state["value"]
            generate_params = {
                "prompt": final_prompt,
                "max_tokens": current_max_tokens,
                "temperature": 0.2,  # Lower temperature for structured output
                "case_id": case.case_id if case is not None else None,
            }

            logger.debug(
                f"Structured output generation attempt with max_tokens={current_max_tokens}"
            )

            # Apply strategy-specific parameters
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Use tools/function calling for structured output (Anthropic, etc.)
                from faultmaven.utils.schema_converter import pydantic_to_openai_tools

                generate_params["tools"] = pydantic_to_openai_tools(schema_model)
                generate_params["tool_choice"] = "required"  # Force tool use
                # Don't include response_format for function calling
            else:
                # Use response_format for JSON modes (STRICT, BEST_EFFORT, NONE)
                if strategy.response_format:
                    generate_params["response_format"] = strategy.response_format

            response = await self.llm_provider.generate(**generate_params)
            content = response if isinstance(response, str) else response.content

            # For function calling, extract from tool_calls
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Parse response to handle tool_calls format
                if hasattr(response, "tool_calls") and response.tool_calls:
                    # Extract arguments from first tool call
                    args = response.tool_calls[0].function.get("arguments", "{}")

                    # arguments may be a string (most providers) or dict (some providers)
                    if isinstance(args, dict):
                        # Convert dict to JSON string for model_validate_json
                        content = json.dumps(args)
                    else:
                        # Already a string
                        content = args
            else:
                # For non-function-calling modes, strip markdown code blocks if present
                # Some LLMs return: ```json\n{...}\n``` instead of raw JSON
                # Or even worse: "Here's the response:\n```json\n{...}\n```"
                if isinstance(content, str):
                    content = content.strip()

                    # Check if content contains a markdown code block
                    if "```" in content:
                        # Extract JSON from markdown code block
                        # Handle both cases:
                        # 1. ```json\n{...}\n```
                        # 2. Some text\n```json\n{...}\n```\nMore text
                        # Match ```json (or ```JSON or just ```) followed by content until closing ```
                        pattern = r"```(?:json|JSON)?\s*\n(.*?)\n```"
                        match = re.search(pattern, content, re.DOTALL)
                        if match:
                            content = match.group(1).strip()
                        elif content.startswith("```"):
                            # Fallback to old logic if regex fails
                            lines = content.split("\n")
                            if lines and lines[0].startswith("```"):
                                lines = lines[1:]
                            if lines and lines[-1].strip() == "```":
                                lines = lines[:-1]
                            content = "\n".join(lines).strip()

            try:
                # First, try to load content as JSON if it's a string
                if isinstance(content, str):
                    content_obj = json.loads(content, strict=False)
                else:
                    content_obj = content

                # Parse any nested JSON strings (reuse class static method)
                content_obj = MilestoneEngine._parse_nested_json(content_obj)

                # Fix any hallucinated enum values (reuse class static method)
                schema_dict = schema_model.model_json_schema()
                content_obj = MilestoneEngine._fix_enum_violations(
                    content_obj, schema_dict, root_defs=schema_dict.get("$defs")
                )

                # Convert back to JSON string for Pydantic validation
                content = json.dumps(content_obj)

                return schema_model.model_validate_json(content)
            except Exception as validation_error:
                # If JSON validation fails due to truncation, increase max_tokens for retry
                error_str = str(validation_error).lower()
                if "eof while parsing" in error_str or "truncated" in error_str:
                    # Double max_tokens for next retry attempt
                    old_max = max_tokens_state["value"]
                    max_tokens_state["value"] = min(old_max * 2, 16000)  # Cap at 16k
                    logger.warning(
                        f"JSON truncation detected, increasing max_tokens: "
                        f"{old_max} → {max_tokens_state['value']}"
                    )
                # Re-raise to trigger retry
                raise

        # Execute with retry and error handling
        result, error_result = await self.llm_error_handler.with_retry(
            operation=llm_operation
        )

        if result is not None:
            return result

        # All retries exhausted or non-retryable error
        if error_result:
            error_msg = error_result.message
            logger.error(f"Structured generation failed after retries: {error_msg}")
            raise MilestoneEngineError(
                f"Structured output generation failed: {error_msg}"
            )
        else:
            raise MilestoneEngineError(
                "Structured output generation failed with unknown error"
            )

    # =========================================================================
    # Response Processing
    # =========================================================================

    async def _process_response_structured(
        self,
        case: Case,
        user_message: str,
        response_obj: BaseInteractionResponse,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[Case, dict[str, Any]]:
        """Process structured response and update case state."""

        # NOTE: Validation moved AFTER post-processing to allow fallback evidence creation
        # See line 1500 for actual validation

        # Initialize metadata for this response processing
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            "outcome": TurnOutcome.CONVERSATION,
        }

        # Handle file uploads (common across all states)
        # UploadedFile tracks raw file metadata. Evidence classification is
        # content-based and LLM-driven — the LLM evaluates the data and
        # creates Evidence via evidence_to_add with the appropriate category.
        if attachments:
            for attachment in attachments:
                uploaded_file = self._create_uploaded_file_from_attachment(
                    case=case, attachment=attachment, turn_number=case.current_turn
                )
                case.uploaded_files.append(uploaded_file)
                metadata["files_uploaded"] = metadata.get("files_uploaded", []) + [
                    uploaded_file.file_id
                ]

        # POST-PROCESSING: Apply LLM failure mitigation (Pattern-based fallback)
        # This repairs LLM classification failures before applying state updates
        # Reference: docs/working/LLM-FAILURE-MITIGATION-STRATEGY.md
        logger.debug(
            f"Post-processing LLM response: response_type={type(response_obj).__name__}, "
            f"has_state_updates={hasattr(response_obj, 'state_updates')}, "
            f"state_updates_exists={response_obj.state_updates is not None if hasattr(response_obj, 'state_updates') else False}"
        )
        if isinstance(response_obj, (InquiryResponse,)) or (
            hasattr(response_obj, "state_updates") and response_obj.state_updates
        ):
            # Apply post-processing to repair state_updates
            logger.debug(
                f"Applying post-processing to state_updates with user_message preview: {user_message[:100]}..."
            )
            response_obj.state_updates = _post_process_llm_response(
                updates=response_obj.state_updates,
                user_message=user_message,
                case=case,
            )
            # None-safe logging
            evidence_list = getattr(response_obj.state_updates, "evidence_to_add", [])
            evidence_count = len(evidence_list) if evidence_list is not None else 0
            logger.debug(
                f"Post-processing complete, evidence_to_add count: {evidence_count}"
            )

        # Validate reasoning-first requirement (AFTER post-processing to allow fallback evidence creation)
        is_valid, validation_errors = validate_reasoning_first(response_obj, case)
        if not is_valid:
            error_msg = "Reasoning validation failed:\n" + "\n".join(validation_errors)
            logger.warning(
                f"Reasoning validation failed for case {case.case_id}: {error_msg}"
            )
            # Degrade gracefully: strip milestone completions and continue with the response
            # instead of crashing with a 500 error
            if (
                hasattr(response_obj, "internal_reasoning")
                and response_obj.internal_reasoning
            ):
                response_obj.internal_reasoning.milestone_justifications = {}
            if hasattr(response_obj, "state_updates"):
                milestones = getattr(response_obj.state_updates, "milestones", None)
                if milestones:
                    # Reset all milestone booleans to None (uncompleted)
                    for field_name in milestones.model_fields:
                        setattr(milestones, field_name, None)
            logger.info(
                f"Stripped invalid milestones for case {case.case_id}, continuing with response"
            )

        # Dispatch based on response type
        if isinstance(response_obj, InquiryResponse):
            await self._apply_inquiry_updates(
                case, response_obj.state_updates, metadata, user_message
            )
        elif isinstance(response_obj, TerminalResponse):
            # Terminal updates typically just documentation, no deep state change
            pass
        else:
            # Investigation updates (Verification, Hypothesis, Resolution, General)
            # All check 'state_updates' which matches InvestigationStateUpdate structure
            await self._apply_investigation_updates(
                case,
                response_obj.state_updates,
                metadata,
                attachments,
                response_obj,
                user_message,
            )

        # Store response_obj in metadata so _check_automatic_transitions can
        # access ProposedTransition for the User-Agent Handshake flow
        metadata["response_obj"] = response_obj

        return case, metadata

    async def _apply_inquiry_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        user_message: str = "",
    ) -> None:
        """Apply updates during INQUIRY phase."""
        if updates.proposed_problem_statement:
            case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

        # Convert and store problem_confirmation from LLM schema to domain model
        if updates.problem_confirmation:
            from faultmaven.modules.case.domain.models import (
                ProblemConfirmation as DomainProblemConfirmation,
            )

            case.inquiry.problem_confirmation = DomainProblemConfirmation(
                problem_type=updates.problem_confirmation.problem_type,
                severity_guess=updates.problem_confirmation.severity_guess,
                preliminary_guidance=updates.problem_confirmation.preliminary_guidance
                or "",  # Convert None to empty string
            )

        # Convert and store preliminary_urgency from LLM schema to domain model
        if updates.preliminary_urgency:
            from faultmaven.modules.case.domain.models import (
                PreliminaryUrgency as DomainPreliminaryUrgency,
            )
            from faultmaven.modules.case.domain.models import UrgencyLevel

            case.inquiry.preliminary_urgency = DomainPreliminaryUrgency(
                level=UrgencyLevel(
                    updates.preliminary_urgency.level.lower()
                ),  # Convert uppercase to lowercase enum
                is_ongoing=getattr(updates.preliminary_urgency, "is_ongoing", False),
                is_incident_report=getattr(
                    updates.preliminary_urgency, "is_incident_report", False
                ),
                impact_assessment=updates.preliminary_urgency.impact_assessment,
                assessed_at_turn=case.current_turn,  # Use current turn number
            )

        # STAGE 1: Extract problem statement from LLM (first turn only)
        # Extract problem statement but DON'T auto-confirm yet
        if updates.problem_confirmation and not case.inquiry.proposed_problem_statement:
            if updates.problem_confirmation.preliminary_guidance:
                case.inquiry.proposed_problem_statement = (
                    updates.problem_confirmation.preliminary_guidance
                )
                logger.info(
                    f"Problem statement extracted from preliminary_guidance: {updates.problem_confirmation.problem_type}"
                )
            # If no preliminary_guidance but proposed_problem_statement exists in updates,
            # it was already set above at line 685-686

        # STAGE 2: Two-Step Confirmation (Design Doc Section 1.2)
        #
        # The design requires explicit user confirmation before INQUIRY → INVESTIGATING.
        # Auto-confirm is NOT used — even for CRITICAL/HIGH urgency issues.
        #
        # Flow:
        #   Turn N: User reports incident → Agent presents problem statement + asks "Is this accurate?"
        #   Turn N+1: User confirms ("Yes") → LLM sets user_confirmed_investigation=True → transition fires
        #
        # This block handles two scenarios:
        # (a) LLM signals user confirmation via user_confirmed_investigation=True
        # (b) Logging for informational/urgent cases (no auto-transition)
        _is_incident = updates.preliminary_urgency and getattr(
            updates.preliminary_urgency, "is_incident_report", False
        )

        # Check if LLM detected user confirmation of the problem statement
        if (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
        ):
            case.inquiry.problem_statement_confirmed = True
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            case.inquiry.decided_to_investigate = True
            case.inquiry.decision_made_at = datetime.now(UTC)
            logger.info(
                f"User confirmed problem statement — transitioning to INVESTIGATING. "
                f"statement='{case.inquiry.proposed_problem_statement[:80]}...'"
            )
        elif (
            updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not _is_incident
        ):
            # LLM flagged HIGH urgency but did NOT mark as incident report.
            # This typically means the user asked an informational/how-to question
            # about a topic that involves failures (e.g., "How do I check logs of a
            # restarting pod?"). Stay in INQUIRY.
            logger.info(
                f"Urgent signals detected but is_incident_report=False — "
                f"treating as informational query, staying in INQUIRY. "
                f"level={updates.preliminary_urgency.level}, "
                f"problem_type={updates.problem_confirmation.problem_type if updates.problem_confirmation else 'unknown'}"
            )
        elif (
            _is_incident
            and updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not case.inquiry.problem_statement_confirmed
        ):
            # Urgent incident detected — agent should present problem statement
            # and ask for confirmation in its response. Transition will happen on
            # the NEXT turn when user confirms.
            logger.info(
                f"Urgent incident detected ({updates.preliminary_urgency.level} + ongoing). "
                f"Agent will present problem statement for user confirmation. "
                f"has_statement={bool(case.inquiry.proposed_problem_statement)}"
            )

        # Store KB match on case when LLM identifies one (Gap #5a)
        # This populates InquiryData.knowledge_matches so we can validate
        # confidence thresholds when knowledge_resolution arrives (possibly in a later turn)
        if updates.knowledge_match:
            km = updates.knowledge_match
            case.inquiry.knowledge_matches.append(
                KnowledgeMatch(
                    match_id=km.match_type
                    + "_"
                    + str(len(case.inquiry.knowledge_matches)),
                    match_type=km.match_type,
                    relevance_score=km.match_likelihood,
                    summary=km.match_summary,
                    potential_solution=km.suggested_solution,
                )
            )
            logger.info(
                f"KB match stored: type={km.match_type}, "
                f"likelihood={km.match_likelihood:.2f}, "
                f"summary={km.match_summary[:80]}"
            )

        # Check for KB Resolution
        if updates.knowledge_resolution:
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=updates.knowledge_resolution.match_id,
                match_type=updates.knowledge_resolution.match_type,
                solution_applied=updates.knowledge_resolution.solution_applied,
                user_confirmation=updates.knowledge_resolution.user_confirmation,
            )
            # This triggers Fast-Track in _check_fast_track_resolution

        # Evidence creation (single-phase): Create evidence from LLM-classified submissions
        # This code is shared between INQUIRY and INVESTIGATING phases

        # Evidence creation from LLM's evidence_to_add
        # Preprocessing now happens in Step 1 of process_turn() (before LLM).
        # Evidence here is agent-derived findings → always SUBMITTED_DATA form.
        has_attr = hasattr(updates, "evidence_to_add")
        evidence_list = getattr(updates, "evidence_to_add", None) if has_attr else None
        evidence_count = len(evidence_list) if evidence_list else 0
        logger.info(
            f"Evidence creation check (INQUIRY): "
            f"hasattr(updates, 'evidence_to_add')={has_attr}, "
            f"evidence_to_add={evidence_list}, "
            f"count={evidence_count}"
        )

        if hasattr(updates, "evidence_to_add") and updates.evidence_to_add:
            # Derive source filename from uploaded files submitted this turn
            turn_files = [
                uf
                for uf in case.uploaded_files
                if uf.uploaded_at_turn == case.current_turn
            ]
            source_filename = turn_files[0].filename if len(turn_files) == 1 else None

            for ev_item in updates.evidence_to_add:
                # During INQUIRY phase, milestones are not yet being tracked,
                # so we don't infer milestone attribution (advances_milestones will be empty)
                # Evidence will be available for milestone validation once case transitions to INVESTIGATING
                advances_milestones = []  # INQUIRY phase: No milestone tracking yet

                # Compute content hash for deduplication of inline evidence
                evidence_content = ev_item.summary or ""
                content_hash = hashlib.sha256(evidence_content.encode()).hexdigest()

                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    content_ref=_truncate_content_ref(ev_item.content_ref),
                    category=ev_item.category,
                    source_type=ev_item.source_type,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    form=EvidenceForm.SUBMITTED_DATA,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                    preprocessed_content=ev_item.summary,
                    content_size_bytes=len(evidence_content),
                    preprocessing_method="none",
                    data_type=None,
                    content_hash=content_hash,
                    original_filename=source_filename,
                )
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                logger.info(
                    f"Created evidence (INQUIRY): {ev.evidence_id} | "
                    f"category={ev.category.value}, source_type={ev.source_type.value}, "
                    f"form={ev.form.value}, "
                    f"summary='{ev.summary[:80]}...'"
                )

    async def _apply_investigation_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        attachments: list[dict[str, Any]] | None = None,
        response_obj: Any | None = None,
        user_message: str = "",
    ) -> None:
        """Apply updates during INVESTIGATING phase."""
        from faultmaven.modules.case.domain.services.investigation_router import (
            determine_investigation_path,
        )

        # 0. Check for Proactive Blocker Detection — surface as system feedback
        if hasattr(updates, "missing_critical_data") and updates.missing_critical_data:
            blocker = updates.missing_critical_data
            blocker_msg = (
                f"DATA QUALITY ISSUE: {blocker.description}. "
                f"Expected: {blocker.what_was_expected}. Found: {blocker.what_was_found}. "
                f"Impact: {blocker.impact}."
            )
            if blocker.suggested_alternatives:
                blocker_msg += (
                    f" Alternatives: {', '.join(blocker.suggested_alternatives)}"
                )
            current_feedback = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = f"{current_feedback}\n{blocker_msg}".strip()
            metadata["data_blocker_detected"] = True
            logger.warning(f"Case {case.case_id} data blocker: {blocker.description}")

        # Track evidence quality issues (non-blocking)
        if (
            hasattr(updates, "evidence_quality_issues")
            and updates.evidence_quality_issues
        ):
            for issue in updates.evidence_quality_issues:
                logger.info(
                    f"Evidence quality issue detected: {issue.evidence_id} - {issue.issue_type} ({issue.severity})"
                )
                # Could store these in case metadata for future reference
                metadata.setdefault("evidence_quality_issues", []).append(
                    {
                        "evidence_id": issue.evidence_id,
                        "issue_type": issue.issue_type,
                        "severity": issue.severity,
                    }
                )

        # 1. Update Milestones
        # NOTE: solution_verified is excluded — it requires the User-Agent
        # Handshake via ProposedTransition (see terminal_transitions.py).
        if updates.milestones:
            m = updates.milestones
            p = case.progress
            # Only set to True (never revert)
            milestone_fields = [
                # Progress indicators (LLM context, non-stage-driving)
                "symptom_verified",
                "scope_assessed",
                "timeline_established",
                "changes_identified",
                "root_cause_identified",
                # solution_proposed — set programmatically at ProposedAction creation (3F)
                # solution_verified — requires User-Agent Handshake
                # Stage-gate milestones (LLM detects user compliance — Framework §4.1)
                "mitigation_accepted",
                "mitigation_verified",
                "solution_accepted",
            ]
            stage_gate_fields = {
                "mitigation_accepted",
                "mitigation_verified",
                "solution_accepted",
            }

            # Guard: check if a pending ProposedAction exists before allowing
            # stage-gate milestones. Prevents LLM hallucinating compliance
            # when no action was proposed.
            has_pending_action = any(
                a.status == "pending" for a in case.proposed_actions
            )

            for field in milestone_fields:
                if getattr(m, field, False):
                    # Guard: reject stage-gate milestones if no pending action
                    if field in stage_gate_fields and not has_pending_action:
                        logger.warning(
                            f"Rejected stage-gate milestone '{field}' for case "
                            f"{case.case_id}: no pending ProposedAction exists"
                        )
                        continue
                    # Only append if transitioning from False to True
                    if not getattr(p, field, False):
                        setattr(p, field, True)
                        metadata["milestones_completed"].append(field)

            # Trigger path selection when symptom_verified is first completed
            if (
                "symptom_verified" in metadata["milestones_completed"]
                and not case.path_selection
            ):
                case.path_selection = determine_investigation_path(
                    case.problem_verification
                )
                logger.info(
                    f"Path Selection Triggered: {case.path_selection.path} "
                    f"(reason: {case.path_selection.rationale})"
                )
                # If MITIGATION_FIRST selected and confirmed, agent prompts will adapt automatically next turn

            if m.root_cause_likelihood is not None:
                p.root_cause_likelihood = m.root_cause_likelihood
            _valid_methods = {
                "direct_analysis",
                "hypothesis_validation",
                "single_shot_validation",
                "correlation",
                "user_provided",
                "other",
            }
            if m.root_cause_method:
                if m.root_cause_method in _valid_methods:
                    p.root_cause_method = m.root_cause_method
                else:
                    logger.warning(
                        f"LLM returned invalid root_cause_method '{m.root_cause_method}', "
                        f"mapping to 'other'"
                    )
                    p.root_cause_method = "other"

            # Ensure consistency: if root_cause_identified was just set,
            # root_cause_method and root_cause_likelihood must also be set
            if p.root_cause_identified:
                if not p.root_cause_method:
                    p.root_cause_method = m.root_cause_method or "direct_analysis"
                if p.root_cause_likelihood == 0.0:
                    p.root_cause_likelihood = m.root_cause_likelihood or 0.8

            # Stage-gate side effects (Framework §4.1)
            # When the LLM sets a stage-gate milestone, apply corresponding
            # side effects: mark the pending ProposedAction as accepted and
            # create an ActionAttempt audit record.
            stage_gate_completed = {
                "mitigation_accepted",
                "mitigation_verified",
                "solution_accepted",
            } & set(metadata["milestones_completed"])

            if stage_gate_completed:
                _apply_stage_gate_side_effects(
                    case, stage_gate_completed, user_message, metadata
                )

        # 2. Add Evidence
        # Preprocessing now happens in Step 1 of process_turn() (before LLM).
        # Evidence from evidence_to_add is agent-derived findings → always SUBMITTED_DATA form.
        # Evidence from attachments was already created in Step 1 with form=DOCUMENT.
        has_attr = hasattr(updates, "evidence_to_add")
        evidence_list = getattr(updates, "evidence_to_add", None) if has_attr else None
        evidence_count = len(evidence_list) if evidence_list else 0
        logger.info(
            f"Evidence creation check: "
            f"hasattr(updates, 'evidence_to_add')={has_attr}, "
            f"evidence_to_add={evidence_list}, "
            f"count={evidence_count}"
        )

        if hasattr(updates, "evidence_to_add") and updates.evidence_to_add:
            # Derive source filename from uploaded files submitted this turn
            turn_files = [
                uf
                for uf in case.uploaded_files
                if uf.uploaded_at_turn == case.current_turn
            ]
            source_filename = turn_files[0].filename if len(turn_files) == 1 else None

            for ev_item in updates.evidence_to_add:
                # Infer milestone attribution (Tier 2 + Tier 3)
                # Tier 2: System infers from category + milestones completed this turn
                # Tier 3: LLM can override via advances_milestones field
                milestones_completed_this_turn = metadata.get(
                    "milestones_completed", []
                )

                if ev_item.advances_milestones is not None:
                    # Tier 3: LLM provided explicit override
                    advances_milestones = ev_item.advances_milestones
                    logger.debug(
                        f"Evidence milestone attribution: LLM override "
                        f"(category={ev_item.category.value}, "
                        f"explicit_milestones={advances_milestones})"
                    )
                else:
                    # Tier 2: System inference
                    advances_milestones = _infer_milestones(
                        ev_item.category, milestones_completed_this_turn
                    )

                # Compute content hash for deduplication of inline evidence
                evidence_content = ev_item.summary or ""
                content_hash = hashlib.sha256(evidence_content.encode()).hexdigest()

                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    content_ref=_truncate_content_ref(ev_item.content_ref),
                    category=ev_item.category,
                    source_type=ev_item.source_type,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    form=EvidenceForm.SUBMITTED_DATA,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                    preprocessed_content=ev_item.summary,
                    content_size_bytes=len(evidence_content),
                    preprocessing_method="none",
                    data_type=None,
                    content_hash=content_hash,
                    original_filename=source_filename,
                )
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                logger.info(
                    f"Created evidence: {ev.evidence_id} | "
                    f"category={ev.category.value}, source_type={ev.source_type.value}, "
                    f"form={ev.form.value}, "
                    f"summary='{ev.summary[:80]}...'"
                )

        # 2b. Validate Milestone Claims Against Cited Evidence
        # Milestones are applied optimistically from LLM output (step 1 above),
        # then validated here. Invalid claims are REVERTED to prevent milestones
        # advancing without supporting evidence.
        if metadata["milestones_completed"]:
            from faultmaven.core.investigation.evidence_processor import (
                validate_milestone_claims,
            )

            reasoning = getattr(response_obj, "internal_reasoning", None)
            validation_results = validate_milestone_claims(
                case, metadata["milestones_completed"], reasoning
            )
            for result in validation_results:
                if not result.is_valid:
                    # Revert the milestone — evidence doesn't support the claim
                    setattr(case.progress, result.milestone, False)
                    metadata["milestones_completed"].remove(result.milestone)
                    logger.warning(
                        f"Milestone '{result.milestone}' REVERTED: claimed with insufficient evidence "
                        f"({result.cited_count}/{result.expected_min} required). "
                        f"Warnings: {result.warnings}"
                    )
                    metadata.setdefault("milestone_validation_warnings", []).extend(
                        result.warnings
                    )
                    # If symptom_verified was reverted, also revert path selection
                    # that was set optimistically during milestone application
                    if result.milestone == "symptom_verified" and case.path_selection:
                        logger.warning(
                            f"Path selection reverted: symptom_verified milestone was invalid"
                        )
                        case.path_selection = None

        # 3. Add/Update Hypotheses
        if hasattr(updates, "hypotheses_to_add") and updates.hypotheses_to_add:
            for h_item in updates.hypotheses_to_add:
                h = self.hypothesis_manager.create_hypothesis(
                    statement=h_item.statement,
                    category=h_item.category,
                    initial_likelihood=h_item.likelihood,
                    current_turn=case.current_turn,
                    status=HypothesisStatus.ACTIVE,
                )
                case.hypotheses[h.hypothesis_id] = h
                metadata["hypotheses_generated"].append(h.hypothesis_id)

        # 4. Link Evidence (Partial Application Check)
        # Note: Hypothesis-evidence linking is best-effort. The LLM may reference
        # evidence IDs that don't exist yet (timing issue), so we silently skip failed links.
        if (
            hasattr(updates, "hypothesis_evidence_links")
            and updates.hypothesis_evidence_links
        ):
            for link in updates.hypothesis_evidence_links:
                # Resolve partial IDs like 'new_index_0' to actual IDs if we just created them
                h_id = self._resolve_id_ref(
                    link.hypothesis_id_ref,
                    metadata.get("hypotheses_generated", []),
                    "hyp",
                )
                e_id = self._resolve_id_ref(
                    link.evidence_id_ref, metadata.get("evidence_added", []), "ev"
                )

                # Check existence
                if h_id not in case.hypotheses:
                    # Hypothesis ID validation failed - log warning but don't add to system_feedback
                    logger.warning(
                        f"Hypothesis-evidence link skipped: Hypothesis ID '{h_id}' not found "
                        f"(resolved from '{link.hypothesis_id_ref}'). "
                        f"Available hypotheses: {list(case.hypotheses.keys())}, "
                        f"Hypotheses added this turn: {metadata.get('hypotheses_generated', [])}"
                    )
                    continue

                # Check evidence existence (scan list)
                ev_exists = any(e.evidence_id == e_id for e in case.evidence)
                if not ev_exists:
                    # Evidence reference failed to resolve
                    # This is only a problem if LLM tried to link evidence but used wrong format/ID
                    # It's acceptable if no evidence exists (e.g., user_text message)

                    # Build diagnostic info
                    evidence_this_turn = metadata.get("evidence_added", [])
                    all_evidence_ids = [e.evidence_id for e in case.evidence]

                    logger.warning(
                        f"Hypothesis-evidence link validation failed: "
                        f"Cannot resolve reference '{link.evidence_id_ref}' to evidence ID '{e_id}'. "
                        f"Evidence created this turn: {evidence_this_turn}. "
                        f"Recent evidence IDs: {all_evidence_ids[-5:] if len(all_evidence_ids) > 5 else all_evidence_ids}. "
                        f"Note: This is expected if no evidence was created (user_text messages)."
                    )
                    continue

                self.hypothesis_manager.link_evidence(
                    case.hypotheses[h_id],
                    e_id,
                    link.stance == EvidenceStance.SUPPORTS,
                    case.current_turn,
                    reasoning=link.reasoning,
                    stance_confidence=link.stance_confidence,
                )
                metadata["hypothesis_evidence_links_applied"] = (
                    metadata.get("hypothesis_evidence_links_applied", 0) + 1
                )

        # 5. Solutions
        if hasattr(updates, "solutions_to_add") and updates.solutions_to_add:
            for s_item in updates.solutions_to_add:
                sol = Solution(
                    solution_id=f"sol_{uuid4().hex[:12]}",
                    solution_type=s_item.solution_type,
                    title=f"Solution: {s_item.solution_type}",
                    immediate_action=s_item.description,
                    commands=s_item.commands or [],
                    risks=[s_item.risks] if s_item.risks else [],
                    proposed_at=datetime.now(UTC),
                )
                case.solutions.append(sol)
                metadata["solutions_proposed"].append(sol.solution_id)

                # Gap 0: Create ProposedAction for compliance detection chain
                action_type = _determine_action_type(case, s_item.solution_type)

                # 3C: Hypothesis gate — SOLUTION requires at least one hypothesis.
                # If no hypotheses exist, downgrade to DIAGNOSTIC to prevent
                # premature TREATMENT entry.
                if (
                    action_type == InvestigationActionType.SOLUTION
                    and not case.hypotheses
                ):
                    logger.warning(
                        f"Downgrading SOLUTION to DIAGNOSTIC for case {case.case_id}: "
                        f"no hypotheses exist yet"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC

                proposed_action = ProposedAction(
                    case_id=case.case_id,
                    action_type=action_type,
                    description=s_item.description,
                    commands=s_item.commands or [],
                    proposed_in_turn=case.current_turn,
                )
                case.proposed_actions.append(proposed_action)

                # 3F: Set solution_proposed programmatically when SOLUTION action created
                if action_type == InvestigationActionType.SOLUTION:
                    case.progress.solution_proposed = True

        # Bug #4: Evidence-Milestone Linking (Moved here to ensure evidence exists)
        if metadata["milestones_completed"] and metadata["evidence_added"]:
            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    ev.advances_milestones.extend(metadata["milestones_completed"])

        # Bug #8: Robust Turn Outcome Determination
        metadata["outcome"] = self._determine_turn_outcome(
            case, metadata, updates.outcome
        )

    # =========================================================================
    # State Management
    # =========================================================================

    async def _transition_to_investigating(self, case: Case) -> None:
        """
        Transition case from INQUIRY to INVESTIGATING.

        This creates the initial investigation structures and copies the
        confirmed problem statement to the case description.

        Evidence lifecycle:
            - File uploads already created Evidence(contextual) at upload time.
            - LLM may have created categorized evidence via evidence_to_add during INQUIRY.
            - At transition, milestones are retroactively attributed based on evidence
              categories. Contextual evidence naturally gets [] (no milestone mapping).
            - Manual flow (only contextual evidence) → 0 milestones (natural consequence).
            - Natural flow (LLM-categorized evidence may exist) → milestones from categories.
        """
        logger.info(f"Transitioning case {case.case_id} to INVESTIGATING")

        # Gap #6: Checkpoint before status change
        if self.checkpoint_service:
            await self.checkpoint_service.create_checkpoint(
                case,
                trigger="pre_case_action",
                metadata={
                    "from_status": case.status.value,
                    "to_status": "investigating",
                },
            )

        # Copy confirmed problem statement to description BEFORE changing status
        # (Pydantic validation requires description to be set before INVESTIGATING status)
        if case.inquiry.proposed_problem_statement:
            case.description = case.inquiry.proposed_problem_statement
        elif not case.description:
            # Manual flow: user may transition before agent proposes a statement.
            # Use case title as fallback to satisfy Pydantic validation.
            case.description = case.title or "Investigation requested by user"

        # Change status (Pydantic validation happens here)
        case.status = CaseStatus.INVESTIGATING

        # Initialize investigation progress
        case.progress = InvestigationProgress()

        # Initialize problem verification with confirmed statement
        verification_kwargs = {
            "symptom_statement": case.description or "Unspecified issue",
            "severity": "MEDIUM",  # Default when unknown (valid value: CRITICAL|HIGH|MEDIUM|LOW)
        }

        # Hydrate from problem confirmation if available
        if case.inquiry.problem_confirmation:
            pc = case.inquiry.problem_confirmation
            if pc.severity_guess.upper() in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                verification_kwargs["severity"] = pc.severity_guess.upper()
            # else: keep default "MEDIUM" — severity_guess="unknown" is valid
            # for ProblemConfirmation but not for ProblemVerification

        # Hydrate from preliminary urgency if available
        if case.inquiry.preliminary_urgency:
            pu = case.inquiry.preliminary_urgency
            if pu.level:
                verification_kwargs["urgency_level"] = (
                    pu.level.lower()
                )  # Convert to lowercase for enum
                # If severity still at default (MEDIUM), use urgency level as severity (keep uppercase for severity)
                if (
                    verification_kwargs["severity"] == "MEDIUM"
                    and pu.level != UrgencyLevel.UNKNOWN
                ):
                    verification_kwargs["severity"] = (
                        pu.level.value.upper()
                    )  # Convert urgency level to uppercase for severity field
            # Bug fix: Transfer temporal_state from preliminary urgency
            # Without this, path selection receives Temporal:None and falls
            # back to USER_CHOICE even when the urgency signals are clear.
            if pu.is_ongoing:
                verification_kwargs["temporal_state"] = TemporalState.ONGOING
            else:
                verification_kwargs["temporal_state"] = TemporalState.HISTORICAL

        case.problem_verification = ProblemVerification(**verification_kwargs)

        # Determine path selection
        case.path_selection = determine_investigation_path(case.problem_verification)
        logger.info(
            f"Selected investigation path: {case.path_selection.path} (reason: {case.path_selection.rationale})"
        )

        # Gap #4: Retroactively attribute milestones to INQUIRY-phase evidence.
        # During INQUIRY, evidence was created with advances_milestones=[] because
        # milestone tracking wasn't active yet. Now that we've initialized progress,
        # we can infer milestone attribution based on evidence categories.
        # Contextual evidence naturally gets [] from _infer_milestones() because
        # CATEGORY_MILESTONE_MAP[CONTEXTUAL_EVIDENCE] = [].
        if case.evidence:
            # Check which milestones are already satisfied from the transition itself
            initial_milestones = []
            if case.progress.verification_complete:
                initial_milestones.append("symptom_verified")
            if case.progress.scope_assessed:
                initial_milestones.append("scope_assessed")

            for ev in case.evidence:
                if not ev.advances_milestones:
                    inferred = _infer_milestones(ev.category, initial_milestones)
                    if inferred:
                        ev.advances_milestones = inferred
                        logger.info(
                            f"Gap #4: Retroactively attributed milestones {inferred} "
                            f"to INQUIRY evidence {ev.evidence_id} "
                            f"(category={ev.category.value})"
                        )

    async def _check_automatic_transitions(
        self, case: Case, metadata: dict[str, Any], user_message: str = ""
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions (non-terminal):
        - INQUIRY -> INVESTIGATING when decided_to_investigate=True
        - INQUIRY -> RESOLVED for fast-track KB resolution (user already confirmed)

        User-Agent Handshake Transitions (terminal):
        - INVESTIGATING -> RESOLVED requires ProposedTransition + user confirmation
        - Any -> CLOSED requires explicit user action

        ProposedTransition handling:
        - If the LLM response includes a proposed_transition, store it as pending
        - The transition is NOT executed until the user confirms in the next turn
        - If a pending_transition exists and user confirms, execute it
        """
        old_status = case.status

        # 0. Handle pending transition confirmation from previous turn
        # Skip confirmation check if we just proposed a transition this turn (User-Agent Handshake)
        if hasattr(case, "pending_transition") and case.pending_transition:
            # Don't confirm a transition that was just proposed in this same turn
            if metadata.get("transition_proposed_this_turn", False):
                logger.info(
                    f"Skipping confirmation check - transition was just proposed this turn"
                )
            elif case.pending_transition.get("needs_info"):
                # User was told what's missing and has now responded.
                # Don't re-evaluate readiness (it checks formal objects the LLM
                # may not have populated yet). Trust the user — clear needs_info
                # and present the confirmation prompt with enrichment hints.
                case.pending_transition["needs_info"] = False
                metadata["resolution_ready_for_confirmation"] = True
                logger.info(
                    f"Case {case.case_id}: needs_info turn received, "
                    f"presenting confirmation prompt"
                )
            else:
                from faultmaven.core.investigation.terminal_transitions import (
                    cancel_pending_transition,
                    confirm_pending_transition,
                )

                # Use the user_message parameter directly, not from metadata
                if self._user_confirms_transition(user_message):
                    # Gap #6: Checkpoint before terminal transition
                    if self.checkpoint_service:
                        to_status = case.pending_transition.get("to_status", "unknown")
                        await self.checkpoint_service.create_checkpoint(
                            case,
                            trigger="pre_case_action",
                            metadata={
                                "from_status": case.status.value,
                                "to_status": to_status,
                            },
                        )
                    confirm_pending_transition(case, case.user_id)
                    metadata["status_transitioned"] = True
                    return case
                elif self._user_declines_transition(user_message):
                    cancel_pending_transition(case)
                    # Continue normal processing
                # else: user said something ambiguous, let LLM handle it

        # 1. INQUIRY transitions
        if case.status == CaseStatus.INQUIRY:
            # Check for fast-track resolution via KB
            if self._check_fast_track_resolution(case):
                # Status changed in _check_fast_track_resolution
                metadata["status_transitioned"] = True
                case.action_history.append(
                    CaseAction(
                        from_status=old_status,
                        to_status=CaseStatus.RESOLVED,
                        triggered_by="system",
                        reason="Fast-track resolution via KB match",
                    )
                )
                return case

            # Check for transition to investigation
            if case.inquiry.decided_to_investigate or (
                case.inquiry.problem_statement_confirmed
                and case.inquiry.problem_confirmation
            ):
                await self._transition_to_investigating(case)
                metadata["status_transitioned"] = True
                case.action_history.append(
                    CaseAction(
                        from_status=old_status,
                        to_status=CaseStatus.INVESTIGATING,
                        triggered_by="system",
                        reason="Problem confirmed and investigation triggered",
                    )
                )
                return case

        # 2. Handle ProposedTransition from LLM response (User-Agent Handshake)
        # The LLM proposes a terminal transition; we store it pending.
        # Auto-transition on solution_verified is REMOVED — all terminal
        # transitions require explicit user confirmation.
        response_obj = metadata.get("response_obj")
        if response_obj and hasattr(response_obj, "state_updates"):
            proposed = getattr(response_obj.state_updates, "proposed_transition", None)
            if proposed:
                from faultmaven.core.investigation.terminal_transitions import (
                    propose_transition,
                )

                propose_transition(
                    case=case,
                    to_status=proposed.to_status,
                    reason=proposed.reason,
                    summary=proposed.summary,
                    evidence_ids=proposed.evidence_ids,
                )
                metadata["transition_proposed"] = True
                logger.info(
                    f"Agent proposed transition → {proposed.to_status} "
                    f"(pending user confirmation)"
                )

        return case

    def _user_confirms_transition(self, user_message: str) -> bool:
        """Check if user message confirms a pending transition."""
        if not user_message:
            return False
        msg = user_message.strip().lower()
        confirm_patterns = [
            "yes",
            "yeah",
            "yep",
            "correct",
            "confirmed",
            "approve",
            "mark as resolved",
            "close it",
            "that's right",
            "sounds good",
            "looks good",
        ]
        return any(msg.startswith(p) or msg == p for p in confirm_patterns)

    def _user_declines_transition(self, user_message: str) -> bool:
        """Check if user message declines a pending transition."""
        if not user_message:
            return False
        msg = user_message.strip().lower()
        decline_patterns = [
            "no",
            "nope",
            "not yet",
            "wait",
            "cancel",
            "don't",
            "not ready",
            "hold on",
            "stop",
        ]
        return any(msg.startswith(p) or msg == p for p in decline_patterns)

    # Documented >70% confidence threshold for KB fast-track resolution
    # References: opportunistic-investigation-framework.md line 111,
    #             investigation-lifecycle-logic.md line 277,
    #             templates.py line 50
    KB_FAST_TRACK_THRESHOLD = 0.7

    def _check_fast_track_resolution(self, case: Case) -> bool:
        """Check if case can be Fast-Track resolved via KB match.

        Fast-track resolution uses closure_reason="resolved" (same as normal resolution)
        because a solution WAS found (via knowledge base). The distinction between
        fast-track and normal resolution is captured in case.inquiry.knowledge_resolution
        and status transition history.

        Gap #5b: Validates that a stored KB match meets the >70% confidence threshold
        before allowing fast-track resolution. If no stored matches exist (edge case),
        the resolution proceeds with a warning log.
        """
        if case.inquiry.knowledge_resolution:
            # Gap #5b: Validate confidence threshold against stored matches
            resolution = case.inquiry.knowledge_resolution
            best_match = max(
                case.inquiry.knowledge_matches,
                key=lambda m: m.relevance_score,
                default=None,
            )

            if best_match and best_match.relevance_score < self.KB_FAST_TRACK_THRESHOLD:
                logger.warning(
                    f"KB fast-track blocked for case {case.case_id}: "
                    f"best match confidence {best_match.relevance_score:.2f} "
                    f"< threshold {self.KB_FAST_TRACK_THRESHOLD}. "
                    f"match_id={resolution.match_id}",
                    extra={
                        "case_id": case.case_id,
                        "match_confidence": best_match.relevance_score,
                        "threshold": self.KB_FAST_TRACK_THRESHOLD,
                        "metric": "kb.fast_track_blocked",
                    },
                )
                return False

            if not best_match:
                # Edge case: resolution without stored match (shouldn't happen with 5a fix,
                # but could occur if knowledge_match wasn't in the LLM response)
                logger.warning(
                    f"KB fast-track for case {case.case_id} proceeding without stored match "
                    f"confidence validation. match_id={resolution.match_id}",
                    extra={
                        "case_id": case.case_id,
                        "metric": "kb.fast_track_no_match",
                    },
                )

            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(UTC)
            case.closed_at = datetime.now(UTC)
            case.closure_reason = "resolved"  # KB match = solution found = resolved
            case.progress.solution_verified = True

            # Log transition
            match_confidence = (
                f", confidence={best_match.relevance_score:.2f}" if best_match else ""
            )
            logger.info(
                f"Case {case.case_id} Fast-Track resolved via KB match: "
                f"{case.inquiry.knowledge_resolution.match_id}{match_confidence}"
            )
            return True
        return False

    def _determine_turn_outcome(
        self, case: Case, metadata: dict[str, Any], reported_outcome: TurnOutcome
    ) -> TurnOutcome:
        """
        Determine turn outcome classification (Bug #8).
        Checked AFTER milestone detection and evidence processing.
        """
        from faultmaven.core.investigation.turn_outcome import determine_turn_outcome

        return determine_turn_outcome(
            case=case,
            progress_made=metadata.get("progress_made", False),
            milestones_completed=metadata.get("milestones_completed", []),
            evidence_added=metadata.get("evidence_added", []),
            hypotheses_generated=len(metadata.get("hypotheses_generated", [])),
            solutions_proposed=len(metadata.get("solutions_proposed", [])),
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _create_uploaded_file_from_attachment(
        self, case: Case, attachment: dict[str, Any], turn_number: int
    ) -> "UploadedFile":  # noqa: F821
        """
        Create uploaded file record from attachment.

        Args:
            case: Current case
            attachment: Attachment metadata with file_id, filename, data_type, etc.
            turn_number: Current turn number

        Returns:
            UploadedFile object
        """
        from faultmaven.modules.case.contracts import UploadedFile

        uploaded_file = UploadedFile(
            file_id=attachment.get("file_id", f"file_{uuid4().hex[:12]}"),
            filename=attachment.get("filename", "unknown"),
            size_bytes=attachment.get("size", 0),
            data_type=attachment.get("data_type", "unknown"),
            uploaded_at_turn=turn_number,
            uploaded_at=datetime.now(UTC),
            source_type=attachment.get("source_type", "file_upload"),
            preprocessing_summary=attachment.get("summary", None),
            content_ref=attachment.get("s3_uri", attachment.get("file_id", "unknown")),
        )

        return uploaded_file

    def _create_evidence_from_attachment(
        self, case: Case, attachment: dict[str, Any], turn_number: int
    ) -> Evidence:
        """
        Create evidence object from file attachment.

        Args:
            case: Current case
            attachment: Attachment metadata
            turn_number: Current turn number

        Returns:
            Evidence object
        """
        # Infer category based on investigation state
        category = self._infer_evidence_category(case)

        # Create evidence
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            summary=f"Uploaded file: {attachment.get('filename', 'unknown')}",
            preprocessed_content="[Content to be preprocessed]",  # Placeholder
            content_ref=attachment.get("s3_uri", "unknown"),
            content_size_bytes=attachment.get("size", 0),
            preprocessing_method="pending",
            category=category,
            source_type=EvidenceSourceType.LOGS,  # Default (simplified from LOG_FILE)
            form=EvidenceForm.DOCUMENT,
            source_file_id=attachment.get("file_id"),
            advances_milestones=[],  # Calculated later
            collected_at=datetime.now(UTC),
            collected_by=case.user_id,
            collected_at_turn=turn_number,
            primary_purpose="File Analysis",  # Mandatory
        )

        return evidence

    def _infer_evidence_category(self, case: Case) -> EvidenceCategory:
        """
        Infer evidence category from investigation stage.

        Rules:
        - MITIGATION stage → MITIGATION_EVIDENCE
        - TREATMENT stage → SOLUTION_EVIDENCE
        - DIAGNOSIS stage, verification incomplete → SYMPTOM_EVIDENCE
        - DIAGNOSIS stage, otherwise → CAUSAL_EVIDENCE
        """
        stage = case.progress.current_stage

        if stage == InvestigationStage.MITIGATION:
            return EvidenceCategory.MITIGATION_EVIDENCE

        if stage == InvestigationStage.TREATMENT:
            return EvidenceCategory.SOLUTION_EVIDENCE

        if not case.progress.verification_complete:
            return EvidenceCategory.SYMPTOM_EVIDENCE

        return EvidenceCategory.CAUSAL_EVIDENCE

    def _create_turn_record(
        self,
        turn_number: int,
        milestones_completed: list[str],
        evidence_added: list[str],
        hypotheses_generated: list[str],
        hypotheses_validated: list[str],
        solutions_proposed: list[str],
        progress_made: bool,
        outcome: TurnOutcome,
        user_message: str,
        agent_response: str,
        system_feedback: str | None = None,
        momentum: InvestigationMomentum | None = None,
        blocked_reasons: list[str] | None = None,
        next_steps: list[str] | None = None,
        stagnation_detected: str | None = None,
        validation_repairs: list[str] | None = None,
    ) -> TurnProgress:
        """Create turn progress record."""
        return TurnProgress(
            turn_number=turn_number,
            timestamp=datetime.now(UTC),
            milestones_completed=milestones_completed,
            evidence_added=evidence_added,
            hypotheses_generated=hypotheses_generated,
            hypotheses_validated=hypotheses_validated,
            solutions_proposed=solutions_proposed,
            progress_made=progress_made,
            actions_taken=self._extract_actions(agent_response),
            outcome=outcome,
            user_message_summary=self._summarize_text(user_message, 200),
            agent_response_summary=self._summarize_text(agent_response, 500),
            system_feedback=system_feedback,
            momentum=momentum,
            blocked_reasons=blocked_reasons or [],
            next_steps=next_steps or [],
            stagnation_detected=stagnation_detected,
            validation_repairs=validation_repairs or [],
        )

    def _record_deterministic_turn(
        self,
        case: Case,
        user_message: str,
        agent_response: str,
    ) -> None:
        """Record a minimal TurnProgress for deterministic early-return paths.

        Deterministic paths (dropdown confirmations, closure summaries, etc.)
        skip the full LLM pipeline but still consume a turn number. Without
        recording a TurnProgress entry, the turn_history validator rejects
        the case on the next load due to non-sequential turn numbers.
        """
        case.turn_history.append(
            TurnProgress(
                turn_number=case.current_turn,
                timestamp=datetime.now(UTC),
                milestones_completed=[],
                evidence_added=[],
                hypotheses_generated=[],
                hypotheses_validated=[],
                solutions_proposed=[],
                progress_made=False,
                actions_taken=[],
                outcome=TurnOutcome.CONVERSATION,
                user_message_summary=self._summarize_text(user_message, 200),
                agent_response_summary=self._summarize_text(agent_response, 500),
            )
        )

    def _extract_actions(self, agent_response: str) -> list[str]:
        """Extract action keywords from agent response."""
        action_keywords = [
            "verified",
            "identified",
            "proposed",
            "tested",
            "confirmed",
            "analyzed",
        ]
        actions = []

        response_lower = agent_response.lower()
        for keyword in action_keywords:
            if keyword in response_lower:
                actions.append(keyword)

        return actions[:5]  # Limit to 5

    def _check_if_progress_made(self, metadata: dict[str, Any]) -> bool:
        """Check if any meaningful investigative activity occurred.

        Progress includes structural artifacts (milestones, evidence, hypotheses)
        AND active investigative behaviors (requesting data, testing hypotheses,
        linking evidence). A skilled troubleshooter gathering information IS
        making progress.
        """
        # Structural progress: new artifacts created or status changed
        structural_keys = [
            "milestones_completed",
            "evidence_added",
            "hypotheses_generated",
            "hypotheses_validated",
            "solutions_proposed",
            "files_uploaded",
        ]
        for key in structural_keys:
            if metadata.get(key):
                return True

        if metadata.get("status_transitioned"):
            return True

        # Investigative progress: active diagnostic behaviors
        outcome = metadata.get("outcome")
        if outcome in (
            TurnOutcome.DATA_REQUESTED,
            TurnOutcome.HYPOTHESIS_TESTED,
            TurnOutcome.DATA_PROVIDED,
        ):
            return True

        # Hypothesis-evidence linking counts as progress
        if metadata.get("hypothesis_evidence_links_applied"):
            return True

        return False

    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Summarize long text for storage."""
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    # =============================================================================
    # Phase 4 Housekeeping & Helpers
    # =============================================================================

    def _perform_hypothesis_housekeeping(
        self, case: Case, metadata: dict[str, Any]
    ) -> None:
        """Apply confidence decay and anchoring detection."""
        active_hypotheses = [
            h for h in case.hypotheses.values() if h.status == HypothesisStatus.ACTIVE
        ]

        if not active_hypotheses:
            return

        # 1. Apply confidence decay to stagnant hypotheses
        for h in active_hypotheses:
            # We decay if NO progress was made this turn for this specific hypothesis
            # (Note: link_evidence resets iterations_without_progress to 0)
            self.hypothesis_manager.apply_likelihood_decay(h, case.current_turn)

        # 2. Detect anchoring and add system feedback if necessary
        is_anchored, reason, hypothesis_ids = self.hypothesis_manager.detect_anchoring(
            active_hypotheses, case.current_turn
        )

        if is_anchored:
            logger.warning(f"Anchoring detected for case {case.case_id}: {reason}")
            # Add to system feedback for next turn
            anchoring_msg = f"CRITICAL: {reason}. You are stalled on these theories. Diversify your approach and generate alternative hypotheses from different categories."

            current_feedback = metadata.get("system_feedback", "")
            metadata["system_feedback"] = (
                (current_feedback + "\n" + anchoring_msg)
                if current_feedback
                else anchoring_msg
            )

    def _resolve_id_ref(self, ref: str, created_ids: list[str], prefix: str) -> str:
        """Resolve 'new_index_N' to actual ID from created_ids list or return ref as-is."""
        if ref and ref.startswith("new_index_"):
            try:
                idx_str = ref.replace("new_index_", "")
                idx = int(idx_str)
                if 0 <= idx < len(created_ids):
                    return created_ids[idx]
            except (ValueError, IndexError):
                pass
        return ref


# =============================================================================
# Exceptions
# =============================================================================


class MilestoneEngineError(Exception):
    """Base exception for milestone engine errors."""

    pass
