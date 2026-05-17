"""Terminal Transition Handler

Implements the User-Agent Handshake pattern for terminal state transitions.

All terminal transitions (RESOLVED, CLOSED) require explicit user confirmation.
The agent proposes a transition via ProposedTransition in its response schema,
and the system holds it pending until the user confirms in the next turn.

Design Decision (Issue B):
- solution_verified is a collaborative state, not system-determined.
- The LLM's interpretation of "it works" can be wrong (user may mean
  "this command works" not "the whole system is fixed").
- Terminal transitions are irreversible, so false positives are costly.

Flow:
1. Agent detects resolution conditions and includes ProposedTransition in response
2. System stores pending_transition on the case (does NOT execute)
3. Agent's response message asks user to confirm
4. Next turn: if user confirms, system executes transition + sets solution_verified
5. If user declines, pending_transition is cleared

Reference: investigation-lifecycle-logic.md Section 1.4
"""

import logging
from datetime import UTC, datetime
from typing import Any, Optional

from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseStatus,
)

logger = logging.getLogger(__name__)


def derive_closure_reason(case: "Case") -> str:
    """Engine-only derivation of closure_reason from case state.

    Returns one of VALID_CLOSURE_REASONS based on (case.status,
    progress.mitigation_verified). Called when proposing CLOSED transition.

    The LLM never authors closure_reason; it's purely engine-derived from
    structured case state.
    """
    from faultmaven.modules.case.domain.models import CaseStatus

    if case.status == CaseStatus.INQUIRY:
        return "inquiry_only"
    if case.progress and getattr(case.progress, "mitigation_verified", False):
        return "mitigation_sufficient"
    return "closed_after_investigation"


