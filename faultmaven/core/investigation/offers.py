"""The offer record: its writers, what a decline binds, and when it lapses.

This docstring is where the record's rules are stated (#2379, the rulings of
2026-10-30). The invariants (INV-26 and the ones that read it) point here.

**What an offer is.** A question only the user can answer, about the case
itself: resolve it, close it, or accept a revised problem statement
(``OfferKind``). Five things open one: the engine (the resolution backstop,
the deferred close and its resolve pivot, the false-alarm close, the
rca-infeasible close, the close after an unmet resolution request), the model
(its ``proposed_transition``, and its revised statement), and the user (the
status menu's Close and the "Mark it resolved" chip). ``OfferBasis`` names
which; its origin is derived. Gate 1 is not an offer, and neither is a prose
question from the model.

**One slot.** At most one offer is open (``OfferRecord.open``), so Close,
Resolve and a revision never stand together. ``open_offer`` raises when one is
open: the caller answers or lapses it first.

**Three writers.** ``open_offer``, ``answer_offer`` and ``lapse_offers`` write
the record, and return a new one that the caller assigns
(``case.offer_record = ...``); the models are frozen, so nothing else can
edit one in place. Two narrow helpers write one part each:
``remember_resolve_request`` the request, ``restage_revision`` the open
revision's staged cause work. The revision offer is opened and answered only
by ``problem_status``, which stays the one writer of ``problem_status``;
``lapse_offers`` never touches it.

**Answers.** ``accepted`` (recorded only after the transition executed, so an
executor that raises leaves the offer open), ``declined``, ``withdrawn``,
``lapsed``, ``pivoted`` (a close the INV-37 re-check turned into a resolve
proposal) and ``superseded`` (a revision replaced by a re-wording). Each
answer records the case's effective driver (ruling 7; None when nobody
answered) and the premise it was answered against. Only the current driver
answers: the turn refuses anyone else with a 403, and ``answer_offer``'s own
check is an invariant behind it. A driver change lapses nothing.

**What a decline binds** (``binding_decline``). Exactly today's scopes:

- a declined resolve offer, any basis, binds every resolve offer that is not
  the user's own (a model close that pivots to a resolve included);
- a declined ``deferred`` close binds the engine's deferred close and a model
  close the closure check kept at CLOSED (not a model ``resolved`` the
  readiness check turned into a close);
- a declined false-alarm close (``closed_false_alarm``), any basis, binds every
  terminal offer that is not the user's own, while its finding stands;
- a declined revision binds the same wording, and never lapses.

A declined rca-infeasible, needs-info-unmet, model or menu close records its
answer and binds nothing. The user's own offers are never bound: the menu
close, and the reopen chip, which is how a user re-opens a declined
resolution.

**When a decline lapses** (ruling 3). A resolve or deferred decline lapses only
when a solution id or a resolution-confirmation row id outside its premise is
recorded: a withdrawn solution, a contested cause, a revision, the problem
status, the turn count and the driver move nothing. A false-alarm decline
lapses when its finding is withdrawn, edited away or replaced. ``decline_stands``
is the one live predicate every reader uses, so the backstop re-offers on the
very turn a new confirmation arrives. ``lapse_offers`` also stamps
``lapsed_at_turn`` on a decline whose premise moved, so a later
disqualification of the new row does not revive it; and it widens the
premise of a decline given on the current turn to cover what that turn
recorded, so the model's same-turn updates never un-answer it (#1895). It runs at step 3b of a turn, before the
automatic transitions read the record, and again on every save.

**The request** (ruling 1). A model ``resolved`` the case has not earned
(``NEEDS_INFO``) opens no offer and shows no chips: the readiness ask goes
below the reply, and ``remember_resolve_request`` records what is missing. The
next turn's re-check reads it once: READY opens the resolve offer, anything
else opens the needs-info-unmet close. Until it has, the apply-step openers
that run before it (deferred, false-alarm, rca-infeasible) stand down
(``awaiting_request_recheck``).

**Opening a terminal offer.** ``terminal_transitions.open_disposition_offer``
opens a resolve or close offer: it derives a close's ``closure_reason`` from
the case as it stands and hands it to ``open_offer``, which refuses a close
without one.

**The projection** (``offer_projection.project_offers``). Pure, and the only
place an affordance is derived from the record: ``disposition_eligibility``
(on every save) and the reply's chips (the open offer's confirmation pair,
``confirmation_pair``; with nothing open, the reopen chip while a resolve
decline stands and the problem is not on hold).
"""

