"""The pending-transition confirm/decline turns and the explicit status_transition intent to 'closed'."""

import logging

from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _finish_deterministic_turn,
)
from faultmaven.modules.case.contracts import CaseState

from .progress import confirmed_transition_arms
from .stage_gates import _close_confirmation_suggestions
from .terminal_replies import (
    _compose_terminal_reply,
    _resolution_confirmation_suggestions,
    _select_ack_follow_ups,
)

logger = logging.getLogger(__name__)


async def _confirm_pending_transition(
    checkpoint_service,
    report_service,
    repository,
    terminal,
    *,
    case,
    upload_report,
    user_message,
    confirmed_via,
):
    """Execute a confirmed pending transition: checkpoint, commit, generate the closure/resolution report and the ack turn."""
    from faultmaven.core.investigation.terminal_transitions import (
        confirm_pending_transition,
    )

    if checkpoint_service:
        to_state = case.pending_transition.get("to_state", "unknown")
        await checkpoint_service.create_checkpoint(
            case,
            trigger="pre_case_action",
            metadata={
                "from_state": case.state.value,
                "to_state": to_state,
            },
        )

    executed = confirm_pending_transition(
        case, case.user_id, confirmed_via=confirmed_via
    )
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
        await repository.save(case)
        return {
            "agent_response": resolve_msg,
            "suggested_follow_ups": (_resolution_confirmation_suggestions()),
            "case_updated": case,
            "metadata": turn_metadata,
        }

    # Persist the terminal status before generating the
    # summary — the Report row FKs to case_id.
    await repository.save(case)

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
    ) = await terminal.auto_generate_report(case)

    agent_response = _compose_terminal_reply(case, summary_payload)
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=True,
        **confirmed_transition_arms(case, executed, confirmed_via),
    )
    await repository.save(case)

    # Closure-ack follow-ups depend on whether
    # generation succeeded. Success: minimal
    # suggestions (the summary is rendered inline,
    # so a regen card next to it would be noise).
    # Failure: include the regen affordance so the
    # user can retry immediately — the "noise next
    # to inline summary" rationale doesn't apply
    # when there's no summary inline.
    remaining = await _remaining_regens_for(report_service, repository, case)
    follow_ups = _select_ack_follow_ups(case, summary_failed, remaining)

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


async def _decline_bare_reply(repository, *, case, upload_report, user_message):
    """Reply to a bare (non-substantive) decline of a pending transition, leaving the case open."""
    agent_response = "Understood. The case remains open for further investigation."
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )
    await repository.save(case)

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": [],
        "case_updated": case,
        "metadata": turn_metadata,
    }


async def _represent_pending_transition(
    repository, *, case, stripped_message, upload_report, user_message
):
    """Re-present the pending transition's options when the user's reply answered neither yes nor no."""
    if stripped_message:
        case.pending_transition["re_presented"] = True
    to_state = case.pending_transition.get("to_state", "resolved")
    summary = case.pending_transition.get("summary", "")

    agent_response = (
        "Please select one of the options above to continue."
        if not summary
        else f"{summary}\n\nPlease select one of the options above to continue."
    )
    if to_state == "resolved":
        follow_ups = _resolution_confirmation_suggestions()
    else:
        follow_ups = _close_confirmation_suggestions()

    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        agent_response,
        upload_report,
        progress_made=False,
    )
    await repository.save(case)

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": follow_ups,
        "case_updated": case,
        "metadata": turn_metadata,
    }


async def _close_on_explicit_intent(
    repository,
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
        await repository.save(case)
        return {
            "agent_response": closure.message,
            "suggested_follow_ups": _resolution_confirmation_suggestions(),
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

    # Save and return with closure summary + canonical
    # confirm/decline pair (alignment with agent-initiated path).
    turn_metadata = _finish_deterministic_turn(
        case,
        user_message or "",
        closure.message,
        upload_report,
        progress_made=False,
    )
    await repository.save(case)
    return {
        "agent_response": closure.message,
        "suggested_follow_ups": _close_confirmation_suggestions(),
        "case_updated": case,
        "metadata": turn_metadata,
    }
