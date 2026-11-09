"""Terminal Transition Handler

Implements the User-Agent Handshake pattern for terminal state transitions.

All terminal transitions (RESOLVED, CLOSED) require explicit user confirmation.
An offer is opened on the case's offer record (``core/investigation/offers.py``,
which states the record's rules), and the transition executes only when the
user accepts it on a later turn.

Design Decision (Issue B):
- solution_verified is a collaborative state, not system-determined.
- The LLM's interpretation of "it works" can be wrong (user may mean
  "this command works" not "the whole system is fixed").
- Terminal transitions are irreversible, so false positives are costly.

Flow:
1. An opener (the engine, the model, the user's menu) opens a resolve or close
   offer (``open_disposition_offer``); nothing executes
2. The reply carries the offer's confirmation pair
3. Next turn: if the user accepts, ``confirm_open_offer`` executes the
   transition and records the answer; a decline is recorded as an answer

Reference: investigation-lifecycle-logic.md Section 1.4
"""

import importlib
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Optional

from faultmaven.core.investigation.cause_assurance import (
    CONFIRMED_RCC_LIKELIHOOD_FLOOR,
    FIX_FAILED,
    FIX_INTERCEPTION,
    SUBJECT_PROBLEM,
    CauseAssuranceGrade,
    SubjectIndex,
    _graph_hooks,
    cause_elimination_rows,
    conclusion_overclaims,
    confirm_root_from_resolution_absence,
    grade_cause_assurance,
    has_actionable_solution,
    has_problem_definition,
    has_resolution_confirmation,
    has_root_cause_record,
    latest_fix_window,
    mitigation_holds,
    mitigation_observation,
    mitigation_relief,
    off_cause_attempt,
    pending_symptom_check,
    remediations_at,
    resolution_confirmation_rows,
    subject_index,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    close_pivoted_to_resolve_total,
    resolution_cause_leg_total,
)
from faultmaven.core.investigation.narration.offers import (
    close_summary_message,
    readiness_message,
    suggest_resolve_message,
)
from faultmaven.core.investigation.offers import (
    OfferRefusal,
    answer_offer,
    offer_refusal,
    open_offer,
    open_terminal_offer,
    require_answerer,
    standing_resolve_decline,
)
from faultmaven.core.investigation.verification_status import (
    assess_verification_status,
    restatement_hold_governs,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RESULTS_UNREADABLE_CODE
from faultmaven.modules.case.contracts import (
    DECLARED_STABLE,
    PROCEED_UNMITIGATED,
    AnswerKind,
    AnswerVia,
    Case,
    CaseAction,
    CaseState,
    CauseState,
    CloseEligibility,
    ClosureOutcome,
    ClosureReason,
    ConfirmationGap,
    DispositionEligibility,
    EvidenceCategory,
    EvidenceNeed,
    InvestigationActionType,
    NeedState,
    OfferAnswer,
    OfferBasis,
    OfferKind,
    OfferRecord,
    ProblemStatus,
    ResolutionReason,
    ResolveEligibility,
    SolutionState,
    WorkingConclusion,
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
# between the bare readers and Gate 1's intent-resolver guard (#721, #1794).
BARE_CONSENT_MAX_LENGTH = 100


#: The question marks the consent gate reads (#1840), in these scripts and
#: forms: ASCII, fullwidth (U+FF1F), inverted (U+00BF), Arabic (U+061F),
#: reversed (U+2E2E), the small and vertical forms (U+FE56, U+FE16), Greek
#: (U+037E), Armenian (U+055E) and Ethiopic (U+1367); the question emoji, red
#: and white (U+2753, U+2754); and the interrobang and the double marks
#: (U+203D, U+2049, U+2048, U+2047). A script missing here is read as no
#: question, which only costs a re-ask, never a consent or a refusal.
QUESTION_MARKS = frozenset(
    "?\uff1f\u00bf\u061f\u2e2e\ufe56\ufe16\u037e\u055e\u1367"
    "\u2753\u2754\u203d\u2049\u2048\u2047"
)

#: Slack's wire shortcodes for the question emoji (#1840): Slack sends
#: ``:question:`` where the user picked ❓, so a reply carrying one is a
#: question as surely as one carrying the emoji. Read case-insensitively.
QUESTION_SHORTCODES = (":question:", ":grey_question:", ":interrobang:")


def is_question(user_message: "str | None") -> bool:
    """Whether ``user_message`` carries a question mark, in these scripts
    (``QUESTION_MARKS``), as an emoji, or as Slack's shortcode for one
    (``QUESTION_SHORTCODES``; #1840).

    The one question rule of the consent gate. It used to be four separate
    ``"?" in`` checks that read ASCII only, so ``can we close it on friday？``,
    ``¿ok`` and ``friday :question:`` were recorded as refusals. Its readers:
    ``is_substantive_reply`` below, and the engine's ``message_is_substantive``
    (a question withdraws an offer and hands the turn to the model; no typed
    reply but a bare decline token records a refusal, #2222).
    """
    text = user_message or ""
    return any(c in QUESTION_MARKS for c in text) or any(
        code in text.lower() for code in QUESTION_SHORTCODES
    )


def is_substantive_reply(user_message: "str | None") -> bool:
    """INV-26 substance test for a reply that would commit a gate.

    Named for the pending TERMINAL transition because that is the gate it was
    written for. It is read through the bare readers
    (``transition_consent.confirmation_token_class`` and through it
    ``gate1_bare_consent``), so the terminal gate and Gate 1 — whose
    adoption-site guard reads ``gate1_bare_consent`` (fm#918, #1794) — share
    it: two confirm lanes with two substance predicates is the drift this
    exists to prevent, so tuning it (``BARE_CONSENT_MAX_LENGTH``, the
    contrastive tokens) moves both gates at once.

    ‼ "Shared" means shared by the CONFIRM-side readers, and not
    that it is the only substance test in the gate machinery. The engine's 0b
    handler computes its own ``message_is_substantive`` for a different
    question — *is this an answer to the gate at all, or should the proposal
    be withdrawn?* — at ``_PENDING_GATE_SUBSTANTIVE_LEN`` (40) with no
    contrastive token, against this one's 100 plus a question mark
    (``is_question``) and `` but ``. Measured,
    the two disagree in a real band: "we will do it in friday's maintenance
    window instead of now" (59 chars) is substantive to 0b and bare consent
    here. Whether those two should be one predicate is an open question on
    fm#918 — it cannot be settled by narrowing this docstring, because
    unifying them moves shipped terminal-consent behaviour.

    A message is substantive — and therefore can never be consumed as consent
    to an irreversible RESOLVED/CLOSED transition — when it is long (>
    ``BARE_CONSENT_MAX_LENGTH`` chars), carries a question mark
    (``is_question``), or carries a contrastive continuation ("yes but what
    about the replication lag?").
    Substantive input falls to the pending-gate escape lane and is processed
    as a normal turn; only a bare confirmation may execute a terminal
    transition.

    This is the single source of truth for the confirm-side substance test:
    ``transition_consent.confirmation_token_class`` applies it, and through it
    the IntentResolver adoption guard in ``investigation_service`` (#721,
    #1794; a minted Gate 1 confirmation). An empty message is not substantive
    — it is also not consent; callers reject it separately.
    """
    if not user_message:
        return False
    msg = user_message.strip().lower()
    if len(msg) > BARE_CONSENT_MAX_LENGTH:
        return True
    return is_question(msg) or " but " in msg or msg.endswith(" but")


def cause_identification_leg(
    case: "Case", working_conclusion: WorkingConclusion | None = None
) -> "str | None":
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
    # Nor while it is held on an open root (#2036): the chain has validated a
    # cause whose why is unanswered, so a conclusion or a likelihood naming that
    # cause would assert as the root what the chain says is not yet the root.
    if getattr(case.progress, "cause_identification_held_open", False):
        return None

    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        return "rcc"
    wc = (
        working_conclusion
        if working_conclusion is not None
        else case.working_conclusion
    )
    if (
        wc
        and getattr(wc, "statement", None)
        and getattr(wc, "likelihood", 0) >= CAUSE_IDENTIFIED_LIKELIHOOD
        # #987: a MIRROR of the RootCauseConclusion is not an independent
        # signal — whenever one exists the `rcc` leg above already governs. A
        # working conclusion can be built before a retraction later in the same
        # turn (M5's settled preview is built in the solutions step, and a
        # caller's case can still carry last turn's), so counting a mirror here
        # would let a retracted conclusion keep satisfying the backstop through
        # its own stale reflection after retraction cleared every other consumer.
        and not getattr(wc, "mirrors_root_cause_conclusion", False)
    ):
        return "working_conclusion"
    return None


def _cause_identified(
    case: "Case", working_conclusion: WorkingConclusion | None = None
) -> bool:
    """Whether the root cause is known, per the authoritative signal.

    Thin predicate over :func:`cause_identification_leg` — the cause is known iff
    some leg (chain / rcc / working_conclusion) is satisfied. Keeping the leg
    logic in one place means the INV-41 backstop-reliance metric
    (``resolution_cause_leg_total``) can never disagree with the gate it observes.
    """
    return (
        cause_identification_leg(case, working_conclusion=working_conclusion)
        is not None
    )


def _resolution_confirmed(case: "Case") -> bool:
    """Whether the case CONFIRMS a resolution — gone ⇒ gone (M2), both legs on
    record (``cause_assurance.has_resolution_confirmation``): a qualifying
    ``causal_absence_evidence`` row (the cause observed removed) AND a
    qualifying ``symptom_absence_evidence`` row at or after it (the reported
    problem observed gone). The cause leg is the SAME METADATA bar the
    confirm-stamp's candidate filter uses, so the gate can never call a case
    confirmable on a row the stamp refuses for metadata; the stamp's
    additional content-bearing refusal deliberately does NOT bind the gate —
    a mis-citable row still evidences that the user confirmed resolution.

    This is the discriminator between RESOLVED (the cause removed and the
    problem gone with it) and CLOSED-with-documented-solution: a stabilized
    case has the problem leg without the cause (a mitigation relieves the
    symptom while the cause persists), and a fix whose effect on the reported
    problem is unchecked has the cause leg without the problem (#1906: a
    service shown running again, the scrape target the user reported down not
    yet checked). See investigation-lifecycle-logic.md ("Gate strictness —
    absence-driven") and the methodology doc §9.5 (INV-30).

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
    """The ONE resolution-time truth-surface finalizer, called only by
    ``_execute_resolved_transition`` (the one writer of RESOLVED) — a second
    hand-mirrored copy is how two resolve surfaces once diverged on terminal
    truth.

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
    # is the single chokepoint of the one RESOLVED executor
    # (_execute_resolved_transition), so it fires exactly once per resolution.
    # Metric-only.
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


def _rca_declared_infeasible(case: "Case") -> bool:
    """Whether the cause was declared STRUCTURALLY unreachable, with a reason.

    Requires the rationale, not just the flag. ``rca_infeasible`` is an LLM
    self-declared boolean, and this label is consequential in a way the others
    are not: it tells a future reader the cause cannot be found and not to try.
    A model that set it to excuse a hard case would convert a contingent
    evidence gap into a permanent one — worse than a vague label, because it
    forecloses the work rather than merely failing to describe it.

    Requiring a non-empty rationale makes the label carry its own justification
    (the same shape as the symptom-retraction guard). Note the closure-proposal
    path substitutes a generic string when the rationale is missing; that
    fallback is fine for a user-facing sentence but must NOT satisfy this gate,
    so the raw field is read here rather than the message.
    """
    verification = getattr(case, "problem_verification", None)
    if verification is None or not getattr(verification, "rca_infeasible", False):
        return False
    rationale = getattr(verification, "rca_infeasible_rationale", None)
    return bool(rationale and rationale.strip())


def _mitigation_verified(case: "Case") -> bool:
    """Whether a mitigation is on record as verified and still holding: no
    bound persistence after its latest relief (``mitigation_holds``, #2100).
    A stored verdict with no bound rows reads ``(None, None)`` and holds."""
    mitigation = getattr(case.progress, "mitigation", None) if case.progress else None
    if mitigation is None or not getattr(mitigation, "verified", False):
        return False
    return mitigation_holds(*mitigation_relief(case))


def _mitigation_declared_stable(case: "Case") -> bool:
    """Whether the user declared the mitigation stable (#2100, D3.3) and no
    re-check bound to the problem saw it present after the declaration as its
    latest observation (F5, ruled 2026-11-07): a persistence later than the
    declaration turn, with no relief after it. A persistence in the
    declaration's own turn or before it is what the user declared over, so
    it stays their call. ``disposition_at_turn`` is ``emission_turn``-dated,
    so a replayed declaration compares at its hold turn. An undated attempt
    has no bound reads (``mitigation_relief`` dates them from the attempt),
    so F5 cannot apply to it and its declaration stands."""
    mitigation = getattr(case.progress, "mitigation", None) if case.progress else None
    if getattr(mitigation, "disposition", None) != DECLARED_STABLE:
        return False
    declared = getattr(mitigation, "disposition_at_turn", None)
    relief, present = mitigation_relief(case)
    relapsed = (
        declared is not None
        and present is not None
        and present > declared
        and not mitigation_holds(relief, present)
    )
    return not relapsed


def shown_milestones(case: "Case") -> list[str]:
    """The completed milestones a close shows the user: the close prompt's
    count and the report's Investigation State read this one list. A
    ``mitigation_verified`` a bound relapse followed is left out, as the
    closure reason leaves it (``_mitigation_verified``, #2100); a stored
    verdict with no bound row is read as stored."""
    progress = getattr(case, "progress", None)
    milestones = list(progress.completed_milestones) if progress else []
    if "mitigation_verified" in milestones and not _mitigation_verified(case):
        milestones.remove("mitigation_verified")
    return milestones


def _span_words(span) -> str:
    """A data window in the unit a reader takes in, rounded half up before
    the unit is picked: minutes under 2 hours, hours under 2 days, then
    days (#2100, C2)."""
    seconds = span.total_seconds()
    n, unit = math.floor(seconds / 60 + 0.5), "minute"
    if n >= 120:
        n, unit = math.floor(seconds / 3600 + 0.5), "hour"
        if n >= 48:
            n, unit = math.floor(seconds / 86400 + 0.5), "day"
    return f"about {n} {unit}{'' if n == 1 else 's'}"


def mitigation_closure_line(case: "Case") -> Optional[str]:
    """What the record holds about the case's mitigation, as one line for the
    engine's closure texts (#2100, D3.3, D5.3): the closure reply, the
    rca-infeasible propose-close and the report. None with no accepted
    mitigation. It states each fact that holds, in this order: the user's
    declaration, the bound relief (with the relief row's data window when
    ``mitigation_observation`` dates one), the problem seen again, the
    stall's proceed; with none of them, that no bound re-check is on record,
    or, for an attempt with no turn, that none can be tied to it.
    The word for the user's declaration is "stable", and "stabilized" is
    never used: the line states the record, not a verdict."""
    seen = mitigation_observation(case)
    if seen is None:
        return None

    def at(turn) -> str:
        return f" (turn {turn})" if turn is not None else ""

    clauses = []
    if seen.disposition == DECLARED_STABLE:
        clauses.append(f"you reported the mitigation stable{at(seen.disposition_turn)}")
    if seen.relief_turn is not None:
        after = "the mitigation" + (
            " and a later fix" if seen.fix_before_relief else ""
        )
        clause = (
            f"the problem was observed relieved after {after} (turn {seen.relief_turn})"
        )
        if seen.span is not None:
            clause += f", and the data it cites covers {_span_words(seen.span)}"
        clauses.append(clause)
    if seen.present_turn is not None:
        clauses.append(
            f"the problem was seen again at turn {seen.present_turn}"
            + (", before that relief" if seen.holding else "")
        )
    if seen.disposition == PROCEED_UNMITIGATED:
        clauses.append(
            "the investigation went on before the mitigation was verified"
            + at(seen.disposition_turn)
        )
    if not clauses and seen.attempt_turn is None:
        clauses.append(
            "a mitigation was applied; its attempt is undated, so no re-check can "
            "be tied to it"
        )
    elif not clauses:
        clauses.append(
            f"a mitigation was applied{at(seen.attempt_turn)}; no re-check bound to "
            "the problem is on record"
        )
    return "; ".join(clauses)


def _fix_documented_not_applied(case: "Case") -> bool:
    """Whether a fix is on record but was never applied.

    Two conditions, both load-bearing:

    - **The cause is established** - ``_cause_identified``, the same predicate
      ``_maybe_propose_deferred_close`` requires via ``_solution_cause_validated``
      (M5's gate delegates here). Without it, a stale ``solution_feasible`` flag
      on a case whose cause has since fallen would report "cause identified and
      fix documented" for a case with no cause: solution records are monotone and
      are never withdrawn, so the flag alone outlives the thing it describes.
    - **A solution is on record.** "Deferred" with nothing deferred is not an
      outcome.
    - **Not only interceptions run.** When every fix the user ran was an
      interception (#2036 D2.3) and none is pending, the fix on record was
      applied, so the case closes under another reason.

    Deliberately NOT gated on ``solution_feasible == DEFERRED``. A case whose fix
    was marked FEASIBLE but simply closed without executing is in exactly the
    same position - the cause was found, the fix is written down, nobody ran it -
    and routing it to ``closed_insufficient_evidence`` would report "insufficient
    evidence to establish the problem or its cause" about a case that established
    both. Whether the user formally declared the fix deferred is bookkeeping; the
    outcome is the same.
    """
    progress = getattr(case, "progress", None)
    if progress is None or not _cause_identified(case):
        return False
    fixes = [
        a
        for a in getattr(case, "proposed_actions", None) or []
        if a.action_type == InvestigationActionType.SOLUTION
        and a.state in ("pending", "accepted")
    ]
    if fixes and all(getattr(a, "fix_kind", None) == FIX_INTERCEPTION for a in fixes):
        return False
    return bool(
        getattr(progress, "solution_proposed", False)
        or getattr(case, "solutions", None)
    )


def _restatement_held_close(case: "Case") -> bool:
    """Whether this case ends on the §7.1 restatement hold — the evidence
    supported a cause and the guard held EVERY unsettled root for want of a
    distinct mechanism statement (#1195).

    Reads the SAME governing predicate the disposition layer reads
    (``verification_status.restatement_hold_governs``), so the closure reason
    and the status can never describe one case differently. It used to read the
    raw graph summary and apply only the sole-block arm, which let two shapes
    through that the status path was shielded from (#1195 review): a case
    holding a VALIDATED root — a PROMOTED cause — closed under "a cause was
    supported but never stated distinctly", contradicting this predicate's own
    name; and a case whose symptom was never verified closed the same way. Both
    guards now live in the single predicate, so this consumer cannot apply half
    of them.

    Keyed on the structural hold and NOT on ``verification_status ==
    RESTATEMENT_HELD``: that cell additionally requires a stall, and a user may
    close before the stall thresholds are met. Keying on the cell would repeat,
    one label over, the mistake this function's docstring already records — the
    old ``closed_insufficient_evidence`` was gated on the INSUFFICIENT_EVIDENCE
    cell and so was unreachable for exactly the population it best described.
    An absent hook degrades to the generic bucket, which is the pre-#1195
    behaviour (handled inside the shared predicate).
    """
    return restatement_hold_governs(case) is not None


def derive_closure_reason(case: "Case") -> ClosureReason:
    """Engine-only derivation of closure_reason from case state.

    Every close carries a REASON — why the case ended, never merely when.
    Derived most-specific-first:

    - ``inquiry_only`` — case is still in INQUIRY (no investigation started).
    - ``closed_false_alarm`` — the evidence showed the reported symptom was
      never present (``problem_status`` INVALIDATED). Decisive, so first among
      the INVESTIGATING reasons: nothing else can stand on a problem that never
      existed.
    - ``resolution_declined`` — the fix was confirmed to work (the closure
      verdict is SUGGEST_RESOLVE) and the user declined to mark it resolved:
      a resolve decline stands (``declined_resolve_entry``, #1895). The user's
      own close is then not pivoted to resolve (#2360), and this says why the
      case closed unresolved. Ahead of ``solution_deferred``, which on such a
      case would report a confirmed fix as never applied.
    - ``solution_deferred`` — the cause is identified and a fix is documented,
      but it was never applied: implementation happens out-of-band (a change
      request, a maintenance window, another team), or the case simply closed
      before anyone ran it. The most complete non-resolved outcome there is —
      nothing was missing. Ahead of the remaining INVESTIGATING reasons because
      where several hold, the one carrying the most established knowledge
      should win.
    - ``closed_rca_infeasible`` — the cause is structurally unreachable (an
      uncontrollable external dependency, an EOL system), declared via
      ``rca_infeasible`` WITH a rationale. Ranked above ``mitigation_sufficient``
      because the engine's only path to it fires on a mitigation verified or
      declared stable, so both hold on essentially every such close; this label
      additionally explains why RCA stopped, so letting the other win would
      shadow the more informative fact every time. It asserts nothing about
      the mitigation: the closure texts state its record beside it
      (``mitigation_closure_line``), a relapse included.
    - ``mitigation_sufficient`` — a mitigation verified by a bound re-check and
      still holding, or declared stable by the user with no bound re-check
      seeing the problem present after the declaration as its latest
      observation (#2100, F5 ruled 2026-11-07); a ``proceed_unmitigated``
      or such a relapse falls through. The symptom is
      relieved and RCA was deferred BY CHOICE, the cause still reachable if
      anyone returns to it. The one closure here that is not a failure of any
      kind, which is why it is not folded into a generic bucket.
    - ``closed_restatement_held`` — the evidence supported a cause, but the §7.1
      restatement guard held EVERY unsettled root: the statement never added
      anything over the problem, so the case ends holding a cause it could not
      promote for want of a distinct MECHANISM (#1195). Ranked immediately above
      the generic bucket and nowhere higher — a verified mitigation or a
      declared-infeasible RCA is the more informative fact where either holds,
      and a case whose cause WAS promoted never reaches this predicate. Keyed on
      the structural hold rather than on the ``RESTATEMENT_HELD`` status cell,
      for the reason recorded below: the cell additionally requires a stall, and
      a user may close before one, which would drop the case into a bucket that
      misdescribes it.
    - ``closed_insufficient_evidence`` — the default for any other close from
      INVESTIGATING: what was needed was never established, at the SYMPTOM level
      (the problem could not be verified) or at the CAUSE level (verified, but
      no cause grounded). Capture-on-close: the honest partial is recorded for
      calibration and the flywheel. The residual candidates (non-refuted /
      retired hypotheses) and the specific unmet (unobtainable) need are already
      persisted on the case and surfaced by the closure report — no separate
      snapshot is stored. It is capture-ONLY: the engine never nudges toward
      this close (no ``SUGGEST_CLOSE``); the structured handoff already lists
      pause/close as a user option (insufficient-evidence-handling.md §5.4 / D4).

    This no longer keys on the ``INSUFFICIENT_EVIDENCE`` verification-status
    cell. That cell sits behind ``work_gate_passed`` (>=2 hypotheses across >=2
    categories) — CAUSE work that a case stuck at symptom verification never
    does — so the label was unreachable for exactly the population it best
    described, and every such close fell to the generic bucket instead.

    The LLM never authors closure_reason; it's purely engine-derived from
    structured case state.
    """
    if case.state == CaseState.INQUIRY:
        return ClosureReason.INQUIRY_ONLY

    if case.progress.problem_status == ProblemStatus.INVALIDATED:
        return ClosureReason.CLOSED_FALSE_ALARM

    if declined_resolve_entry(case) is not None:
        return ClosureReason.RESOLUTION_DECLINED

    if _fix_documented_not_applied(case):
        return ClosureReason.SOLUTION_DEFERRED

    if _rca_declared_infeasible(case):
        return ClosureReason.CLOSED_RCA_INFEASIBLE

    if _mitigation_verified(case) or _mitigation_declared_stable(case):
        return ClosureReason.MITIGATION_SUFFICIENT

    if _restatement_held_close(case):
        return ClosureReason.CLOSED_RESTATEMENT_HELD

    return ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE


# ============================================================
# DISPOSITION ELIGIBILITY (denormalized read view)
# ============================================================

# Per-disposition eligibility, published on every case read so a client can
# gate the status menu's Close on case content, not only on the structural
# action graph. The half of the offer projection (``offer_projection.project_offers``)
# that is written on save; the value sets are the published enums
# ``ResolveEligibility`` and ``CloseEligibility``, whose ``x-enum-descriptions``
# carry each value's meaning (contract text).
#
# ‼ The two keys answer for DIFFERENT AUDIENCES, and conflating them is how a
# client ends up rendering a control that cannot do what it says.
#
#   "closed"   — gates a user CONTROL. Closing is user-selectable, so this key
#                says whether to render it.
#   "resolved" — gates NOTHING in the UI. RESOLVED is not on the status menu:
#                this is the engine's own readiness verdict, and what it
#                decides is whether the agent OFFERS the resolution (INV-43).
#
# ``closed`` in INVESTIGATING is ``suggests_alternative`` exactly when the open
# offer is a resolve offer (Close and Resolve are never offered together), or
# when the case is resolvable (closure verdict SUGGEST_RESOLVE) and no resolve
# decline stands (INV-37 would pivot the close to a resolve proposal);
# otherwise ``ready``. A standing decline keeps Close under a pending revision
# (#2379), and a resolution request is not an offer, so Close shows beside it
# (#2408, ruling 1).
#
# ‼ KNOWN ASYMMETRY, stated rather than hidden. ``resolved`` was removed from
# ``valid_next_states`` outright, on the argument that a description is not a
# gate. ``closed`` on a SUGGEST_RESOLVE case is still LISTED and gated only by
# this field. The consequence differs in kind: a rendered Resolve produced a
# validation ERROR, while a rendered Close produces a coherent REDIRECT — the
# engine pivots it to "shall I mark this resolved?" and nothing is lost.
# Closing the gap properly means making ``valid_next_states`` depend on case
# content, which is a contract decision rather than a wording one.


def derive_disposition_eligibility(case: "Case") -> DispositionEligibility:
    """The per-disposition eligibility of ``case``: the projection's
    eligibility, written at the save chokepoint (``CaseRepository.
    prepare_for_save``). Pure.

    - INQUIRY: only CLOSED is a valid edge; ``{resolved: not_eligible,
      closed: ready}``.
    - INVESTIGATING: ``resolved`` maps ``assess_resolution_readiness`` (READY
      → ready, NEEDS_INFO → needs_info, SUGGEST_CLOSE → not_eligible).
      ``closed`` is ``suggests_alternative`` while a resolve offer is open, or
      while ``close_pivots_to_resolve`` holds; otherwise ``ready``.
    - Terminal: both ``not_eligible``.
    """
    if case.state == CaseState.INQUIRY:
        return DispositionEligibility(
            resolved=ResolveEligibility.NOT_ELIGIBLE, closed=CloseEligibility.READY
        )
    if case.state != CaseState.INVESTIGATING:
        return DispositionEligibility(
            resolved=ResolveEligibility.NOT_ELIGIBLE,
            closed=CloseEligibility.NOT_ELIGIBLE,
        )

    resolution = assess_resolution_readiness(case)
    if resolution.verdict == ResolutionReadiness.READY:
        resolved = ResolveEligibility.READY
    elif resolution.verdict == ResolutionReadiness.NEEDS_INFO:
        resolved = ResolveEligibility.NEEDS_INFO
    else:  # SUGGEST_CLOSE
        resolved = ResolveEligibility.NOT_ELIGIBLE

    offer = open_terminal_offer(case)
    if offer is not None and offer.kind == OfferKind.RESOLVE:
        closed = CloseEligibility.SUGGESTS_ALTERNATIVE
    elif close_pivots_to_resolve(case, closure_verdict(case)):
        closed = CloseEligibility.SUGGESTS_ALTERNATIVE
    else:
        closed = CloseEligibility.READY
    return DispositionEligibility(resolved=resolved, closed=closed)


def declined_resolve_entry(
    case: Case, verdict: Optional[str] = None
) -> Optional[OfferAnswer]:
    """The resolve decline that stands, or None (#1895).

    One stands while the case is INVESTIGATING, the closure verdict is
    SUGGEST_RESOLVE (a resolve is what would be offered) and a decline of a
    resolve offer stands on the record (``offers.decline_stands``: no
    solution or confirmation it never saw has been recorded). The newest one,
    so the reopen chip's builder and its verifiers name the same decline.
    ``verdict`` is the closure verdict when the caller already holds it.
    """
    if case.state != CaseState.INVESTIGATING:
        return None
    if verdict is None:
        verdict = closure_verdict(case)
    if verdict != ClosureReadiness.SUGGEST_RESOLVE:
        return None
    return standing_resolve_decline(case)


def close_pivots_to_resolve(case: Case, verdict: str) -> bool:
    """Whether a close on this case becomes a resolve proposal (INV-37), given
    its closure ``verdict``: the verdict is SUGGEST_RESOLVE and no resolve
    decline stands (#2360).

    Close is the user's choice at any time. A resolvable case still pivots a
    close to the resolution it has earned, but once the user has declined that
    resolution the pivot would only re-ask what they answered, so their close
    closes, with ``resolution_declined`` as its reason. Read by the status
    menu's Close (``derive_disposition_eligibility``, which also hides Close
    while a resolve offer is open), the menu close itself
    (``_close_on_explicit_intent``) and the confirm-time guard
    (``confirm_open_offer``). The model's close does not read it: it still
    pivots, and is refused against the decline.
    """
    if verdict != ClosureReadiness.SUGGEST_RESOLVE:
        return False
    return declined_resolve_entry(case, verdict) is None


def _outcome():
    """The case outcome record's module (#2615): ``closure_outcome`` for the
    four closure entries below and ``record_case_outcome`` for the two
    transitions. Loaded by name at call time, as ``cause_assurance`` loads
    ``causal_graph``: the record imports this module for the derivations it
    records, so an import of it here would close a cycle."""
    return importlib.import_module("faultmaven.core.investigation.outcome")


def open_disposition_offer(
    case: Case,
    *,
    kind: OfferKind,
    basis: OfferBasis,
    summary: str = "",
    evidence_ids: Iterable[str] = (),
    pivoted_from_close: bool = False,
    readiness_pivot: bool = False,
) -> Optional[OfferRecord]:
    """``offers.open_offer`` for a resolve or close offer: a close carries the
    reason derived from the case as it stands (``closure_outcome``, #2615).
    Returns the new record, or None when a standing decline binds the offer or
    the turn quiet holds it (``disposition_refusal`` names which).
    """
    return open_offer(
        case,
        kind=kind,
        basis=basis,
        summary=summary,
        evidence_ids=evidence_ids,
        closure_reason=(
            _outcome().closure_outcome(case).reason if kind == OfferKind.CLOSE else None
        ),
        pivoted_from_close=pivoted_from_close,
        readiness_pivot=readiness_pivot,
    )


def disposition_refusal(
    case: Case,
    *,
    kind: OfferKind,
    basis: OfferBasis,
    readiness_pivot: bool = False,
) -> Optional[OfferRefusal]:
    """Why ``open_disposition_offer`` refuses this offer, or None: the
    writer's own decision (``offers.offer_refusal``) on the same arguments."""
    return offer_refusal(
        case,
        kind=kind,
        basis=basis,
        closure_reason=(
            _outcome().closure_outcome(case).reason if kind == OfferKind.CLOSE else None
        ),
        readiness_pivot=readiness_pivot,
    )


def resolve_reopen_key(entry: OfferAnswer) -> str:
    """The key the "Mark it resolved" chip carries as its ``proposal_id``:
    ``reopen:`` and the declined offer's id (#1895). The chip's builder and
    both verifiers derive it from the same standing decline
    (``declined_resolve_entry``), so a chip on screen stays valid while that
    decline stands."""
    return f"reopen:{entry.proposal_id}"


def resolve_reopen_admitted(
    case: Case, to_state: Any, proposal_id: Optional[str]
) -> bool:
    """Whether a ``status_transition`` to RESOLVED is the reopen chip for the
    resolve decline standing now (#1895).

    RESOLVED is not user-selectable (``earned_edge_refusal``), and this does
    not make it so: it admits exactly one request, the engine's own
    re-presentation of the offer the user declined, named by the key of the
    decline that stands. Any other key, or no decline standing, is refused as
    before. The admitted request only PROPOSES; the user confirms (INV-03).
    """
    if not proposal_id or str(getattr(to_state, "value", to_state)) != "resolved":
        return False
    entry = declined_resolve_entry(case)
    return entry is not None and resolve_reopen_key(entry) == proposal_id


def confirm_open_offer(case: Case, user_id: str, *, via: AnswerVia) -> bool:
    """Execute the open resolve or close offer the user accepted.

    Returns True when a terminal transition executed. Returns False on an
    INV-37 resolve-preservation pivot: an accepted close on a resolvable case
    whose resolution the user has not declined is answered ``pivoted`` and a
    resolve offer opened in its place, carrying the close's basis
    (``pivoted_from_close``); the caller re-presents it. ``accepted`` is
    recorded only after the executor returns, so one that raises leaves the
    offer open.

    Raises:
        ValueError: when no resolve or close offer is open, ``user_id`` is not
            the case's effective driver (checked before anything executes), or
            the case is in an invalid state for the transition.
    """
    offer = open_terminal_offer(case)
    if offer is None:
        raise ValueError(f"case {case.case_id} has no resolve or close offer open")
    require_answerer(case, user_id)

    reason = offer.closure_reason
    closure = None
    # Closed-gate resolve-preservation (INV-37): a resolvable case must never
    # terminally commit to CLOSED. A resolution confirmation can land AFTER the
    # close was offered, so the pivot is re-checked here, at the execution
    # chokepoint. "resolve = close WITH resolution; close = close WITHOUT
    # resolution": it is always safe to resolve a case that can be resolved.
    # Scoped to INVESTIGATING (RESOLVED is not an edge from INQUIRY), and not
    # while a resolve decline stands (``close_pivots_to_resolve``, #2360): the
    # user said "not resolved", so the close closes, whoever offered it.
    if offer.kind == OfferKind.CLOSE and case.state == CaseState.INVESTIGATING:
        readiness = assess_closure_readiness(case)
        if close_pivots_to_resolve(case, readiness.verdict):
            before = case.offer_record
            case.offer_record = answer_offer(
                case,
                proposal_id=offer.proposal_id,
                answer=AnswerKind.PIVOTED,
                via=via,
                actor_id=user_id,
            )
            record = open_disposition_offer(
                case,
                kind=OfferKind.RESOLVE,
                basis=offer.basis,
                summary=readiness.message,
                pivoted_from_close=True,
            )
            if record is not None:
                case.offer_record = record
                close_pivoted_to_resolve_total.inc()
                logger.info(
                    f"Case {case.case_id}: accepted CLOSE pivoted to a RESOLVED "
                    f"offer — case is resolvable (SUGGEST_RESOLVE). "
                    f"Resolve-preservation (INV-37): a resolvable case is never "
                    f"closed."
                )
                return False
            # A decline binds the resolve after all: the close closes.
            case.offer_record = before
        if (
            readiness.verdict == ClosureReadiness.SUGGEST_RESOLVE
            or reason == ClosureReason.RESOLUTION_DECLINED
        ):
            # Re-derived: the stored reason may describe a state that no
            # longer holds. A close offered before the case became resolvable
            # would record a confirmed fix as ``solution_deferred``; one
            # offered under the decline, before a failed fix, would record an
            # unconfirmed fix as ``resolution_declined``.
            closure = _outcome().closure_outcome(case)
            reason = closure.reason

    if offer.kind == OfferKind.RESOLVE:
        _execute_resolved_transition(case, user_id)
    else:
        _execute_closed_transition(
            case, user_id, ClosureReason(reason), closure=closure
        )
    case.offer_record = answer_offer(
        case,
        proposal_id=offer.proposal_id,
        answer=AnswerKind.ACCEPTED,
        via=via,
        actor_id=user_id,
    )
    return True


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
    # run but the model never named (``accepted_action_ref``; after a first
    # fix an acceptance registers only the fix it names, #2105) also reads
    # PROPOSED rather than falsely-certain APPLIED.
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

    _outcome().record_case_outcome(case, at="resolved")
    logger.info(f"Case {case.case_id} transitioned to RESOLVED (terminal state)")


def _execute_closed_transition(
    case: Case,
    user_id: str,
    closure_reason: ClosureReason,
    *,
    closure: Optional[ClosureOutcome] = None,
) -> None:
    """Execute → CLOSED after user confirmation.

    closure_reason is the engine-derived reason. Callers are
    `confirm_open_offer`, which reads it from the close offer, where
    `open_disposition_offer` placed it via `closure_outcome`, and
    `execute_user_closure`, which derives it. ``closure``: the
    ``ClosureOutcome`` the caller computed just before (``closure_outcome``),
    which the case outcome record written with the transition persists
    (#2615); None for a close offer accepted on its stored reason, whose
    closure the record reads off the closed case.

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

    _outcome().record_case_outcome(case, at="closed", closure=closure)
    logger.info(f"Case {case.case_id} transitioned to CLOSED (terminal state)")


def execute_user_closure(case: Case, user_id: str) -> ClosureReason:
    """Close a case outside the chat confirmation flow (REST close, #915).

    The API call itself is the user's confirmation — there is no pending
    proposal to confirm — so the closure_reason is derived at execution
    time by the same rule the chat flow applies at propose time
    (``derive_closure_reason``), and the transition runs through the same
    executor as the chat close. One closure rule for both surfaces; the
    previous REST route mutated ``case.state`` directly and died on the
    terminal-state validator (no ``closed_at``).

    A case the chat path would pivot to a resolve offer (INV-37,
    ``close_pivots_to_resolve``) is closed as the user's decline of that
    resolution (#2418): an open resolve offer is answered ``declined``, or,
    with none open, the resolve offer the pivot would open is opened and
    answered ``declined``. The reason then derives as
    ``resolution_declined``, the reason the chat path's decline-then-close
    records; the record here is a MENU decline of the resolve offer, with no
    close offer accepted (an open one lapses).

    Mutates ``case`` in place; the caller owns persistence. Returns the
    derived closure_reason. ``user_id`` must be the case's effective driver:
    the close and any decline are that user's (``require_answerer``).

    Raises:
        ValueError: If ``user_id`` is not the case's effective driver,
            checked before anything is written; or if the case is not in a
            closable (INQUIRY or INVESTIGATING) state — callers should
            pre-check terminal states and surface those as a conflict.
    """
    from faultmaven.core.investigation.problem_status import cancel_revision

    require_answerer(case, user_id)
    # The user's own close moves past a revision awaiting re-confirmation: it
    # lapses (it is not declined), and the reason derives from where the case
    # stood before the revision was proposed.
    cancel_revision(case, via=AnswerVia.MENU, answer=AnswerKind.LAPSED)
    qualifies = case.state == CaseState.INVESTIGATING and close_pivots_to_resolve(
        case, closure_verdict(case)
    )
    # An open offer lapses: the case is closing whatever was offered. On a
    # case that qualifies, an open resolve offer is the one the close answers.
    offer = open_terminal_offer(case)
    if offer is not None and not (qualifies and offer.kind == OfferKind.RESOLVE):
        case.offer_record = answer_offer(
            case,
            proposal_id=offer.proposal_id,
            answer=AnswerKind.LAPSED,
            via=AnswerVia.MENU,
        )
        offer = None
    if qualifies:
        if offer is None:
            # The resolve offer the chat path's pivot opens. A menu basis is
            # user-origin, so no decline or turn quiet refuses it.
            record = open_disposition_offer(
                case,
                kind=OfferKind.RESOLVE,
                basis=OfferBasis.MENU,
                summary=assess_closure_readiness(case).message,
                pivoted_from_close=True,
            )
            if record is None or record.open is None:
                raise ValueError(
                    f"case {case.case_id}: the resolve offer a close declines "
                    "was refused"
                )
            case.offer_record = record
            offer = record.open
        case.offer_record = answer_offer(
            case,
            proposal_id=offer.proposal_id,
            answer=AnswerKind.DECLINED,
            via=AnswerVia.MENU,
            actor_id=user_id,
        )
    closure = _outcome().closure_outcome(case)
    _execute_closed_transition(case, user_id, closure.reason, closure=closure)
    return closure.reason


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

    def __init__(
        self,
        verdict: str,
        missing: list[str],
        *,
        reason: ResolutionReason,
        pending_check: EvidenceNeed | None = None,
        confirmation_gaps: list[ConfirmationGap] | None = None,
        **params: Any,
    ):
        self.verdict = verdict
        self.missing = missing
        # What the message names beyond the reason, the missing items and
        # the gaps (#2636): the composer is ``narration.offers``.
        self.params = params
        # NEEDS_INFO on a confirmed case: the check the model asked for after
        # the fix, still unanswered (``pending_symptom_check``, #1906 D5.2).
        # The readers branch on it, never on the message.
        self.pending_check = pending_check
        # Which return answered, and each confirmation ask the
        # documentation-gap return made, for the case outcome record (#2615).
        self.reason = reason
        self.confirmation_gaps = list(confirmation_gaps or [])

    @property
    def message(self) -> str:
        """The user-facing message (``narration.offers.readiness_message``)."""
        return readiness_message(
            self.reason,
            missing=self.missing,
            gaps=self.confirmation_gaps,
            **self.params,
        )

    def offer_params(self) -> dict:
        """What an offer code carries to render this message after the
        record (#2636): the decision's reason and fields, and its params."""
        return {
            "reason": self.reason,
            "missing": list(self.missing),
            "gaps": list(self.confirmation_gaps),
            **self.params,
        }


#: What a model-authored name can carry: a record id, or the ref the model writes.
_RECORD_ID = re.compile(
    r"\b(?:(?:act|att|case|cdo|ce|cn|doc|eneed|ev|file|hyp|sol)_[0-9a-f]{12}"
    r"|new_index_\d+)\b"
)


def _cause_recheck_names(case: "Case", window) -> dict:
    """What the readiness ask for the cause leg after a dated fix names
    (#1927, #2557): the root's statement and the fix, so the user shows what
    the fix changed at the cause (what its cited basis shows, D6), not the
    relief. Several roots or fixes are listed; with nothing to name (an
    out-of-band fix, window 0), or a name carrying a record id, none, and the
    ask is the generic line (``narration.offers``)."""
    nodes = getattr(case, "causal_nodes", None) or {}
    subjects = subject_index(case)
    roots = subjects.pool_roots
    if window.kind is not None:
        roots = roots.intersection(window.targets)
    statements = [
        nodes[n].statement
        for n in sorted(roots)
        if n in nodes and getattr(nodes[n], "statement", None)
    ]
    fixes = [
        a.description
        for a in remediations_at(case, window.fix_turn)
        if getattr(a, "description", None)
    ]
    if (
        statements
        and fixes
        and not any(_RECORD_ID.search(name) for name in (*statements, *fixes))
    ):
        return {
            "recheck_cause": "; ".join(_clip(name) for name in statements),
            "recheck_fix": "; ".join(_clip(name) for name in fixes),
        }
    return {}


def _relief_without_cause_recheck(
    case: "Case", window, subjects: "SubjectIndex | None" = None
) -> bool:
    """The latest fix window's latest observation of the problem is a
    problem-gone row, and the window holds no cause re-check (#1927): the one
    leg the user still owes is the cause. Not when the problem was seen again
    after the relief — then it is not gone. Both observations are bound to
    the problem and dated by their data (#1906 D1, #2145), as the windows
    read them. A cause re-check counts only when it could confirm
    (``FixWindow.confirmed_cause_rows``): a re-check of a fallen cause after
    another cause's fix still leaves the cause leg owed. Never on an
    ``off_cause`` window: no re-check confirms it (#2036 D2.3). On window 0 only
    with a pool and a row the window binds: with nothing confirmed, the rule
    refused that re-check (#2557)."""
    if window.confirmed_cause_rows or window.off_cause:
        return False
    subjects = subject_index(case) if subjects is None else subjects
    if window.fix_turn is None and not (subjects.pool_roots and window.cause_rows):
        return False
    latest: dict = {}
    for e in case.evidence or []:
        category = getattr(e, "category", None)
        turn = subjects.observed(e)
        if subjects.subject(e) == SUBJECT_PROBLEM and window.holds(turn):
            latest[category] = max(latest.get(category, -1), turn)
    gone = latest.get(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    return gone is not None and gone >= latest.get(
        EvidenceCategory.SYMPTOM_EVIDENCE, -1
    )


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
    # THE resolution bar: gone ⇒ gone, CONFIRMED — the cause observed removed (a
    # causal_absence row) and the reported problem observed gone after it (a
    # symptom_absence row). That alone makes a case RESOLVED: it implies the cause
    # is known and a fix took effect, so it is sufficient on its own. A separate
    # root-cause / solution *record* is for documentation quality (and the higher
    # runbook bar), NOT a resolution gate. Critically, an out-of-band fix still
    # resolves with no SolutionToAdd: the user submits the output of the checks
    # (the cause gone, the problem gone) and both rows come from that data. Their
    # words alone ("I fixed X, it works now") are a claim and yield no row (#1930).
    # Requiring a solution record here was
    # the documented stuck-loop: the gate refused a clear "yes, it's resolved" and
    # kept demanding a "documented solution" the user did not have, then closed.
    # A false alarm is never resolved: the evidence showed there was nothing to
    # fix. SUGGEST_CLOSE, so disposition eligibility reads resolved
    # "not_eligible" rather than "needs_info".
    if case.progress.problem_status == ProblemStatus.INVALIDATED:
        return ResolutionReadiness(
            verdict=ResolutionReadiness.SUGGEST_CLOSE,
            missing=[],
            reason=ResolutionReason.FALSE_ALARM,
        )

    has_cause = _cause_identified(case)
    has_solution = bool(case.solutions and len(case.solutions) > 0)
    has_evidence = bool(case.evidence and len(case.evidence) > 0)
    # Named to avoid shadowing the imported has_resolution_confirmation().
    resolution_confirmed = _resolution_confirmed(case)

    if resolution_confirmed:
        # A check of the problem the model asked for after the fix, still
        # unanswered, holds the offer (#1906 D5.2, ruling 4): the reply is
        # asking for evidence the offer would claim. The frame below without
        # its close paragraph: without a standing resolve decline a close of a
        # confirmed case pivots to the resolve (ruling 2, INV-37), and under
        # one the turn's "Close without resolving" chip carries the close
        # (#2360).
        check = pending_symptom_check(case)
        if check is not None:
            return ResolutionReadiness(
                verdict=ResolutionReadiness.NEEDS_INFO,
                missing=["the check you asked for"],
                pending_check=check,
                reason=ResolutionReason.PENDING_CHECK,
                request_text=check.request_text,
            )
        return ResolutionReadiness(
            verdict=ResolutionReadiness.READY,
            missing=[],
            reason=ResolutionReason.CONFIRMED,
        )

    # Not confirmed yet: the user wants to resolve but the case does not show the
    # cause gone and the problem gone with it. This is the documentation-gap-fill
    # ask (common out of band, or mid-stream) — collect what's needed to document a
    # resolution AND confirm the fix actually worked. A stabilized/deferred account
    # never confirms it and converges to Close. Close is offered up front as the
    # alternative.
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
            missing=missing,
            reason=ResolutionReason.NOTHING_ON_RECORD,
        )

    # The latest fix was seen failing (#1927): the cause shown removed, then the
    # problem seen again. Nothing observed in that fix's window can confirm a
    # resolution any more — not a relief after a workaround, a later re-check of
    # the cause, or "it's fine now" — so asking the user to confirm again would
    # loop. A resolution needs a NEW fix, recorded, then seen working.
    window = latest_fix_window(case)
    # The latest attempt acted on no cause now identified (#2036 D2.3): an
    # interception, or a remediation of a cause since deepened past. No check
    # after it confirms the cause, so asking for one would loop.
    off_cause = off_cause_attempt(case)
    if off_cause is not None:
        return ResolutionReadiness(
            verdict=ResolutionReadiness.NEEDS_INFO,
            missing=["a fix of the root cause", *missing],
            reason=ResolutionReason.FIX_OFF_CAUSE,
            off_cause_turn=off_cause,
        )
    if window.state == FIX_FAILED:
        return ResolutionReadiness(
            verdict=ResolutionReadiness.NEEDS_INFO,
            missing=["a new fix after the one that did not hold", *missing],
            reason=ResolutionReason.FIX_FAILED,
        )

    # Otherwise: ask the user to fill the documentation gaps. The problem may
    # have been resolved out of band — invite the missing essentials rather than
    # rejecting the request (the asks: ``narration.offers.readiness_message``).
    gaps: list[ConfirmationGap] = []
    names: dict = {}
    if _relief_without_cause_recheck(case, window):
        # The latest fix's window shows the problem gone but no cause re-check
        # (#1927): a relief alone may be a workaround's, so the fix is
        # unconfirmed. Ask for the leg that is missing, not the one the user
        # just gave.
        gaps.append(ConfirmationGap.RELIEF_WITHOUT_CAUSE_RECHECK)
        names = _cause_recheck_names(case, window)
    elif cause_elimination_rows(case):
        # The cause leg stands; only the problem the user reported is
        # unchecked (#1906). Say so, rather than re-asking for the fix.
        gaps.append(ConfirmationGap.PROBLEM_UNRECHECKED)
    pv = case.problem_verification
    if (
        case.progress.problem_status == ProblemStatus.VERIFIED
        and pv is not None
        and not getattr(pv, "verifying_evidence_ids", None)
        and not any(
            getattr(e, "category", None) == EvidenceCategory.SYMPTOM_EVIDENCE
            for e in case.evidence or []
        )
    ):
        # Nothing on record showed the problem (#1906, ruled 2026-11-03): no
        # re-check can bind to it until the original failing output is cited.
        # With a symptom row on record the model cites that row instead
        # (the prompt says so), so the user is not asked to paste it again.
        gaps.append(ConfirmationGap.NO_SYMPTOM_ROW)
    return ResolutionReadiness(
        verdict=ResolutionReadiness.NEEDS_INFO,
        missing=missing,
        reason=ResolutionReason.DOCUMENTATION_GAPS,
        confirmation_gaps=gaps,
        **names,
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

    def __init__(
        self,
        verdict: str,
        message: "str | Callable[[], str]",
        params: "dict | Callable[[], dict] | None" = None,
    ):
        self.verdict = verdict
        self._message = message
        self._params = params

    def offer_params(self) -> dict:
        """What an offer code carries to render this prompt after the record
        (#2636, ``narration.offers.closure_message``): the verdict and the
        builder's inputs; the record lines are the record's."""
        params = self._params() if callable(self._params) else self._params
        return {"verdict": self.verdict, **(params or {})}

    @property
    def message(self) -> str:
        """The prompt, built on first read when given as a callable."""
        if callable(self._message):
            self._message = self._message()
        return self._message


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


def closure_verdict(case: "Case") -> str:
    """``assess_closure_readiness(case).verdict`` without building the message.

    The full assessment formats a user-facing markdown block — the root-cause
    line, the joined solution titles — which a caller wanting only the verdict
    discards. Two of them do: the deferred-disposition proposer's re-nag
    signature and the resolution backstop's, both on the per-turn generation
    path, where #1195 has already had to de-duplicate this family of reads once.

    A thin delegate rather than a reimplementation of the branch conditions. A
    second copy of "which disposition does this case warrant" is exactly the
    drift ``assess_closure_readiness`` centralises; the saving here is the
    string building, not the predicates.
    """
    return assess_closure_readiness(case).verdict


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
    has_solutions = bool(case.solutions and len(case.solutions) > 0)

    # SUGGEST_RESOLVE — the case is "resolution-grade" (matches the minimum
    # criteria assess_resolution_readiness uses for READY). Closing would
    # discard the resolution attribution. Engine pivots; user still confirms.
    # Symmetric to ResolutionReadiness.SUGGEST_CLOSE.
    #
    # Gate on the resolution confirmation — the SAME bar as
    # assess_resolution_readiness READY (both legs: cause gone, problem gone),
    # less READY's hold on a check the model asked for after the fix
    # (``pending_symptom_check``): the verdict stays SUGGEST_RESOLVE while one
    # is pending, so a close is read as on any resolvable case (#1906 D5.2,
    # ruling 2; ``close_pivots_to_resolve`` applies a standing decline).
    # A close request on a resolution-ready case pivots to resolve: "resolved" is
    # a closed case WITH a resolution, and it is always safe to resolve a case the
    # user moved to close. We do NOT require a separate root-cause/solution record
    # (an out-of-band fix yields the two absence rows with neither) — that was the
    # same over-constraint the resolution gate carried. A merely stabilized case
    # has symptom_absence but no causal_absence, and a fix whose effect on the
    # reported problem is unchecked has causal_absence but no symptom_absence
    # after it (#1906); neither pivots.
    # Never on a false alarm: there is no cause to have eliminated.
    if (
        case.progress.problem_status != ProblemStatus.INVALIDATED
        and _resolution_confirmed(case)
    ):
        # Built only when read: verdict-only callers (``closure_verdict``,
        # disposition eligibility on every save) never compose it.
        return ClosureReadiness(
            verdict=ClosureReadiness.SUGGEST_RESOLVE,
            message=lambda: _suggest_resolve_message(case, has_solutions),
            params=lambda: _suggest_resolve_params(case, has_solutions),
        )

    return assess_close_summary(case)


#: How much of a row's summary (or a cause's statement) a record line quotes.
RECORD_TEXT_CHARS = 120


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split()).rstrip(" .")
    if len(text) <= RECORD_TEXT_CHARS:
        return text
    return text[: RECORD_TEXT_CHARS - 1].rstrip() + "…"


def _row_text(row) -> str:
    return _clip(getattr(row, "summary", None) or getattr(row, "extract", None))


def resolution_record_lines(case) -> str:
    """The rows a resolve offer cites (#1906 D5.1), as markdown bullets; ``""``
    when the case confirms no resolution.

    Every resolve offer and close→resolve pivot says what the record shows and
    nothing stronger (design P3): for each cause the cause leg re-observed, its
    latest row; for each row that showed the problem and was re-checked, its
    latest re-check; then each row that showed the problem with no re-check in
    the problem leg. Read only from the confirmation's legs
    (``resolution_confirmation_rows``), the rows' data turns and subjects
    (``subject_index``), the verifying rows, and whether a root cause is on
    record (``cause_identification_leg``, the one source every surface reads:
    without a leg the cause line says "no root cause on record" instead of
    naming the root the row re-checked). Here, beside the closure pivot that
    composes with it, because the replies in ``milestone_engine`` import this
    module.

    Residual, stated: "Not re-checked since the fix" names only RECORDED
    verifying rows; a signal the user reported but never submitted as data
    (#1906's Prometheus target) has no row to name.
    """
    rows = resolution_confirmation_rows(case)
    if not rows:
        return ""
    cause_on_record = cause_identification_leg(case) is not None
    subjects = subject_index(case)
    nodes = getattr(case, "causal_nodes", None) or {}
    hypotheses = getattr(case, "hypotheses", None) or {}

    def latest(by: dict, key, row) -> None:
        if key not in by or subjects.observed(row) >= subjects.observed(by[key]):
            by[key] = row

    causes: dict = {}
    rechecked: dict = {}
    for row in rows:
        named = getattr(row, "rechecks_evidence_id", None)
        if row.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:
            latest(causes, subjects.cause_targets(named), row)
        else:
            latest(rechecked, named, row)

    lines = []
    for targets, row in causes.items():
        if not cause_on_record:
            label = "no root cause on record"
        else:
            # A rootless pool binds to its hypothesis (``cause_targets``).
            label = " / ".join(
                _clip(
                    nodes[t].statement
                    if t in nodes
                    else getattr(hypotheses.get(t), "statement", None)
                )
                for t in sorted(targets)
            )
        lines.append(
            f"- **The cause** — {label}: {_row_text(row)} "
            f"(turn {subjects.observed(row)})"
        )
    for verifying_id in subjects.verifying:
        if verifying_id in rechecked:
            row = rechecked[verifying_id]
            lines.append(
                f"- **The problem** — {_row_text(subjects.rows[verifying_id])}, "
                f"re-checked: {_row_text(row)} (turn {subjects.observed(row)})"
            )
    for verifying_id in subjects.verifying:
        if verifying_id not in rechecked and verifying_id in subjects.rows:
            lines.append(
                "- **Not re-checked since the fix** — "
                + _row_text(subjects.rows[verifying_id])
            )
    return "\n".join(lines)


def _suggest_resolve_message(case: "Case", has_solutions: bool) -> str:
    params = _suggest_resolve_params(case, has_solutions)
    return suggest_resolve_message(
        params["root_cause"],
        params["solution_titles"],
        resolution_record_lines(case),
    )


def _suggest_resolve_params(case: "Case", has_solutions: bool) -> dict:
    """What the closure→resolve pivot's prompt names (``narration.offers``):
    the root cause only when a cause-identification leg stands on it, as the
    record lines decide, and the solutions on record."""
    name = getattr(case.root_cause_conclusion, "root_cause", None)
    leg = cause_identification_leg(case)
    return {
        "root_cause": name if leg is not None else None,
        # Named after the record when it identifies a cause (#2636).
        "root_cause_name": name,
        # The identification decided on, as the record states it (#2636).
        "identification_leg": leg,
        "solution_titles": (
            _solution_display_titles(case.solutions) if has_solutions else []
        ),
    }


def assess_close_summary(case: "Case") -> ClosureReadiness:
    """What the investigation accomplished, for a close's confirmation prompt:
    TRIVIAL or HAS_SUBSTANCE, never the SUGGEST_RESOLVE pivot.

    ``assess_closure_readiness`` answers with it whenever the case confirms no
    resolution. A user's close on a resolvable case whose resolution they
    declined is not pivoted (#2360), so its prompt is this summary rather than
    the pivot's "mark it resolved instead?".
    """
    evidence_count = len(case.evidence) if case.evidence else 0
    hypothesis_count = len(case.hypotheses) if case.hypotheses else 0
    milestones_completed = len(shown_milestones(case))
    name = (
        getattr(case.root_cause_conclusion, "root_cause", None)
        or "the identified root cause"
    )
    leg = cause_identification_leg(case)
    root_cause = name if leg is not None else None
    titles = _solution_display_titles(case.solutions) if case.solutions else []
    trivial = (
        not (evidence_count or hypothesis_count or milestones_completed or root_cause)
        and not titles
    )
    summary = {
        "evidence_count": evidence_count,
        "hypothesis_count": hypothesis_count,
        "milestones_completed": milestones_completed,
        "root_cause": root_cause,
        "solution_titles": titles,
    }
    return ClosureReadiness(
        verdict=(
            ClosureReadiness.TRIVIAL if trivial else ClosureReadiness.HAS_SUBSTANCE
        ),
        message=close_summary_message(**summary),
        params={**summary, "root_cause_name": name, "identification_leg": leg},
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

    # Problem Definition ← problem_verification.symptom_statement
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
                    "Some sections of the runbook draft will be thin. "
                    "This information would improve it:\n\n" + "\n".join(missing_desc)
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

    There is deliberately no "existing covers" verdict. Best-chunk-max
    similarity detects OVERLAP, not whole-runbook equivalence, and the old
    ≥0.85 auto-suppression threshold never fired against any real
    distribution — so a match is never ASSERTED to cover the case (owner
    decision, fm#1030). What a ≥0.70 match does do is return SIMILAR_FOUND:
    the caller stops, names the candidate by title and score, and creates
    nothing until the user explicitly chooses — surfacing a likely duplicate
    only to create it anyway would defeat the point of checking. The top
    score is emitted as a metric so the band can be calibrated later.
    """

    SUGGEST = "suggest"
    SUGGEST_WITH_CAVEATS = "suggest_with_caveats"
    #: A ≥0.70 best-chunk match exists. NOT a coverage claim — the caller
    #: presents the candidate and lets the user decide; generation proceeds
    #: only on explicit confirmation.
    SIMILAR_FOUND = "similar_found"
    NOT_READY = "not_ready"

    def __init__(self, verdict: str, message: str):
        self.verdict = verdict
        self.message = message


async def evaluate_runbook_suggestion(
    case: "Case",
    runbook_kb: Any = None,
    scope_resolver: Any = None,
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
        scope_resolver: Zero-arg async callable returning the case driver's
            KB read scope filter (``case_retrieval_scope``, #1919: global ∪ the
            driver's personal KB ∪ the driver's team-shared runbooks; the driver
            is the case's effective driver, ADR-020). Supplied by the engine, which owns the team-membership and share-table dependencies. Required for dedup to
            run: without it the search cannot be honestly scoped, so dedup is
            SKIPPED with the "did not run" caveat rather than quietly searching
            a narrower scope. If the resolver RAISES (e.g. the team arm could
            not be resolved), dedup is treated as FAILED — surfacing the
            failure rather than degrading to a narrower scope, because a
            silently narrowed search would underpin a "checked, nothing
            similar" claim it did not establish. (Deliberate divergence from
            the engine KB pre-fetch, which degrades — correct for prompt context,
            wrong for a dedup verdict.)
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
    # Why dedup did not complete, when it did not. Three causes, three
    # remedies, and naming the wrong one sends the user after the wrong thing.
    #
    # The flags NEST rather than partition: `dedup_failed` means an exception
    # reached us at all, and `dedup_unreadable` narrows that to the one
    # deterministic cause (re-index; retrying never clears it), so
    # `dedup_unreadable` implies `dedup_failed`. The branch ORDER below is what
    # makes them behave as three cases — do not read either flag on its own as
    # "transient". Neither set means the search simply never ran: no KB wired,
    # no usable tenant, or too little case content to query with, where calling
    # anything "unavailable" would be false.
    dedup_failed = False
    dedup_unreadable = False
    if not runbook_kb:
        logger.warning(
            f"Runbook deduplication skipped for case {case.case_id}: "
            f"runbook_kb is not available. Duplicate runbooks may be created "
            f"if a similar one already exists in the KB.",
            extra={"case_id": case.case_id, "metric": "runbook.dedup_skipped"},
        )
    elif scope_resolver is None:
        # A KB with no scope resolver cannot run an honestly scoped search.
        # Skipped, not failed: nothing is broken, but no search ran, so the
        # "did not run" caveat below owns it — quietly falling back to a
        # narrower scope would license a dedup verdict the search never
        # established (#944).
        logger.warning(
            f"Runbook deduplication skipped for case {case.case_id}: "
            f"no scope resolver supplied, so the search cannot be scoped to "
            f"the case driver. Duplicate runbooks may be created if a similar "
            f"one already exists in the KB.",
            extra={"case_id": case.case_id, "metric": "runbook.dedup_skipped"},
        )
    if runbook_kb and scope_resolver is not None:
        try:
            # Scope resolution runs INSIDE this try on purpose: a failure here
            # (team arm unresolvable, say) must surface as a failed dedup and
            # take the caveat branch below — not narrow the scope and answer.
            scope_filter = await scope_resolver()
            similar = await _find_similar_runbooks_for_case(
                case, runbook_kb, scope_filter
            )
            # None means no search was issued (too little case content to form
            # a query) — NOT "nothing similar found". Only a real search
            # licenses the plain SUGGEST verdict below (#944).
            dedup_ran = similar is not None
            if similar:
                top_match = similar[0]
                similarity = top_match.get("similarity_score", 0)
                title = top_match.get("title", "existing runbook")

                if similarity >= 0.70:
                    # SIMILAR_FOUND, not a caveat on a proceeding creation:
                    # the caller must STOP and let the user choose. The
                    # wording states overlap, never coverage — best-chunk-max
                    # cannot establish that the existing runbook covers this
                    # case (owner decision, fm#1030).
                    return RunbookSuggestion(
                        verdict=RunbookSuggestion.SIMILAR_FOUND,
                        message=(
                            f"A similar runbook already exists: **{title}** "
                            f"({similarity:.0%} best-chunk match). That "
                            "measures overlap, not full coverage, so I have "
                            "not created a draft. The existing runbook is in the "
                            "knowledge searched for whoever drove the case at "
                            "that point; review it there, or generate a new one "
                            "anyway."
                        ),
                    )
        except Exception as e:
            # `dedup_ran` stays False, so the caveat branch below owns this.
            # `dedup_unreadable` separates the one cause whose remedy differs:
            # everything else here is a transient failure that retrying clears,
            # but RUNBOOK_RESULTS_UNREADABLE means the search ran, the KB is
            # up, and the closest runbooks are unreadable until someone
            # re-indexes them. Telling the user to wait would be wrong (#912).
            dedup_failed = True
            dedup_unreadable = getattr(e, "error_code", None) == RESULTS_UNREADABLE_CODE
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
        if dedup_unreadable:
            cause = (
                "the closest-matching runbooks could not be read and need re-indexing"
            )
        elif dedup_failed:
            cause = "the knowledge base search is unavailable"
        else:
            # Skipped, not failed: no knowledge base wired, no usable tenant, or
            # too little case content to form a query. Nothing is broken and
            # there is nothing to retry, so calling it "unavailable" would be a
            # false statement about a healthy system (#912).
            cause = "the knowledge base search did not run"
        caveats.append(
            f"I could not check whether a similar runbook already exists — "
            f"{cause}, so the draft may duplicate a runbook already in the "
            f"knowledge base."
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
    case: "Case", runbook_kb: Any, scope_filter: dict
) -> Optional[list[dict]]:
    """Search for existing runbooks similar to a resolved case, within a scope.

    Builds a query from case title + root cause + solution for semantic search.

    Returns:
        A list of matches (possibly empty) when a search actually ran, or
        ``None`` when no search was issued at all — too little case content to
        form a query.

        The distinction is load-bearing: ``[]`` licenses the caller's "checked,
        nothing similar exists" verdict and ``None`` does not. Collapsing both
        into ``[]`` is what let an unsearched case reach the plain ``SUGGEST``
        branch, which asserts a dedup result that was never obtained (#944).

    ``scope_filter`` is the case driver's KB read scope
    (``case_retrieval_scope``), resolved by the caller's ``scope_resolver``. It is passed
    through to ``search_by_text``, which refuses a falsy one with a typed
    error rather than searching unscoped — the fail-closed property that
    replaced the old mandatory-org predicate (fm#1030).
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
        scope_filter=scope_filter,
        top_k=3,
        min_similarity=0.65,
    )
    # Attributes are read directly, NOT via getattr with a default.
    # ``search_by_text`` returns ``List[RunbookMatch]``, so a missing
    # attribute is a contract break — and a ``getattr(r, "similarity_score", 0)``
    # default would silently score it 0, drop it below every threshold, and
    # reproduce "no similar runbook found" from a match that WAS returned:
    # the exact defect this function was fixed for (#944).
    return [
        {
            "similarity_score": r.similarity_score,
            "title": r.title,
        }
        for r in results or []
    ]
