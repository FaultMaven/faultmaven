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

from faultmaven.config.tenant_context import usable_tenant_id
from faultmaven.core.investigation.cause_assurance import (
    CONFIRMED_RCC_LIKELIHOOD_FLOOR,
    CauseAssuranceGrade,
    _graph_hooks,
    conclusion_overclaims,
    confirm_root_from_resolution_absence,
    grade_cause_assurance,
    has_actionable_solution,
    has_problem_definition,
    has_resolution_confirmation,
    has_root_cause_record,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    close_pivoted_to_resolve_total,
    resolution_cause_leg_total,
)
from faultmaven.core.investigation.verification_status import (
    assess_verification_status,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseAction,
    CaseState,
    CauseState,
    EvidenceCategory,
    InvestigationActionType,
    SolutionState,
    VerificationStatus,
)

# Likelihood at or above which a ``working_conclusion`` counts as a known cause
# (the proxy fallback in ``_cause_identified`` below). This is the single source
# of truth for that threshold: new-hypothesis priors are capped strictly BELOW
# this value (``hypothesis_manager.NEW_HYPOTHESIS_MAX_PRIOR``) so no emission
# ALONE can trip the gate — only real case evidence can lift belief past it.
# The ``cap < gate`` invariant is pinned by an import-time check.
CAUSE_IDENTIFIED_LIKELIHOOD = 0.6

# INV-26 substantive-input bound: a typed reply longer than this cannot be a
# bare consent to a pending terminal transition. Shared by every consumer of
# ``is_substantive_reply`` so the confirm-side substance test cannot drift
# between the pattern matcher and the intent-resolver guard (#721).
BARE_CONSENT_MAX_LENGTH = 100


def is_substantive_reply(user_message: "str | None") -> bool:
    """INV-26 substance test for replies to a pending TERMINAL transition.

    A message is substantive — and therefore can never be consumed as consent
    to an irreversible RESOLVED/CLOSED transition — when it is long (>
    ``BARE_CONSENT_MAX_LENGTH`` chars), carries a question, or carries a
    contrastive continuation ("yes but what about the replication lag?").
    Substantive input falls to the pending-gate escape lane and is processed
    as a normal turn; only a bare confirmation may execute a terminal
    transition.

    This is the single source of truth for the confirm-side substance test:
    ``MilestoneEngine._user_confirms_transition`` (typed pattern matching) and
    the IntentResolver adoption guard in ``investigation_service`` (#721,
    classifier-minted confirmation intents) both apply it. An empty message is
    not substantive — it is also not consent; callers reject it separately.
    """
    if not user_message:
        return False
    msg = user_message.strip().lower()
    if len(msg) > BARE_CONSENT_MAX_LENGTH:
        return True
    return "?" in msg or " but " in msg or msg.endswith(" but")


