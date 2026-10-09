"""The pending-transition confirm/decline turns, the refused confirmation click, and the explicit status_transition intents: 'closed', and 'resolved' from the reopen chip.

None of these turns writes anything. Each mutates the case in memory and
returns it; the turn commits once, at the service, with the report rows a
confirm adds to the turn's ``TurnCommitPlan`` (#1882).
"""

import logging
from typing import Optional

from faultmaven.core.investigation.lifecycle_metrics import (
    confirmation_click_refused_total,
)
from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _finish_deterministic_turn,
)
from faultmaven.core.investigation.problem_status import (
    false_alarm_close_declined_at,
    problem_on_hold,
)
from faultmaven.modules.case.contracts import CaseState

from .cause_state import (
    _count_gate1_turn,
    _gate1_statement_presentation,
    _investigation_confirmation_suggestions,
)
from .progress import confirmed_transition_arms
from .stage_gates import (
    _close_confirmation_suggestions,
    declined_close_card,
    declined_resolve_card,
)
from .statement_revision import (
    revision_confirmation_suggestions,
    revision_presentation,
)
from .terminal_replies import (
    _build_resolution_confirmation,
    _compose_terminal_reply,
    _resolution_confirmation_suggestions,
    _select_ack_follow_ups,
)
from .transition_consent import TYPED_CONFIRMATION_LINE, OfferRefusal

logger = logging.getLogger(__name__)

#: The first line of the reply to a confirmation click that names no standing
#: offer (#1812, ruling (a)). Also the reply to a click that named no offer at
#: all (``untargeted``): from the user's side both are a button for an offer
#: that is not the one open now.
STALE_OFFER_LINE = "That button was for an earlier offer that's no longer open."


async def _confirm_pending_transition(
    report_service,
    repository,
    terminal,
    *,
    case,
    plan: TurnCommitPlan,
    upload_report,
    user_message,
    confirmed_via,
):
    """Execute a confirmed pending transition: transition, render the closure/resolution report and compose the ack turn.

    ``repository`` is read for the regeneration count only. The report row
    goes into ``plan`` and commits with the terminal state, in the turn's one
    transaction: a turn whose commit fails leaves no CLOSED/RESOLVED state and
    no report (#1882).
    """
    from faultmaven.core.investigation.terminal_transitions import (
        confirm_pending_transition,
    )

    # The confirming ACTOR (ADR-020 D6): the turn's principal, which is the
    # case's effective driver — no one else may submit a turn.
    executed = confirm_pending_transition(case, case.effective_driver_id)
    if not executed and (case.pending_transition or {}).get("to_state") == "resolved":
        # INV-37 resolve-preservation: the pending CLOSE
        # pivoted to a RESOLVED proposal (the case became
        # resolvable). Nothing terminal committed — present
        # the resolve confirmation instead of a CLOSED
        # report, which would falsely record the case as
        # closed-unresolved. The pivot's user-facing message
        # is the SUGGEST_RESOLVE prose the guard already
        # computed and stored on the resolved pending (same
        # text the proposal-time pivot shows — one source of
        # truth, and it renders the no-record out-of-band-fix
        # case correctly, which _build_resolution_confirmation
        # does not).
        resolve_msg = case.pending_transition["summary"]
        turn_metadata = _finish_deterministic_turn(
            case,
            user_message or "",
            resolve_msg,
            upload_report,
            progress_made=False,
        )
        return {
            "agent_response": resolve_msg,
            "suggested_follow_ups": _resolution_confirmation_suggestions(case),
            "case_updated": case,
            "metadata": turn_metadata,
        }

    # Synchronous summary render, its row added to the plan so it commits
    # with the terminal state (the report row FKs to the case, which the same
    # transaction writes first). Returns rendered markdown on success, a skip
    # note when the gate blocks generation, a failure note when the render
    # raised, or None when no report service is configured. The second tuple
    # element flags a render failure so the ack-turn can offer the regen
    # affordance (G2 — there's no inline summary to be noisy next to when
    # generation failed).
    (
        summary_payload,
        summary_failed,
    ) = await terminal.auto_generate_report(case, plan=plan)

    agent_response = _compose_terminal_reply(case, summary_payload)
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=True,
        **confirmed_transition_arms(case, executed, confirmed_via),
    )

    # Closure-ack follow-ups depend on whether
    # generation succeeded. Success: minimal
    # suggestions (the summary is rendered inline,
    # so a regen card next to it would be noise).
    # Failure: include the regen affordance so the
    # user can retry immediately — the "noise next
    # to inline summary" rationale doesn't apply
    # when there's no summary inline.
    remaining = await _remaining_regens_for(
        report_service, repository, case, pending=plan.pending_reports()
    )
    follow_ups = _select_ack_follow_ups(case, summary_failed, remaining)

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


