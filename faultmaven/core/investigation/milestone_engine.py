"""Data-Driven and Opportunistic Investigation Engine

This module implements the data-driven investigation system: instead of
rigid phase orchestration, the engine completes milestones
opportunistically based on data availability.

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
from faultmaven.core.investigation.lifecycle_metrics import (
    engine_owned_affordance_served_total,
    inquiry_gate2_confirmed_total,
    inquiry_gate3_reached_total,
    inquiry_gate3_resolved_total,
    inquiry_handshake_deferred_total,
    inquiry_handshake_recovered_total,
)
from faultmaven.core.investigation.llm_error_handler import ErrorAction, LLMErrorHandler
from faultmaven.core.investigation.progress_monitor import (
    ProgressMonitor,
)
from faultmaven.core.investigation.prompts.templates import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
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
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisStatus,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationPath,
    InvestigationProgress,
    InvestigationStage,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
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
from faultmaven.modules.case.exceptions import StaleCaseException
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
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
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
    # Baseline/environmental data lives on ``uploaded_files``, not Evidence;
    # Evidence rows are only created when the agent extracts a
    # claim-relevant slice.
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
        # Gate 3 boundary marker: capture the turn mitigation completed so
        # later post-mitigation RCA runs can identify the pre-mitigation
        # evidence window. Set ONCE (idempotent) — re-entry to MITIGATION
        # preserves the first-completion turn as the canonical boundary.
        # See INV-21 in investigation-lifecycle-logic.md and slice 3 of
        # docs/working/WIP-investigation-gates-implementation.md.
        if (
            case.path_selection is not None
            and case.path_selection.mitigation_completed_at_turn is None
        ):
            case.path_selection = case.path_selection.model_copy(
                update={"mitigation_completed_at_turn": case.current_turn}
            )
            logger.info(
                f"Case {case.case_id}: mitigation_completed_at_turn set to "
                f"{case.current_turn} (Gate 3 boundary)"
            )
            # Outcome telemetry: every case that reaches mitigation_verified
            # on the mitigation-first path passes through Gate 3.
            inquiry_gate3_reached_total.inc()

        # rca_infeasible advisory signal: propose closure as mitigated rather
        # than push RCA on a problem the LLM has flagged as intractable.
        # Reference: investigation-lifecycle-logic.md §2.4.
        rca_infeasible = case.problem_verification and getattr(
            case.problem_verification, "rca_infeasible", False
        )
        if rca_infeasible:
            rationale = (
                getattr(case.problem_verification, "rca_infeasible_rationale", None)
                or "root cause analysis is not feasible for this problem"
            )
            closure_message = (
                "The mitigation is verified and stable. "
                f"Since {rationale}, shall we close this case as mitigated?"
            )
            # Propose BEFORE the flag reset so derive_closure_reason reads
            # mitigation_verified=True and snapshots closure_reason as
            # "mitigation_sufficient" into pending_transition.
            from faultmaven.core.investigation.terminal_transitions import (
                propose_transition,
            )

            propose_transition(
                case=case,
                to_status="closed",
                summary=closure_message,
            )
            metadata["transition_proposed"] = True
            metadata["override_suggestions"] = _close_confirmation_suggestions()
            metadata["rca_infeasible_closure_message"] = closure_message
            logger.info(
                f"Proposed CLOSED transition for case {case.case_id} "
                f"(rca_infeasible=True, closure_reason=mitigation_sufficient, "
                f"rationale: {rationale})"
            )

        case.progress.mitigation_accepted = False
        case.progress.mitigation_verified = False

        if not rca_infeasible:
            logger.info(
                f"Reset mitigation flags for case {case.case_id} "
                f"(return to DIAGNOSIS for RCA)"
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
        category: The evidence category (SYMPTOM / CAUSAL / MITIGATION /
            SOLUTION)
        milestones_completed_this_turn: Milestones completed this turn from MilestoneUpdates

    Returns:
        List of milestone names this evidence contributed to

    Logic:
        1. Get eligible milestones for this category from CATEGORY_MILESTONE_MAP
        2. Intersect with milestones completed this turn (from MilestoneUpdates)
        3. Result = milestones this evidence can claim credit for

    Example:
        category = SYMPTOM_EVIDENCE
        milestones_completed_this_turn = ["symptom_verified"]
        eligible = ["symptom_verified"]
        result = ["symptom_verified"]

    Key Insight:
        With one-file-per-turn constraint (UI limitation), inference is UNAMBIGUOUS.
        There's only one evidence record per turn, so all eligible milestones completed
        that turn get attributed to it. No guessing needed.

    Note:
        - Post-010: 4 categories (SYMPTOM/CAUSAL/MITIGATION/SOLUTION).
          MITIGATION_EVIDENCE and SOLUTION_EVIDENCE map to [] —
          mitigation_verified / solution_verified are gate milestones
          set by compliance detection, not by evidence category.
        - If category not in map, returns [] (safe fallback).
        - LLM can override by explicitly setting advances_milestones in EvidenceToAdd.
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
    # Every evidence row is claim-anchored — any existing or to-add row counts.
    has_actionable_evidence = bool(case.evidence) or bool(evidence_being_added)

    if internal_reasoning.milestone_justifications and not has_actionable_evidence:
        errors.append(
            "Cannot complete milestones when no actionable evidence has been collected. "
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
        # Try fields in order of specificity. Skip titles that look like
        # raw enum references (e.g., "Solution: SolutionType.CONFIG_CHANGE")
        # which indicate the LLM wrote a placeholder instead of a description.
        title = getattr(sol, "title", None)
        if title and "SolutionType." not in title:
            return title[:200] + "..." if len(title) > 200 else title
        longterm = getattr(sol, "longterm_fix", None)
        if longterm:
            return longterm[:200] + "..." if len(longterm) > 200 else longterm
        immediate = getattr(sol, "immediate_action", None)
        if immediate:
            return immediate[:200] + "..." if len(immediate) > 200 else immediate
        # Last resort: return title even if it has enum reference
        if title:
            return title[:200] + "..." if len(title) > 200 else title
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
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not quite, let me clarify",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not quite — let me clarify the problem before we investigate.",
            "body": "Refine the problem statement before starting the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _path_selection_suggestions(case: "Case") -> list:
    """Generate COOPERATIVE follow-up suggestions for Gate 2 (path confirmation).

    Surfaces the router's recommended path alongside its alternate, both as
    PATH_SELECTION intents. The recommended option appears first and is
    decorated in its body text so the frontend can emphasize it; both options
    commit the path on click — the user can either accept the recommendation
    or override to the alternate.

    The "ask the user" semantic lives entirely in
    ``path_selection.user_confirmed=False`` — no third enum value, no
    separate ambiguity flag. This pair of suggestions is the user-facing
    surface that resolves that pending state. See INV-19.
    """
    ps = case.path_selection
    if ps is None or ps.alternate_path is None:
        # Defensive: caller should gate on these. Return empty so the engine
        # doesn't emit malformed suggestions if invariants are violated.
        return []

    recommended_label = (
        "Mitigation-first"
        if ps.path == InvestigationPath.MITIGATION_FIRST
        else "Root-cause analysis"
    )
    alternate_label = (
        "Mitigation-first"
        if ps.alternate_path == InvestigationPath.MITIGATION_FIRST
        else "Root-cause analysis"
    )
    recommended_body = (
        f"Recommended. {ps.rationale}"
        if ps.auto_selected
        else f"Default. {ps.rationale}"
    )
    alternate_body_descriptions = {
        InvestigationPath.MITIGATION_FIRST: (
            "Apply a quick workaround to stop the impact first, then return "
            "for root-cause analysis after the system is stable."
        ),
        InvestigationPath.ROOT_CAUSE: (
            "Skip mitigation and go straight to root-cause analysis for a "
            "permanent fix."
        ),
    }
    return [
        {
            "label": recommended_label,
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": f"Let's start with {recommended_label.lower()}.",
            "body": recommended_body,
            "intent": {
                "type": "path_selection",
                "investigation_path": ps.path.value,
            },
        },
        {
            "label": alternate_label,
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": f"I'd prefer {alternate_label.lower()}.",
            "body": alternate_body_descriptions[ps.alternate_path],
            "intent": {
                "type": "path_selection",
                "investigation_path": ps.alternate_path.value,
            },
        },
    ]


def _post_mitigation_suggestions() -> list:
    """Generate COOPERATIVE follow-up suggestions for Gate 3.

    Surfaces the two post-mitigation outcomes:
      1. Continue with root-cause analysis (POST_MITIGATION_CHOICE intent)
      2. Close the case as mitigation-sufficient (STATUS_TRANSITION intent
         to CLOSED with closure_reason=mitigation_sufficient — reuses
         the existing closure-summary path; the substance gate produces
         a coherent summary or marks the report as skipped per
         closure_summary_redesign).

    The close-branch body text mentions the runbook implication so the
    user understands the trade-off at the click moment: closing as
    mitigation-sufficient does NOT generate a root-cause runbook (only
    RESOLVED cases do, per INV-18).

    See INV-21 in investigation-lifecycle-logic.md.
    """
    return [
        {
            "label": "Continue with root-cause analysis",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Mitigation worked — let's continue with root-cause analysis.",
            "body": (
                "Recommended. The system is stable but the underlying cause "
                "is still unknown — RCA produces a permanent fix and a "
                "runbook for next time."
            ),
            "intent": {
                "type": "post_mitigation_choice",
                "continue_to_rca": True,
            },
        },
        {
            "label": "Mitigation is sufficient, close case",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "The mitigation is sufficient — let's close this case.",
            "body": (
                "Close the case without further investigation. Note: no "
                "root-cause runbook will be generated — only a mitigation "
                "summary."
            ),
            "intent": {
                "type": "status_transition",
                "to_status": "closed",
                "closure_reason": "mitigation_sufficient",
                "user_confirmed": True,
            },
        },
    ]


def _gate3_is_pending(case: "Case") -> bool:
    """Whether Gate 3 (post-mitigation continuation) is open for this case.

    Returns True when:
      - path == MITIGATION_FIRST
      - mitigation_completed_at_turn is set (mitigation was verified)
      - rca_after_mitigation_confirmed is False (user has not yet chosen)
      - case is still in INVESTIGATING (not yet closed)

    Used by both the deterministic suggestion-emission backstop and the
    INV-21 milestone guard. See investigation-lifecycle-logic.md INV-21.
    """
    if case.status != CaseStatus.INVESTIGATING:
        return False
    ps = case.path_selection
    if ps is None or ps.path != InvestigationPath.MITIGATION_FIRST:
        return False
    if ps.mitigation_completed_at_turn is None:
        return False
    return not ps.rca_after_mitigation_confirmed


def _gate1_is_pending(case: "Case") -> bool:
    """Whether Gate 1 (problem-statement confirmation) is open for this case.

    Returns True when the LLM has proposed a problem statement and the user
    has not yet confirmed it. Subsumes the prior handshake-deferred-recovery
    condition: the same affordance pair is appropriate on every Gate-1-pending
    turn, not only on the recovery turn after the same-turn guard fires.

    Used by ``engine_owned_affordances`` so the engine emits the canonical
    confirmation pair deterministically regardless of LLM compliance with the
    INQUIRY prompt's confirmation-suggestion enumeration. Matches the pattern
    already established for Gate 2 and Gate 3.
    """
    if case.status != CaseStatus.INQUIRY:
        return False
    inq = case.inquiry
    if inq is None:
        return False
    if not inq.proposed_problem_statement:
        return False
    return not inq.problem_statement_confirmed


def _gate2_is_pending(case: "Case") -> bool:
    """Whether Gate 2 (investigation-path selection) is open for this case.

    Returns True when Gate 1 has closed (problem_statement_confirmed=True)
    and the router has populated path_selection but the user has not yet
    accepted or overridden the recommendation. See INV-19.
    """
    if case.status != CaseStatus.INQUIRY:
        return False
    inq = case.inquiry
    if inq is None or not inq.problem_statement_confirmed:
        return False
    ps = case.path_selection
    if ps is None:
        return False
    return not ps.user_confirmed


def engine_owned_affordances(
    case: "Case", metadata: Optional[dict[str, Any]] = None
) -> Optional[tuple[str, list]]:
    """Return ``(gate_name, affordance_list)`` when a state-machine gate is pending.

    The state machine has a small enumerable set of gates: imperative
    pending_transition (set by ``propose_transition`` via
    ``metadata['override_suggestions']``), Gate 3 (post-mitigation
    continuation), Gate 2 (investigation path), Gate 1 (problem-statement
    confirmation). When any gate is pending, the engine knows the canonical
    affordance pair; the LLM cannot add value there and shouldn't try.

    Returns ``None`` when no gate is pending — the LLM's own COOPERATIVE /
    EVIDENCE / FREE_SPEECH suggestions pass through unmodified.

    Gate identifiers (telemetry-stable labels):
      - ``"disposition"`` — pending_transition / propose_transition override
      - ``"gate3"`` — post-mitigation continuation
      - ``"gate2"`` — investigation-path selection
      - ``"gate1"`` — problem-statement confirmation

    Priority order matches the identifier order above. Gates 1/2/3 are
    mutually exclusive by case-state construction (each depends on a
    different combination of inquiry/path_selection flags), so the ordering
    between them is defensive rather than load-bearing. The disposition
    branch sits above the gates because pending_transition can fire while a
    gate is technically open (e.g., user proposes closing during Gate 2).
    """
    md = metadata or {}

    if md.get("override_suggestions"):
        return ("disposition", md["override_suggestions"])

    if _gate3_is_pending(case) and not md.get("rca_infeasible_closure_message"):
        return ("gate3", _post_mitigation_suggestions())

    if _gate2_is_pending(case):
        return ("gate2", _path_selection_suggestions(case))

    if _gate1_is_pending(case):
        return ("gate1", _investigation_confirmation_suggestions())

    return None


def _compute_inquiry_path_selection(case: "Case") -> None:
    """Compute case.path_selection from inquiry signals if Gate 1 has passed.

    Called from _apply_inquiry_updates whenever the LLM emits or revises
    preliminary_urgency. Idempotent — only fires when:
      - Gate 1 passed (inquiry.problem_statement_confirmed = True)
      - preliminary_urgency is populated (level + is_ongoing available)
      - path_selection has not yet been computed (None)

    The mutation watcher in _apply_inquiry_updates handles the
    revise-after-Gate-2-passed case (INV-20) by clearing path_selection
    before this function is called, so an idempotency guard on path_selection
    being None is sufficient here.
    """
    if not case.inquiry or not case.inquiry.problem_statement_confirmed:
        return
    if case.path_selection is not None:
        return
    pu = case.inquiry.preliminary_urgency
    if pu is None or pu.level is None:
        return

    temporal = TemporalState.ONGOING if pu.is_ongoing else TemporalState.HISTORICAL
    severity = pu.level.value.upper() if pu.level != UrgencyLevel.UNKNOWN else "MEDIUM"

    # Build a transient ProblemVerification for the router. The case-level
    # case.problem_verification is populated lazily in _transition_to_investigating;
    # constructing one here avoids prematurely materializing case state that the
    # transition path owns.
    verification = ProblemVerification(
        symptom_statement=case.description
        or case.inquiry.proposed_problem_statement
        or "Unspecified issue",
        severity=severity,
        temporal_state=temporal,
        urgency_level=pu.level,
    )
    case.path_selection = determine_investigation_path(verification)
    logger.info(
        f"Computed path recommendation for case {case.case_id}: "
        f"path={case.path_selection.path.value}, "
        f"auto_selected={case.path_selection.auto_selected}, "
        f"rationale={case.path_selection.rationale}"
    )


def _inquiry_path_signals_changed(old_pu: Optional[Any], new_pu: Optional[Any]) -> bool:
    """Detect whether the inputs that drive path selection have changed.

    INV-20 mutation watcher: when ``preliminary_urgency.level`` or
    ``preliminary_urgency.is_ongoing`` differ between turns, the path
    recommendation may no longer match — clear the existing path_selection
    so it gets re-computed on the next turn and Gate 2 re-fires.

    Mutations to other fields (impact_assessment, etc.) do not invalidate
    Gate 2 — the router only consumes level + temporal_state.
    """
    if old_pu is None and new_pu is None:
        return False
    if old_pu is None or new_pu is None:
        return True
    return getattr(old_pu, "level", None) != getattr(new_pu, "level", None) or getattr(
        old_pu, "is_ongoing", None
    ) != getattr(new_pu, "is_ongoing", None)


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

    Each suggestion carries an ``intent`` dict so the frontend can send the
    click as IntentType.CONFIRMATION instead of plain text. This routes
    through the deterministic _handle_confirmation() path, bypassing the
    tool loop and pattern matching entirely.
    """
    return [
        {
            "label": "Yes, mark as resolved",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Yes, the issue is resolved. Please mark this case as resolved.",
            "body": "Confirm resolution and close the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not yet — I'd like to continue investigating before resolving.",
            "body": "Decline resolution and continue refining the root cause or exploring alternative solutions.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _close_confirmation_suggestions() -> list:
    """Generate COOPERATIVE follow-up suggestions for close (abandon) confirmation.

    Mirrors the INQUIRY and RESOLVED confirmation patterns: one positive
    (confirm close) and one mild negative (continue investigating).

    Note: the confirmation prompt is purely about the irreversibility of
    closing. The summary is a downstream Dashboard artifact; mentioning it
    here would either promise unconditionally (sometimes false, when the
    substance gate skips) or muddy the decision the user is being asked to
    make. The body text deliberately stays silent about the report.
    """
    return [
        {
            "label": "Yes, close this case",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Yes, close this case without resolution.",
            "body": "Confirm closing the case. Closing is irreversible — the case becomes read-only.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": "Not yet — I'd like to continue investigating.",
            "body": "Keep the investigation open and continue working toward a solution.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _terminal_confirmation_response(case) -> str:
    """Deterministic status line after a transition is confirmed.

    Closure-reason-aware so the user can tell at a glance what was preserved.
    The terminal reply is composed by ``_compose_terminal_reply`` which
    appends the auto-generated summary content (when produced).
    """
    if case.status == CaseStatus.RESOLVED:
        return "Case resolved."

    closure_reason = getattr(case, "closure_reason", "") or ""
    if closure_reason == "inquiry_only":
        return "Case closed without investigation."
    if closure_reason == "closed_after_investigation":
        return "Case closed without resolution. Investigation history preserved."
    if closure_reason == "mitigation_sufficient":
        return "Case closed; mitigation deemed sufficient."
    return "Case closed."


def _compose_terminal_reply(case, summary_payload: str | None) -> str:
    """Compose the closure-turn chat reply for the *deterministic* paths.

    Used by the two paths where the engine controls the reply text
    directly: the explicit confirm-button path and the dropdown-resolution
    path. Prepends a deterministic status line (e.g. "Case closed.") and
    appends the auto-generated summary content (or skip / failure note).

    Not used by the LLM-driven transition path (end of process_turn), where
    the LLM has already produced narrative text for the turn — that path
    appends ``summary_payload`` directly to the LLM's text. The end-state
    chat content is equivalent (status line / LLM narrative, then the
    summary inline) but the composition site differs.

    ``summary_payload`` may be:
      - The rendered summary markdown (gate PASS, generation succeeded).
      - A skip note (gate FAIL — low-substance closure).
      - A failure note (gate PASS, LLM error).
      - None (no report service configured — stays silent).
    """
    status_line = _terminal_confirmation_response(case)
    if not summary_payload:
        return status_line
    return f"{status_line}\n\n{summary_payload}"


REGENERATE_RESOLUTION_SUMMARY_PAYLOAD = (
    "Regenerate the resolution summary report for this case"
)

REGENERATE_CLOSURE_SUMMARY_PAYLOAD = (
    "Regenerate the closure summary report for this case"
)

GENERATE_RUNBOOK_PAYLOAD = "Generate a runbook from this resolved case"


def _runbook_suggestion() -> dict:
    """The runbook-generation COOPERATIVE suggestion (RESOLVED-only)."""
    return {
        "label": "Generate runbook from this case",
        "action_type": "COOPERATIVE",
        "cooperative_action": "query_submit",
        "payload": GENERATE_RUNBOOK_PAYLOAD,
        "body": "Create a reusable troubleshooting runbook from the root cause and solution.",
    }


def _regenerate_resolution_summary_suggestion() -> dict:
    return {
        "label": "Regenerate resolution summary",
        "action_type": "COOPERATIVE",
        "cooperative_action": "query_submit",
        "payload": REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
        "body": "Re-create the resolution report.",
    }


def _resolved_ack_suggestions() -> list:
    """Suggestions for the resolution-acknowledgment turn.

    The summary was just generated and is rendered inline above in this
    same agent reply — offering "Regenerate" beside it would be noise.
    Only the forward action (runbook) is offered here. Regen is reserved
    for subsequent terminal Q&A turns via ``_resolved_suggestions``.
    """
    return [_runbook_suggestion()]


def _select_ack_follow_ups(case, summary_failed: bool) -> list:
    """Choose follow-up suggestions for the closure-acknowledgment turn.

    Success path: minimal suggestions per ``_resolved_ack_suggestions`` /
    ``[]`` for CLOSED — the summary is rendered inline, so a regen card
    next to it would be noise.

    Failure path (G2): include the standard terminal Q&A suggestions —
    ``_resolved_suggestions`` (regen + runbook) for RESOLVED, or
    ``_closed_suggestions`` (regen when substance gate passes) for CLOSED.
    Generation reaches the failure branch only when generation was
    attempted (so the substance gate has already PASSED for CLOSED),
    which means ``_closed_suggestions`` will return a non-empty list with
    the regen affordance. The "noise next to inline summary" rationale
    doesn't apply when there's no inline summary — only a failure note.
    """
    if summary_failed:
        if case.status == CaseStatus.RESOLVED:
            return _resolved_suggestions()
        if case.status == CaseStatus.CLOSED:
            return _closed_suggestions(case)
        return []
    if case.status == CaseStatus.RESOLVED:
        return _resolved_ack_suggestions()
    return []


def _resolved_suggestions() -> list:
    """Suggestions for terminal Q&A turns on a RESOLVED case.

    Both the regen affordance and the runbook affordance are offered.
    The regen path serves as the chat-side recovery if initial generation
    failed and as a way to iterate; the runbook path is the forward
    action. Symmetric with ``_closed_suggestions`` for CLOSED cases.
    """
    return [
        _regenerate_resolution_summary_suggestion(),
        _runbook_suggestion(),
    ]


def _closed_suggestions(case) -> list:
    """Suggestions offered on terminal Q&A turns for a CLOSED case.

    Returned only on subsequent terminal Q&A turns — NOT on the
    closure-acknowledgment turn itself (that turn's reply renders the
    summary inline; offering "Regenerate" beside the freshly-generated
    summary is noise). Callers must respect that.

    The regenerate affordance is offered when the substance gate would
    PASS — independent of whether the Report row currently exists. This
    handles two cases with one rule: a successful initial generation (user
    wants a re-roll) and a failed initial generation (user wants to retry).
    For low-substance closures (gate FAIL), nothing is offered — there is
    nothing summarizable.

    Runbooks are intentionally not offered for CLOSED cases — they require
    a confirmed root cause + verified solution, which RESOLVED implies and
    CLOSED does not.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        should_generate_terminal_summary,
    )

    if not should_generate_terminal_summary(case):
        return []
    return [
        {
            "label": "Regenerate closure summary",
            "action_type": "COOPERATIVE",
            "cooperative_action": "query_submit",
            "payload": REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
            "body": "Re-create the closure report. View the current report in the Dashboard.",
        },
    ]


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


class MilestoneEngine:
    """
    Data-Driven and Opportunistic Investigation Engine.

    The agent completes milestones opportunistically based on available
    data, rather than following a rigid phase pipeline.

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
        self.da_provider = da_provider
        self.da_model = da_model
        self.sanitizer = sanitizer
        self.redis_client = redis_client
        self.report_service = report_service
        self.hypothesis_manager = create_hypothesis_manager()
        self.state_validator = StateValidator()
        self.progress_monitor = ProgressMonitor()
        self.llm_error_handler = LLMErrorHandler()

        # G10: Per-case asyncio locks to prevent concurrent process_turn
        # calls on the same case from interleaving and corrupting state
        self._case_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # In-flight proactive vectorization tasks, keyed by evidence_id.
        # MilestoneEngine is a DI singleton, so this dict survives across
        # turns. The persistent Evidence.vectorized flag covers the
        # "already completed" state; this dict covers the "currently
        # running" window between start and completion. Without it, turn
        # N+1 sees vectorized=False (still running) and starts a second
        # concurrent task for the same evidence — the stacking pattern
        # that drove every task past the 60s wait_for bound in the
        # 2026-04-21 test run.
        self._inflight_vectorize: dict[str, asyncio.Task] = {}

        logger.info("MilestoneEngine initialized with structured output engine")

    async def _auto_generate_report(self, case: "Case") -> tuple[str | None, bool]:
        """Synchronous auto-generation of terminal summary.

        RESOLVED cases always generate (a confirmed solution is meaningful
        content by definition). CLOSED cases generate only when the
        substance gate passes — gated by
        ``should_generate_terminal_summary``.

        Returns:
            A tuple ``(payload, generation_failed)``:

            - ``(rendered_markdown, False)`` on success — embed inline.
            - ``(failure_note, True)`` on LLM exception — embed inline AND
              offer the regen affordance on the ack-turn (G2).
            - ``(skip_note, False)`` when the substance gate skipped
              generation (CLOSED-only path).
            - ``(None, False)`` when no report service is configured.

        Callers embed ``payload`` in the closure-turn agent reply and use
        ``generation_failed`` to decide whether to offer the regen
        affordance on the ack-turn. Exceptions are caught and reported as
        a return value rather than propagated — the closure state
        transition has already committed and must not be undone by a
        synthesis-LLM hiccup.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )

        if case.status == CaseStatus.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case)
            logger.info(f"Auto-summary skipped for case {case.case_id}: {skip}")
            return skip, False

        if not self.report_service:
            logger.debug("No report service available — skipping auto-summary")
            return None, False

        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.status == CaseStatus.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution summary"
        elif case.status == CaseStatus.CLOSED:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure summary"
        else:
            logger.warning(
                f"Unexpected status {case.status} for auto-summary on case {case.case_id}"
            )
            return None, False

        try:
            reports = await self.report_service.generate_reports(case, [report_type])
            logger.info(
                f"Auto-generated {report_type.value} for case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
            # Pull the rendered markdown content from the freshly-generated
            # report so it can be embedded in the closure-turn reply.
            if reports:
                content = getattr(reports[0], "content", None) or getattr(
                    reports[0], "markdown_content", None
                )
                if content:
                    return content, False
            return None, False
        except Exception as e:
            logger.warning(
                f"Auto-summary generation failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            return (
                f"{report_label} generation did not complete. "
                f"You can retry from the **Regenerate** option.",
                True,
            )

    # Only the precomposed payloads submitted by the COOPERATIVE regen
    # suggestions reach this set. Free-typed summary-shaped requests
    # (e.g. "give me a recap", "summarize what we discussed") fall through
    # to terminal Q&A on purpose: typing should never produce a persisted
    # Report side effect. The Q&A prompt is instructed to redirect those
    # asks to the existing summary + regen affordance.
    _REPORT_REGEN_PATTERNS = (
        "regenerate the closure summary report for this case",
        "regenerate the resolution summary report for this case",
    )

    # Same exact-match policy as _REPORT_REGEN_PATTERNS: only the
    # precomposed COOPERATIVE-suggestion payload reaches the runbook
    # creation path. Free-typed paraphrases ("create a runbook please")
    # fall through to Q&A. This keeps the principle consistent across
    # terminal-state actions: clicking triggers persisted side effects;
    # typing never does.
    _RUNBOOK_CREATION_PATTERNS = (GENERATE_RUNBOOK_PAYLOAD.lower(),)

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
          2. User accepts runbook suggestion → evaluate, create draft.
             Eligible: RESOLVED cases only — runbooks codify complete
             troubleshooting scenarios (root cause + verified solution).
          3. User asks questions about the case → answer via TERMINAL_TEMPLATE.
        """
        msg_lower = user_message.lower().strip().rstrip(".!? ")

        # Scenario 1: Report regeneration. Strict exact-match against the
        # COOPERATIVE suggestion payloads — free-typed paraphrases fall
        # through to Q&A so typing can never produce a persisted Report
        # side effect.
        if msg_lower in self._REPORT_REGEN_PATTERNS:
            return await self._handle_report_regeneration(case, metadata)

        # Scenario 2: Runbook creation. Strict exact-match (same policy
        # as regen): only the COOPERATIVE suggestion's precomposed
        # payload triggers persisted runbook generation; paraphrases
        # fall through to Q&A. RESOLVED-only — runbooks codify a
        # confirmed root-cause-to-solution chain.
        is_runbook_eligible = case.status == CaseStatus.RESOLVED
        if is_runbook_eligible and msg_lower in self._RUNBOOK_CREATION_PATTERNS:
            return await self._handle_runbook_creation(case, metadata)

        # Scenario 3: Q&A
        return await self._process_terminal_qa(case, user_message, metadata)

    async def _handle_report_regeneration(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Regenerate the terminal summary report for a terminal case.

        For CLOSED cases, the same substance gate applied at closure time
        applies here — strict gating, no end-run around
        ``should_generate_terminal_summary``. RESOLVED cases regenerate
        unconditionally (a confirmed solution is always summarizable).

        The freshly-generated content is rendered inline in chat (same
        principle as the closure-ack turn), since summary writing is an
        interactive operation in this codebase.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )
        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.status == CaseStatus.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution Summary"
        else:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure Summary"

        # Strict gating for CLOSED: the verdict at regen time must agree
        # with the verdict at closure time. Substance signals are frozen
        # in CLOSED state, so this is a stable check.
        if case.status == CaseStatus.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case) or (
                "No closure summary can be generated for this case."
            )
            return {
                "agent_response": skip,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

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
            reports = await self.report_service.generate_reports(case, [report_type])
            content = None
            if reports:
                content = getattr(reports[0], "content", None) or getattr(
                    reports[0], "markdown_content", None
                )
            agent_response = (
                content
                if content
                else f"The {report_label} has been regenerated. "
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
                f"Failed to regenerate the {report_label}. Please try again."
            )

        # Re-offer the regen affordance — the user may want to iterate.
        follow_ups = (
            _resolved_suggestions()
            if case.status == CaseStatus.RESOLVED
            else _closed_suggestions(case)
        )

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    async def _handle_runbook_creation(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate readiness + dedup, then create runbook draft (fire-and-forget).

        Only RESOLVED cases reach this path — runbooks codify complete
        troubleshooting scenarios (root cause + verified solution).
        Eligibility is gated by the caller (`_process_terminal_turn`).

        Flow:
        1. Check content readiness (assess_runbook_readiness via evaluate_runbook_suggestion)
        2. Check deduplication
        3. If eligible: call ConversionService.convert_from_case() in background
        4. Return immediately with a message directing user to Dashboard Drafts
        """
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
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

        # Step 3: Create the draft
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
                "I'll let you know here when it's ready — you'll also find it in "
                "the Dashboard under **Knowledge > Drafts**."
            )
            logger.info(
                f"Runbook creation initiated for case {case.case_id}",
                extra={"case_id": case.case_id},
            )
            # Success path re-offers the standard terminal Q&A affordances
            # (regenerate summary, generate runbook) so the user can iterate
            # on the summary while the background runbook conversion runs,
            # or retry the runbook if the background task fails (the
            # completion notification will tell them so).
            follow_ups = _resolved_suggestions()
        except Exception as e:
            logger.warning(
                f"Failed to initiate runbook creation for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                "Failed to start runbook generation. "
                "You can try again or create one from the Dashboard."
            )
            # Failure path stays empty — the text already says "try again",
            # and the user will see the standard terminal Q&A suggestions
            # on the next turn anyway.
            follow_ups = []

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    async def _run_runbook_conversion(
        self,
        conversion_service,
        request,
        user_id: str,
    ) -> None:
        """Background task for runbook conversion.

        Logs success/failure and writes a completion notification to the
        case transcript so the chat-side user sees a confirmation message
        (success) or a failure note with a retry path. The notification is
        best-effort: if writing it fails, the background task swallows the
        secondary error rather than masking the primary outcome.
        """
        notification_content: str
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
                notification_content = (
                    f"Your runbook draft **{draft.title}** is ready. "
                    f"View it in the Dashboard under **Knowledge > Drafts**."
                )
            else:
                logger.warning(
                    f"Runbook conversion completed but no drafts produced "
                    f"for case {request.case_id}",
                    extra={"case_id": request.case_id},
                )
                notification_content = (
                    "Runbook generation completed but no draft was produced. "
                    "Click **Generate runbook from this case** to retry."
                )
        except Exception as e:
            logger.error(
                f"Background runbook creation failed for case {request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )
            notification_content = (
                "Runbook generation failed. "
                "Click **Generate runbook from this case** to retry."
            )

        # Best-effort completion notification. The case is loaded fresh
        # because terminal cases can still receive Q&A turns that mutate
        # `messages`, and the per-case lock prevents this write from
        # interleaving with a concurrent Q&A turn.
        try:
            async with self._case_locks[request.case_id]:
                case = await self.repository.get(request.case_id)
                if case is None:
                    logger.warning(
                        f"Case {request.case_id} not found when writing "
                        f"runbook completion notification — case may have "
                        f"been deleted while the background task was running.",
                        extra={"case_id": request.case_id},
                    )
                    return
                case.messages.append(
                    {
                        "message_id": f"msg_{uuid4().hex[:12]}",
                        "case_id": case.case_id,
                        "author_id": "system",
                        "role": "system",
                        "content": notification_content,
                        "created_at": datetime.now(UTC).isoformat(),
                        "turn_number": case.current_turn,
                        "metadata": {"source": "runbook_conversion_complete"},
                    }
                )
                case.message_count = len(case.messages)
                await self.repository.save(case)
        except Exception as e:
            logger.warning(
                f"Failed to write runbook completion notification for case "
                f"{request.case_id}: {e}",
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

        # Attach terminal-Q&A suggestions deterministically. The
        # TERMINAL_TEMPLATE instructs the LLM to leave its own
        # suggested_follow_ups empty; the engine owns these so the rules
        # don't drift turn-to-turn:
        #   - CLOSED: regen-closure-summary card iff the substance gate
        #     PASSes (also the chat-side retry path when initial
        #     generation failed).
        #   - RESOLVED: regen-resolution-summary + runbook cards. Regen
        #     mirrors CLOSED's offering; runbook is the forward action
        #     RESOLVED enables.
        if case.status == CaseStatus.CLOSED:
            follow_ups = follow_ups + _closed_suggestions(case)
        elif case.status == CaseStatus.RESOLVED:
            follow_ups = follow_ups + _resolved_suggestions()

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
            #
            # Two detection paths (checked in order):
            # 1. Intent-based: COOPERATIVE suggestion clicks carry
            #    intent_type="confirmation" + confirmation_value — deterministic
            # 2. Pattern-based: fallback for users who type instead of clicking
            if hasattr(case, "pending_transition") and case.pending_transition:
                # Contradicting status_transition intent cancels the pending
                # transition. Example: user clicked "Close" (pending), then
                # clicked "Investigating" — cancel the close and process the
                # new intent normally.
                if (
                    intent_type == "status_transition"
                    and intent_data
                    and intent_data.get("to_status")
                    != case.pending_transition.get("to_status")
                ):
                    from faultmaven.core.investigation.terminal_transitions import (
                        cancel_pending_transition,
                    )

                    old_target = case.pending_transition.get("to_status")
                    new_target = intent_data.get("to_status")
                    cancel_pending_transition(case)
                    logger.info(
                        f"Pending transition to '{old_target}' cancelled — user "
                        f"requested different transition to '{new_target}' "
                        f"for case {case.case_id}"
                    )
                    # Fall through to normal intent processing (section 0c)
                elif not case.pending_transition.get("needs_info"):
                    # Resolve confirm/decline from intent or pattern matching
                    intent_confirms = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is True
                    )
                    # A repeated status_transition intent matching the pending
                    # transition's target is an implicit confirmation — the user
                    # clicked the same dropdown/button again after the agent
                    # proposed the transition.
                    status_transition_confirms = (
                        intent_type == "status_transition"
                        and (intent_data or {}).get("to_status")
                        == case.pending_transition.get("to_status")
                    )
                    intent_confirms = intent_confirms or status_transition_confirms
                    intent_declines = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is False
                    )
                    user_confirms = intent_confirms or self._user_confirms_transition(
                        user_message
                    )
                    user_declines = intent_declines or self._user_declines_transition(
                        user_message
                    )

                    if user_confirms:
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

                        # Gate 3 outcome telemetry: capture whether this
                        # close resolves a pending Gate 3 with
                        # mitigation_sufficient. Read state BEFORE confirm
                        # fires (the closure_reason lives on pending_transition;
                        # path_selection state is unchanged by the confirm call).
                        _gate3_close = (
                            case.path_selection is not None
                            and case.path_selection.path
                            == InvestigationPath.MITIGATION_FIRST
                            and case.path_selection.mitigation_completed_at_turn
                            is not None
                            and not case.path_selection.rca_after_mitigation_confirmed
                            and (case.pending_transition or {}).get("closure_reason")
                            == "mitigation_sufficient"
                            and (case.pending_transition or {}).get("to_status")
                            == "closed"
                        )

                        confirm_pending_transition(case, case.user_id)

                        if _gate3_close:
                            inquiry_gate3_resolved_total.labels(
                                outcome="closed_mitigation_sufficient"
                            ).inc()
                            logger.info(
                                f"Case {case.case_id}: Gate 3 resolved by "
                                f"close as mitigation-sufficient."
                            )

                        # Persist the terminal status before generating the
                        # summary — the Report row FKs to case_id.
                        await self.repository.save(case)

                        # Synchronous summary generation. Returns rendered
                        # markdown on success, a skip note when the gate
                        # blocks generation, a failure note on LLM error,
                        # or None when no report service is configured.
                        # The second tuple element flags an LLM-error
                        # failure so the ack-turn can offer the regen
                        # affordance (G2 — there's no inline summary to
                        # be noisy next to when generation failed).
                        (
                            summary_payload,
                            summary_failed,
                        ) = await self._auto_generate_report(case)

                        agent_response = _compose_terminal_reply(case, summary_payload)
                        self._record_deterministic_turn(
                            case, user_message or "", agent_response
                        )
                        await self.repository.save(case)

                        # Closure-ack follow-ups depend on whether
                        # generation succeeded. Success: minimal
                        # suggestions (the summary is rendered inline,
                        # so a regen card next to it would be noise).
                        # Failure: include the regen affordance so the
                        # user can retry immediately — the "noise next
                        # to inline summary" rationale doesn't apply
                        # when there's no summary inline.
                        follow_ups = _select_ack_follow_ups(case, summary_failed)

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
                    elif user_declines:
                        from faultmaven.core.investigation.terminal_transitions import (
                            cancel_pending_transition,
                        )

                        cancel_pending_transition(case)

                        agent_response = "Understood. The case remains open for further investigation."
                        self._record_deterministic_turn(
                            case, user_message or "", agent_response
                        )
                        await self.repository.save(case)

                        return {
                            "agent_response": agent_response,
                            "suggested_follow_ups": [],
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": False,
                            },
                        }
                    else:
                        # User said something that isn't a clear yes/no.
                        # Re-present the confirmation — don't fall through
                        # to the LLM tool loop, which crashes on short or
                        # ambiguous messages (tool_choice=required fails).
                        to_status = case.pending_transition.get("to_status", "resolved")
                        summary = case.pending_transition.get("summary", "")

                        if to_status == "resolved":
                            agent_response = (
                                "Please select one of the options above to continue."
                                if not summary
                                else f"{summary}\n\nPlease select one of the options above to continue."
                            )
                            follow_ups = _resolution_confirmation_suggestions()
                        else:
                            agent_response = (
                                "Please select one of the options above to continue."
                                if not summary
                                else f"{summary}\n\nPlease select one of the options above to continue."
                            )
                            follow_ups = _close_confirmation_suggestions()

                        self._record_deterministic_turn(
                            case, user_message or "", agent_response
                        )
                        await self.repository.save(case)

                        return {
                            "agent_response": agent_response,
                            "suggested_follow_ups": follow_ups,
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": False,
                            },
                        }

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
                    # closure_reason derived inside propose_transition from
                    # case state — engine handles the enum; caller never
                    # specifies it.
                    propose_transition(
                        case=case,
                        to_status="closed",
                        summary=closure.message,
                    )

                    logger.info(
                        f"Proposed CLOSED transition for case {case.case_id} via dropdown "
                        f"(pending user confirmation)"
                    )

                    # Save and return with closure summary + canonical
                    # confirm/decline pair (alignment with agent-initiated path).
                    self._record_deterministic_turn(
                        case, user_message or "", closure.message
                    )
                    await self.repository.save(case)
                    return {
                        "agent_response": closure.message,
                        "suggested_follow_ups": _close_confirmation_suggestions(),
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

                        # Persist terminal state before synthesis (Report
                        # row FKs to case_id), then synthesize, then record
                        # the composed reply.
                        await self.repository.save(case)
                        (
                            summary_payload,
                            summary_failed,
                        ) = await self._auto_generate_report(case)
                        _resp = _compose_terminal_reply(case, summary_payload)
                        self._record_deterministic_turn(case, user_message or "", _resp)
                        await self.repository.save(case)

                        return {
                            "agent_response": _resp,
                            "suggested_follow_ups": _select_ack_follow_ups(
                                case, summary_failed
                            ),
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

                    if readiness.verdict == readiness.SUGGEST_CLOSE:
                        # Case lacks fundamentals — pivot to CLOSED. Propose the
                        # closed transition so the COOPERATIVE pair the user
                        # sees matches what they will be confirming.
                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: case {case.case_id} "
                            f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}). "
                            f"Pivoting to CLOSED."
                        )
                        propose_transition(
                            case=case,
                            to_status="closed",
                            summary=readiness.message,
                        )
                        self._record_deterministic_turn(
                            case, user_message or "", readiness.message
                        )
                        await self.repository.save(case)
                        return {
                            "agent_response": readiness.message,
                            "suggested_follow_ups": _close_confirmation_suggestions(),
                            "case_updated": case,
                            "metadata": {
                                "turn_number": case.current_turn,
                                "milestones_completed": [],
                                "progress_made": False,
                            },
                        }

                    if readiness.verdict == readiness.NEEDS_INFO:
                        # Partially ready — ask user for the missing pieces but
                        # remember their resolve intent so a follow-up turn
                        # with the missing detail can move forward.
                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: case {case.case_id} "
                            f"verdict=NEEDS_INFO (missing: {readiness.missing}). "
                            f"Remembering resolve intent."
                        )
                        propose_transition(
                            case=case,
                            to_status="resolved",
                            summary=readiness.message,
                        )
                        case.pending_transition["needs_info"] = True
                        self._record_deterministic_turn(
                            case, user_message or "", readiness.message
                        )
                        await self.repository.save(case)
                        return {
                            "agent_response": readiness.message,
                            "suggested_follow_ups": _resolution_confirmation_suggestions(),
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
                        summary="Case meets resolution criteria. Awaiting user confirmation.",
                    )
                    metadata["transition_proposed_this_turn"] = True

                    logger.info(
                        f"INVESTIGATING->RESOLVED dropdown: proposed transition for "
                        f"case {case.case_id} (pending user confirmation)"
                    )

                    # Return immediately with confirmation prompt + canonical
                    # confirm/decline pair (alignment with agent-initiated path).
                    _resp = (
                        "You've indicated this issue is resolved.\n\n"
                        + _build_resolution_confirmation(case)
                    )
                    self._record_deterministic_turn(case, user_message or "", _resp)
                    await self.repository.save(case)
                    return {
                        "agent_response": _resp,
                        "suggested_follow_ups": _resolution_confirmation_suggestions(),
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
                    # Update inquiry state (Gate 1 passes)
                    case.inquiry.problem_statement_confirmed = True
                    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
                    case.inquiry.decided_to_investigate = True
                    case.inquiry.decision_made_at = datetime.now(UTC)

                    # Compute path recommendation now that Gate 1 has passed.
                    # path_selection.user_confirmed is False — Gate 2 fires next.
                    _compute_inquiry_path_selection(case)

                    logger.info(
                        f"Case {case.case_id}: Gate 1 confirmed via confirmation intent. "
                        f"path_selection={case.path_selection.path.value if case.path_selection else None} "
                        f"(awaiting Gate 2 user confirmation before transitioning to INVESTIGATING)"
                    )

                    # Do NOT transition here — _check_automatic_transitions
                    # enforces INV-19 (Gate 2 must pass before INQUIRY ->
                    # INVESTIGATING). Continue to normal LLM flow so the
                    # agent's response carries the Gate 2 prompt and the
                    # response builder attaches the deterministic Gate 2
                    # suggestions.

            elif intent_type == "path_selection":
                # Gate 2 confirmation. User clicked one of the two path
                # COOPERATIVE suggestions; intent_data.investigation_path is
                # either the recommended path or the alternate.
                logger.info(
                    f"Explicit path_selection intent for case {case.case_id} "
                    f"(intent_data={intent_data})"
                )

                if case.status != CaseStatus.INQUIRY:
                    logger.warning(
                        f"Received path_selection intent for case {case.case_id} "
                        f"but status is {case.status.value}"
                    )
                elif not case.path_selection:
                    logger.warning(
                        f"Received path_selection intent for case {case.case_id} "
                        f"but no path recommendation exists (Gate 1 not yet passed?)"
                    )
                elif not intent_data or "investigation_path" not in intent_data:
                    logger.warning(
                        f"path_selection intent missing investigation_path payload "
                        f"for case {case.case_id}"
                    )
                else:
                    chosen_path_str = intent_data["investigation_path"]
                    try:
                        chosen_path = InvestigationPath(chosen_path_str)
                    except ValueError:
                        logger.warning(
                            f"path_selection intent with unknown path value "
                            f"{chosen_path_str!r} for case {case.case_id}"
                        )
                    else:
                        ps = case.path_selection
                        # PathSelection is frozen — build a new instance with
                        # the user's choice applied. If the user picked the
                        # alternate, swap path/alternate_path so the data
                        # model stays internally consistent.
                        updates_for_path: dict[str, Any] = {
                            "user_confirmed": True,
                            "user_confirmed_at_turn": case.current_turn,
                        }
                        if chosen_path != ps.path:
                            updates_for_path["path"] = chosen_path
                            updates_for_path["alternate_path"] = ps.path
                            updates_for_path["selected_by"] = case.user_id
                        case.path_selection = ps.model_copy(update=updates_for_path)
                        logger.info(
                            f"Case {case.case_id}: Gate 2 confirmed via "
                            f"path_selection intent. path={chosen_path.value}, "
                            f"override={chosen_path != ps.path}"
                        )

                    # Don't transition here — _check_automatic_transitions
                    # picks up the state change and fires the transition if
                    # both gates have now passed.

                    # Gate 2 outcome telemetry: track whether the router's
                    # recommendation was accepted as-is or overridden.
                    inquiry_gate2_confirmed_total.labels(
                        outcome=(
                            "override" if chosen_path != ps.path else "recommended"
                        )
                    ).inc()

            elif intent_type == "post_mitigation_choice":
                # Gate 3 confirmation. User clicked the "Continue with RCA"
                # suggestion (the "close as mitigation-sufficient" branch
                # uses STATUS_TRANSITION instead — handled above).
                logger.info(
                    f"Explicit post_mitigation_choice intent for case "
                    f"{case.case_id} (intent_data={intent_data})"
                )

                if case.status != CaseStatus.INVESTIGATING:
                    logger.warning(
                        f"Received post_mitigation_choice intent for case "
                        f"{case.case_id} but status is {case.status.value}"
                    )
                elif not _gate3_is_pending(case):
                    logger.warning(
                        f"Received post_mitigation_choice intent for case "
                        f"{case.case_id} but Gate 3 is not pending "
                        f"(path={case.path_selection.path.value if case.path_selection else None}, "
                        f"mitigation_completed_at_turn="
                        f"{case.path_selection.mitigation_completed_at_turn if case.path_selection else None}, "
                        f"rca_after_mitigation_confirmed="
                        f"{case.path_selection.rca_after_mitigation_confirmed if case.path_selection else None})"
                    )
                elif not intent_data or "continue_to_rca" not in intent_data:
                    logger.warning(
                        f"post_mitigation_choice intent missing continue_to_rca "
                        f"payload for case {case.case_id}"
                    )
                else:
                    continue_to_rca = bool(intent_data["continue_to_rca"])
                    ps = case.path_selection
                    if continue_to_rca:
                        case.path_selection = ps.model_copy(
                            update={
                                "rca_after_mitigation_confirmed": True,
                                "rca_after_mitigation_confirmed_at_turn": case.current_turn,
                            }
                        )
                        inquiry_gate3_resolved_total.labels(
                            outcome="continued_to_rca"
                        ).inc()
                        logger.info(
                            f"Case {case.case_id}: Gate 3 confirmed — "
                            f"continuing with RCA. INV-21 guard now allows "
                            f"root_cause_identified."
                        )
                    else:
                        # continue_to_rca=False is informational only — the
                        # close branch uses STATUS_TRANSITION (which fires
                        # the gate3_resolved_total{closed_mitigation_sufficient}
                        # counter at the transition site). Logging here for
                        # observability if the frontend ever emits this branch.
                        logger.info(
                            f"Case {case.case_id}: post_mitigation_choice "
                            f"intent with continue_to_rca=False — expected "
                            f"path is STATUS_TRANSITION to CLOSED."
                        )

            # ============================================================
            # HYPOTHESIS ACTION - Explicit Intent (Frontend/IntentResolver)
            # ============================================================
            # Applies the state change BEFORE LLM processing so the agent
            # sees updated hypothesis state in its context and can acknowledge.
            elif intent_type == "hypothesis_action" and intent_data:
                hypothesis_id = intent_data.get("hypothesis_id")
                action = intent_data.get("action")  # validate | refute | retire

                if hypothesis_id and action and case.hypotheses:
                    hypothesis = case.hypotheses.get(hypothesis_id)

                    if hypothesis:
                        if action == "refute":
                            self.hypothesis_manager.refute_hypothesis(
                                hypothesis=hypothesis,
                                current_turn=case.current_turn,
                                refuting_evidence_ids=[],
                                reason=user_message or "User refuted",
                            )
                        elif action == "validate":
                            hypothesis.status = HypothesisStatus.VALIDATED
                            hypothesis.likelihood = 1.0
                            hypothesis.last_updated_turn = case.current_turn
                        elif action == "retire":
                            hypothesis.status = HypothesisStatus.RETIRED
                            hypothesis.retirement_reason = (
                                user_message or "User retired"
                            )
                            hypothesis.last_updated_turn = case.current_turn

                        metadata["hypothesis_action_applied"] = True
                        logger.info(
                            f"Hypothesis {hypothesis_id} {action}d via explicit intent "
                            f"for case {case.case_id}"
                        )
                    else:
                        logger.warning(
                            f"Hypothesis {hypothesis_id} not found in case {case.case_id}"
                        )

                # Fall through to normal LLM processing for acknowledgment

            # NL transition detection happens upstream in
            # InvestigationService._detect_transition_intent — typed
            # transition requests reach this point as
            # intent_type == "status_transition" (handled above), never as
            # "conversation". Conversation that does not request a
            # transition flows directly into the LLM block below.

            # 1. Gather Context & Build Prompt
            # KB retrieval during turns is handled by the kb_qa tool in the
            # tool-augmented generation loop. The agent decides when to call
            # kb_qa based on prompt directives (Rule 6: Knowledge First).
            # This ensures proper scope filtering via ToolContext (user_id,
            # team_ids) which the engine doesn't have at this level.

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

            # Phase 4c — prefetch entity highlights from the Phase 4
            # ``case_entities`` registry when the feature is on. When
            # the flag is off (or the producer wrote no entities),
            # ``fetch_entity_highlights`` returns "" and the template
            # slot renders empty.
            entity_highlights_block = ""
            try:
                from faultmaven.config.settings import get_settings
                from faultmaven.core.investigation.prompts.context_builder import (
                    fetch_entity_highlights,
                )

                if get_settings().preprocessing.entity_registry_enabled:
                    entity_highlights_block = await fetch_entity_highlights(
                        self.repository, case.case_id
                    )
            except Exception as exc:
                logger.warning(
                    "Entity highlights prefetch failed for case %s " "(non-fatal): %s",
                    case.case_id,
                    exc,
                )

            prompt = get_prompt_for_case(
                case,
                user_message,
                kb_results=None,
                provider_name=provider_name,
                model_name=model_name,
                processing_mode=classification.mode.value,
                entity_highlights=entity_highlights_block,
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
            #
            # Safety net: when a pending_transition exists, the user is in a
            # confirmation flow. Don't force tool_choice=required — the user's
            # message is a confirmation/decline that may have fallen through
            # pattern matching (typed instead of clicked). Forcing tools crashes
            # the tool loop when the LLM has nothing to search for.
            query_mode = (intent_data or {}).get("query_mode")
            has_pending = (
                hasattr(case, "pending_transition") and case.pending_transition
            )
            if self.investigation_tools:
                force_tools = (
                    query_mode == "directed_analysis"
                    and bool(case.evidence)
                    and not has_pending
                )
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
                        "Ground your reasoning in specific evidence (cite at least 2 types "
                        "of data — timestamps like HH:MM, error messages, IPs/usernames, "
                        "or metrics/counts — directly from the search results above) and "
                        "explain WHY using causal language like 'because', 'therefore', "
                        "'this indicates'. Reference evidence by filename or description, "
                        "never by ev_ IDs. "
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

            # Step 5.9: Progress monitoring (before recording turn)
            # Check if transparent mode should activate and/or repair
            # patterns are detected. Replaces the old stagnation detector.
            progress_result = self.progress_monitor.check_progress(case_updated)
            stagnation_str: str | None = None
            if progress_result:
                # Record repair pattern if detected
                if progress_result.repair_type:
                    stagnation_str = progress_result.repair_type.value
                    metadata["stagnation_type"] = progress_result.repair_type.value
                    metadata["breakout_action"] = progress_result.repair_action

                metadata["progress_transparent"] = True
                metadata["pending_milestone"] = progress_result.pending_milestone
                metadata["milestone_description"] = (
                    progress_result.milestone_description
                )

                # Store prompt injection in system_feedback for next turn
                if progress_result.prompt_injection:
                    current_feedback = metadata.get("system_feedback", "") or ""
                    metadata["system_feedback"] = (
                        f"{current_feedback}\n{progress_result.prompt_injection}".strip()
                    )

                log_msg = (
                    f"Progress transparency activated: pending milestone "
                    f"'{progress_result.pending_milestone}'"
                )
                if progress_result.repair_type:
                    log_msg += f", repair: {progress_result.repair_type.value}"
                logger.info(log_msg)

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
                repair_pattern=stagnation_str,
                validation_repairs=validation_repairs,
            )
            case_updated.turn_history.append(turn_record)

            # Step 7: Save case (only if changes made, but turn history always updates)
            case_updated.updated_at = datetime.now(UTC)
            case_updated.last_activity_at = datetime.now(UTC)
            await self.repository.save(case_updated)

            # Step 7b: Auto-generate terminal summary synchronously on
            # terminal transition. The rendered summary (or skip / failure
            # note) is appended to the agent reply below so it appears in
            # chat at the moment of generation — consistent with the
            # explicit-confirmation path. `summary_failed` flags an LLM-
            # error so the ack-turn follow-ups can include the regen
            # affordance (G2).
            summary_payload: str | None = None
            summary_failed: bool = False
            if metadata.get("status_transitioned") and case_updated.status in (
                CaseStatus.RESOLVED,
                CaseStatus.CLOSED,
            ):
                summary_payload, summary_failed = await self._auto_generate_report(
                    case_updated
                )

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

            # Post-LLM overrides for resolution readiness re-evaluation.
            # After a needs_info turn, check whether requirements are now met.
            if metadata.get("resolution_ready_for_confirmation"):
                agent_response_text = (
                    "Thanks for the additional details.\n\n"
                    + _build_resolution_confirmation(case_updated)
                )
                follow_ups = _resolution_confirmation_suggestions()
            elif metadata.get("resolution_suggest_close"):
                # User didn't provide required info — suggest Close instead.
                agent_response_text = metadata["resolution_readiness_message"]
                follow_ups = _close_confirmation_suggestions()
            elif metadata.get("resolution_needs_info_first_pass"):
                # LLM proposed RESOLVED but readiness check returned NEEDS_INFO.
                # Override the LLM's agent_response with the readiness message
                # so the prompt the user sees matches the missing-info ask
                # the UI dropdown path produces in the same situation.
                agent_response_text = metadata["resolution_needs_info_message"]
                follow_ups = metadata["override_suggestions"]
            elif metadata.get("rca_infeasible_closure_message"):
                # Stage-gate side effect: mitigation_verified + rca_infeasible=True.
                # Replace the LLM's mitigation-confirmation reply with the engine-
                # built closure proposal so the user sees a coherent prompt + the
                # canonical close confirm/decline pair.
                agent_response_text = metadata["rca_infeasible_closure_message"]
                follow_ups = metadata["override_suggestions"]
            elif metadata.get("override_suggestions"):
                # ProposedTransition was emitted by the LLM this turn (either
                # detecting solution success or routing user-expressed
                # transition intent). Replace the LLM's follow-ups with the
                # canonical confirm/decline pair so all three trigger paths
                # (UI click, NL via this branch, agent-initiated) converge on
                # the same deterministic confirmation UX.
                follow_ups = metadata["override_suggestions"]

            # Engine-owned gate affordances. When a state-machine gate is
            # pending (Gate 1 — problem-statement confirmation; Gate 2 —
            # investigation path; Gate 3 — post-mitigation continuation;
            # or a pending_transition disposition handshake), the engine
            # emits the canonical clickable affordance pair regardless of
            # LLM compliance with the prompt's suggestion-emission
            # directives. The consolidator is a single source of truth that
            # replaced the previously-scattered handshake-deferred / Gate 2
            # / Gate 3 branches. Gate 1 now fires on every Gate-1-pending
            # turn (not only the handshake-deferred recovery turn) — the
            # architectural completion that makes Gate 1 symmetric with
            # Gate 2 and Gate 3, and removes LLM compliance from the
            # correctness path. See INV-01, INV-19, INV-21.
            gate_result = engine_owned_affordances(case_updated, metadata)
            if gate_result is not None:
                gate_name, gate_affordances = gate_result
                follow_ups = gate_affordances
                engine_owned_affordance_served_total.labels(gate=gate_name).inc()
                logger.info(
                    "engine_owned_affordances_served",
                    extra={
                        "case_id": case_updated.case_id,
                        "turn": case_updated.current_turn,
                        "gate": gate_name,
                        "affordance_count": len(gate_affordances),
                    },
                )

            # Closure-ack turn (LLM-driven path): when generation
            # succeeded, suggestions stay minimal — the rendered summary
            # is right above and a regen card next to it would be noise.
            # When generation failed, include the regen affordance so the
            # user can retry immediately (G2 — the "noise" guard doesn't
            # apply when there's no inline summary).
            if metadata.get("status_transitioned") and case_updated.status in (
                CaseStatus.RESOLVED,
                CaseStatus.CLOSED,
            ):
                follow_ups = _select_ack_follow_ups(case_updated, summary_failed)

            # Append the synthesized summary (or skip / failure note) so it
            # appears in chat at the moment of generation. Update the
            # already-recorded turn_record in place and re-save so chat
            # history persists the composed reply, not just the LLM's text.
            if summary_payload:
                agent_response_text = (
                    f"{agent_response_text}\n\n{summary_payload}".strip()
                )
                if case_updated.turn_history:
                    case_updated.turn_history[-1].agent_response = agent_response_text
                    await self.repository.save(case_updated)

            # Compliance instrumentation: per-turn signal on whether the LLM
            # is honoring the transition-handling prompt rules. Used for
            # quarterly drift review across model-version changes and prompt
            # growth. Cheap regex on agent_response checks for completion
            # phrases the rule explicitly forbids.
            #
            # Scope (INV-15 §1.3.1): scan is deliberately narrow — only
            # transition-completion claims. The broader _ADVISOR_ROLE_-
            # CONSTRAINT banned-phrase list ("Let me check", "I will run",
            # etc.) is NOT scanned here because those phrases have higher
            # false-positive rates in legitimate context. If broader
            # advisor-role drift detection becomes valuable, add a
            # separately-tagged "advisor_role_compliance" log signal
            # alongside this one — don't dilute the transition_compliance
            # tuple. See investigation-lifecycle-logic.md §1.3.1
            # (INV-15 drift note).
            _completion_phrases = (
                "case closed",
                "case is closed",
                "case is now closed",
                "marking as resolved",
                "marking this as resolved",
                "marking this resolved",
                "marked as resolved",
                "case resolved",
                "case is resolved",
                "case is now resolved",
                "i have resolved",
                "i've resolved",
                "i have closed",
                "i've closed",
            )
            _agent_text_lower = (agent_response_text or "").lower()
            # Capture LLM-vs-engine drift on the proposed-transition path.
            # When the LLM emits to_status=resolved on a thin case, the engine
            # pivots to closed (see _check_automatic_transitions). Recording
            # the pivot here lets us compare LLM intent against engine action
            # over time without diffing log lines.
            _llm_proposed = getattr(
                getattr(response_obj, "state_updates", None),
                "proposed_transition",
                None,
            )
            _llm_proposed_to_status = (
                getattr(_llm_proposed, "to_status", None) if _llm_proposed else None
            )
            _engine_to_status = (
                case_updated.pending_transition.get("to_status")
                if case_updated.pending_transition
                else None
            )
            _transition_pivoted = bool(
                _llm_proposed_to_status
                and _engine_to_status
                and _llm_proposed_to_status != _engine_to_status
            )
            logger.info(
                "transition_compliance",
                extra={
                    "case_id": case_updated.case_id,
                    "turn": case_updated.current_turn,
                    "status": case_updated.status.value,
                    "proposed_transition_emitted": bool(
                        metadata.get("transition_proposed")
                    ),
                    "llm_proposed_to_status": _llm_proposed_to_status,
                    "engine_effective_to_status": _engine_to_status,
                    "transition_pivoted": _transition_pivoted,
                    "user_confirmed_investigation_emitted": bool(
                        getattr(
                            getattr(response_obj, "state_updates", None),
                            "user_confirmed_investigation",
                            False,
                        )
                    ),
                    "agent_response_contains_completion_phrase": any(
                        p in _agent_text_lower for p in _completion_phrases
                    ),
                    "status_transitioned": bool(metadata.get("status_transitioned")),
                },
            )

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

        except StaleCaseException:
            # OCC conflict on the case row — the route handler maps this
            # to HTTP 409. Do NOT wrap in MilestoneEngineError, or the
            # type identity is lost and the handler falls through to 500.
            raise
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
        # "Already vectorized" is sourced from the persistent
        # Evidence.vectorized flag (set + saved by _vectorize_evidence on
        # success) so dedup holds both within a turn and across turns.
        da_empty_search_counts: dict[str, int] = {}  # evidence_id → consecutive empties

        # Proactive vectorization: start background tasks for large evidence
        # files before the tool loop begins. Runs concurrently so semantic
        # search is available by the time the agent needs it.
        # Gated on force_tool_use=True (Directed Analysis). Triage and
        # Knowledge Query turns don't consult case evidence via semantic
        # search, so preemptive embedding would be wasted work — and on a
        # cold-cached model it can dominate the turn budget. See
        # data-preprocessing-design-specification.md §5 (vectorization is
        # scoped to DA-mode turns).
        proactive_tasks: dict[str, asyncio.Task] = {}
        if case and force_tool_use:
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
                # No tool calls. Two scenarios:
                # 1. Recoverable (force_schema_next=False): the LLM emitted text
                #    instead of calling a tool. Append the text plus a user-role
                #    nudge directing the schema-tool call, then retry with only
                #    schema tools. The nudge is what makes the next turn coherent
                #    — without it, the LLM "already answered" and won't act.
                # 2. Unrecoverable (force_schema_next=True or is_final): we already
                #    nudged once and the LLM still won't call the schema tool. Try
                #    parsing the text as schema JSON; if that fails, raise
                #    ToolCallingUnsupportedError so _generate_structured_output's
                #    fallback path retries via the non-tool structured-output route.
                if is_final or force_schema_next:
                    from faultmaven.exceptions import ToolCallingUnsupportedError

                    text = (response.content or "").strip()
                    if text:
                        try:
                            return self._parse_text_as_schema(text, schema_model)
                        except Exception as parse_err:
                            logger.warning(
                                "Tool loop: text content after forced-schema "
                                "iteration not parseable as schema (%s)",
                                parse_err,
                            )
                    logger.warning(
                        "Tool loop: provider %s ignored tool_choice=required "
                        "with only the schema tool exposed; escalating to "
                        "non-tool fallback path",
                        provider_name,
                    )
                    raise ToolCallingUnsupportedError(
                        message=(
                            f"Provider {provider_name} returned no tool calls "
                            f"under tool_choice=required with the schema tool "
                            f"as the only option. Falling back to non-tool path."
                        ),
                        provider=provider_name,
                        model=self.da_model,
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
                # Append a user nudge that explicitly directs the schema-tool
                # call. Without this the conversation ends on an assistant
                # message with no fresh user instruction — most models read that
                # as "already answered" and either repeat themselves or return
                # empty content, defeating the recovery.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You must now produce your structured response by "
                            f"calling the `{schema_tool_name}` tool. Use the text "
                            f"above as the `agent_response` field and fill in the "
                            f"remaining required fields. Do not reply with plain "
                            f"text — only a tool call is acceptable."
                        ),
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

        Runs concurrently with the tool loop so case_evidence_search is
        available by the time the agent needs it. Only vectorizes files
        above the size threshold that haven't already been vectorized.

        Uses ``self._inflight_vectorize`` to dedup across turns: if a
        task is already running for a given evidence_id, the current
        turn reuses it instead of creating a second concurrent encode.
        The persistent ``Evidence.vectorized`` flag covers the already-
        completed state; the in-flight registry covers the running
        state. Together they prevent cross-turn task stacking.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        settings = get_settings()
        min_size = settings.agent.vectorization_min_size_bytes
        tasks: dict[str, asyncio.Task] = {}

        for ev in getattr(case, "evidence", []):
            # Vectorization size gate. Post-010: file-backed evidence has
            # its size on uploaded_files.size_bytes; chat-extracted evidence
            # (USER_DESCRIPTION, source_file_id IS NULL) has no backing file
            # and is never large enough to vectorize — treat size=0 so it
            # falls below the min-size threshold.
            file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
            size = (
                int(file_meta.size_bytes) if file_meta and file_meta.size_bytes else 0
            )
            if not (
                size >= min_size
                and size <= VECTORIZATION_MAX_SIZE_BYTES
                and not ev.vectorized
            ):
                continue

            existing = self._inflight_vectorize.get(ev.evidence_id)
            if existing is not None and not existing.done():
                # Another turn already started this; reuse the same task
                # so both turns observe the same completion.
                tasks[ev.evidence_id] = existing
                logger.debug(
                    "proactive_vectorization_reused_inflight",
                    extra={"evidence_id": ev.evidence_id},
                )
                continue

            task = asyncio.create_task(
                self._vectorize_evidence(ev.evidence_id, tool_context)
            )
            self._inflight_vectorize[ev.evidence_id] = task
            # Remove from registry once the task settles (success,
            # failure, or cancellation). If persistence succeeded the
            # flag is True and this evidence won't re-enter the loop;
            # if it failed the next turn can retry cleanly.
            task.add_done_callback(
                lambda t, eid=ev.evidence_id: self._inflight_vectorize.pop(eid, None)
            )
            tasks[ev.evidence_id] = task
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
        """Vectorize a single evidence file via the registered tool.

        On success, flips ``Evidence.vectorized`` to True via a scoped
        single-row repository UPDATE so proactive + reactive gates skip
        this evidence on subsequent turns. The flag is the single source
        of truth for "is this evidence already in the case vector store".

        No internal ``asyncio.wait_for``: time-bound policy belongs at the
        caller. Proactive callers run this unbounded as a background task
        — the in-flight registry prevents duplicates, and bounding a
        background task that the caller never synchronously awaits only
        guarantees wasted CPU when ``asyncio.wait_for`` cancels the
        asyncio Future while the thread-pool worker (which can't be
        safely killed) continues to completion. Reactive callers wrap
        this with ``asyncio.wait_for`` using
        ``AgentSettings.vectorization_reactive_timeout_seconds`` because
        they do block the agent.
        """
        try:
            result = await self.investigation_tools.execute_tool(
                "vectorize_file",
                {"evidence_id": evidence_id},
                tool_context,
            )
        except Exception as e:
            logger.warning(
                "Vectorization failed for %s: %s",
                evidence_id,
                e,
                exc_info=True,
            )
            return False

        if not result.success:
            logger.warning(
                "vectorize_file returned failure for %s: %s",
                evidence_id,
                result.error,
            )
            return False

        logger.info("vectorize_file succeeded for %s", evidence_id)

        # Persist vectorized=True via a scoped single-row UPDATE. Must NOT
        # use repository.save(case) — this runs as a fire-and-forget task
        # that can complete after subsequent turns have written. An
        # aggregate save from a stale snapshot would silently wipe those
        # newer writes across every case-owned table.
        case_id = getattr(tool_context, "case_id", None)
        if case_id:
            try:
                await self.repository.update_evidence_vectorized(
                    case_id, evidence_id, True
                )
            except Exception as e:
                logger.debug(
                    "Failed to persist vectorized flag for %s: %s",
                    evidence_id,
                    e,
                )

        # Flip the flag on the in-memory snapshot so the current turn's
        # gate sees it without another DB read.
        case = getattr(tool_context, "in_memory_case", None)
        if case is not None:
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    ev.vectorized = True
                    break

        return True

    @staticmethod
    def _evidence_is_vectorized(case: Any, evidence_id: str) -> bool:
        """Return True if the given evidence is marked vectorized on the
        in-memory case. Source of truth for dedup — the persistent
        Evidence.vectorized flag set by _vectorize_evidence on success.
        """
        if case is None:
            return False
        for ev in getattr(case, "evidence", []) or []:
            if getattr(ev, "evidence_id", None) == evidence_id:
                return bool(getattr(ev, "vectorized", False))
        return False

    _VECTORIZED_SYSTEM_MESSAGE = (
        "\n\n[SYSTEM] This file has been automatically "
        "indexed for semantic search. Use "
        "case_evidence_search to find content by "
        "meaning rather than keywords."
    )

    async def _track_da_result(
        self,
        func_name: str,
        evidence_id: str,
        tool_result: Any,
        result_text: str,
        case: Any | None,
        tool_context: Any,
        da_empty_search_counts: dict[str, int],
        proactive_tasks: dict[str, asyncio.Task],
    ) -> str:
        """Track DA failure signals and trigger vectorization when needed.

        Returns result_text, potentially with [SYSTEM] messages appended.
        Dedup of "already vectorized" is sourced from Evidence.vectorized
        (persistent) — within-turn and across-turn.
        """
        # If the proactive task for this evidence has just completed this
        # turn, emit the [SYSTEM] advisory once. _vectorize_evidence has
        # already flipped and persisted the flag by the time we see
        # task.result()==True, so subsequent reactive checks naturally
        # skip this evidence via _evidence_is_vectorized.
        if evidence_id in proactive_tasks:
            task = proactive_tasks[evidence_id]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(
                        "Proactive vectorization task failed for %s: %s",
                        evidence_id,
                        exc,
                    )
                elif (
                    task.result() and self._VECTORIZED_SYSTEM_MESSAGE not in result_text
                ):
                    result_text += self._VECTORIZED_SYSTEM_MESSAGE
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

        already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Track deep_analysis confidence for the low-confidence trigger
        # below. In-turn only — see agent_orchestration_service for why
        # cross-turn persistence was dropped.
        if func_name == "deep_analysis" and tool_result.success and case:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict):
                    confidence = float(data.get("confidence", 1.0))

                    # Low confidence trigger
                    if confidence < 0.2 and not already_vectorized:
                        result_text = await self._reactive_vectorize(
                            evidence_id,
                            tool_context,
                            result_text,
                            "low_confidence",
                        )
                        already_vectorized = self._evidence_is_vectorized(
                            case, evidence_id
                        )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Track timeouts
        if (
            not tool_result.success
            and "timed out" in (getattr(tool_result, "error", "") or "").lower()
            and not already_vectorized
        ):
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "tool_timeout",
            )
            already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Reactive vectorization on repeated empty searches
        empty_count = da_empty_search_counts.get(evidence_id, 0)
        if empty_count >= 3 and not already_vectorized:
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "repeated_empty_searches",
            )

        return result_text

    async def _reactive_vectorize(
        self,
        evidence_id: str,
        tool_context: Any,
        result_text: str,
        trigger: str,
    ) -> str:
        """Attempt reactive vectorization for a qualifying evidence file.

        On success, _vectorize_evidence flips + persists the Evidence
        vectorized flag, so subsequent reactive triggers in this turn
        will see it and skip via _evidence_is_vectorized.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        # Storage redesign 2026-04 phase 2: resolve size from case.evidence
        # (standalone evidence service deleted).
        ev_size = 0
        try:
            case = getattr(tool_context, "in_memory_case", None)
            if case is None and getattr(tool_context, "case_repository", None):
                case = await tool_context.case_repository.get(tool_context.case_id)
            if case is not None:
                for ev in getattr(case, "evidence", []) or []:
                    if getattr(ev, "evidence_id", None) == evidence_id:
                        # Post-010: size lives on uploaded_files via the
                        # source_file_id FK. Chat-extracted evidence has no
                        # backing file → size=0 (which falls below the
                        # vectorization min-size gate below).
                        file_meta = case.find_uploaded_file(
                            getattr(ev, "source_file_id", None)
                        )
                        ev_size = (
                            int(file_meta.size_bytes)
                            if file_meta and file_meta.size_bytes
                            else 0
                        )
                        break
        except Exception:
            return result_text

        settings = get_settings()
        if ev_size < settings.agent.vectorization_min_size_bytes:
            return result_text
        if ev_size > VECTORIZATION_MAX_SIZE_BYTES:
            return result_text

        # Reactive vectorization blocks the agent inside the tool loop;
        # bound it by the configurable reactive budget so a slow encode
        # can't eat the turn timeout. Proactive is unbounded elsewhere —
        # see _vectorize_evidence docstring for the split rationale.
        reactive_timeout = float(settings.agent.vectorization_reactive_timeout_seconds)
        try:
            success = await asyncio.wait_for(
                self._vectorize_evidence(evidence_id, tool_context),
                timeout=reactive_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Reactive vectorization timed out for %s after %ss "
                "(trigger=%s). Agent proceeds without semantic search "
                "results for this turn; a proactive task for the same "
                "evidence may still be in flight.",
                evidence_id,
                reactive_timeout,
                trigger,
            )
            return result_text

        if success:
            result_text += self._VECTORIZED_SYSTEM_MESSAGE
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
            "→ You MUST search kb_qa first for documented solutions, runbooks, "
            "or best practices. If kb_qa returns relevant results, ground your "
            "answer in them and cite the source. If no relevant results, answer "
            "from your own knowledge (do not mention the failed search). "
            "Optionally use web_search for supplementary detail. Connect your "
            f"answer to the case context when relevant, then call {schema_tool_name}.\n\n"
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
            "SEARCHABLE EVIDENCE — Only use search_file on evidence with "
            'searchable="true" in <evidence_collected>. These are uploaded '
            "files with raw content on disk. Evidence WITHOUT this attribute "
            "are investigation notes — they have no file to search. If you "
            "need to search a file, check the evidence id and filename from "
            "the searchable entries.\n\n"
            "EVIDENCE vs KNOWLEDGE — These are fundamentally different data types:\n"
            "- EVIDENCE is case-specific data submitted by the user: log files, "
            "metrics, configs, pasted text, screenshots, user statements about "
            "their environment. Only user-submitted data goes in evidence_to_add.\n"
            "- KNOWLEDGE is pre-built reference material from kb_qa, web_search, "
            "or your own training data. Knowledge informs your analysis but is "
            "NEVER recorded as evidence. Do NOT create evidence_to_add entries "
            "from kb_qa results, web_search results, or your own knowledge.\n\n"
            "RESPONSE FORMAT — Ground your response in evidence:\n"
            "- For case questions, cite the filename and line numbers from "
            "search results (e.g., 'In data_6-1.log, line 42: ...') and "
            "explain the significance using causal language.\n"
            "- For knowledge questions, state the relevant facts and relate "
            "them to the user's investigation context when possible.\n"
            "- Reference evidence by filename or description, never by ev_ IDs."
        )

    def _build_tool_context(self, case: Any, intent_data: dict | None = None) -> Any:
        """Build ToolContext for tool execution during DA turns."""
        from faultmaven.modules.agent.tools.base import ToolContext

        user_id = (intent_data or {}).get("user_id", "system")
        organization_id = getattr(case, "organization_id", "")

        # Extract current investigation stage for tool context enrichment
        metadata: dict[str, Any] = {}
        progress = getattr(case, "progress", None)
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
            case_repository=self.repository,
            metadata=metadata,
            in_memory_case=case,
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

        # Coerce unresolvable state_updates to {} so Pydantic field defaults apply.
        # Covers two Fireworks/DeepSeek V3 failure modes:
        #   (a) null — LLM omitted the field entirely
        #   (b) string — JSON was truncated/malformed and _parse_nested_json
        #       could not repair it (e.g. closing "} cut off before XML tag)
        _su = (
            content_obj.get("state_updates") if isinstance(content_obj, dict) else None
        )
        if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
            content_obj["state_updates"] = {}

        # Fix hallucinated enum values
        schema_dict = schema_model.model_json_schema()
        content_obj = self._fix_enum_violations(
            content_obj,
            schema_dict,
            root_defs=schema_dict.get("$defs"),
        )

        # Validate with Pydantic
        content = json.dumps(content_obj)
        parsed = schema_model.model_validate_json(content)

        # Dropped-field detection: compare what the LLM emitted to what the
        # schema accepted. Any key the LLM put in the dict that isn't a
        # field on the schema gets silently dropped by Pydantic's default
        # extra="ignore". Log it so prompt-schema drift becomes observable.
        # Motivated by the prompt-instructs/schema-rejects bug class found
        # via behavioral eval — see ADR / docs.
        self._log_dropped_fields(content_obj, parsed, schema_model)
        return parsed

    def _log_dropped_fields(
        self,
        raw: Any,
        parsed: Any,
        schema_model: Any,
    ) -> None:
        """Log when the LLM emitted top-level or state_updates fields that
        the schema doesn't accept (and thus silently dropped). One log line
        per dropped field — feed observability/quarterly review.

        TODO: walk depth limited to top-level + state_updates. Drops nested
        deeper (e.g., state_updates.hypotheses_to_add[].some_unknown_field)
        are invisible. Generalize to recursive descent if state schemas
        grow more nested or if the runtime signal misses real drift.
        """
        try:
            top_known = set(getattr(schema_model, "model_fields", {}).keys())
            if isinstance(raw, dict):
                top_dropped = [k for k in raw.keys() if k not in top_known]
                for k in top_dropped:
                    logger.warning(
                        "structured_output_dropped_field",
                        extra={
                            "schema": schema_model.__name__,
                            "level": "top",
                            "field": k,
                        },
                    )

                # Walk one level into state_updates (the most common drop site).
                state_updates = raw.get("state_updates")
                if isinstance(state_updates, dict):
                    su_field = getattr(schema_model, "model_fields", {}).get(
                        "state_updates"
                    )
                    su_schema = (
                        getattr(su_field, "annotation", None) if su_field else None
                    )
                    su_known = (
                        set(getattr(su_schema, "model_fields", {}).keys())
                        if su_schema
                        else set()
                    )
                    if su_known:
                        for k in state_updates.keys():
                            if k not in su_known:
                                logger.warning(
                                    "structured_output_dropped_field",
                                    extra={
                                        "schema": getattr(su_schema, "__name__", "?"),
                                        "level": "state_updates",
                                        "field": k,
                                    },
                                )
        except Exception:
            # Logging must never break the response path.
            logger.debug("dropped-field detection failed", exc_info=True)

    def _parse_text_as_schema(
        self,
        text: str,
        schema_model: Any,
    ) -> BaseInteractionResponse:
        """Parse free-form LLM text as a schema instance.

        Last-resort path used when a provider ignores tool_choice=required and
        emits the structured response inline as text (often wrapped in a
        ```json fence). Mirrors the markdown stripping + nested-JSON +
        enum-fix logic in _generate_structured_output's single-shot path.

        Raises ValueError if the parsed object is structurally valid but
        semantically empty (e.g., agent_response blank). This guards against
        false positives where prose happens to embed a JSON block that fits
        the schema but doesn't represent a real response — those should
        escalate to the non-tool fallback path, not be returned as-is.
        """
        cleaned = text.strip()
        if "```" in cleaned:
            match = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1).strip()
            elif cleaned.startswith("```"):
                lines = cleaned.split("\n")
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()

        content_obj = json.loads(cleaned, strict=False)
        content_obj = self._parse_nested_json(content_obj)
        _su = (
            content_obj.get("state_updates") if isinstance(content_obj, dict) else None
        )
        if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
            content_obj["state_updates"] = {}
        schema_dict = schema_model.model_json_schema()
        content_obj = self._fix_enum_violations(
            content_obj,
            schema_dict,
            root_defs=schema_dict.get("$defs"),
        )
        parsed = schema_model.model_validate_json(json.dumps(content_obj))
        self._log_dropped_fields(content_obj, parsed, schema_model)

        # Semantic guard: agent_response is the user-facing payload of every
        # BaseInteractionResponse subclass. An empty value means the recovered
        # JSON was structurally valid but contained no actual response — most
        # likely we picked up an example block from the LLM's prose. Reject
        # it so the caller escalates to the non-tool fallback path instead of
        # surfacing an empty bubble to the user.
        agent_response = getattr(parsed, "agent_response", None)
        if not agent_response or not str(agent_response).strip():
            raise ValueError(
                "parsed schema has empty agent_response — likely a prose-embedded "
                "JSON example, not a real response"
            )
        return parsed

    @staticmethod
    def _build_assistant_message(response: Any) -> dict:
        """Convert LLMResponse to OpenAI-format assistant message.

        Round-trips two kinds of provider-specific artifacts when present:
        1. Per-tool-call `provider_metadata` (e.g. signatures bound to a
           specific functionCall).
        2. Response-level `provider_metadata` (e.g. Gemini 3.x's full
           `assistant_parts` array, which carries thoughtSignatures attached
           to text/thought/functionCall parts that must all round-trip
           together — skipping any one produces a 400 on the next turn).

        Both are absent for providers/models that don't emit reasoning
        artifacts (Gemini 2.5, OpenAI Chat Completions, etc.) — the keys
        are omitted entirely so downstream serializers see no change.
        """
        tool_calls_list = []
        for tc in response.tool_calls or []:
            entry = {
                "id": tc.id,
                "type": tc.type,
                "function": tc.function,
            }
            if getattr(tc, "provider_metadata", None):
                entry["provider_metadata"] = tc.provider_metadata
            tool_calls_list.append(entry)

        msg = {
            "role": "assistant",
            "content": response.content or "",
        }
        if tool_calls_list:
            msg["tool_calls"] = tool_calls_list
        if getattr(response, "provider_metadata", None):
            msg["provider_metadata"] = response.provider_metadata
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
                "KNOWLEDGE BASE RESULT — Place the content below into the "
                "`agent_response` field of your structured response. Preserve "
                "key details, diagnostic steps, and resolution procedures — do "
                "NOT collapse it into a single sentence.\n\n"
                f"{content}\n\n"
                "SOURCE CITATION: At the end of `agent_response`, append a "
                "compact source line in italic markdown using this exact format:\n"
                "*Sources: [title1], [title2]*\n"
                "Use only the primary source title(s) from the content above. "
                "One short line — no verbose attribution paragraph.\n\n"
                "Then return the structured response by calling the response "
                "schema tool. Do not reply with plain text."
            )

        # search_file results: append citation guidance so the LLM cites
        # filename and line numbers in its response.
        if tool_name == "search_file" and isinstance(result.data, dict):
            filename = result.data.get("filename", "unknown")
            results_count = result.data.get("results_count", 0)
            content = json.dumps(result.data)
            if results_count > 0:
                content += (
                    f"\n\nCITATION: When referencing these results, cite the "
                    f'filename and line numbers (e.g., "In {filename}, '
                    f'line 42: ...").'
                )
            return content

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
                # Fireworks/DeepSeek V3 leaks XML tool-call format artifacts.
                # Apply two repair passes before giving up:
                #
                # Pass 1: strip trailing XML closing tags (e.g. </parameter></invoke>)
                # Pass 2: for JSON containers, truncate at the last valid terminator
                #         to handle stray closing braces/brackets (e.g. "[...]}")
                stripped_obj = obj.strip()

                # Pass 1 — XML closing tags
                stripped = re.sub(r"(\s*</\w+>)+\s*$", "", stripped_obj)
                if stripped != stripped_obj:
                    try:
                        parsed = json.loads(stripped)
                        return MilestoneEngine._parse_nested_json(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Pass 2 — truncate at last valid JSON container terminator
                if stripped_obj:
                    first_ch = stripped_obj[0]
                    search_ch = (
                        "]" if first_ch == "[" else "}" if first_ch == "{" else None
                    )
                    if search_ch:
                        last_pos = stripped_obj.rfind(search_ch)
                        if last_pos > 0:
                            candidate = stripped_obj[: last_pos + 1]
                            if candidate != stripped_obj:
                                try:
                                    parsed = json.loads(candidate)
                                    return MilestoneEngine._parse_nested_json(parsed)
                                except (json.JSONDecodeError, TypeError):
                                    pass

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

                # Some LLMs (Fireworks/DeepSeek V3) return null for required
                # object fields, or leave state_updates as an unparsed string
                # when JSON was truncated. Coerce both to {} so Pydantic field
                # defaults apply instead of a hard validation error.
                _su = (
                    content_obj.get("state_updates")
                    if isinstance(content_obj, dict)
                    else None
                )
                if isinstance(content_obj, dict) and (
                    _su is None or isinstance(_su, str)
                ):
                    content_obj["state_updates"] = {}

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
        # Capture pre-turn state for the same-turn-confirmation guard
        # applied later in this method. The design requires the user to
        # confirm a problem statement that was presented on a PRIOR turn —
        # never one that was first written this turn. The INQUIRY_TEMPLATE
        # instructs the LLM accordingly ("Never set user_confirmed_-
        # investigation=True on the same turn you first present the
        # problem statement"), but LLMs are stochastic and the rule was
        # observed to be violated on first-turn cases with explicit
        # "please investigate" phrasing. This local makes the invariant
        # enforceable independently of prompt compliance.
        _statement_existed_before_turn = bool(
            case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
        )

        # INV-20 mutation watcher input: snapshot the path-selection signals
        # BEFORE this turn's updates land, so we can detect changes that
        # invalidate a previously-confirmed Gate 2.
        _old_preliminary_urgency = case.inquiry.preliminary_urgency

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

        # INV-20: if path-selection signals have changed (level or
        # is_ongoing), invalidate any existing path_selection so Gate 2
        # re-fires with the updated recommendation. The mutation watcher is
        # deterministic and runs every turn — does NOT depend on the LLM
        # noticing the change. See investigation-gates design (slice 2).
        if (
            _inquiry_path_signals_changed(
                _old_preliminary_urgency, case.inquiry.preliminary_urgency
            )
            and case.path_selection is not None
        ):
            logger.info(
                f"Path-selection signals changed for case {case.case_id} "
                f"(old_level={_old_preliminary_urgency.level if _old_preliminary_urgency else None}, "
                f"new_level={case.inquiry.preliminary_urgency.level if case.inquiry.preliminary_urgency else None}; "
                f"old_ongoing={_old_preliminary_urgency.is_ongoing if _old_preliminary_urgency else None}, "
                f"new_ongoing={case.inquiry.preliminary_urgency.is_ongoing if case.inquiry.preliminary_urgency else None}) "
                f"— clearing path_selection for re-evaluation (INV-20)"
            )
            case.path_selection = None

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

        # Check if LLM detected user confirmation of the problem statement.
        # Same-turn-confirmation guard: the proposed_problem_statement must
        # have existed BEFORE this turn — otherwise the LLM is trying to
        # write the statement and confirm it in one shot, which collapses
        # the User-Agent Handshake. See the captured
        # _statement_existed_before_turn at the top of this method.
        if (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and _statement_existed_before_turn
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
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and not _statement_existed_before_turn
        ):
            # LLM tried to set the problem statement AND confirm investigation
            # in the same turn — design forbids this (the user must see the
            # statement first, then confirm on a subsequent turn). Refuse
            # the transition; the agent will re-present the statement on
            # the next turn. Logged so drift is observable in telemetry.
            #
            # Set handshake_deferred_at_turn so the next turn's
            # context_builder switches from NOT_YET_CONFIRMED ("don't re-
            # propose") to HANDSHAKE_DEFERRED ("re-present and ask"), and
            # so process_turn deterministically emits confirmation
            # suggestions regardless of LLM compliance.
            case.inquiry.handshake_deferred_at_turn = case.current_turn
            inquiry_handshake_deferred_total.inc()
            logger.warning(
                f"Same-turn-confirmation guard rejected INQUIRY→INVESTIGATING "
                f"for case {case.case_id}: LLM emitted "
                f"user_confirmed_investigation=True on the same turn that "
                f"first set proposed_problem_statement. Deferring to next turn.",
                extra={
                    "case_id": case.case_id,
                    "turn": case.current_turn,
                    "statement_preview": case.inquiry.proposed_problem_statement[:80],
                },
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
            # v3: knowledge_resolution received during INQUIRY is stored
            # for visibility but is NOT a transition trigger. The LLM
            # should emit knowledge_resolution during INVESTIGATING (when
            # the user confirms a runbook fix worked), not INQUIRY.
            logger.warning(
                "Case %s: knowledge_resolution emitted during INQUIRY; "
                "v3 expects this during INVESTIGATING (after problem confirmation). "
                "Storing for audit but not transitioning.",
                case.case_id,
            )

        # Post-010 (strict evidence model): NO evidence creation during
        # INQUIRY. Evidence presupposes a confirmed claim; during INQUIRY
        # the claim is still being formed. Uploaded files persist in
        # ``case.uploaded_files`` with their preprocessing artifacts
        # (summary, structural_index, data_type, coverage timestamps);
        # the LLM evaluates them and emits ``evidence_to_add`` once the
        # case transitions to INVESTIGATING.
        # See docs/architecture/investigation-engine/
        # evidence-driven-investigation-framework.md §5.

        # Gate 2 setup: compute path_selection now that Gate 1 has passed and
        # urgency signals are populated. Idempotent — only fires when the
        # signals are available and path_selection has not already been
        # computed (or was just cleared by the INV-20 mutation watcher
        # earlier in this method). The resulting PathSelection has
        # user_confirmed=False; Gate 2 surfaces it to the user for
        # confirmation before INQUIRY -> INVESTIGATING can fire (INV-19).
        _compute_inquiry_path_selection(case)

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

        # 1a. Save Root Cause Conclusion
        # Must happen before milestone processing so the KB pre-fetch below
        # can use the conclusion text in the same turn.
        if hasattr(updates, "root_cause_conclusion") and updates.root_cause_conclusion:
            rcc = updates.root_cause_conclusion
            case.root_cause_conclusion = RootCauseConclusion(
                root_cause=rcc.root_cause,
                mechanism=rcc.mechanism,
                evidence_basis=rcc.evidence_ids,
                likelihood=rcc.likelihood,
                confidence_level=ConfidenceLevel.from_score(rcc.likelihood),
            )

        # 1b. v3 KB-Resolution signal: same-turn milestone collapse.
        # When the user confirms a runbook fix worked, the LLM emits
        # `knowledge_resolution` alongside `root_cause_conclusion`,
        # `solutions_to_add`, and the gate milestones (`solution_accepted`).
        # The standard ProposedTransition handshake (handled later in the
        # turn) recognizes the user's confirmation as the disposition
        # acknowledgment. We store the resolution signal here for metrics
        # and audit. See investigation-lifecycle-logic.md §1.2 →
        # "KB-Resolution Path (Same-Turn Variant)".
        if hasattr(updates, "knowledge_resolution") and updates.knowledge_resolution:
            kr = updates.knowledge_resolution
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=kr.match_id,
                match_type=kr.match_type,
                solution_applied=kr.solution_applied,
                user_confirmation=kr.user_confirmation,
            )
            metadata["knowledge_resolution_signalled"] = True
            logger.info(
                "Case %s: knowledge_resolution signalled during INVESTIGATING; "
                "match_id=%s, type=%s. Standard ProposedTransition handshake handles disposition.",
                case.case_id,
                kr.match_id,
                kr.match_type,
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

            # INV-21: on a mitigation-first case where mitigation has been
            # verified but the user has not yet confirmed continuing to RCA
            # (Gate 3 pending), reject RCA-side milestone updates. Prevents
            # the engine from silently restarting RCA when the user might
            # have wanted to close as mitigation-sufficient. Stage-gate
            # mitigation milestones are exempt (a follow-up mitigation
            # cycle is allowed via the existing re-entry mechanism).
            gate3_blocks_rca_milestones = _gate3_is_pending(case)
            rca_side_milestones = {"root_cause_identified"}

            for field in milestone_fields:
                if getattr(m, field, False):
                    # Guard: reject stage-gate milestones if no pending action
                    if field in stage_gate_fields and not has_pending_action:
                        logger.warning(
                            f"Rejected stage-gate milestone '{field}' for case "
                            f"{case.case_id}: no pending ProposedAction exists"
                        )
                        continue
                    # INV-21 guard: reject RCA-side milestones until Gate 3 passes
                    if gate3_blocks_rca_milestones and field in rca_side_milestones:
                        logger.warning(
                            f"INV-21: Rejected RCA-side milestone '{field}' for "
                            f"case {case.case_id} — mitigation is verified but "
                            f"the user has not yet chosen to continue with RCA "
                            f"(Gate 3 pending)."
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

            # KB pre-fetch: when root_cause_identified is newly completed,
            # search KB for remediation procedures matching the root cause.
            if "root_cause_identified" in metadata.get("milestones_completed", []):
                root_cause_query = None
                if case.root_cause_conclusion and getattr(
                    case.root_cause_conclusion, "root_cause", None
                ):
                    root_cause_query = case.root_cause_conclusion.root_cause
                elif case.working_conclusion and getattr(
                    case.working_conclusion, "statement", None
                ):
                    root_cause_query = case.working_conclusion.statement
                if root_cause_query:
                    await self._prefetch_kb_context(
                        case, root_cause_query, "root_cause"
                    )

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
        # Post-010: every Evidence row comes from the LLM declaring an
        # `evidence_to_add` entry on this turn. Files uploaded earlier in
        # the turn live on `uploaded_files` only — they become Evidence
        # only when the LLM extracts a claim-relevant slice and records
        # it here.
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
            # Post-010: source_file_id is declared by the LLM directly on
            # EvidenceToAdd. The Pydantic ``_source_file_required_unless_user_description``
            # validator on EvidenceToAdd has already enforced the
            # ``evidence_source_invariant``: by the time we get here,
            # ``ev_item.source_file_id is None`` implies
            # ``source_type == USER_DESCRIPTION``. We pass the value
            # through unchanged — no turn-file fallback, because that
            # would silently mis-attribute a chat-extracted USER_DESCRIPTION
            # quote to whatever file happens to be in the same turn.
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

                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    extract=ev_item.extract,
                    category=ev_item.category,
                    source_type=ev_item.source_type,
                    source_file_id=ev_item.source_file_id,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                )
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                logger.info(
                    f"Created evidence: {ev.evidence_id} | "
                    f"category={ev.category.value}, source_type={ev.source_type.value}, "
                    f"source_file_id={ev.source_file_id}, "
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

        # 6. Journal Entries (append-only investigation memory)
        if hasattr(updates, "journal_entries") and updates.journal_entries:
            for je_item in updates.journal_entries:
                entry = JournalEntry(
                    turn=case.current_turn,
                    entry_type=je_item.entry_type,
                    content=je_item.content[:200],
                    evidence_id=je_item.evidence_id,
                    hypothesis_id=je_item.hypothesis_id,
                )
                case.investigation_journal.append(entry)
            logger.info(
                f"Case {case.case_id}: added {len(updates.journal_entries)} journal entries "
                f"(total: {len(case.investigation_journal)})"
            )

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
            - File uploads create only ``UploadedFile`` rows at intake; no
              Evidence is auto-created. Preprocessing artifacts (summary,
              structural_index, data_type, coverage_*) live on the file row.
            - During INQUIRY no Evidence rows exist — the
              ``InquiryStateUpdate`` schema does not carry ``evidence_to_add``
              and the engine does not synthesize Evidence on transition.
              The LLM reads files via ``<uploaded_file>`` context blocks.
            - Evidence is born during INVESTIGATING: the LLM extracts
              claim-anchored slices via ``evidence_to_add``, each carrying a
              category (symptom / causal / mitigation / solution_evidence)
              and a ``source_file_id`` back to the originating file.
            - Milestones derive from evidence categories as those rows are
              created turn-by-turn, not retroactively at the transition.

        Reference: ``docs/architecture/investigation-engine/
        evidence-driven-investigation-framework.md`` §5.
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

        # Outcome telemetry for INV-01: count cases that reached
        # INVESTIGATING after a prior same-turn-confirmation guard fire.
        # Divided by inquiry_handshake_deferred_total this gives the
        # recovery ratio — sustained ratio drops are the signal that
        # the deferral->recovery path has silently broken.
        if case.inquiry.handshake_deferred_at_turn is not None:
            inquiry_handshake_recovered_total.inc()

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
            # Without this, path selection receives Temporal:None and the
            # router falls back to the ROOT_CAUSE default (auto_selected=False)
            # rather than matching a definitive matrix row.
            if pu.is_ongoing:
                verification_kwargs["temporal_state"] = TemporalState.ONGOING
            else:
                verification_kwargs["temporal_state"] = TemporalState.HISTORICAL

        case.problem_verification = ProblemVerification(**verification_kwargs)

        # Determine path selection. Normally Gate 2 has already populated
        # case.path_selection during inquiry (see _compute_inquiry_path_selection
        # in _apply_inquiry_updates), in which case we preserve the
        # user-confirmed choice — recomputing here would overwrite
        # user_confirmed and lose any override the user made via Gate 2.
        # If path_selection is somehow missing at transition time (e.g.,
        # tests that bypass inquiry, or a path-selection signal that arrived
        # too late during inquiry), fall back to computing it now with
        # user_confirmed=False; the case will then be in a transient state
        # where Gate 2 is still pending in storage but the case has already
        # transitioned, which the engine handles defensively elsewhere.
        if case.path_selection is None:
            case.path_selection = determine_investigation_path(
                case.problem_verification
            )
            logger.warning(
                f"Case {case.case_id}: path_selection was None at transition time; "
                f"recomputed from problem_verification (path={case.path_selection.path.value}, "
                f"user_confirmed=False). Gate 2 should normally have populated this during inquiry."
            )
        else:
            logger.info(
                f"Case {case.case_id}: preserving inquiry-confirmed path_selection "
                f"(path={case.path_selection.path.value}, "
                f"user_confirmed={case.path_selection.user_confirmed})"
            )

        # Post-010: no retroactive milestone attribution at INQUIRY→
        # INVESTIGATING. INQUIRY no longer creates Evidence rows, so
        # there is no INQUIRY-phase evidence to back-fill milestones for.
        # KB pre-fetch: search for runbooks matching the confirmed problem.
        # Deterministic, code-level — not an LLM tool call decision.
        # Results are stored on the case and injected into context by
        # context_builder so the LLM sees relevant runbooks from turn 1.
        await self._prefetch_kb_context(case, case.description, "symptom")

    async def _prefetch_kb_context(
        self,
        case: "Case",
        query: str,
        trigger: str,
    ) -> None:
        """Search KB for runbooks matching the query, store on case.

        Args:
            case: Case to update
            query: Search query (problem statement or root cause)
            trigger: What triggered this search ("symptom" or "root_cause")
        """
        if not self.knowledge_service:
            return

        try:
            results = await self.knowledge_service.search_knowledge(
                query=query, limit=3
            )
            if results:
                case.kb_context = [
                    {
                        "title": r.title,
                        "summary": r.snippet,
                        "score": r.score,
                        "type": getattr(r, "document_type", "runbook"),
                        "trigger": trigger,
                    }
                    for r in results
                    if r.score >= 0.3  # Minimum relevance threshold
                ]
                if case.kb_context:
                    logger.info(
                        f"KB pre-fetch ({trigger}): {len(case.kb_context)} matches "
                        f"for case {case.case_id}"
                    )
                else:
                    case.kb_context = None
        except Exception:
            logger.warning(
                f"KB pre-fetch ({trigger}) failed for case {case.case_id}",
                exc_info=True,
            )

    async def _check_automatic_transitions(
        self, case: Case, metadata: dict[str, Any], user_message: str = ""
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions (non-terminal):
        - INQUIRY -> INVESTIGATING when decided_to_investigate=True

        v3: INQUIRY -> RESOLVED edge removed. KB-driven cases route through
        INVESTIGATING via same-turn milestone collapse — see
        docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        §1.2 INVESTIGATING -> RESOLVED -> KB-Resolution Path.

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
            # KB-Resolution Path same-turn collapse (§1.2). When the LLM
            # emits ``knowledge_resolution`` (user confirmed a runbook fix
            # worked) alongside ``ProposedTransition``, the user's
            # confirmation message IS the disposition acknowledgment —
            # no separate confirmation turn required. This is the only
            # path that fires confirm_pending_transition in the same turn
            # the proposal was written; every other transition follows
            # the standard 2-turn handshake.
            #
            # Gating on BOTH ``transition_proposed_this_turn`` (set by
            # propose_transition) AND ``knowledge_resolution_signalled``
            # (set in _apply_investigation_updates when ``updates.knowledge_-
            # resolution`` is present) ensures this special path fires
            # only on the well-scoped KB-resolution scenario. All other
            # ProposedTransition emissions still flow through the
            # 2-turn handshake via the elif branch below.
            if metadata.get("transition_proposed_this_turn", False) and metadata.get(
                "knowledge_resolution_signalled", False
            ):
                from faultmaven.core.investigation.terminal_transitions import (
                    confirm_pending_transition,
                )

                confirm_pending_transition(case, case.user_id)
                metadata["status_transitioned"] = True
                logger.info(
                    f"KB-Resolution same-turn collapse: confirmed "
                    f"pending transition for case {case.case_id} "
                    f"(user's runbook-confirmation message covers "
                    f"both signals — §1.2 KB-Resolution Path)"
                )
            # Don't confirm a transition that was just proposed in this same turn
            elif metadata.get("transition_proposed_this_turn", False):
                logger.info(
                    f"Skipping confirmation check - transition was just proposed this turn"
                )
            elif case.pending_transition.get("needs_info"):
                # User was told what's missing and has now responded.
                # Re-evaluate readiness: did the LLM actually capture root
                # cause / solution from what the user provided?
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_resolution_readiness,
                    cancel_pending_transition,
                )

                readiness = assess_resolution_readiness(case)

                if readiness.verdict == readiness.READY:
                    # Requirements met — clear needs_info, show confirmation
                    case.pending_transition["needs_info"] = False
                    metadata["resolution_ready_for_confirmation"] = True
                    logger.info(
                        f"Case {case.case_id}: needs_info resolved, "
                        f"requirements met — presenting confirmation"
                    )
                elif readiness.verdict == readiness.SUGGEST_CLOSE:
                    # Still fundamentally lacking — suggest Close instead
                    cancel_pending_transition(case)
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = readiness.message
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied, "
                        f"suggesting Close instead (missing: {readiness.missing})"
                    )
                else:
                    # NEEDS_INFO still — user was already asked once and
                    # didn't (or couldn't) provide the missing info.
                    # Don't loop asking again. Pivot to suggest Close.
                    cancel_pending_transition(case)
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = (
                        "I understand you don't have additional details. "
                        "Without a documented solution, I can't mark this "
                        "as **resolved**.\n\n"
                        "You can **close** the case instead — this preserves "
                        "the root cause analysis and investigation history."
                    )
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied after "
                        f"second ask, pivoting to suggest Close "
                        f"(missing: {readiness.missing})"
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
        # v3: INQUIRY → RESOLVED edge removed. KB-driven cases route through
        # INVESTIGATING via the same-turn milestone collapse documented in
        # docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        # §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path. Confirming the
        # problem statement is mandatory even when a runbook applies cleanly.
        #
        # INV-19 (Gate 2): INQUIRY -> INVESTIGATING also requires
        # path_selection.user_confirmed=True. Gate 1 (problem statement
        # confirmation) opens the path-selection prompt; Gate 2 commits to
        # mitigation_first or root_cause. The transition only fires once
        # both gates have passed.
        if case.status == CaseStatus.INQUIRY:
            gate1_passed = case.inquiry.decided_to_investigate or (
                case.inquiry.problem_statement_confirmed
                and case.inquiry.problem_confirmation
            )
            gate2_passed = (
                case.path_selection is not None and case.path_selection.user_confirmed
            )
            if gate1_passed and gate2_passed:
                await self._transition_to_investigating(case)
                metadata["status_transitioned"] = True
                case.action_history.append(
                    CaseAction(
                        from_status=old_status,
                        to_status=CaseStatus.INVESTIGATING,
                        triggered_by="system",
                        reason="Problem confirmed and investigation path selected",
                    )
                )
                return case
            elif gate1_passed and not gate2_passed:
                # Gate 1 passed but Gate 2 hasn't — leave the case in INQUIRY
                # so the engine surfaces the path-selection prompt + Gate 2
                # COOPERATIVE suggestions on this turn. INV-19 holds.
                logger.info(
                    f"Case {case.case_id}: Gate 1 passed but Gate 2 pending "
                    f"(path_selection={'present' if case.path_selection else 'None'}, "
                    f"user_confirmed={case.path_selection.user_confirmed if case.path_selection else None}). "
                    f"Staying in INQUIRY until path is confirmed."
                )

        # 2. Handle ProposedTransition from LLM response (User-Agent Handshake)
        # The LLM proposes a terminal transition; we store it pending.
        # Auto-transition on solution_verified is REMOVED — all terminal
        # transitions require explicit user confirmation.
        response_obj = metadata.get("response_obj")
        if response_obj and hasattr(response_obj, "state_updates"):
            proposed = getattr(response_obj.state_updates, "proposed_transition", None)
            if proposed:
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_closure_readiness,
                    assess_resolution_readiness,
                    propose_transition,
                )

                # The LLM emits only to_status (and optional evidence_ids).
                # Engine handles everything else: closure_reason is derived
                # inside propose_transition; summary is built programmatically
                # via the same helpers the UI dropdown path uses, so all
                # three trigger paths produce identical confirmation prompts.
                #
                # When the LLM proposes RESOLVED, run the same readiness
                # check the UI dropdown path uses so the user sees a
                # coherent prompt + suggestion pair:
                #   SUGGEST_CLOSE → pivot to CLOSED (close suggestion pair)
                #   NEEDS_INFO    → keep RESOLVED but flag needs_info; the
                #                   response builder overrides agent_response
                #                   with the readiness message
                #   READY         → propose RESOLVED with confirmation prompt
                effective_to_status = proposed.to_status
                needs_info_message: str | None = None

                if proposed.to_status == "resolved":
                    readiness = assess_resolution_readiness(case)
                    if readiness.verdict == readiness.SUGGEST_CLOSE:
                        effective_to_status = "closed"
                        summary = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}); "
                            f"pivoting to CLOSED."
                        )
                    elif readiness.verdict == readiness.NEEDS_INFO:
                        summary = readiness.message
                        needs_info_message = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=NEEDS_INFO (missing: {readiness.missing}); "
                            f"keeping RESOLVED intent with needs_info flag."
                        )
                    else:
                        summary = _build_resolution_confirmation(case)
                else:  # closed
                    summary = assess_closure_readiness(case).message

                propose_transition(
                    case=case,
                    to_status=effective_to_status,
                    summary=summary,
                    evidence_ids=getattr(proposed, "evidence_ids", None),
                )
                if needs_info_message is not None:
                    case.pending_transition["needs_info"] = True
                    # The response builder reads this to override the LLM's
                    # agent_response with the readiness message, matching the
                    # UI dropdown path's first-pass behavior.
                    metadata["resolution_needs_info_first_pass"] = True
                    metadata["resolution_needs_info_message"] = needs_info_message
                metadata["transition_proposed"] = True
                # Override LLM-emitted suggestions with the canonical
                # confirm/decline pair, so all three trigger paths
                # (UI click, NL via this branch, agent-initiated) produce
                # the same structured COOPERATIVE confirmation UX. The
                # response builder consumes metadata["override_suggestions"]
                # at the final assembly point.
                if effective_to_status == "resolved":
                    metadata["override_suggestions"] = (
                        _resolution_confirmation_suggestions()
                    )
                else:  # closed
                    metadata["override_suggestions"] = _close_confirmation_suggestions()
                logger.info(
                    f"Agent proposed transition → {effective_to_status} "
                    f"(pending user confirmation)"
                )

        return case

    def _user_confirms_transition(self, user_message: str) -> bool:
        """Fallback check for typed confirmations (not COOPERATIVE clicks).

        COOPERATIVE suggestion clicks now carry intent metadata and route
        through IntentType.CONFIRMATION deterministically. This matcher
        is a safety net for users who type instead of clicking.

        Uses a 100-char length guard: short messages are direct responses
        to the confirmation prompt; longer messages likely contain context
        that should go through normal LLM processing.
        """
        if not user_message:
            return False
        msg = user_message.strip().lower()
        if len(msg) > 100:
            return False
        confirm_patterns = [
            "yes",
            "yeah",
            "yep",
            "yup",
            "correct",
            "confirmed",
            "confirm",
            "approve",
            "approved",
            "ok",
            "okay",
            "sure",
            "absolutely",
            "go ahead",
            "go for it",
            "do it",
            "please do",
            "proceed",
            "mark as resolved",
            "mark it as resolved",
            "resolve it",
            "close it",
            "that's right",
            "that's correct",
            "sounds good",
            "looks good",
            "lgtm",
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

    # v3: `_check_fast_track_resolution` and `KB_FAST_TRACK_THRESHOLD` removed.
    # KB-driven cases route through INVESTIGATING via same-turn milestone
    # collapse. See indicator-resolution.md + investigation-lifecycle-logic.md
    # §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path. The collapse is
    # applied in `_apply_investigation_updates`'s `knowledge_resolution`
    # branch (gate milestones set there); RootCauseConclusion + Solution
    # are populated from the LLM's structured emissions in the same turn.

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
            content_type=attachment.get("content_type"),
            content_hash=attachment.get("content_hash"),
            uploaded_at_turn=turn_number,
            uploaded_at=datetime.now(UTC),
            upload_source=attachment.get("source_type", "file_upload"),
            storage_ref=attachment.get("s3_uri", attachment.get("file_id", "unknown")),
        )

        return uploaded_file

    # Post-010: auto-Evidence creation at file-upload time is gone.
    # Under the strict evidence model, files are data (uploaded_files)
    # and evidence is a claim-anchored extract that the LLM produces
    # via evidence_to_add during INVESTIGATING. The previous
    # ``_create_evidence_from_attachment`` and ``_infer_evidence_category``
    # helpers (auto-DOCUMENT path) have been removed.

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
        repair_pattern: str | None = None,
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
            outcome=outcome,
            user_message_summary=self._summarize_text(user_message, 200),
            agent_response_summary=self._summarize_text(agent_response, 500),
            system_feedback=system_feedback,
            momentum=momentum,
            blocked_reasons=blocked_reasons or [],
            next_steps=next_steps or [],
            repair_pattern=repair_pattern,
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
                outcome=TurnOutcome.CONVERSATION,
                user_message_summary=self._summarize_text(user_message, 200),
                agent_response_summary=self._summarize_text(agent_response, 500),
            )
        )

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