def cause_identification_leg(case: "Case") -> "str | None":
    """Which leg of the cause-identification gate is satisfied, or ``None``.

    The single source of truth for cause-knowledge. Returns the FIRST satisfied
    leg, in trust order:

    - ``"chain"`` — the engine-derived ``progress.cause_state`` is
      ``IDENTIFIED``: a validated, uncontested chain root. This is the
      authoritative signal — recomputed every turn and never path-stripped
      (two-dimensional-hypothesis-methodology.md §9.2; the R1 cause_state gate is
      recorded in investigation-invariants.md's INV-17/19/20/21 flow-redesign
      retirement note, #411).
    - ``"rcc"`` — the LLM-authored ``root_cause_conclusion`` **backstop**: the
      chain recompute under-reported ``cause_state`` (the LLM grounded the cause
      without the chain formally validating), but the conclusion names a cause.
    - ``"working_conclusion"`` — the engine working-conclusion proxy
      **backstop**: a standing hypothesis above ``CAUSE_IDENTIFIED_LIKELIHOOD``.
    - ``None`` — no leg; the cause is not known.

    The ``rcc``/``working_conclusion`` legs are kept because the LLM does not
    always populate the conclusion even when the cause IS identified — relying on
    the proxies alone under-reports a known cause and stalls an otherwise
    resolvable case (the k8s-pvc gate failure). Both backstop legs are gated on a
    **verified symptom** (the evidence-grounded anchor: an unanchored or stale
    conclusion, emitted before the symptom is verified or left behind after a
    symptom claim is withdrawn, must not report the cause as known) and suppressed
    while identification is MECE-contested (§7.1.2 / INV-34: the arbitrary pick
    between competing validated causes, or an LLM over-claim of ONE contested
    cause, is READ-suppressed — not retracted — until the contest resolves; a
    decisively DISCONFIRMED RCC is already ``None`` here, retracted at source via
    retract_disconfirmed_rcc / the M6 demotion, so every consumer sees one truth).
    The ``chain`` leg already implies a verified, uncontested cause, so these
    constraints only bind the backstops.

    The ``chain`` vs ``rcc``/``working_conclusion`` split is exactly the #673
    dual-authoring retirement gate: a resolution licensed by a backstop leg with
    no validated chain root is what INV-41's ``resolution_cause_leg_total``
    measures at the provider floor.

    Scope: enforced here, at the authoritative gate that drives disposition / the
    M5 solution gate. Other readers of ``root_cause_conclusion`` (the report
    renderer, the case UI) may surface an unanchored RCC mid-investigation
    (informational only); the one consequential consumer — KB runbook harvesting —
    runs on RESOLVED cases (symptom verified), so it never harvests an unanchored
    cause. If a non-terminal RCC harvest is ever added, enforce the anchor at RCC
    production instead.
    """
    if (
        case.progress
        and getattr(case.progress, "cause_state", None) == CauseState.IDENTIFIED
    ):
        return "chain"

    # Backstop legs are trusted ONLY once the symptom is verified.
    if not (case.progress and getattr(case.progress, "symptom_verified", False)):
        return None

    # While identification is MECE-contested, NO backstop proxy counts.
    if getattr(case.progress, "cause_identification_contested", False):
        return None

    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        return "rcc"
    if (
        case.working_conclusion
        and getattr(case.working_conclusion, "statement", None)
        and getattr(case.working_conclusion, "likelihood", 0)
        >= CAUSE_IDENTIFIED_LIKELIHOOD
        # #987: a MIRROR of the RootCauseConclusion is not an independent
        # signal — whenever one exists the `rcc` leg above already governs. The
        # working conclusion is read from the PREVIOUS turn (it regenerates
        # after the recompute), so counting a mirror here would let a conclusion
        # retracted THIS turn keep satisfying the backstop through its own
        # stale reflection, for one turn after retraction cleared every other
        # consumer.
        and not getattr(case.working_conclusion, "mirrors_root_cause_conclusion", False)
    ):
        return "working_conclusion"
    return None


def _cause_identified(case: "Case") -> bool:
    """Whether the root cause is known, per the authoritative signal.

    Thin predicate over :func:`cause_identification_leg` — the cause is known iff
    some leg (chain / rcc / working_conclusion) is satisfied. Keeping the leg
    logic in one place means the INV-41 backstop-reliance metric
    (``resolution_cause_leg_total``) can never disagree with the gate it observes.
    """
    return cause_identification_leg(case) is not None


def _has_causal_absence(case: "Case") -> bool:
    """Whether a QUALIFYING ``causal_absence_evidence`` row is on the case
    (``cause_assurance.has_resolution_confirmation`` — the SAME METADATA bar
    the confirm-stamp's candidate filter uses, so the gate can never call a
    case confirmable on a row the stamp refuses for metadata; the stamp's
    additional content-bearing refusal deliberately does NOT bind the gate —
    a mis-citable row still evidences that the user confirmed resolution).

    This is the ground-truth signal that the root cause was confirmed
    ELIMINATED — not merely that the symptom was relieved (a mitigation /
    failover / traffic-shift produces ``symptom_absence_evidence`` while the
    cause persists). It is the discriminator between RESOLVED (cause gone) and
    CLOSED-with-documented-solution (stabilized, deferred, or unfixed). See
    investigation-lifecycle-logic.md ("Gate strictness — absence-driven") and
    the methodology doc §9.5 (INV-30).

    Qualification matters (#656): the bare any-row read this replaced
    counted the ENGINE's own M6 failed-fix DISCONFIRMATION rows — so a failed
    fix satisfied "confirmation the problem is now resolved" — and premature
    "it's stable" rows from fix windows that later failed.
    """
    return has_resolution_confirmation(case)


logger = logging.getLogger(__name__)


def _resolve_resolution_provider() -> str:
    """CHAT provider label for the INV-41 backstop-reliance metric.

    Resolved from settings — this finalizer runs on both the chat and API resolve
    surfaces and has no engine handle. This is **label-equivalent** to
    ``milestone_engine._resolve_chat_provider_name(self.llm_provider)``: in the
    real deployment ``self.llm_provider`` is the ``LLMRouter`` (no ``provider_name``),
    so that helper already falls back to ``settings.llm.provider`` — the same value
    read here. Returns ``"unknown"`` when it cannot resolve, mirroring INV-39.
    """
    try:
        from faultmaven.config.settings import get_settings

        provider = getattr(getattr(get_settings(), "llm", None), "provider", None)
        if provider is not None:
            return getattr(provider, "value", None) or str(provider)
    except Exception:
        pass
    return "unknown"


def finalize_resolution_truth_surface(case: "Case") -> bool:
    """The ONE resolution-time truth-surface finalizer, shared by EVERY
    resolve surface (the chat-side ``_execute_resolved_transition`` and the
    dashboard/API ``ApiCaseService.close_case``) — a second hand-mirrored
    copy is how the two surfaces previously diverged on terminal truth.

    M2 confirm-side stamp: the user just CONFIRMED the resolution — the
    strongest confirmation signal the flow produces — so the engine links the
    (prompt-contract unlinked) causal_absence row to the sole standing
    validated (or count-held, INV-29) root and re-persists the FULL
    grade-derived set: grade, over-claim flag, verification_status, and the
    cause_state coherence block. Deliberately NOT done on an absence row's
    mere appearance during investigation (an LLM self-claim; a premature
    "it's stable" row must not confirm anything). Returns True if the stamp
    landed.

    Coherence is keyed on the GRADE, not on hypothesis shape: a CONFIRMED
    grade with ``cause_state`` below IDENTIFIED is incoherent regardless of
    whether the confirmed root carries a standing hypothesis (the stamp's
    node-level fallback deliberately confirms hypothesis-less roots — the
    weak-model chain-without-hypothesis shape — and the terminal blob must
    not read "no identified cause" beside the sole harvest-authority grade).
    The ``root_cause_consistency`` validator requires method + non-zero
    likelihood beside IDENTIFIED, so the same defaults the per-turn recompute
    uses are applied (a bare assignment 500s the RESOLVED execution on the
    progress-blob reload and strands the case — caught live in the INV-29
    sim gate).
    """
    # INV-41 (#673 retirement gate): record which leg of cause-identification
    # licensed THIS resolution — captured PRE-STAMP, because the confirm-stamp
    # below promotes a decayed/count-held root to cause_state=IDENTIFIED, which
    # would relabel a backstop-licensed resolution as "chain" and understate
    # backstop reliance (biasing the #673 gate toward premature retirement — the
    # NO-COLLAPSE regression the gate guards). Emitted HERE because this finalizer
    # is the single chokepoint every RESOLVED executor shares (the chat-side
    # _execute_resolved_transition and the dashboard/API ApiCaseService.close_case),
    # so it fires exactly once per resolution across every surface. Metric-only.
    resolution_cause_leg_total.labels(
        provider=_resolve_resolution_provider(),
        leg=cause_identification_leg(case) or "none",
    ).inc()

    stamped = confirm_root_from_resolution_absence(case)
    # #695 Defect A (terminal gap): a case reaching CONFIRMED via the confirm-stamp
    # validates its root HERE, outside the per-turn recompute, and a terminal case
    # never recomputes — so re-project hypothesis states from the just-validated
    # root, or the report would show the hypothesis un-validated beside a CONFIRMED
    # grade (the exact report-Validated ⟺ grade invariant this fix establishes).
    # Reached via the graph-hooks registry (not a direct import): terminal_transitions
    # -> causal_graph would close the cause_assurance/hypothesis_manager import cycle.
    # Read defensively (.get, like every other hook consumer): this sits on the
    # unguarded RESOLVED-execution path, so a missing registration must degrade to
    # a skipped projection, never a KeyError that 500s the transition.
    project_hyp_states = _graph_hooks().get("project_hyp_states")
    if project_hyp_states is not None:
        project_hyp_states(case)
    # The grade set is refreshed UNCONDITIONALLY: the API surface reaches here
    # without a fresh per-turn recompute, so even a no-stamp resolve must not
    # freeze a stale persisted grade into the terminal blob (idempotent on the
    # chat surface, where the recompute just wrote the same values).
    case.progress.cause_assurance = grade_cause_assurance(case)
    case.progress.cause_overclaim = conclusion_overclaims(
        case.root_cause_conclusion, case.progress.cause_assurance
    )
    # The per-turn recompute derives verification_status from the same grade
    # snapshot precisely so the two signals can never diverge; the terminal
    # blob must honor the same invariant.
    case.progress.verification_status = assess_verification_status(
        case, grade=case.progress.cause_assurance
    )
    if case.progress.cause_assurance == CauseAssuranceGrade.CONFIRMED:
        case.progress.cause_state = CauseState.IDENTIFIED
        if not case.progress.root_cause_method:
            case.progress.root_cause_method = "hypothesis_validation"
        if not case.progress.root_cause_likelihood:
            case.progress.root_cause_likelihood = (
                getattr(case.root_cause_conclusion, "likelihood", None)
                or CONFIRMED_RCC_LIKELIHOOD_FLOOR
            )
    # INV-32 terminal coherence: both resolve surfaces stamp the solution
    # ladder BEFORE calling this finalizer, and no per-turn recompute runs on
    # a terminal case — without a fresh derivation the blob would read
    # solution_proposed=True × solution_state=unknown (observed live in the
    # #656 offer-liveness sim gate).
    derive_solution_surface(case)
    return stamped


