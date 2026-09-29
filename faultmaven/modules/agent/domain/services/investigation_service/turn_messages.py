"""Per-turn message-row bookkeeping: appending the user's turn and the agent's reply to the case, and the completion side effects (save + #1142 telemetry emission + the #1748 terminal-confirmation counters) that go with the agent one."""

import logging
from typing import Optional

from faultmaven.core.investigation.case_telemetry import (
    TurnPath,
    collect_progress_arms,
    emit_case_turn,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    terminal_confirmation_total,
    terminal_followup_total,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    terminal_card_action,
)
from faultmaven.models.api_models import IntentType
from faultmaven.modules.agent.domain.services.orientation import OrientationKind
from faultmaven.modules.case.contracts import (
    MessageRowKind,
    append_message_row,
)

logger = logging.getLogger(__name__)


def _build_user_message(*, case, case_id, next_turn, payload, query, user_id):
    """Re-derive a client-sent GREETING, append the user message row, and advance the case's turn counters."""
    intent = payload.intent
    intent_type = intent.type if intent else IntentType.CONVERSATION
    # GREETING is server-minted: the service derives it from the text
    # (or from its absence) below. A client-sent GREETING used to be
    # obeyed as-is — any text, any state, with any attachment — and
    # answered from the static onboarding string. It is now read as
    # plain conversation and re-derived; the enum value stays on the
    # wire for the clients' generated types.
    if intent is not None and intent_type == IntentType.GREETING:
        logger.info(
            "Ignoring client-sent GREETING intent on case %s; deriving "
            "the intent from the message instead",
            case_id,
        )
        intent = None
        intent_type = IntentType.CONVERSATION
    orientation_kind: Optional[OrientationKind] = None

    # Appended unconditionally. NOTHING upstream de-duplicates this
    # route, and an earlier version of this comment claimed otherwise
    # (#1419) — read that claim before trusting it:
    #
    # ``DeduplicationMiddleware`` skips ``multipart/form-data``
    # outright (``_should_skip``), and this route is declared with
    # ``Form(...)``/``File(...)``, so its content hash is never
    # computed for a turn. ``IdempotencyMiddleware`` only engages when
    # the client sends an ``Idempotency-Key`` header. So two identical
    # back-to-back submissions from a client that sends neither are
    # both processed and both charged.
    #
    # That is the open question in #1419, not a settled one. What IS
    # settled is that a role+content comparison here is the wrong
    # answer: it cannot tell a resubmission from two members of a
    # team-shared case posting the same adjacent text ("still broken",
    # "+1"), which are two real turns. Such a guard was tried in
    # ``CaseService.add_message_to_case``, had no callers so never ran,
    # was "fixed" by #855 to compare ``author_id`` and still never ran,
    # and both were retired in #1412.
    #
    # A blank ``query`` — every whitespace spelling, which
    # ``detect_orientation`` already calls ``EMPTY`` — is recorded as
    # ``EMPTY_TURN_TEXT`` and flagged, never written blank: the row is
    # part of the aggregate save, and a blank one aborts it (#1420).
    # That decision is the row kind's, not this call site's (#1452).
    # ``query`` is non-blank for every turn carrying data: a paste
    # becomes an attachment, and any attachment has already replaced
    # ``query`` via ``generate_implicit_query`` above.
    user_message_obj = append_message_row(
        case,
        MessageRowKind.USER_TURN,
        query,
        turn_number=next_turn,
        author_id=user_id,
        metadata={
            "has_attachments": payload.has_attachments,
            "attachment_count": len(payload.attachments),
            "intent_type": intent_type.value,
            "intent_metadata": (
                intent.model_dump(exclude_unset=True, exclude={"type"})
                if intent
                else {}
            ),
        },
    )
    case.message_count += 1
    case.current_turn = next_turn
    # #1142: this assignment is what "a turn was consumed" MEANS, and it
    # is the reason the telemetry row is emitted from this method rather
    # than from the engine — several routes below consume a turn number
    # without reaching ``MilestoneEngine.process_turn`` at all. Read
    # terminality here, before dispatch: the engine's terminal
    # short-circuit returns before any turn bookkeeping, so afterwards
    # nothing distinguishes it from a generation turn that did nothing.
    was_terminal = case.is_terminal
    return intent, intent_type, orientation_kind, user_message_obj, was_terminal