def propose_transition(
    case: Case,
    to_status: str,
    summary: str,
    evidence_ids: Optional[list] = None,
) -> None:
    """
    Store a pending transition proposal on the case.

    The transition is NOT executed. It is held pending until the user
    confirms in the next turn.

    Engine-derived: closure_reason is derived from case state via
    ``derive_closure_reason()`` for CLOSED transitions; None for RESOLVED.
    The LLM does not author closure_reason — it's structured engine state.

    Args:
        case: Case to propose transition for
        to_status: Target status ("resolved" or "closed")
        summary: Summary presented to user for confirmation
        evidence_ids: Evidence IDs supporting the proposal
    """
    pending: dict = {
        "to_status": to_status,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    if to_status == "closed":
        pending["closure_reason"] = derive_closure_reason(case)
    case.pending_transition = pending
    logger.info(
        f"Transition proposed for case {case.case_id}: → {to_status} "
        f"(pending user confirmation)"
    )


def confirm_pending_transition(case: Case, user_id: str) -> bool:
    """
    Execute a pending transition after user confirmation.

    Returns True if transition was executed, False if no pending transition
    or if the transition target is unknown.

    Raises:
        ValueError: If case is in an invalid state for the requested transition.

    Args:
        case: Case with pending_transition
        user_id: User confirming the transition
    """
    if not hasattr(case, "pending_transition") or not case.pending_transition:
        return False

    pending = case.pending_transition
    to_status = pending["to_status"]

    if to_status == "resolved":
        _execute_resolved_transition(case, user_id)
    elif to_status == "closed":
        _execute_closed_transition(case, user_id, pending["closure_reason"])
    else:
        logger.error(f"Unknown pending transition target: {to_status}")
        case.pending_transition = None
        return False

    # Clear pending transition only after successful execution
    case.pending_transition = None
    return True


def cancel_pending_transition(case: Case) -> bool:
    """
    Cancel a pending transition (user declined).

    Returns True if a pending transition was cleared.
    """
    if hasattr(case, "pending_transition") and case.pending_transition:
        logger.info(
            f"Pending transition cancelled for case {case.case_id}: "
            f"→ {case.pending_transition['to_status']} (user declined)"
        )
        case.pending_transition = None
        return True
    return False


def _execute_resolved_transition(case: Case, user_id: str):
    """Execute INVESTIGATING → RESOLVED after user confirmation.

    Raises:
        ValueError: If case is not in INVESTIGATING status.
    """
    if case.status != CaseStatus.INVESTIGATING:
        raise ValueError(
            f"Cannot resolve case {case.case_id}: status is {case.status}, "
            f"expected INVESTIGATING"
        )

    logger.info(
        f"User {user_id} confirmed resolution for case {case.case_id}. "
        f"Executing INVESTIGATING → RESOLVED transition."
    )

    # Gap #8: Warn if resolving with no evidence (non-blocking)
    if not case.evidence:
        logger.warning(
            f"Case {case.case_id} resolving with zero evidence records. "
            f"User confirmed resolution but no evidence was collected during investigation.",
            extra={"case_id": case.case_id, "metric": "case.resolved_without_evidence"},
        )

    # Set solution milestones since user confirmed resolution.
    # Must respect ordering: proposed → accepted → verified.
    # The milestone pipeline may not have set these if the user resolved
    # via dropdown/NLP before the LLM reached the TREATMENT stage.
    if not case.progress.solution_proposed:
        case.progress.solution_proposed = True
    if not case.progress.solution_accepted:
        case.progress.solution_accepted = True
    case.progress.solution_verified = True

    now = datetime.now(UTC)

    # Mark any remaining pending ProposedActions as accepted and create audit records.
    # This covers revised fixes proposed during the TREATMENT failure path: when a fix
    # fails and the LLM proposes a revised solution (SolutionToAdd → ProposedAction),
    # the user executes it and the case resolves via ProposedTransition rather than a
    # stage-gate milestone. Because solution_accepted is already True, no stage-gate
    # fires and _apply_stage_gate_side_effects is never called for the revised action.
    for action in case.proposed_actions:
        if action.status == "pending":
            action.status = "accepted"
            case.action_attempts.append(
                ActionAttempt(
                    action_id=action.action_id,
                    user_message="Resolution confirmed by user",
                    submitted_at=now,
                    compliance_detected=True,
                    compliance_confidence=1.0,
                )
            )
            logger.info(
                f"Marked pending ProposedAction {action.action_id} as accepted "
                f"on resolution of case {case.case_id}"
            )
    case.atomic_update(
        status=CaseStatus.RESOLVED,
        resolved_at=now,
        closed_at=now,
        # closure_reason is None for RESOLVED — resolution itself is the
        # categorization. Sub-categorization would be redundant.
    )
    case.action_history.append(
        CaseAction(
            from_status=CaseStatus.INVESTIGATING,
            to_status=CaseStatus.RESOLVED,
            triggered_at=now,
            triggered_by=user_id,
            reason="User confirmed resolution",
        )
    )

    logger.info(f"Case {case.case_id} transitioned to RESOLVED (terminal state)")


def _execute_closed_transition(case: Case, user_id: str, closure_reason: str):
    """Execute → CLOSED after user confirmation.

    closure_reason is the engine-derived enum value (one of
    VALID_CLOSURE_REASONS). Caller is `confirm_pending_transition`,
    which reads it from pending_transition where `propose_transition`
    placed it via `derive_closure_reason`.

    Raises:
        ValueError: If case is not in INVESTIGATING or INQUIRY status.
    """
    from_status = case.status
    if from_status not in (CaseStatus.INVESTIGATING, CaseStatus.INQUIRY):
        raise ValueError(
            f"Cannot close case {case.case_id}: status is {from_status}, "
            f"expected INVESTIGATING or INQUIRY"
        )

    logger.info(
        f"User {user_id} confirmed closure for case {case.case_id}. "
        f"Executing {from_status.value} → CLOSED transition."
    )

    now = datetime.now(UTC)
    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=now,
        closure_reason=closure_reason,
    )
    case.action_history.append(
        CaseAction(
            from_status=from_status,
            to_status=CaseStatus.CLOSED,
            triggered_at=now,
            triggered_by=user_id,
            reason=f"User confirmed closure ({closure_reason})",
        )
    )

    logger.info(f"Case {case.case_id} transitioned to CLOSED (terminal state)")


# ============================================================
# RESOLUTION READINESS ASSESSMENT
# ============================================================


class ResolutionReadiness:
    """Assessment of whether a case is ready to be marked as RESOLVED.

    Three possible outcomes:
    - READY: Case has enough information for resolution (root cause + solution).
    - NEEDS_INFO: Case is partially ready but missing key details. Ask user to provide them.
    - SUGGEST_CLOSE: Case lacks fundamental information. Suggest CLOSED instead.
    """

    READY = "ready"
    NEEDS_INFO = "needs_info"
    SUGGEST_CLOSE = "suggest_close"

    def __init__(self, verdict: str, message: str, missing: list[str]):
        self.verdict = verdict
        self.message = message
        self.missing = missing


def assess_resolution_readiness(case: "Case") -> ResolutionReadiness:
    """Check whether a case meets minimum criteria for RESOLVED status.

    Minimum criteria for RESOLVED:
    - Root cause identified (root_cause_conclusion OR working_conclusion with high likelihood)
    - At least one solution proposed or applied
    - Problem verification exists (symptom_statement)

    If the case has none of these, suggest CLOSED instead.
    If the case has some but not all, ask user to provide missing information.

    Args:
        case: Case being assessed for resolution readiness

    Returns:
        ResolutionReadiness with verdict, user-facing message, and missing items list
    """
    missing = []

    # Check 1: Problem verification (basic — should always exist if INVESTIGATING)
    has_problem = bool(
        case.problem_verification
        and getattr(case.problem_verification, "symptom_statement", None)
    )
    if not has_problem:
        missing.append("problem statement")

    # Check 2: Root cause identified
    has_root_cause = bool(
        case.root_cause_conclusion
        and getattr(case.root_cause_conclusion, "root_cause", None)
    )
    has_working_conclusion = bool(
        case.working_conclusion
        and getattr(case.working_conclusion, "statement", None)
        and getattr(case.working_conclusion, "likelihood", 0) >= 0.6
    )
    has_cause = has_root_cause or has_working_conclusion
    if not has_cause:
        missing.append("root cause")

    # Check 3: At least one solution
    has_solution = bool(case.solutions and len(case.solutions) > 0)
    if not has_solution:
        missing.append("solution")

    # Check 4: Any evidence collected
    has_evidence = bool(case.evidence and len(case.evidence) > 0)
    if not has_evidence:
        missing.append("evidence")

    # Determine verdict
    critical_missing = [m for m in missing if m in ("root cause", "solution")]

    if not missing:
        # Everything present
        return ResolutionReadiness(
            verdict=ResolutionReadiness.READY,
            message="",
            missing=[],
        )

    if len(critical_missing) >= 2 and not has_evidence:
        # No root cause, no solution, no evidence — this isn't a resolved case
        return ResolutionReadiness(
            verdict=ResolutionReadiness.SUGGEST_CLOSE,
            message=(
                "This case doesn't have enough information to be marked as **resolved**. "
                "There's no identified root cause, no solution on record, and no evidence collected.\n\n"
                "If the issue is no longer relevant, you can **close** the case instead "
                "(abandoned, escalated, or mitigation sufficient).\n\n"
                "If the issue was actually resolved, please describe:\n"
                "1. What was the root cause?\n"
                "2. What fixed it?"
            ),
            missing=missing,
        )

    if critical_missing:
        # Partially ready — ask for the missing pieces
        missing_desc = []
        if "root cause" in critical_missing:
            missing_desc.append("- **Root cause**: What caused the problem?")
        if "solution" in critical_missing:
            missing_desc.append("- **Solution**: What action resolved the issue?")

        return ResolutionReadiness(
            verdict=ResolutionReadiness.NEEDS_INFO,
            message=(
                "Before I can mark this as resolved, I need a bit more detail:\n\n"
                + "\n".join(missing_desc)
                + "\n\nPlease provide this information so I can properly document the resolution."
            ),
            missing=missing,
        )

    # Non-critical items missing (just evidence or problem statement) — still ready
    return ResolutionReadiness(
        verdict=ResolutionReadiness.READY,
        message="",
        missing=missing,
    )


# ============================================================
# CLOSURE READINESS ASSESSMENT
# ============================================================


class ClosureReadiness:
    """Assessment of what was accomplished during an investigation before closing.

    Two verdicts:
    - HAS_SUBSTANCE: Investigation produced meaningful work worth summarizing.
    - TRIVIAL: Investigation had minimal substance (quick close, no real work done).
    """

    HAS_SUBSTANCE = "has_substance"
    TRIVIAL = "trivial"

    def __init__(self, verdict: str, message: str):
        self.verdict = verdict
        self.message = message


def assess_closure_readiness(case: "Case") -> ClosureReadiness:
    """Summarize what was accomplished during investigation for the CLOSED confirmation prompt.

    Uses the same data points as should_generate_terminal_summary() but presents
    them as a user-facing summary rather than a boolean gate.

    The actual CLOSURE_SUMMARY report is generated only after user confirms.

    Args:
        case: Case being assessed for closure

    Returns:
        ClosureReadiness with verdict and user-facing summary message
    """
    evidence_count = len(case.evidence) if case.evidence else 0
    hypothesis_count = len(case.hypotheses) if case.hypotheses else 0
    milestones_completed = (
        len(case.progress.completed_milestones) if case.progress else 0
    )
    has_root_cause = bool(
        case.root_cause_conclusion
        and getattr(case.root_cause_conclusion, "root_cause", None)
    )
    has_solutions = bool(case.solutions and len(case.solutions) > 0)

    # Build summary parts
    parts = []
    if evidence_count > 0:
        parts.append(
            f"- **Evidence collected**: {evidence_count} item{'s' if evidence_count != 1 else ''}"
        )
    if hypothesis_count > 0:
        parts.append(f"- **Hypotheses explored**: {hypothesis_count}")
    if milestones_completed > 0:
        parts.append(f"- **Milestones completed**: {milestones_completed}")
    if has_root_cause:
        rc = getattr(case.root_cause_conclusion, "root_cause", "")
        parts.append(f"- **Root cause identified**: {rc}")
    if has_solutions:
        sol_titles = [getattr(s, "title", "Untitled") for s in case.solutions]
        parts.append(f"- **Solutions on record**: {', '.join(sol_titles)}")

    if not parts:
        return ClosureReadiness(
            verdict=ClosureReadiness.TRIVIAL,
            message=(
                "This case has minimal investigation data. "
                "Are you sure you want to close it?"
            ),
        )

    summary = "Here's what was accomplished during this investigation:\n\n" + "\n".join(
        parts
    )
    summary += "\n\nAre you sure you want to close this case without resolution?"

    return ClosureReadiness(
        verdict=ClosureReadiness.HAS_SUBSTANCE,
        message=summary,
    )


# ============================================================
# RUNBOOK READINESS ASSESSMENT
# ============================================================


class RunbookReadiness:
    """Assessment of whether a resolved case has enough data for quality runbook generation.

    This is a HIGHER bar than ResolutionReadiness. A case can be resolved with a
    brief root cause + solution title, but a runbook needs concrete commands,
    diagnostic steps, and verification procedures.

    Three verdicts:
    - READY: Case has rich enough data for all critical runbook sections.
    - NEEDS_ENRICHMENT: Case has basics but key sections will be thin. Agent
      should tell the user what's missing before attempting generation.
    - NOT_SUITABLE: Case lacks the structural data needed. Don't offer runbook.
    """

    READY = "ready"
    NEEDS_ENRICHMENT = "needs_enrichment"
    NOT_SUITABLE = "not_suitable"

    def __init__(self, verdict: str, message: str, section_coverage: dict):
        self.verdict = verdict
        self.message = message
        self.section_coverage = section_coverage  # section_name → bool


def assess_runbook_readiness(case: "Case") -> RunbookReadiness:
    """Check whether a resolved case has enough data for quality runbook generation.

    Maps case data to the 7 canonical runbook sections and checks coverage.

    Required for READY:
    - Problem Definition: symptom_statement exists
    - Root Cause Resolution: root_cause + (commands OR implementation_steps OR longterm_fix)
    - At least one solution with actionable content (commands, steps, or longterm_fix)

    Enriches quality (not required, but flagged if missing):
    - Diagnostic Steps: evidence items with summaries
    - Mitigation: action_attempts with MITIGATION type, or immediate_action
    - Verification: verification_method on any solution

    Always available (LLM generates from context):
    - Prevention, Sources
    """
    coverage = {}

    # Problem Definition ← problem_verification.symptom_statement + symptom_indicators
    has_problem_def = bool(
        case.problem_verification
        and getattr(case.problem_verification, "symptom_statement", None)
    )
    coverage["problem_definition"] = has_problem_def

    # Diagnostic Steps ← evidence summaries + hypotheses tested
    evidence_count = len(case.evidence) if case.evidence else 0
    hypothesis_count = len(case.hypotheses) if case.hypotheses else 0
    has_diagnostic_steps = evidence_count >= 1 or hypothesis_count >= 1
    coverage["diagnostic_steps"] = has_diagnostic_steps

    # Mitigation ← action_attempts with MITIGATION type, or solutions[].immediate_action
    has_mitigation = False
    if case.action_attempts:
        has_mitigation = any(
            getattr(a, "action_type", "").upper() == "MITIGATION"
            for a in case.action_attempts
        )
    if not has_mitigation and case.solutions:
        has_mitigation = any(
            getattr(s, "immediate_action", None) for s in case.solutions
        )
    coverage["mitigation"] = has_mitigation

    # Root Cause Resolution ← root_cause_conclusion + solution with actionable content
    has_root_cause = bool(
        case.root_cause_conclusion
        and getattr(case.root_cause_conclusion, "root_cause", None)
    )
    has_actionable_solution = False
    if case.solutions:
        for sol in case.solutions:
            has_commands = bool(getattr(sol, "commands", None))
            has_steps = bool(getattr(sol, "implementation_steps", None))
            has_longterm = bool(getattr(sol, "longterm_fix", None))
            if has_commands or has_steps or has_longterm:
                has_actionable_solution = True
                break
    coverage["root_cause_resolution"] = has_root_cause and has_actionable_solution

    # Verification ← solution with verification_method
    has_verification = False
    if case.solutions:
        has_verification = any(
            getattr(s, "verification_method", None) for s in case.solutions
        )
    coverage["verification"] = has_verification

    # Prevention + Sources — always LLM-generated
    coverage["prevention"] = True
    coverage["sources"] = True

    # Determine verdict
    critical_sections = ["problem_definition", "root_cause_resolution"]
    critical_missing = [s for s in critical_sections if not coverage[s]]

    enrichment_sections = ["diagnostic_steps", "mitigation", "verification"]
    enrichment_missing = [s for s in enrichment_sections if not coverage[s]]

    if not critical_missing:
        if len(enrichment_missing) <= 1:
            return RunbookReadiness(
                verdict=RunbookReadiness.READY,
                message="",
                section_coverage=coverage,
            )
        else:
            # Has the essentials but multiple enrichment sections are thin
            missing_names = {
                "diagnostic_steps": "diagnostic steps (evidence or hypotheses tested)",
                "mitigation": "mitigation procedures",
                "verification": "verification method for the solution",
            }
            missing_desc = [f"- {missing_names[s]}" for s in enrichment_missing]
            return RunbookReadiness(
                verdict=RunbookReadiness.NEEDS_ENRICHMENT,
                message=(
                    "I can generate a runbook, but some sections will be thin. "
                    "The following information would improve quality:\n\n"
                    + "\n".join(missing_desc)
                    + "\n\nWould you like to proceed anyway, or provide more detail first?"
                ),
                section_coverage=coverage,
            )

    # Critical sections missing — break root_cause_resolution into specifics
    missing_desc = []
    for section in critical_missing:
        if section == "problem_definition":
            missing_desc.append("- problem description (symptoms, error messages)")
        elif section == "root_cause_resolution":
            if not has_root_cause:
                missing_desc.append("- identified root cause")
            if not has_actionable_solution:
                missing_desc.append(
                    "- actionable fix details (commands, steps, or solution description)"
                )
    return RunbookReadiness(
        verdict=RunbookReadiness.NOT_SUITABLE,
        message=(
            "This case doesn't have enough structured data for a quality runbook. "
            "Missing:\n\n"
            + "\n".join(missing_desc)
            + "\n\nThe resolution summary should have already been generated — you can view it in the Dashboard."
        ),
        section_coverage=coverage,
    )