#: The reply to a bare "no" to closing on a false-alarm finding. "Remains open
#: for further investigation" would be untrue there: nothing is investigated on
#: a false alarm, the case holds until one of two things moves it, and a user
#: on a client with no status menu needs to be told the close is still theirs.
FALSE_ALARM_HOLD_REPLY = (
    "Understood, the case stays open. It holds on the finding that the "
    "reported problem was not present, so nothing more is investigated unless "
    "one of two things moves it: new evidence of a different problem, or "
    "information showing the reported problem was real (that disputes the "
    "finding). I won't ask about closing again on this finding; the close "
    "stays available whenever you want it."
)


#: The reply to a bare "no" to a resolution (#1895). It says what brings the
#: offer back, and where a user who changes their mind finds it: the reopen
#: chip, the reply's one follow-up.
RESOLVE_DECLINED_REPLY = (
    "Understood, the case stays open. I won't ask about resolving again unless "
    "a new confirmation that the fix held is recorded; if you change your "
    "mind, use 'Mark it resolved' below."
)


def _decline_bare_reply(*, case, upload_report, user_message):
    """Reply to a bare (non-substantive) decline of a pending transition, leaving the case open.

    On a false-alarm close the decline was just recorded on the finding, and
    the reply says what the hold is and how it moves, with the close card
    (``declined_close_card``) as its one follow-up (#1889). On a resolution the
    decline was just recorded against the confirmations on record, and the
    reply says what brings the offer back, with the "Mark it resolved" chip
    (``declined_resolve_card``) as its one follow-up (#1895).
    """
    follow_ups: list = []
    resolve_card = declined_resolve_card(case)
    if false_alarm_close_declined_at(case) is not None:
        agent_response = FALSE_ALARM_HOLD_REPLY
        follow_ups = [declined_close_card("false_alarm")]
    elif resolve_card is not None:
        agent_response = RESOLVE_DECLINED_REPLY
        follow_ups = [resolve_card]
    else:
        agent_response = "Understood. The case remains open for further investigation."
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


def _pending_transition_reask(case) -> tuple[str, list]:
    """The standing terminal offer's re-ask text and its confirmation pair.

    The pair names the offer by its current key (#1812). The last paragraph
    says what a typed confirmation must look like (#1814, ruling (b)), since a
    typed reply executes only when it is one bare consent token (#1783).
    """
    to_state = case.pending_transition.get("to_state", "resolved")
    summary = case.pending_transition.get("summary", "")

    ask = f"Please select one of the options above to continue.\n\n{TYPED_CONFIRMATION_LINE}"
    agent_response = ask if not summary else f"{summary}\n\n{ask}"
    if to_state == "resolved":
        follow_ups = _resolution_confirmation_suggestions(case)
    else:
        follow_ups = _close_confirmation_suggestions(case)
    return agent_response, follow_ups


def _represent_pending_transition(*, case, upload_report, user_message):
    """Re-present the pending transition's options when the user's reply answered neither yes nor no.

    Every time it is asked for, and recording nothing: a re-ask is never a
    refusal and never withdraws the proposal (#1783, ruling (a)).
    """
    agent_response, follow_ups = _pending_transition_reask(case)

    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


def _refuse_offer_click(
    *,
    case,
    upload_report,
    user_message,
    standing: Optional[str],
    reason: OfferRefusal,
):
    """Refuse a confirmation click that does not name the offer standing now (#1812).

    Nothing executes, nothing is withdrawn and no refusal is recorded. The
    reply is ``STALE_OFFER_LINE`` and then the standing offer again, exactly as
    the gate re-asks any non-answer: ``standing="terminal"`` re-shows the
    pending transition with its pair, ``"gate1"`` re-shows the problem
    statement with its pair, ``"revision"`` re-shows the revised statement
    with its pair, and ``None`` (nothing a click can answer is standing) is the
    line alone. No LLM call on any of them.
    """
    follow_ups: list = []
    agent_response = STALE_OFFER_LINE
    presentation: Optional[str] = None
    if standing == "terminal":
        reask, follow_ups = _pending_transition_reask(case)
        agent_response = f"{STALE_OFFER_LINE}\n\n{reask}"
    elif standing == "gate1":
        presentation = _gate1_statement_presentation(case)
        agent_response = f"{STALE_OFFER_LINE}\n\n{presentation}"
        follow_ups = _investigation_confirmation_suggestions(case)
    elif standing == "revision":
        agent_response = f"{STALE_OFFER_LINE}\n\n{revision_presentation(case)}"
        follow_ups = revision_confirmation_suggestions(case)

    confirmation_click_refused_total.labels(
        gate=standing or "none", reason=reason
    ).inc()
    logger.info(
        "confirmation_click_refused",
        extra={
            "case_id": case.case_id,
            "turn": case.current_turn,
            "gate": standing or "none",
            "reason": reason,
        },
    )

    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )

    if presentation is not None:
        # INV-01's pair, through the one helper ``_compose_turn_reply`` uses.
        _count_gate1_turn(case, presentation, agent_response)

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