import logging
import secrets
from typing import TYPE_CHECKING, Iterable, Optional

from faultmaven.core.investigation.cause_assurance import (
    resolution_confirmation_rows,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    CaseState,
    ClosureReason,
    Offer,
    OfferAnswer,
    OfferBasis,
    OfferKind,
    OfferOrigin,
    OfferPremise,
    OfferRecord,
    PendingRevision,
    ResolveRequest,
    StagedCauseWork,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

#: How many answers a case keeps. The metadata blob is rewritten on every save,
#: so the list is bounded; a standing decline is never evicted.
MAX_ANSWERS = 64

#: The offers that move the case to a terminal state.
TERMINAL_KINDS = (OfferKind.RESOLVE, OfferKind.CLOSE)

# The scopes a decline binds (``_scope``).
_RESOLVE = "resolve"
_DEFERRED = "deferred"
_FALSE_ALARM = "false_alarm"
_REVISION = "revision"


# --------------------------------------------------------------------------
# Reading the record
# --------------------------------------------------------------------------


def open_terminal_offer(case: "Case") -> Optional[Offer]:
    """The open resolve or close offer, or None."""
    offer = case.offer_record.open
    return offer if offer is not None and offer.kind in TERMINAL_KINDS else None


def open_revision(case: "Case") -> Optional[Offer]:
    """The open revision offer, or None."""
    offer = case.offer_record.open
    return offer if offer is not None and offer.kind == OfferKind.REVISION else None


def terminal_offer_opened_this_turn(case: "Case") -> Optional[Offer]:
    """The resolve or close offer opened on the current turn, or None."""
    offer = open_terminal_offer(case)
    if offer is None or offer.opened_at_turn != case.current_turn:
        return None
    return offer


def terminal_offer_answered_this_turn(case: "Case") -> bool:
    """Whether a resolve or close offer was answered on the current turn: the
    turn began inside a disposition handshake, or the engine withdrew its own
    offer. The engine's proposers stand down for the rest of the turn, so they
    never put an offer back in front of a user who just acted on one."""
    return any(
        answer.at_turn == case.current_turn and answer.kind in TERMINAL_KINDS
        for answer in case.offer_record.answers
    )


def awaiting_request_recheck(case: "Case") -> bool:
    """Whether a remembered resolution request awaits step 0's re-check. The
    apply-step openers stand down meanwhile, so the re-check decides the turn
    (#2408), as the standing ``needs_info`` proposal kept them out before."""
    return case.offer_record.resolve_request is not None


def last_answer(case: "Case") -> Optional[OfferAnswer]:
    """The newest answer on the record, or None."""
    answers = case.offer_record.answers
    return answers[-1] if answers else None


# --------------------------------------------------------------------------
# Premises and declines
# --------------------------------------------------------------------------


def _solution_ids(case: "Case") -> tuple[str, ...]:
    return tuple(sorted(s.solution_id for s in case.solutions or []))


def _confirmation_ids(case: "Case") -> tuple[str, ...]:
    return tuple(sorted(e.evidence_id for e in resolution_confirmation_rows(case)))


def _finding_turn(case: "Case") -> Optional[int]:
    pv = case.problem_verification
    invalidation = pv.invalidation if pv is not None else None
    return invalidation.turn if invalidation is not None else None


def _is_false_alarm(kind: OfferKind, closure_reason: Optional[ClosureReason]) -> bool:
    return (
        kind == OfferKind.CLOSE and closure_reason == ClosureReason.CLOSED_FALSE_ALARM
    )


def _premise(
    case: "Case",
    kind: OfferKind,
    closure_reason: Optional[ClosureReason],
    wording_key: Optional[str] = None,
) -> OfferPremise:
    """The state an answer of ``kind`` is given against: only its own parts."""
    if kind == OfferKind.REVISION:
        return OfferPremise(wording_key=wording_key)
    if _is_false_alarm(kind, closure_reason):
        return OfferPremise(finding_turn=_finding_turn(case))
    return OfferPremise(
        solution_ids=_solution_ids(case), confirmation_ids=_confirmation_ids(case)
    )


def _widened(premise: OfferPremise, now: OfferPremise) -> OfferPremise:
    """``premise`` widened to cover ``now``: the union of their ids. The
    finding is the declined one: a finding the turn recorded in its place is
    one the user never declined."""
    return OfferPremise(
        solution_ids=tuple(sorted({*premise.solution_ids, *now.solution_ids})),
        confirmation_ids=tuple(
            sorted({*premise.confirmation_ids, *now.confirmation_ids})
        ),
        finding_turn=premise.finding_turn,
        wording_key=premise.wording_key,
    )


def _scope(answer: OfferAnswer) -> Optional[str]:
    """What a decline binds, or None when the answer binds nothing."""
    if answer.answer != AnswerKind.DECLINED:
        return None
    if answer.kind == OfferKind.REVISION:
        return _REVISION
    if _is_false_alarm(answer.kind, answer.closure_reason):
        return _FALSE_ALARM
    if answer.kind == OfferKind.RESOLVE:
        return _RESOLVE
    if answer.basis == OfferBasis.DEFERRED:
        return _DEFERRED
    return None


def _premise_holds(answer: OfferAnswer, case: "Case") -> bool:
    scope = _scope(answer)
    if scope == _REVISION:
        return True
    if scope == _FALSE_ALARM:
        # The finding itself, not the status: a revision awaiting the user
        # holds REVISION_PENDING over a finding that still stands, and a
        # declined revision returns to it.
        finding = _finding_turn(case)
        return finding is not None and finding == answer.premise.finding_turn
    return set(_solution_ids(case)) <= set(answer.premise.solution_ids) and set(
        _confirmation_ids(case)
    ) <= set(answer.premise.confirmation_ids)


def decline_stands(answer: OfferAnswer, case: "Case") -> bool:
    """Whether ``answer`` is a decline that binds now: one with a scope, not
    stamped lapsed, and whose premise still holds (every current id of its
    kind's parts is inside its snapshot, or its finding stands). Live, so a
    reader sees a premise move on the turn it happens."""
    return (
        _scope(answer) is not None
        and answer.lapsed_at_turn is None
        and _premise_holds(answer, case)
    )


def _standing(
    case: "Case", scope: str, wording_key: Optional[str] = None
) -> Optional[OfferAnswer]:
    """The newest standing decline of ``scope``, or None."""
    for answer in reversed(case.offer_record.answers):
        if (
            _scope(answer) == scope
            and (wording_key is None or answer.premise.wording_key == wording_key)
            and decline_stands(answer, case)
        ):
            return answer
    return None


def standing_resolve_decline(case: "Case") -> Optional[OfferAnswer]:
    """The newest standing decline of a resolve offer, or None."""
    return _standing(case, _RESOLVE)


def standing_deferred_decline(case: "Case") -> Optional[OfferAnswer]:
    """The newest standing decline of the engine's deferred close, or None."""
    return _standing(case, _DEFERRED)


def standing_false_alarm_decline(case: "Case") -> Optional[OfferAnswer]:
    """The newest standing decline of a close on the false-alarm finding."""
    return _standing(case, _FALSE_ALARM)


def revision_declined(case: "Case", wording_key: str) -> bool:
    """Whether the user declined a revision worded as ``wording_key``."""
    return _standing(case, _REVISION, wording_key) is not None


def binding_decline(
    case: "Case",
    *,
    kind: OfferKind,
    basis: OfferBasis,
    wording_key: Optional[str] = None,
    readiness_pivot: bool = False,
) -> Optional[OfferAnswer]:
    """The standing decline that binds an offer of ``kind`` opened as
    ``basis``, or None. The user's own offers are never bound.
    ``readiness_pivot`` marks a model ``resolved`` the readiness check turned
    into a close: a deferred decline does not bind it, only a model close
    the closure check kept at CLOSED."""
    if basis.origin == OfferOrigin.USER:
        return None
    if kind == OfferKind.REVISION:
        return _standing(case, _REVISION, wording_key)
    false_alarm = standing_false_alarm_decline(case)
    if false_alarm is not None:
        return false_alarm
    if kind == OfferKind.RESOLVE:
        return standing_resolve_decline(case)
    if basis == OfferBasis.DEFERRED or (
        basis == OfferBasis.MODEL and not readiness_pivot
    ):
        return standing_deferred_decline(case)
    return None


# --------------------------------------------------------------------------
# The writers
# --------------------------------------------------------------------------


def require_answerer(case: "Case", actor_id: Optional[str]) -> None:
    """Raise ``ValueError`` unless ``actor_id`` (when given) is the case's
    effective driver: only the driver answers (ruling 7). An invariant check
    behind the turn's 403."""
    if actor_id is not None and actor_id != case.effective_driver_id:
        raise ValueError(
            f"case {case.case_id}: only the case's driver answers its offers"
        )


def new_proposal_id() -> str:
    """An opaque, random offer id (16 hex characters)."""
    return secrets.token_hex(8)


def _capped(answers: Iterable[OfferAnswer], case: "Case") -> tuple[OfferAnswer, ...]:
    """``answers`` with the oldest evicted down to ``MAX_ANSWERS``, never a
    standing decline."""
    kept = list(answers)
    excess = len(kept) - MAX_ANSWERS
    if excess <= 0:
        return tuple(kept)
    out: list[OfferAnswer] = []
    for answer in kept:
        if excess > 0 and not decline_stands(answer, case):
            excess -= 1
            continue
        out.append(answer)
    return tuple(out)


def open_offer(
    case: "Case",
    *,
    kind: OfferKind,
    basis: OfferBasis,
    summary: str = "",
    evidence_ids: Iterable[str] = (),
    closure_reason: Optional[ClosureReason] = None,
    revision: Optional[PendingRevision] = None,
    pivoted_from_close: bool = False,
    readiness_pivot: bool = False,
) -> Optional[OfferRecord]:
    """The record with a new ``kind`` offer open, or None when a standing
    decline binds it (``binding_decline``), in which case nothing is written.

    Raises ``ValueError`` when an offer is already open (answer or lapse it
    first), and when ``closure_reason`` is missing from a close offer or given
    for any other kind. A terminal offer is opened through
    ``terminal_transitions.open_disposition_offer``, which derives a close's
    reason from the case as it stands. Opening an offer ends a remembered
    resolution request.
    """
    record = case.offer_record
    if record.open is not None:
        raise ValueError(
            f"case {case.case_id} already has a {record.open.kind.value} offer "
            "open; answer or lapse it first"
        )
    if (closure_reason is None) == (kind == OfferKind.CLOSE):
        raise ValueError(
            f"case {case.case_id}: a close offer carries its closure_reason, "
            f"and only a close offer does (kind={kind.value})"
        )
    wording_key = revision.wording_key if revision is not None else None
    if (
        binding_decline(
            case,
            kind=kind,
            basis=basis,
            wording_key=wording_key,
            readiness_pivot=readiness_pivot,
        )
        is not None
    ):
        logger.info(
            "Case %s: %s offer (%s) not opened: a standing decline binds it",
            case.case_id,
            kind.value,
            basis.value,
        )
        return None
    offer = Offer(
        proposal_id=new_proposal_id(),
        kind=kind,
        basis=basis,
        closure_reason=closure_reason,
        summary=summary,
        evidence_ids=tuple(evidence_ids or ()),
        opened_at_turn=case.current_turn,
        pivoted_from_close=pivoted_from_close,
        revision=revision,
    )
    logger.info(
        "Case %s: %s offer opened (%s, %s) at turn %s",
        case.case_id,
        kind.value,
        basis.value,
        offer.proposal_id,
        case.current_turn,
    )
    return record.model_copy(update={"open": offer, "resolve_request": None})


def answer_offer(
    case: "Case",
    *,
    proposal_id: str,
    answer: AnswerKind,
    via: AnswerVia,
    actor_id: Optional[str] = None,
) -> OfferRecord:
    """The record with the open offer answered.

    Takes only the open offer's id: any other is refused (``ValueError``).
    ``answered_by`` is the case's effective driver, or None when nobody
    answered (a lapse, an engine withdrawal); an ``actor_id`` that is anyone
    else raises, an invariant check behind the turn's 403 (ruling 7). The
    premise is snapshotted for the answer, and the slot cleared. Accepting
    executes nothing: the caller executes, then records ``accepted``.
    """
    record = case.offer_record
    offer = record.open
    if offer is None or offer.proposal_id != proposal_id:
        raise ValueError(
            f"case {case.case_id}: offer {proposal_id!r} is not the open offer"
        )
    require_answerer(case, actor_id)
    nobody = answer == AnswerKind.LAPSED or via == AnswerVia.ENGINE
    entry = OfferAnswer(
        proposal_id=offer.proposal_id,
        kind=offer.kind,
        basis=offer.basis,
        closure_reason=offer.closure_reason,
        answer=answer,
        via=via,
        answered_by=None if nobody else case.effective_driver_id,
        at_turn=case.current_turn,
        premise=_premise(
            case,
            offer.kind,
            offer.closure_reason,
            offer.revision.wording_key if offer.revision is not None else None,
        ),
    )
    logger.info(
        "Case %s: %s offer %s %s via %s at turn %s",
        case.case_id,
        offer.kind.value,
        offer.proposal_id,
        answer.value,
        via.value,
        case.current_turn,
    )
    return record.model_copy(
        update={"open": None, "answers": _capped((*record.answers, entry), case)}
    )


def _premise_gone(case: "Case", offer: Offer) -> bool:
    """Whether an open terminal offer's premise has gone: the case is terminal,
    or a false-alarm close's finding was withdrawn, edited away or replaced."""
    if case.is_terminal:
        return True
    if _is_false_alarm(offer.kind, offer.closure_reason):
        finding = _finding_turn(case)
        return finding is None or finding > offer.opened_at_turn
    return False


def lapse_offers(case: "Case") -> OfferRecord:
    """The record with what no longer holds lapsed. Idempotent.

    - An open resolve or close offer whose premise has gone is answered
      ``lapsed`` (``_premise_gone``). A revision is never touched: only
      ``problem_status`` opens and answers it.
    - A decline given on the current turn has its premise widened to cover
      the case as it stands, so the turn's own updates never un-answer it.
      Widened, never narrowed: a row the turn disqualified is still one the
      decline saw, and re-entering later moves nothing.
    - An older decline whose premise moved is stamped ``lapsed_at_turn``.
    - The resolution request ends when the case leaves INVESTIGATING.
    """
    record = case.offer_record
    answers = list(record.answers)
    offer = record.open
    if (
        offer is not None
        and offer.kind in TERMINAL_KINDS
        and _premise_gone(case, offer)
    ):
        answers.append(
            OfferAnswer(
                proposal_id=offer.proposal_id,
                kind=offer.kind,
                basis=offer.basis,
                closure_reason=offer.closure_reason,
                answer=AnswerKind.LAPSED,
                via=AnswerVia.ENGINE,
                at_turn=case.current_turn,
                premise=_premise(case, offer.kind, offer.closure_reason),
            )
        )
        logger.info(
            "Case %s: %s offer %s lapsed: its premise has gone",
            case.case_id,
            offer.kind.value,
            offer.proposal_id,
        )
        offer = None
    for i, answer in enumerate(answers):
        scope = _scope(answer)
        if scope in (None, _REVISION) or answer.lapsed_at_turn is not None:
            continue
        if answer.at_turn == case.current_turn:
            fresh = _widened(
                answer.premise, _premise(case, answer.kind, answer.closure_reason)
            )
            if fresh != answer.premise:
                answers[i] = answer.model_copy(update={"premise": fresh})
        elif not _premise_holds(answer, case):
            answers[i] = answer.model_copy(update={"lapsed_at_turn": case.current_turn})
            logger.info(
                "Case %s: the %s decline of turn %s lapsed at turn %s",
                case.case_id,
                scope,
                answer.at_turn,
                case.current_turn,
            )
    request = record.resolve_request
    if request is not None and case.state != CaseState.INVESTIGATING:
        request = None
    updated = record.model_copy(
        update={
            "open": offer,
            "answers": _capped(answers, case),
            "resolve_request": request,
        }
    )
    return record if updated == record else updated


def remember_resolve_request(
    case: "Case", missing: Optional[Iterable[str]]
) -> OfferRecord:
    """The record with the resolution request set to ``missing`` at the
    current turn, or ended when ``missing`` is None."""
    request = (
        None
        if missing is None
        else ResolveRequest(missing=tuple(missing), at_turn=case.current_turn)
    )
    return case.offer_record.model_copy(update={"resolve_request": request})


def restage_revision(case: "Case", staged: Iterable[StagedCauseWork]) -> OfferRecord:
    """The record with the open revision's staged cause work replaced by
    ``staged``. Raises ``ValueError`` when no revision is open."""
    offer = open_revision(case)
    if offer is None:
        raise ValueError(f"case {case.case_id} has no revision open")
    revision = offer.revision.model_copy(update={"staged": tuple(staged)})
    return case.offer_record.model_copy(
        update={"open": offer.model_copy(update={"revision": revision})}
    )


# --------------------------------------------------------------------------
# Confirmation pairs (read by ``offer_projection``)
# --------------------------------------------------------------------------


def _pair(offer: Offer, yes: tuple[str, str, str], no: tuple[str, str, str]) -> list:
    """A confirmation pair, each card naming ``offer`` by its ``proposal_id``
    (#1812): a click executes only when it names the offer open when it
    arrives. ``yes`` and ``no`` are (label, payload, body)."""
    return [
        {
            "label": label,
            "action_type": "DECIDE",
            "payload": payload,
            "body": body,
            "intent": {
                "type": "confirmation",
                "confirmation_value": value,
                "proposal_id": offer.proposal_id,
            },
        }
        for value, (label, payload, body) in ((True, yes), (False, no))
    ]


def _resolve_pair(offer: Offer) -> list:
    """Confirm the resolution, or continue investigating."""
    return _pair(
        offer,
        (
            "Yes, mark as resolved",
            "Yes, the issue is resolved. Please mark this case as resolved.",
            "Confirm resolution and close the investigation.",
        ),
        (
            "Not yet, continue investigating",
            "Not yet — I'd like to continue investigating before resolving.",
            "Decline resolution and continue refining the root cause or "
            "exploring alternative solutions.",
        ),
    )


def _close_pair(offer: Offer) -> list:
    """Confirm closing, or continue investigating. Purely about the
    irreversibility of closing: the summary is a downstream artifact, and
    promising it here would sometimes be false (the substance gate skips)."""
    return _pair(
        offer,
        (
            "Yes, close this case",
            "Yes, close this case without resolution.",
            "Confirm closing the case. Closing is irreversible — the case "
            "becomes read-only.",
        ),
        (
            "Not yet, continue investigating",
            "Not yet — I'd like to continue investigating.",
            "Keep the investigation open and continue working toward a solution.",
        ),
    )


def _revision_pair(offer: Offer) -> list:
    """Use the revised statement, or clarify it."""
    return _pair(
        offer,
        (
            "Yes, use the revised statement",
            "Yes, the revised problem statement is right.",
            "Continue the investigation on the revised problem statement.",
        ),
        (
            "No, let me clarify",
            "No — let me clarify the problem first.",
            "Keep the confirmed statement and clarify what is wrong.",
        ),
    )


_PAIRS = {
    OfferKind.RESOLVE: _resolve_pair,
    OfferKind.CLOSE: _close_pair,
    OfferKind.REVISION: _revision_pair,
}


def confirmation_pair(offer: Optional[Offer]) -> list[dict]:
    """``offer``'s confirmation pair, each card naming it by its
    ``proposal_id``; empty when no offer is given."""
    return [] if offer is None else _PAIRS[offer.kind](offer)
