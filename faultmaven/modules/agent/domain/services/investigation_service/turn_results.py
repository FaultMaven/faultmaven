"""Unpacking a turn handler's result dict: turn-history backfill (#1264), reverse-redaction, and rebuilding the case's stored suggestions from this turn's clarification and follow-up choices."""

from typing import (
    Any,
)

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
)
from faultmaven.core.investigation.suggestion_liveness import (
    entry_file_id,
)
from faultmaven.modules.agent.domain.services.investigation_service.clarification import (
    _build_classification_clarification,
    _carry_forward_unresolved_clarifications,
    _stored_suggestions,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _backfill_consumed_turn,
    _record_composed_reply,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
)


def _absorb_engine_result(*, preprocess_results, query, result):
    """Backfill turn history, reverse-redact the response, and rebuild stored suggestions from the handler's result."""
    updated_case = result["case_updated"]
    agent_response_text = result["agent_response"]

    # 3a. #1264: every consumed turn gets a ``turn_history`` entry.
    #
    # Both repositories persist ``Case.effective_current_turn`` — the
    # last recorded turn number — rather than the in-flight
    # ``current_turn``. That is #500's prevention half, and it is still
    # right: it stops the stored counter running ahead of the history,
    # which is what let one interrupted turn permanently wedge a case.
    # But it means a route that consumes a turn number WITHOUT recording
    # one freezes the persisted counter. ``process_turn`` reloads the
    # case every request and derives ``next_turn`` from that column, so
    # the very next turn re-derives the number just used — no process
    # boundary required. Measured on the corpus: 7 cases carry a
    # ``(case_id, turn_number)`` pair with two user messages, and one
    # resolved case has THREE user turns all stamped turn 9.
    #
    # The rule this restores is one the engine already states: its
    # deterministic branches record a TurnProgress because "a
    # deterministic branch still consumes a turn number"
    # (``_finish_deterministic_turn``). So ``turn_history`` is already a
    # record of CONSUMED turns rather than of engine turns, and the
    # routes that skip it — greeting, file reclassification, and the
    # terminal short-circuit — are the ones that were missed, not a
    # different kind of turn.
    #
    # Placed at the chokepoint rather than at those three sites for the
    # reason the telemetry emission is here too: this method is where a
    # turn number is consumed, so a backstop here cannot be missed by a
    # route added later. It is a no-op on every path that already
    # recorded, which is the overwhelming majority.
    # ONE binding of the turn's metadata dict, shared by every reader
    # and by the backfill that WRITES to it (#1270). ``or {}`` cannot be
    # used here: ``{}`` is falsy, so a route returning ``{"metadata":
    # {}}`` -- or omitting the key -- got a FRESH dict, the backfill's
    # progress reading was written into that throwaway, and the three
    # surfaces below (the persisted assistant message, the #1142 row,
    # ``TurnResponse.progress_made``) went on reading
    # ``result["metadata"]``, which never received it. That is exactly
    # the one-turn-three-verdicts split this fix exists to close,
    # re-opened by a defensive default. ``setdefault`` binds the real
    # dict and installs one when the key is absent, so the write always
    # lands where the reads look.
    turn_meta: dict[str, Any] = result.setdefault("metadata", {})

    _backfill_consumed_turn(
        updated_case,
        user_message=query or "",
        agent_response=agent_response_text,
        metadata=turn_meta,
    )

    # Placed HERE, not beside the save: ``next_read_turn`` below is
    # ``effective_current_turn + 1``, and every consumer of the turn
    # clock on this path reads it after this point. Recording the turn
    # after them would leave them reading a counter that is one behind
    # for this turn — which is the same off-by-one this issue is about,
    # just relocated. Caught by #1263's window test, which stopped
    # closing its recovery window.

    # #1142: lift the engine's progress-arm reading out of the returned
    # metadata BEFORE step 4 persists that dict onto the assistant
    # ``case_messages`` row. Popped rather than copied: the row is
    # readable through the transcript API, and this is monitoring data
    # collected like logging data, not part of the product surface.
    turn_telemetry = turn_meta.pop(TELEMETRY_HANDOFF_KEY, None) or {}

    # Reverse-substitute PII placeholders so user sees real values.
    # The LLM worked with redacted content; the user should not.
    redaction_ctx = result.get("redaction_ctx")
    if redaction_ctx:
        agent_response_text = redaction_ctx.reverse(agent_response_text)

    # 3b. Store suggestions with intent metadata for next turn's
    #      intent resolver (bounded choice matching). Clarification
    #      suggestions (classification_failed this turn) are built
    #      here — before the save — so a user who *types* a choice
    #      ("application logs") instead of clicking resolves to the
    #      same file_reclassification intent as a click.
    #
    # Read the carry off ``updated_case``: the reclassification
    # handler ``model_copy``s the case, so this is still the PREVIOUS
    # turn's list at this point.
    #
    # ``next_read_turn`` is the number the NEXT turn's adoption site
    # will compute, and BOTH sides of the seam are filtered at it, so
    # what is stored is exactly what the next read accepts. It is
    # ``effective_current_turn + 1``, not ``current_turn + 1``. Since
    # #1264 those agree on every route — the backfill above guarantees
    # this turn is recorded before the counter is read — but deriving
    # from the persisted clock keeps the seam correct BY CONSTRUCTION
    # rather than by the two happening to match. If a route ever stops
    # recording again, that shows up as a clock bug, not as silently
    # dropped clarification questions.
    # Filtering at the wrong one is not a rounding error — it ages
    # every entry an extra turn after every clarification click and
    # permanently drops questions the reader would still have taken.
    resolved_file_id = turn_meta.get("file_reclassified", {}).get("file_id")
    next_read_turn = updated_case.effective_current_turn + 1
    carried_entries = _carry_forward_unresolved_clarifications(
        updated_case.last_suggestions,
        updated_case,
        resolved_file_id,
        as_of_turn=next_read_turn,
    )

    # Choices and the note that introduces them come back together
    # from one filter pass, so the note cannot name a different set
    # of attachments than the choices target.
    clarification, clarification_note = _build_classification_clarification(
        preprocess_results
    )

    raw_follow_ups = result.get("suggested_follow_ups", [])
    stored = _stored_suggestions(
        case=updated_case,
        clarification=clarification,
        carried=carried_entries,
        follow_ups=raw_follow_ups,
        offered_turn=updated_case.current_turn,
        as_of_turn=next_read_turn,
    )
    updated_case.last_suggestions = stored or None

    # The CARDS are derived from what survived storage, so one rule
    # decides both. A turn can deliver an unclassifiable attachment
    # AND close the case; ``_handle_file_reclassification`` refuses on
    # a terminal case, so each card would be a button that answers 422
    # while the typed route is silently dropped by the liveness rule —
    # and "How should I treat it?" is not a question a closed case is
    # asking. Special-casing ``is_terminal`` here instead would put the
    # same judgement in two places and, worse, make the filter inside
    # ``_stored_suggestions`` unreachable: an invariant nothing can
    # break is an invariant nothing is checking.
    offered_ids = {entry_file_id(e) for e in stored}
    clarification = [
        s for s in clarification if (s.intent or {}).get("file_id") in offered_ids
    ]
    if not clarification:
        clarification_note = None
    if clarification_note:
        # #1660: the note is the whole reply when the answer was blank,
        # and the turn record — written above as unanswered — has to
        # say what the row will. A row the engine flagged keeps its
        # flag below, so its record is left as it is.
        composed_onto_nothing = not agent_response_text.strip() and not (
            turn_meta.get(MESSAGE_METADATA_AGENT_SYNTHESIZED)
        )
        agent_response_text += clarification_note
        if composed_onto_nothing:
            _record_composed_reply(updated_case, agent_response_text)
    return (
        agent_response_text,
        clarification,
        raw_follow_ups,
        turn_meta,
        turn_telemetry,
        updated_case,
    )