def derive_solution_surface(case: "Case") -> None:
    """THE ONE derivation of the solution truth pair (INV-32).

    ``solution_proposed`` is True iff a live SOLUTION offer stands
    (``pending`` = awaiting execution; ``accepted`` = executed fix, a
    historical fact) OR the gate ladder has advanced
    (``solution_accepted``/``solution_verified`` are forward-only compliance
    facts, so the state_validator orderings hold by construction).
    ``solution_state`` mirrors it (``SELECTED``/``UNKNOWN``; ``CANDIDATES``
    reserved). MITIGATION/DIAGNOSTIC actions never feed the pair.

    Single definition by design: the per-turn assessment recompute, the
    resolution finalizer above, and the CLOSED executor all call this — a
    hand-mirrored copy is how terminal surfaces previously diverged on
    terminal truth (this module's own finalizer lesson, re-learned when the
    first cut mirrored only the RESOLVED family and CLOSED blobs froze
    stale).
    """
    p = case.progress
    offer_live = (
        p.solution_accepted
        or p.solution_verified
        or any(
            a.action_type == InvestigationActionType.SOLUTION
            and a.state in ("pending", "accepted")
            for a in case.proposed_actions
        )
    )
    p.solution_proposed = offer_live
    p.solution_state = SolutionState.SELECTED if offer_live else SolutionState.UNKNOWN