# ============================================================
# TERMINAL SUMMARY AUTO-GENERATION
# ============================================================


def should_generate_terminal_summary(case: "Case") -> bool:
    """Determine whether a terminal case warrants an auto-generated summary.

    Gated on investigation substance — at least one of evidence, hypotheses,
    or completed milestones. The three signals are naturally frozen for
    CLOSED cases (the API rejects new evidence/transitions in terminal
    state), so the verdict is stable across the terminal lifetime without
    needing a separate snapshot field. Terminal Q&A turns inflate
    message_count, which is why message_count is intentionally NOT part of
    the gate — including it would let conversation depth flip the verdict
    after closure.

    The case description is intentionally excluded — creation-time metadata,
    not investigation output.

    RESOLVED always generates (a confirmed solution is meaningful content
    by definition); the gate matters only for CLOSED.
    """
    evidence_count = len(case.evidence) if case.evidence else 0
    hypothesis_count = len(case.hypotheses) if case.hypotheses else 0
    milestones_completed = (
        len(case.progress.completed_milestones) if case.progress else 0
    )

    has_substance = (
        evidence_count > 0 or hypothesis_count > 0 or milestones_completed > 0
    )

    if not has_substance:
        logger.info(
            f"Skipping terminal summary for case {case.case_id}: no investigation "
            f"substance (evidence={evidence_count}, hypotheses={hypothesis_count}, "
            f"milestones={milestones_completed})"
        )
        return False

    return True


def terminal_summary_skip_reason(case: "Case") -> Optional[str]:
    """Return a human-readable note explaining why no summary was generated.

    Returns None when a summary was (or will be) generated. Returns a short
    note for CLOSED cases that fail the substance heuristic. RESOLVED cases
    always generate a summary, so this function returns None for them.

    Used by the case UI adapter to surface the skip reason in the Report tab
    when no Report row exists for a terminal case.
    """
    if case.status != CaseStatus.CLOSED:
        return None

    if should_generate_terminal_summary(case):
        return None

    return (
        "No closure summary generated: no evidence, hypotheses, "
        "or completed milestones to summarize."
    )


# ============================================================
# RUNBOOK SUGGESTION (Combines All 3 Factors)
# ============================================================


class RunbookSuggestion:
    """Result of evaluating whether to suggest runbook generation.

    Combines three factors:
    1. Content readiness (assess_runbook_readiness)
    2. User request/approval (not checked here — caller handles)
    3. No similar runbook already exists (deduplication via RunbookKnowledgeBase)
    """

    SUGGEST = "suggest"
    SUGGEST_WITH_CAVEATS = "suggest_with_caveats"
    EXISTING_COVERS = "existing_covers"
    NOT_READY = "not_ready"

    def __init__(self, verdict: str, message: str):
        self.verdict = verdict
        self.message = message


async def evaluate_runbook_suggestion(
    case: "Case",
    runbook_kb: Any = None,
) -> RunbookSuggestion:
    """Evaluate whether to suggest runbook generation for a RESOLVED case.

    Runbooks codify complete troubleshooting scenarios — root cause +
    verified solution. Only RESOLVED cases qualify. CLOSED cases (including
    those with closure_reason=mitigation_sufficient) are not eligible
    because they lack a confirmed root-cause-to-solution chain that a
    future investigator can apply.

    Checks three factors in order (cheapest first):
    1. Content readiness — does the case have enough structured data?
    2. Deduplication — is there already a similar runbook in the KB?
    3. User approval — NOT checked here; the caller presents the suggestion.

    Args:
        case: RESOLVED case to evaluate.
        runbook_kb: Optional RunbookKnowledgeBase for similarity search.
            If None, deduplication check is skipped (suggestion still based on content).
    """
    # Factor 1: Content readiness (cheap, no I/O)
    readiness = assess_runbook_readiness(case)

    if readiness.verdict == RunbookReadiness.NOT_SUITABLE:
        return RunbookSuggestion(
            verdict=RunbookSuggestion.NOT_READY,
            message=readiness.message,
        )

    # Factor 3: Deduplication (requires ChromaDB, skip if KB unavailable).
    # Local-dev configurations without ChromaDB legitimately reach here with
    # runbook_kb=None; logging at WARN keeps the silent-skip observable so a
    # production misconfiguration doesn't hide as "quietly working".
    if not runbook_kb:
        logger.warning(
            f"Runbook deduplication skipped for case {case.case_id}: "
            f"runbook_kb is not available. Duplicate runbooks may be created "
            f"if a similar one already exists in the KB.",
            extra={"case_id": case.case_id, "metric": "runbook.dedup_skipped"},
        )
    if runbook_kb:
        try:
            similar = await _find_similar_runbooks_for_case(case, runbook_kb)
            if similar:
                top_match = similar[0]
                similarity = top_match.get("similarity_score", 0)
                title = top_match.get("title", "existing runbook")

                if similarity >= 0.85:
                    return RunbookSuggestion(
                        verdict=RunbookSuggestion.EXISTING_COVERS,
                        message=(
                            f"A similar runbook already exists: **{title}** "
                            f"({similarity:.0%} match). "
                            "You can view or update it from the Dashboard Knowledge Base."
                        ),
                    )
                elif similarity >= 0.70:
                    return RunbookSuggestion(
                        verdict=RunbookSuggestion.SUGGEST_WITH_CAVEATS,
                        message=(
                            f"A partially similar runbook exists: **{title}** "
                            f"({similarity:.0%} match). "
                            "Would you like to generate a new one, or review the existing one first? "
                            "You can manage runbooks from the Dashboard."
                        ),
                    )
        except Exception as e:
            logger.warning(
                f"Runbook deduplication check failed for case {case.case_id}: {e}. "
                "Proceeding without dedup check.",
                extra={"case_id": case.case_id},
            )

    # No similar runbook found (or KB unavailable) — suggest based on content readiness
    if readiness.verdict == RunbookReadiness.NEEDS_ENRICHMENT:
        return RunbookSuggestion(
            verdict=RunbookSuggestion.SUGGEST_WITH_CAVEATS,
            message=readiness.message,
        )

    return RunbookSuggestion(
        verdict=RunbookSuggestion.SUGGEST,
        message=(
            "This case has enough detail to generate a **runbook** "
            "for the knowledge base. Would you like me to create one? "
            "You can also do this later from the Dashboard."
        ),
    )


async def _find_similar_runbooks_for_case(case: "Case", runbook_kb: Any) -> list[dict]:
    """Search for existing runbooks similar to a resolved case.

    Builds a query from case title + root cause + solution for semantic search.
    Returns list of matches with similarity_score and title.
    """
    # Build search text from case context
    parts = []
    if case.title:
        parts.append(case.title)
    if case.root_cause_conclusion:
        rc = getattr(case.root_cause_conclusion, "root_cause", None)
        if rc:
            parts.append(rc)
    if case.solutions:
        sol = case.solutions[-1]
        title = getattr(sol, "title", None)
        if title:
            parts.append(title)

    if not parts:
        return []

    query_text = " | ".join(parts)

    # Use runbook_kb.search_runbooks if available (ChromaDB vector search)
    if hasattr(runbook_kb, "search_by_text"):
        results = await runbook_kb.search_by_text(
            query_text=query_text,
            top_k=3,
            min_similarity=0.65,
        )
        return results
    elif hasattr(runbook_kb, "search_runbooks"):
        # Fallback: some implementations use search_runbooks with query text
        try:
            results = await runbook_kb.search_runbooks(
                query_text=query_text,
                top_k=3,
                min_similarity=0.65,
            )
            return (
                [
                    {
                        "similarity_score": getattr(r, "similarity_score", 0),
                        "title": getattr(r, "title", "Unknown"),
                    }
                    for r in results
                ]
                if results
                else []
            )
        except TypeError:
            # search_runbooks may require query_embedding instead of text
            return []

    return []