async def _save_and_emit_turn(
    repository,
    *,
    agent_response_text,
    attachment_metadata,
    intent,
    intent_type,
    oob_kind,
    payload,
    turn_meta,
    turn_telemetry,
    updated_case,
    was_terminal,
):
    """Append the agent message, save the case, and emit the #1142 turn-telemetry row."""
    agent_message = append_message_row(
        updated_case,
        MessageRowKind.AGENT_ANSWER,
        agent_response_text,
        turn_number=updated_case.current_turn,
        metadata=turn_meta,
    )
    # Read back from the row, not branched beside it: ``TurnResponse``
    # below reads this same name, and a marker that reached only the
    # stored row would leave the live client rendering an empty bubble
    # while a reload showed text that was never delivered. (Slack
    # rejects an empty message outright.) This kind never drops a row.
    agent_response_text = agent_message["content"]
    updated_case.message_count += 1
    await repository.save(updated_case)

    # #1748: the terminal-confirmation pair, counted HERE — after the save, at
    # the one point every route passes through — so a turn that fails or
    # conflicts and is retried counts once, and a route that never reaches the
    # engine (GREETING) counts like any other.
    _count_terminal_confirmation(
        updated_case, intent=intent, user_message=payload.query or ""
    )

    # 4b. #1142: one row per consumed turn, on every route. Emitted
    # AFTER the save so the counter, the case state and both ledgers are
    # the settled post-turn values — the pre-existing
    # ``grounding_assessment`` trace reports from inside response
    # application and therefore carries the PREVIOUS turn's
    # ``turns_without_progress``.
    #
    # The route is taken from the engine's handoff when there is one.
    # The fallbacks are not cosmetic: GREETING and FILE_RECLASSIFICATION
    # are answered here without ever calling the engine, and a terminal
    # case short-circuits inside it, so all three would otherwise be
    # stream GAPS — and a gap silently shortens every streak a consumer
    # computes, making a correct handshake read as an engine-dry run.
    turn_metadata = turn_meta
    # No handoff means the route never reached the engine's progress
    # decision — but it still reported its uploads, because every route
    # runs ``report_turn_uploads``. Reading the arms off the returned
    # metadata rather than defaulting to all-zero is what keeps
    # ``user_supplied_new`` true on a turn where the user DID upload
    # (a file riding a clarification click, or arriving on a closed
    # case). All-zero there would print "the user supplied nothing" on
    # exactly the engine-dry-user-supplying turn this stream exists to
    # surface.
    turn_arms = turn_telemetry.get("arms") or collect_progress_arms(turn_metadata)
    if turn_telemetry.get("path"):
        turn_path = turn_telemetry["path"]
    elif oob_kind is not None:
        turn_path = TurnPath.OUT_OF_BAND
    elif intent_type == IntentType.GREETING:
        turn_path = TurnPath.GREETING
    elif intent_type == IntentType.FILE_RECLASSIFICATION:
        turn_path = TurnPath.RECLASSIFICATION
    elif was_terminal:
        turn_path = TurnPath.TERMINAL
    else:
        turn_path = TurnPath.LLM
    emit_case_turn(
        updated_case,
        path=turn_path,
        arms=turn_arms,
        gate_name=turn_telemetry.get("gate_name"),
        progress_made=bool(turn_metadata.get("progress_made", False)),
        outcome=turn_metadata.get("outcome") or turn_telemetry.get("outcome"),
        validation_repairs=int(turn_telemetry.get("validation_repairs", 0)),
        repair_pattern=turn_telemetry.get("repair_pattern"),
        # ``payload.query``, not ``query``: on an attachment-only turn
        # ``query`` has been replaced by ``generate_implicit_query``'s
        # engine-composed sentence, and reporting its length would say
        # the user wrote a paragraph on a turn they typed nothing. This
        # field's whole job is telling "user went silent" apart from
        # "user wrote a paragraph that produced nothing".
        user_message_chars=len(payload.query or ""),
        attachment_count=len(attachment_metadata or []),
    )
    return agent_response_text


def _count_terminal_confirmation(updated_case, *, intent, user_message) -> None:
    """Count a confirmed terminal transition, and the turn after it (#1748).

    Read from the SAVED turn records. By this point ``turn_history[-1]`` is this
    turn's own record on every route — the engine writes it on the routes that
    reach its bookkeeping and ``_backfill_consumed_turn`` writes it on the rest
    (greeting, file reclassification, out-of-band, the terminal short-circuit) —
    and ``terminal_confirmed_via`` is set only on the record of a turn whose
    confirmation EXECUTED a terminal transition. Terminal states have no
    outgoing transition, so a record carrying a channel also says the case is
    terminal now, and — one record back — that this turn began terminal. No
    separate state check is needed for either count:

    * this turn confirmed one when its own record carries a channel;
    * this turn is the one immediately after a confirmation when the PREVIOUS
      record carries a channel. Every later turn finds a predecessor that
      carries none, so it needs no flag.

    The follow-up counts only a message the user typed: no effective ``intent``
    (the one ``_build_user_message`` settled on, so a client-sent GREETING it
    re-derives from the text counts as typed), and not the payload of one of the
    ack turn's own cards (runbook, regenerate), which carry no intent and arrive
    as their text — recognised by ``terminal_card_action``, the same function
    the terminal handler dispatches on. A click in that turn means this
    confirmation gets no follow-up count at all.

    A metric must never fail a turn: the turn is already saved, and a
    registry failure here is logged and dropped.
    """
    try:
        history = updated_case.turn_history
        to_state = updated_case.state.value
        if history and history[-1].terminal_confirmed_via:
            terminal_confirmation_total.labels(
                via=history[-1].terminal_confirmed_via, to_state=to_state
            ).inc()
        if (
            len(history) >= 2
            and history[-2].terminal_confirmed_via
            and intent is None
            and terminal_card_action(user_message) is None
        ):
            terminal_followup_total.labels(
                via=history[-2].terminal_confirmed_via, to_state=to_state
            ).inc()
    except Exception:
        logger.warning(
            "Terminal-confirmation telemetry failed for case %s",
            getattr(updated_case, "case_id", None),
            exc_info=True,
        )