def _symptom_checked_and_absent(case: "Case") -> bool:
    """Whether the case positively established the problem was NOT occurring.

    Requires BOTH: the symptom is not verified now, AND a
    ``symptom_absence_evidence`` row is on record — someone looked at the
    present state and wrote down that the problem was not there.

    The absence row is what makes this a finding rather than a gap. An
    unverified symptom on its own is ambiguous and mostly means the opposite:
    a case closed because the user stopped supplying data never verified its
    symptom either, and reporting that as "not reproduced" would assert a
    conclusion nobody reached. Requiring the row separates "we checked and it
    was not happening" from "we never got to look" — and leaves the latter on
    whichever generic reason it had before, so nothing regresses.

    Deliberately does NOT require the symptom to have been verified first. The
    problem can be found already-stopped on the very first check, before it was
    ever established; that is the same finding arrived at sooner.
    """
    progress = getattr(case, "progress", None)
    if progress is None or progress.symptom_verified:
        return False
    return any(
        ev.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
        for ev in getattr(case, "evidence", []) or []
    )


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
    - ``closed_not_reproduced`` — the case positively established the problem
      was NOT occurring: no verified symptom AND a ``symptom_absence_evidence``
      row on record (see ``_symptom_checked_and_absent`` — the row is what makes
      this a finding rather than a gap). Checked BEFORE the
      insufficient-evidence cell because it answers an earlier question and is
      the more specific finding: that cell says "a real problem, cause not
      grounded", which presupposes the problem. Reporting a stopped problem as a
      failed cause hunt would credit a hunt that had nothing to find.
    - ``closed_after_investigation`` — any other close from INVESTIGATING. Folds
      in the former ``mitigation_sufficient`` reason: a case stabilized and then
      closed is simply an investigation that closed. The documented mitigation /
      solution is preserved on the closed case.

    The LLM never authors closure_reason; it's purely engine-derived from
    structured case state.
    """
    if case.state == CaseState.INQUIRY:
        return "inquiry_only"

    if _symptom_checked_and_absent(case):
        return "closed_not_reproduced"

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

    Returns True if a terminal transition was executed. Returns False when
    nothing terminal executed, which now covers THREE cases: no pending
    transition, an unknown target, OR an INV-37 resolve-preservation pivot —
    a pending CLOSE on a resolvable case is replaced with a RESOLVED proposal
    (`case.pending_transition` is left pointing at the new "resolved" pending)
    instead of closing. Callers that must re-present a confirmation on the
    pivot detect it via `not executed and pending_transition["to_state"] ==
    "resolved"` (see the milestone-engine confirm sites).

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

    # Closed-gate resolve-preservation (INV-37): a resolvable case must never
    # terminally commit to CLOSED. The SUGGEST_RESOLVE pivot (assess_closure_
    # readiness) already fires at proposal time, but a qualifying causal_absence
    # can land AFTER the close was proposed — so re-check here, at the single
    # execution chokepoint, immediately before the close commits. "resolve =
    # close WITH resolution; close = close WITHOUT resolution": it is always safe
    # to resolve a case that can be resolved, whichever terminal the user asked
    # for, and users have no incentive to close a case that qualifies for
    # resolution. On a hit we DON'T close — we replace the pending close with a
    # RESOLVED proposal and report that nothing terminal executed (return False);
    # the caller re-presents the resolve confirmation. Scoped to INVESTIGATING:
    # RESOLVED is not a valid edge from INQUIRY, and an INQUIRY case cannot carry
    # a qualifying causal_absence anyway.
    if to_state == "closed" and case.state == CaseState.INVESTIGATING:
        closure = assess_closure_readiness(case)
        if closure.verdict == ClosureReadiness.SUGGEST_RESOLVE:
            propose_transition(case, to_state="resolved", summary=closure.message)
            close_pivoted_to_resolve_total.inc()
            logger.info(
                f"Case {case.case_id}: pending CLOSE pivoted to RESOLVED at "
                f"confirm — case is resolvable (SUGGEST_RESOLVE). Resolve-"
                f"preservation (INV-37): a resolvable case is never closed."
            )
            return False

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

    finalize_resolution_truth_surface(case)

    now = datetime.now(UTC)

    # Still-pending ProposedActions stay PENDING at resolution (#787). The
    # user's confirmation is consent to the case-level RESOLVED transition —
    # it carries no per-action execution signal, and the engine records none
    # (Solution.applied_at / ProposedAction have no outcome field). Stamping
    # every pending offer ``accepted`` with a fabricated full-confidence
    # ActionAttempt claimed the user EXECUTED fixes they may never have run
    # (out-of-band resolution), and R5's ``classify_solution_outcome`` then
    # laundered them into generated runbooks as "applied". A pending offer
    # classifies as PROPOSED at the conversion boundary — surfaced, flagged
    # unconfirmed — which is the honest ceiling until a per-action outcome
    # signal exists. Known recall cost, accepted: a revised fix the user DID
    # run on the TREATMENT failure path (no stage gate fires there because
    # ``solution_accepted`` is already latched) now also reads PROPOSED
    # rather than falsely-certain APPLIED.
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

    # INV-32 terminal coherence for the CLOSED family: no per-turn recompute
    # runs after this, and a 0b-confirmed close can arrive with the pair
    # stale from a recompute that predates the confirmation. Same shared
    # derivation as the resolve surfaces — CLOSED does NOT force the ladder
    # (nothing was executed/confirmed), it freezes the derived truth: a
    # standing pending offer reads SELECTED, a withdrawn one honestly does
    # not (the documented fix lives on the monotone Solution rows).
    derive_solution_surface(case)

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


def execute_user_closure(case: Case, user_id: str) -> str:
    """Close a case outside the chat confirmation flow (REST close, #915).

    The API call itself is the user's confirmation — there is no pending
    proposal to confirm — so the closure_reason is derived at execution
    time by the same rule the chat flow applies at propose time
    (``derive_closure_reason``), and the transition runs through the same
    executor as the chat close. One closure rule for both surfaces; the
    previous REST route mutated ``case.state`` directly and died on the
    terminal-state validator (no ``closed_at``).

    Mutates ``case`` in place; the caller owns persistence. Returns the
    derived closure_reason.

    Raises:
        ValueError: If the case is not in a closable (INQUIRY or
            INVESTIGATING) state — callers should pre-check terminal
            states and surface those as a conflict.
    """
    reason = derive_closure_reason(case)
    _execute_closed_transition(case, user_id, reason)
    return reason


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
    # Named to avoid shadowing the imported has_resolution_confirmation().
    resolution_confirmed = _has_causal_absence(case)

    if resolution_confirmed:
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
    has_problem_def = has_problem_definition(case)
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
    rcc_present = has_root_cause_record(case)
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
    has_root_cause = rcc_present and cause_grade == CauseAssuranceGrade.CONFIRMED
    # For the user-facing ask, distinguish the two held shapes: a graph-identified
    # but unconfirmed cause (MECHANISTIC) gets a "confirm it" ask; a record with
    # no validated root (NO_ROOT) falls through to the plain "identify a root
    # cause" ask below.
    root_cause_unverified = (
        rcc_present and cause_grade == CauseAssuranceGrade.MECHANISTIC
    )
    actionable_solution = has_actionable_solution(case)
    coverage["root_cause_resolution"] = has_root_cause and actionable_solution

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
            if not actionable_solution:
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
    #
    # `dedup_ran` records whether the check actually completed. The final
    # SUGGEST below means "checked, nothing similar" — an unchecked case must
    # never reach it, or a dedup result gets stated that was never obtained
    # (#944).
    dedup_ran = False
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
            # None means no search was issued (no usable tenant, or too little
            # case content to form a query) — NOT "nothing similar found". Only
            # a real search licenses the plain SUGGEST verdict below (#944).
            dedup_ran = similar is not None
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
            # `dedup_ran` stays False, so the caveat branch below owns this.
            logger.warning(
                f"Runbook deduplication check failed for case {case.case_id}: {e}.",
                extra={"case_id": case.case_id, "metric": "runbook.dedup_failed"},
            )

    # Collect every caveat rather than returning on the first one. Content
    # readiness and dedup are independent concerns — a thin case whose dedup
    # also failed has two things wrong with it, and reporting only the first
    # hides the duplicate risk entirely (#944).
    caveats = []

    if readiness.verdict == RunbookReadiness.NEEDS_ENRICHMENT:
        caveats.append(readiness.message)

    # Dedup did not complete — skipped or failed. Naming that is what keeps
    # the final SUGGEST below honest: it means "checked, nothing similar
    # found", which an unchecked case is not entitled to.
    if not dedup_ran:
        caveats.append(
            "I could not check whether a similar runbook already exists — the "
            "knowledge base search is unavailable. Creating one now risks "
            "duplicating existing content; you may want to check the "
            "Dashboard Knowledge Base first."
        )

    if caveats:
        return RunbookSuggestion(
            verdict=RunbookSuggestion.SUGGEST_WITH_CAVEATS,
            message="\n\n".join(caveats),
        )

    return RunbookSuggestion(
        verdict=RunbookSuggestion.SUGGEST,
        message=(
            "This case has enough detail to generate a **runbook** "
            "for the knowledge base. Would you like me to create one? "
            "You can also do this later from the Dashboard."
        ),
    )


async def _find_similar_runbooks_for_case(
    case: "Case", runbook_kb: Any
) -> Optional[list[dict]]:
    """Search for existing runbooks similar to a resolved case, within its tenant.

    Builds a query from case title + root cause + solution for semantic search.

    Returns:
        A list of matches (possibly empty) when a search actually ran, or
        ``None`` when no search was issued at all — no usable tenant, or too
        little case content to form a query.

        The distinction is load-bearing: ``[]`` licenses the caller's "checked,
        nothing similar exists" verdict and ``None`` does not. Collapsing both
        into ``[]`` is what let an unsearched case reach the plain ``SUGGEST``
        branch, which asserts a dedup result that was never obtained (#944).

    Scoped to ``case.organization_id`` and fail-closed: a case carrying no
    usable tenant yields ``[]`` **without a search**, never a deployment-wide
    one. The tenant key is passed to whichever similarity entry point the
    injected KB exposes, so an implementation that does not accept it fails
    loudly instead of quietly searching every tenant.

    The org is resolved through ``usable_tenant_id``, not used raw:
    ``CaseService.create_case`` stamps ``organization_id`` from the *total*
    ``get_current_org_id``, so under ``TENANT_PROVIDER=multi`` a case written
    from an execution context that never bound a tenant carries the Standalone
    sentinel — which is not a tenant there, and must not become the similarity
    predicate. ``search_by_text`` refuses a falsy org with a typed error, but
    this check runs first so the common case is a quiet, logged skip rather
    than an exception.
    """
    organization_id = usable_tenant_id(getattr(case, "organization_id", None))
    if not organization_id:
        logger.warning(
            "Runbook similarity search skipped: case carries no usable tenant",
            extra={"case_id": getattr(case, "case_id", None)},
        )
        return None  # no search ran — see the return contract above

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
        # Nothing to query with, so nothing was checked.
        return None

    query_text = " | ".join(parts)

    # Called directly, with no hasattr probe and no fallback arm. The previous
    # shape probed for `search_by_text` — which did not exist, so the guard was
    # permanently False — then called `search_runbooks(query_text=...)` against
    # a signature that takes `query_embedding`, raising TypeError into an
    # `except` that returned []. Dedup therefore never ran, and every
    # resolution proposed a new runbook regardless of what the KB held (#944).
    #
    # A missing or renamed method must now fail loudly rather than degrade to
    # "no similar runbook": an AttributeError/TypeError here is a wiring bug,
    # and silently answering "none found" is precisely how this rotted
    # unnoticed. `evaluate_runbook_suggestion` decides how to handle it.
    results = await runbook_kb.search_by_text(
        query_text=query_text,
        organization_id=organization_id,
        top_k=3,
        min_similarity=0.65,
    )
    # Attributes are read directly, NOT via getattr with a default.
    # ``search_by_text`` returns ``List[SimilarRunbook]``, so a missing
    # attribute is a contract break — and a ``getattr(r, "similarity_score", 0)``
    # default would silently score it 0, drop it below every threshold, and
    # reproduce "no similar runbook found" from a match that WAS returned:
    # the exact defect this function was fixed for (#944).
    return [
        {
            "similarity_score": r.similarity_score,
            "title": r.runbook.title,
        }
        for r in results or []
    ]