def _close_on_explicit_intent(
    *,
    assess_closure_readiness,
    case,
    propose_transition,
    upload_report,
    user_message,
):
    """Handle an explicit status_transition intent to 'closed', proposing or confirming per closure readiness."""
    if case.state not in (
        CaseState.INQUIRY,
        CaseState.INVESTIGATING,
    ):
        raise ValueError(f"Cannot transition to CLOSED from {case.state.value}")

    # Use closure readiness for a meaningful summary, and
    # pivot to RESOLVED if the case has root cause + solution
    # on record (SUGGEST_RESOLVE — symmetric to the LLM-emit
    # path's SUGGEST_CLOSE pivot for the opposite direction).
    closure = assess_closure_readiness(case)
    if closure.verdict == closure.SUGGEST_RESOLVE:
        # closure_reason auto-derives to None inside
        # propose_transition for RESOLVED — resolution itself
        # is the categorization. The user still confirms via
        # the resolution confirmation pair.
        propose_transition(
            case=case,
            to_state="resolved",
            summary=closure.message,
        )
        logger.info(
            f"User dropdown-requested CLOSED for case "
            f"{case.case_id} but verdict=SUGGEST_RESOLVE "
            f"(case has root cause + solution); pivoting "
            f"to RESOLVED."
        )
        turn_metadata = _finish_deterministic_turn(
            case,
            user_message or "",
            closure.message,
            upload_report,
            progress_made=False,
        )
        return {
            "agent_response": closure.message,
            "suggested_follow_ups": _resolution_confirmation_suggestions(case),
            "case_updated": case,
            "metadata": turn_metadata,
        }

    # Standard close — closure_reason derived inside
    # propose_transition from case state.
    propose_transition(
        case=case,
        to_state="closed",
        summary=closure.message,
    )

    logger.info(
        f"Proposed CLOSED transition for case {case.case_id} via dropdown "
        f"(pending user confirmation)"
    )

    # Return with closure summary + canonical confirm/decline pair
    # (alignment with agent-initiated path).
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        closure.message,
        upload_report,
        progress_made=False,
    )
    return {
        "agent_response": closure.message,
        "suggested_follow_ups": _close_confirmation_suggestions(case),
        "case_updated": case,
        "metadata": turn_metadata,
    }


def _resolve_on_reopen_chip(*, case, upload_report, user_message):
    """Handle the "Mark it resolved" chip: re-open the resolution the user
    declined, as a proposal the user confirms on the next step (#1895).

    Reached only with the chip's key admitted (``resolve_reopen_admitted``, at
    the service boundary and again at the engine's guard): RESOLVED is not
    user-selectable, and this is the engine's own declined offer coming back,
    not a request for the state. It PROPOSES and never confirms (INV-03): the
    canonical confirmation pair follows, as on every other opener.

    The bar is re-read here: the offer is made only when
    ``assess_resolution_readiness`` is READY, the bar step 2 applies to the
    model's proposal, and not while the problem statement is on hold (a
    revision awaiting the user, when no transition is proposed). Otherwise
    nothing is proposed or recorded, and the reply says why.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        ResolutionReadiness,
        assess_resolution_readiness,
        propose_transition,
    )

    if problem_on_hold(case):
        agent_response = (
            "This case can't be marked resolved while its problem statement is "
            "in question. Answer the revised statement first."
        )
        turn_metadata = _finish_deterministic_turn(
            case,
            user_message or "",
            agent_response,
            upload_report,
            progress_made=False,
        )
        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [],
            "case_updated": case,
            "metadata": turn_metadata,
        }

    readiness = assess_resolution_readiness(case)
    if readiness.verdict != ResolutionReadiness.READY:
        logger.info(
            f"Reopen chip for case {case.case_id} refused: resolution readiness "
            f"is {readiness.verdict} (missing: {readiness.missing})."
        )
        agent_response = (
            "This case can't be marked resolved right now: the confirmation "
            "that earned the resolution no longer stands."
        )
        if readiness.message:
            agent_response = f"{agent_response}\n\n{readiness.message}"
        follow_ups: list = []
    else:
        agent_response = _build_resolution_confirmation(case)
        propose_transition(case=case, to_state="resolved", summary=agent_response)
        follow_ups = _resolution_confirmation_suggestions(case)
        logger.info(
            f"Proposed RESOLVED transition for case {case.case_id} from the "
            f"reopen chip (pending user confirmation)"
        )

    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )
    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }
