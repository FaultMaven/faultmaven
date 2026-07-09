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

from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    conclusion_overclaims,
    confirm_root_from_resolution_absence,
    grade_cause_assurance,
)
from faultmaven.core.investigation.verification_status import (
    assess_verification_status,
)
from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseState,
    CauseState,
    EvidenceCategory,
    HypothesisState,
    NodeState,
    VerificationStatus,
)

# Likelihood at or above which a ``working_conclusion`` counts as a known cause
# (the proxy fallback in ``_cause_identified`` below). This is the single source
# of truth for that threshold: new-hypothesis priors are capped strictly BELOW
# this value (``hypothesis_manager.NEW_HYPOTHESIS_MAX_PRIOR``) so no emission
# ALONE can trip the gate — only real case evidence can lift belief past it.
# The ``cap < gate`` invariant is pinned by an import-time check.
CAUSE_IDENTIFIED_LIKELIHOOD = 0.6


def _cause_identified(case: "Case") -> bool:
    """Whether the root cause is known, per the authoritative signal.

    The engine-derived ``progress.cause_state`` (CauseState.IDENTIFIED) is the
    truth signal for cause-knowledge — recomputed every turn and never
    path-stripped (investigation-flow-redesign.md §4.1 / R1, #411). It is the
    primary check here. The ``root_cause_conclusion`` / ``working_conclusion``
    fields are kept as a fallback only: the LLM does not always populate the
    documented conclusion even when the cause IS identified (cause_state is set
    but root_cause_conclusion is empty), so relying on those proxies alone
    under-reports a known cause and stalls an otherwise resolvable case. See the
    k8s-pvc gate failure — the same stale-signal trap the redesign set out to
    remove.

    The two fallback proxies are gated on a **verified symptom**: cause
    identification is anchored on evidence that the problem exists, so an
    unanchored conclusion (emitted before the symptom is verified, or left stale
    after a symptom claim is withdrawn) does not count as a known cause. The
    primary ``cause_state`` check already implies a verified symptom.

    Scope: the anchor is enforced here, at the authoritative gate that drives
    disposition / the M5 solution gate. Other readers of ``root_cause_conclusion``
    (the report renderer, the case UI) may surface an unanchored RCC mid-
    investigation, which is informational only; the one consequential consumer —
    KB runbook harvesting — runs on RESOLVED cases, by which point the symptom is
    verified, so it never harvests an unanchored cause. If a non-terminal RCC
    harvest is ever added, enforce the anchor at RCC production instead.
    """
    if (
        case.progress
        and getattr(case.progress, "cause_state", None) == CauseState.IDENTIFIED
    ):
        return True

    # Fallback signals below cover the case where the cause is genuinely known
    # but the chain recompute under-reported ``cause_state`` (the LLM grounded the
    # cause without the chain formally validating). They are trusted ONLY once the
    # symptom is verified: the verified symptom is the evidence-grounded anchor for
    # cause identification, so an unanchored or stale conclusion — one emitted
    # before the symptom is verified, or left behind after a symptom claim is
    # withdrawn — must not report the cause as known and let the resolution /
    # closure / solution gates diverge from the ``cause_state`` signal. (The
    # primary ``cause_state == IDENTIFIED`` check above already implies a verified
    # symptom, so this anchor only constrains the fallbacks.)
    if not (case.progress and getattr(case.progress, "symptom_verified", False)):
        return False

    # A disconfirmed RootCauseConclusion is retracted at its SOURCE
    # (causal_graph.retract_disconfirmed_rcc, run each turn in the chain
    # recompute), so by the time we read it here a disproven cause is already
    # None — every consumer (this gate, the report, the UI, KB runbooks) sees one
    # truth. No per-reader disconfirmation guard is needed.
    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        return True
    return bool(
        case.working_conclusion
        and getattr(case.working_conclusion, "statement", None)
        and getattr(case.working_conclusion, "likelihood", 0)
        >= CAUSE_IDENTIFIED_LIKELIHOOD
    )


def _has_causal_absence(case: "Case") -> bool:
    """Whether a ``causal_absence_evidence`` row is on the case.

    This is the ground-truth signal that the root cause was confirmed
    ELIMINATED — not merely that the symptom was relieved (a mitigation /
    failover / traffic-shift produces ``symptom_absence_evidence`` while the
    cause persists). It is the discriminator between RESOLVED (cause gone) and
    CLOSED-with-documented-solution (stabilized, deferred, or unfixed). See
    investigation-flow-redesign.md §11 and intent-resolution.md §8.
    """
    return any(
        getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        for e in (case.evidence or [])
    )


logger = logging.getLogger(__name__)


def derive_closure_reason(case: "Case") -> str:
    """Engine-only derivation of closure_reason from case state.

    Three outcomes, keyed on the state the case was closed from and its
    verification status:

    - ``inquiry_only`` — case is still in INQUIRY (no investigation started).
    - ``closed_insufficient_evidence`` — closed from INVESTIGATING while in the
      INSUFFICIENT_EVIDENCE verification-status cell (not grounded, work-gated,
      stalled — often a declared data wall). Capture-on-close: the honest
      partial is recorded for calibration and the flywheel. The residual
      candidates (non-refuted/retired hypotheses) and the specific unmet
      (unobtainable) need are already persisted on the case and are surfaced by
      the closure report — no separate snapshot is stored. This reads the
      engine's own last persisted assessment (``progress.verification_status``,
      fresh at propose time) rather than re-deriving grounding at the terminal
      boundary. It is capture-ONLY: the engine never nudges toward this close
      (no ``SUGGEST_CLOSE``); the structured handoff already lists pause/close as
      a user option (insufficient-evidence-handling.md §5.4 / D4).
    - ``closed_after_investigation`` — any other close from INVESTIGATING. Folds
      in the former ``mitigation_sufficient`` reason: a case stabilized and then
      closed is simply an investigation that closed. The documented mitigation /
      solution is preserved on the closed case.

    The LLM never authors closure_reason; it's purely engine-derived from
    structured case state.
    """
    if case.state == CaseState.INQUIRY:
        return "inquiry_only"

    if (
        case.progress
        and case.progress.verification_status
        == VerificationStatus.INSUFFICIENT_EVIDENCE
    ):
        return "closed_insufficient_evidence"

    return "closed_after_investigation"


# ============================================================
# DISPOSITION ELIGIBILITY (denormalized read view)
# ============================================================

# Per-disposition eligibility verdicts surfaced to the frontend so the
# UI can gate the case-action dropdown affordances (Resolve / Close) on
# actual case state, not just the structural action graph. The values
# mirror what ``assess_resolution_readiness`` and
# ``assess_closure_readiness`` already compute at action time; this
# helper produces a stable per-disposition shape suitable for read-time
# consumption and listed queries.
#
# Each value carries a single, disposition-independent semantic so the
# frontend can render copy / icon / tooltip per-value without needing to
# branch on which disposition it's looking at.
#
# Values:
#   "ready"
#       Disposition is a valid action and case content supports it
#       without follow-up prompts. Frontend should render the
#       affordance enabled with the default "click to confirm" UX.
#
#   "needs_info"
#       Disposition is a valid action but the case is partial. The
#       user must provide MORE information (root cause / solution)
#       before the transition can complete. UX: prompt the user to
#       ADD data. Currently only applies to the Resolve side.
#
#   "suggests_alternative"
#       Disposition is a valid action but the system recommends the
#       OTHER disposition based on case content. UX: warn the user
#       and offer the alternative; if they confirm anyway, proceed.
#       Currently only applies to the Close side (when the case has
#       root cause + solution → resolving is recommended; closing
#       would discard attribution). Different UX from "needs_info":
#       the user isn't asked to add data, they're asked to RE-DIRECT
#       to a different action.
#
#   "not_eligible"
#       Disposition is not a valid edge from the current status, OR
#       readiness verdict says this disposition shouldn't be offered
#       at all (e.g., SUGGEST_CLOSE pivots resolve→close, so
#       "resolved" is not_eligible). Frontend should hide the
#       affordance entirely.

DISPOSITION_ELIGIBILITY_READY = "ready"
DISPOSITION_ELIGIBILITY_NEEDS_INFO = "needs_info"
DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE = "suggests_alternative"
DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE = "not_eligible"


def derive_disposition_eligibility(case: "Case") -> dict[str, str]:
    """Compute the per-disposition eligibility view for the given case.

    Pure derivation — no mutation, no DB access. Returns a dict with
    fixed keys ``"resolved"`` and ``"closed"`` whose values are one of
    ``ready`` / ``needs_info`` / ``suggests_alternative`` /
    ``not_eligible``.

    Maintained at write time via the single chokepoint in
    ``CaseRepository.save()`` (pattern P3). All reads then trust the
    persisted ``case.disposition_eligibility`` column. The derivation
    here is the only place that maps readiness verdicts → eligibility
    labels — keep it in sync with ``assess_resolution_readiness`` and
    ``assess_closure_readiness`` outputs.

    Each eligibility value carries a single, disposition-independent
    semantic (see module-level constant docs above) so the frontend
    can render copy / icon / tooltip per value without having to
    branch on which disposition column it's looking at. ``needs_info``
    means "add data"; ``suggests_alternative`` means "consider the
    other action". The two are deliberately distinct because they
    drive different UX patterns.

    Semantics by current state:

    - INQUIRY: only CLOSED is a valid edge per ``ALLOWED_ACTIONS``
      (resolution requires investigation work). Returns
      ``{"resolved": "not_eligible", "closed": "ready"}``.

    - INVESTIGATING: both edges are valid. Resolved eligibility derives
      from ``assess_resolution_readiness`` (READY → ready,
      NEEDS_INFO → needs_info, SUGGEST_CLOSE → not_eligible).
      Closed eligibility is ``ready`` by default; if
      ``assess_closure_readiness`` returns SUGGEST_RESOLVE (case has
      root cause + solution), closed → ``suggests_alternative`` so
      the frontend can warn the user that resolving would preserve
      attribution.

    - Terminal (RESOLVED / CLOSED): no further actions. Returns all
      ``not_eligible``.
    """
    if case.state == CaseState.INQUIRY:
        return {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }

    if case.state != CaseState.INVESTIGATING:
        # Terminal — no further dispositions.
        return {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
        }

    # INVESTIGATING — map readiness verdicts to eligibility labels.
    resolution = assess_resolution_readiness(case)
    if resolution.verdict == ResolutionReadiness.READY:
        resolved_eligibility = DISPOSITION_ELIGIBILITY_READY
    elif resolution.verdict == ResolutionReadiness.NEEDS_INFO:
        # Case is partial; user needs to ADD data before resolving.
        resolved_eligibility = DISPOSITION_ELIGIBILITY_NEEDS_INFO
    else:  # SUGGEST_CLOSE
        # Resolved-readiness says the case is too thin for resolution;
        # closing is the right disposition. Frontend should not offer
        # Resolved on a SUGGEST_CLOSE case.
        resolved_eligibility = DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE

    closure = assess_closure_readiness(case)
    if closure.verdict == ClosureReadiness.SUGGEST_RESOLVE:
        # Case qualifies for resolved; closing would discard the
        # resolution attribution. Surface as ``suggests_alternative``
        # so the frontend can warn the user and offer the resolve
        # path instead — distinct UX from ``needs_info`` which asks
        # the user to add data.
        closed_eligibility = DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE
    else:
        # HAS_SUBSTANCE or TRIVIAL — closing is always ready as a
        # valid action; the confirmation prompt carries the summary
        # or the minimal-data warning.
        closed_eligibility = DISPOSITION_ELIGIBILITY_READY

    return {
        "resolved": resolved_eligibility,
        "closed": closed_eligibility,
    }


def propose_transition(
    case: Case,
    to_state: str,
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
        to_state: Target status ("resolved" or "closed")
        summary: Summary presented to user for confirmation
        evidence_ids: Evidence IDs supporting the proposal
    """
    pending: dict = {
        "to_state": to_state,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    if to_state == "closed":
        pending["closure_reason"] = derive_closure_reason(case)
    case.pending_transition = pending
    logger.info(
        f"Transition proposed for case {case.case_id}: → {to_state} "
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
    to_state = pending["to_state"]

    if to_state == "resolved":
        _execute_resolved_transition(case, user_id)
    elif to_state == "closed":
        _execute_closed_transition(case, user_id, pending["closure_reason"])
    else:
        logger.error(f"Unknown pending transition target: {to_state}")
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
            f"→ {case.pending_transition['to_state']} (user declined)"
        )
        case.pending_transition = None
        return True
    return False


def _execute_resolved_transition(case: Case, user_id: str):
    """Execute INVESTIGATING → RESOLVED after user confirmation.

    Raises:
        ValueError: If case is not in INVESTIGATING status.
    """
    if case.state != CaseState.INVESTIGATING:
        raise ValueError(
            f"Cannot resolve case {case.case_id}: status is {case.state}, "
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

    # M2 confirm-side stamp: the user just CONFIRMED the resolution — the
    # strongest confirmation signal the flow produces — so the engine links
    # the (prompt-contract unlinked) causal_absence row to the sole standing
    # validated root and re-persists the grade, making CONFIRMED the terminal
    # truth for the harvest gate / report / seam analytics. Deliberately NOT
    # done on the row's mere appearance during investigation (an LLM
    # self-claim; a premature "it's stable" row must not confirm anything).
    if confirm_root_from_resolution_absence(case):
        case.progress.cause_assurance = grade_cause_assurance(case)
        case.progress.cause_overclaim = conclusion_overclaims(
            case.root_cause_conclusion, case.progress.cause_assurance
        )
        # The FULL grade-derived persisted set — the per-turn recompute derives
        # verification_status from the same grade snapshot precisely so the two
        # signals can never diverge; the terminal blob must honor the same
        # invariant or every stamp-confirmed case would freeze
        # cause_assurance=confirmed beside a pre-stamp (e.g. not-grounded)
        # verification_status.
        case.progress.verification_status = assess_verification_status(
            case, grade=case.progress.cause_assurance
        )
        # Same coherence rule for cause_state: the last in-flight recompute ran
        # BEFORE the stamp, so a count-held root the confirmation just
        # validated (INV-29) would otherwise freeze the terminal blob at
        # CANDIDATES beside a CONFIRMED grade. Mirror the §9.2 derivation
        # locally (a standing hypothesis whose chain root is VALIDATED —
        # kept literal here for the same import-cycle reason as
        # cause_assurance._STANDING_HYP_STATES).
        if any(
            h.state in (HypothesisState.ACTIVE, HypothesisState.VALIDATED)
            and h.root_node_id
            and getattr(case.causal_nodes.get(h.root_node_id), "node_state", None)
            == NodeState.VALIDATED
            for h in case.hypotheses.values()
        ):
            case.progress.cause_state = CauseState.IDENTIFIED

    now = datetime.now(UTC)

    # Mark any remaining pending ProposedActions as accepted and create audit records.
    # This covers revised fixes proposed during the TREATMENT failure path: when a fix
    # fails and the LLM proposes a revised solution (SolutionToAdd → ProposedAction),
    # the user executes it and the case resolves via ProposedTransition rather than a
    # stage-gate milestone. Because solution_accepted is already True, no stage-gate
    # fires and _apply_stage_gate_side_effects is never called for the revised action.
    for action in case.proposed_actions:
        if action.state == "pending":
            action.state = "accepted"
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
        state=CaseState.RESOLVED,
        resolved_at=now,
        closed_at=now,
        # closure_reason is None for RESOLVED — resolution itself is the
        # categorization. Sub-categorization would be redundant.
    )
    case.action_history.append(
        CaseAction(
            from_state=CaseState.INVESTIGATING,
            to_state=CaseState.RESOLVED,
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
    from_state = case.state
    if from_state not in (CaseState.INVESTIGATING, CaseState.INQUIRY):
        raise ValueError(
            f"Cannot close case {case.case_id}: status is {from_state}, "
            f"expected INVESTIGATING or INQUIRY"
        )

    logger.info(
        f"User {user_id} confirmed closure for case {case.case_id}. "
        f"Executing {from_state.value} → CLOSED transition."
    )

    now = datetime.now(UTC)
    case.atomic_update(
        state=CaseState.CLOSED,
        closed_at=now,
        closure_reason=closure_reason,
    )
    case.action_history.append(
        CaseAction(
            from_state=from_state,
            to_state=CaseState.CLOSED,
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
    # THE resolution bar: the root cause is CONFIRMED eliminated — recorded as a
    # causal_absence row. That alone makes a case RESOLVED: it implies the cause is
    # known and a fix took effect, so it is sufficient on its own. A separate
    # root-cause / solution *record* is for documentation quality (and the higher
    # runbook bar), NOT a resolution gate. Critically, an out-of-band fix the user
    # reports verbally yields causal_absence (source_type=user_description) with no
    # SolutionToAdd — and must still resolve. Requiring a solution record here was
    # the documented stuck-loop: the gate refused a clear "yes, it's resolved" and
    # kept demanding a "documented solution" the user did not have, then closed.
    has_cause = _cause_identified(case)
    has_solution = bool(case.solutions and len(case.solutions) > 0)
    has_evidence = bool(case.evidence and len(case.evidence) > 0)
    has_resolution_confirmation = _has_causal_absence(case)

    if has_resolution_confirmation:
        return ResolutionReadiness(
            verdict=ResolutionReadiness.READY,
            message="",
            missing=[],
        )

    # No causal_absence yet: the user wants to resolve but hasn't confirmed the
    # cause is gone. This is the documentation-gap-fill ask (common out of band, or
    # mid-stream) — collect what's needed to document a resolution AND confirm the
    # cause is actually eliminated. A stabilized/deferred account never confirms it
    # and converges to Close. Close is offered up front as the alternative.
    missing = ["confirmation the problem is now resolved"]
    if not has_cause:
        missing.append("root cause")
    if not has_solution:
        missing.append("solution")

    # Genuinely nothing on record — no investigation to turn into a resolution.
    # Suggest Close rather than asking the user to reconstruct a whole case.
    if not (has_cause or has_solution or has_evidence):
        return ResolutionReadiness(
            verdict=ResolutionReadiness.SUGGEST_CLOSE,
            message=(
                "This case has nothing investigated yet, so there's nothing to "
                "document as a resolution.\n\n"
                "If it's no longer relevant you can **close** it. If it was "
                "actually fixed, tell me what the problem was, what caused it, "
                "and how it was fixed, and I'll document the resolution."
            ),
            missing=missing,
        )

    # Otherwise: ask the user to fill the documentation gaps. The problem may
    # have been resolved out of band — invite the missing essentials rather than
    # rejecting the request.
    asks = []
    if "root cause" in missing:
        asks.append("- **Root cause** — what caused the problem?")
    if "solution" in missing:
        asks.append("- **What fixed it** — the action that resolved it.")
    if "confirmation the problem is now resolved" in missing:
        asks.append(
            "- **Confirmation it's resolved** — that the original problem is "
            "now gone (e.g. the error no longer occurs in the latest output)."
        )
    return ResolutionReadiness(
        verdict=ResolutionReadiness.NEEDS_INFO,
        message=(
            "To mark this **resolved** and write up the resolution summary, I "
            "just need a few essentials:\n\n"
            + "\n".join(asks)
            + "\n\nIf the problem was already fixed outside this session, just "
            "tell me the above and I'll document it.\n\n"
            "If it was only **stabilized** (a workaround/failover while the root "
            "cause is still pending) or the real fix is **deferred** (a change "
            "window, hardware RMA, another team), you can **close** the case "
            "instead — that keeps the investigation and the documented (or "
            "deferred) solution on record without claiming it's permanently "
            "fixed."
        ),
        missing=missing,
    )


# ============================================================
# CLOSURE READINESS ASSESSMENT
# ============================================================


class ClosureReadiness:
    """Assessment of what was accomplished during an investigation before closing.

    Three verdicts:
    - HAS_SUBSTANCE: Investigation produced meaningful work worth summarizing.
    - TRIVIAL: Investigation had minimal substance (quick close, no real work done).
    - SUGGEST_RESOLVE: Case has root cause AND solution on record — qualifies for
      RESOLVED status. Closing would discard resolution attribution; pivot to
      proposing RESOLVED instead.

    SUGGEST_RESOLVE is the symmetric counterpart of
    ``ResolutionReadiness.SUGGEST_CLOSE``. Together they reconcile loose
    user terminology ("close" vs "resolve") with case content when the two
    don't match: a resolve request on a thin case pivots to close; a close
    request on a fully-investigated case pivots to resolve.
    """

    HAS_SUBSTANCE = "has_substance"
    TRIVIAL = "trivial"
    SUGGEST_RESOLVE = "suggest_resolve"

    def __init__(self, verdict: str, message: str):
        self.verdict = verdict
        self.message = message


def _solution_display_titles(solutions: list) -> list[str]:
    """Render solution titles for user-facing summaries.

    Falls back to "Solution N" (1-indexed by list position) when a
    solution lacks a title, instead of the literal string "Untitled".
    Avoids "Solution: My fix, Untitled, Untitled" for multi-solution
    cases where titles are sparse — the indexed fallback keeps each
    entry distinguishable.
    """
    return [
        getattr(s, "title", None) or f"Solution {i + 1}"
        for i, s in enumerate(solutions)
    ]


def assess_closure_readiness(case: "Case") -> ClosureReadiness:
    """Summarize what was accomplished during investigation, or pivot to RESOLVED
    if the case actually qualifies for resolution.

    Verdict logic:
    - SUGGEST_RESOLVE — case has root cause + solution. Closing would lose
      resolution attribution; engine pivots to propose RESOLVED instead.
      Symmetric to ResolutionReadiness.SUGGEST_CLOSE.
    - TRIVIAL — case has minimal investigation data (no evidence, no
      hypotheses, no milestones, no root cause, no solutions).
    - HAS_SUBSTANCE — investigation produced meaningful work (but the
      "resolution-grade" combination of root cause + solution isn't
      present). Closing is the right disposition; the message summarizes
      what was accomplished for the user-facing confirmation prompt.

    The actual CLOSURE_SUMMARY report is generated only after user confirms.

    Args:
        case: Case being assessed for closure

    Returns:
        ClosureReadiness with verdict and user-facing message
    """
    evidence_count = len(case.evidence) if case.evidence else 0
    hypothesis_count = len(case.hypotheses) if case.hypotheses else 0
    milestones_completed = (
        len(case.progress.completed_milestones) if case.progress else 0
    )
    has_root_cause = _cause_identified(case)
    has_solutions = bool(case.solutions and len(case.solutions) > 0)

    # SUGGEST_RESOLVE — the case is "resolution-grade" (matches the minimum
    # criteria assess_resolution_readiness uses for READY). Closing would
    # discard the resolution attribution. Engine pivots; user still confirms.
    # Symmetric to ResolutionReadiness.SUGGEST_CLOSE.
    #
    # Gate on causal_absence — the SAME bar as assess_resolution_readiness READY.
    # A close request on a resolution-ready case pivots to resolve: "resolved" is
    # a closed case WITH a resolution, and it is always safe to resolve a case the
    # user moved to close. We do NOT require a separate root-cause/solution record
    # (an out-of-band fix yields causal_absence with neither) — that was the same
    # over-constraint the resolution gate carried. A merely stabilized case has
    # symptom_absence but no causal_absence, so it correctly does NOT pivot.
    if _has_causal_absence(case):
        rc = (
            getattr(case.root_cause_conclusion, "root_cause", None)
            or "the identified root cause"
        )
        detail = f"- **Root cause**: {rc}\n"
        if has_solutions:
            detail += (
                "- **Solution**: "
                + ", ".join(_solution_display_titles(case.solutions))
                + "\n"
            )
        return ClosureReadiness(
            verdict=ClosureReadiness.SUGGEST_RESOLVE,
            message=(
                "This case has a fix confirmed to have eliminated the root "
                "cause — it qualifies for **resolved** status rather than "
                "closed. Closing would record it as unresolved and discard the "
                "resolution.\n\n"
                + detail
                + "\nWould you like to mark it **resolved** instead?"
            ),
        )

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
        rc = (
            getattr(case.root_cause_conclusion, "root_cause", None)
            or "the identified root cause"
        )
        parts.append(f"- **Root cause identified**: {rc}")
    if has_solutions:
        sol_titles = _solution_display_titles(case.solutions)
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
    has_root_cause_record = bool(
        case.root_cause_conclusion
        and getattr(case.root_cause_conclusion, "root_cause", None)
    )
    # Harvest counts a cause only when it is CONFIRMED — counterfactually borne
    # out (the cause was removed and the problem went with it, M2 gone⇒gone).
    # Read the single assurance grade once and gate on CONFIRMED explicitly: this
    # also holds a RootCauseConclusion with NO validated root (pure LLM prose,
    # zero causal graph) — graded NO_ROOT, not CONFIRMED — which the old negative
    # "fallback-only" view let through (#590 A1). A cause carried only by
    # mechanistic validation (MECHANISTIC — empirical rung evidence OR a
    # deductive derivation) is likewise held: harvesting it would promote an
    # unconfirmed cause (the model authored the validation, and even a deductive
    # proof rests on model-mediated refutations plus an asserted-exhaustive
    # differential) into the corpus.
    cause_grade = grade_cause_assurance(case)
    has_root_cause = (
        has_root_cause_record and cause_grade == CauseAssuranceGrade.CONFIRMED
    )
    # For the user-facing ask, distinguish the two held shapes: a graph-identified
    # but unconfirmed cause (MECHANISTIC) gets a "confirm it" ask; a record with
    # no validated root (NO_ROOT) falls through to the plain "identify a root
    # cause" ask below.
    root_cause_unverified = (
        has_root_cause_record and cause_grade == CauseAssuranceGrade.MECHANISTIC
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
            if root_cause_unverified:
                missing_desc.append(
                    "- a confirmed root cause (one is identified, but its removal "
                    "was never confirmed to remove the problem, so it won't "
                    "auto-seed a runbook — if you've confirmed it yourself, "
                    "document it via 'Create runbook' / POST "
                    "/knowledge/runbooks/create)"
                )
            elif not has_root_cause:
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
    if case.state != CaseState.CLOSED:
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
    verified solution. Only RESOLVED cases qualify. CLOSED cases are not
    eligible because they lack a confirmed root-cause-to-solution chain that
    a future investigator can apply.

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
