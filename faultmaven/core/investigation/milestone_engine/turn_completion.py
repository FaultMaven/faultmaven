"""Finishing a generated turn and composing its final reply: gate prose, follow-ups and telemetry.

Nothing here writes the case. The turn commits once, at the service, with the
rows the turn's ``TurnCommitPlan`` carries (#1882).
"""

import logging
from datetime import UTC, datetime
from typing import Any

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
)
from faultmaven.core.investigation.chat_data import (
    TURN_METADATA_CHAT_DATA_OFFER,
)
from faultmaven.core.investigation.kb_push import TURN_METADATA_KB_PROMPTED
from faultmaven.core.investigation.lifecycle_metrics import (
    engine_owned_affordance_served_total,
    narration_overclaim_total,
)
from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _flatten_follow_ups,
)
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.offers import (
    open_terminal_offer,
    terminal_offer_opened_this_turn,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    CaseState,
    TurnOutcome,
)

from .affordances import (
    _GATE_VERIFICATION_STATUS,
    engine_owned_affordances,
)
from .cause_state import (
    _count_gate1_turn,
    _gate1_statement_presentation,
    _resolve_chat_provider_name,
)
from .progress import summarize_for_turn_record
from .response_synthesis import (
    _NARRATION_OVERCLAIM_NOTICE,
    _NARRATION_OVERCLAIM_NOTICE_PENDING,
    _narration_asserts_disposition,
    _prose_with_gate_notice,
    is_agent_response_synthesized,
)
from .stage_gates import declined_close_card
from .statement_revision import revision_presentation
from .terminal_replies import (
    _build_resolution_confirmation,
    _select_ack_follow_ups,
)

logger = logging.getLogger(__name__)


def _is_resolve_chip(follow_up: Any, resolve_card: dict) -> bool:
    """Whether ``follow_up`` duplicates the "Mark it resolved" chip: the same
    intent, or the same label (a model suggestion naming it, which would
    render as a second, unclickable copy)."""
    if not isinstance(follow_up, dict):
        return False
    label = str(follow_up.get("label") or "").strip().lower()
    return (
        follow_up.get("intent") == resolve_card["intent"]
        or label == resolve_card["label"].lower()
    )


def _narration_overclaim_notice(
    case, agent_text: str | None, *, gate_prose_appended: bool = False
) -> str | None:
    """Return the INV-40 corrective notice when narration over-claims disposition.

    Reconciles the ``_COMPLETION_PHRASES`` scan against engine truth: the notice
    fires only when the LLM asserted an unqualified resolved/closed claim AND the
    engine's state contradicts it — the case is **not** terminal and **no**
    prose gate notice was already composed this turn (any of the
    ``_prose_with_gate_notice`` override branches, which already frame the
    not-yet-terminal state; ``gate_prose_appended`` is the caller's signal that
    one fired). Critically it does **not** suppress on a bare open offer: the
    model's own proposal opens one but appends no prose, so an over-claim there
    would otherwise stand uncontradicted — the guard's most probable
    real-world shape (a model confident enough to over-claim is the same one
    that proposes). The notice wording adapts to whether a resolve or close
    offer is open. Returns ``None`` when there is nothing to correct.

    Pure over ``case`` + ``agent_text`` + ``gate_prose_appended``; the caller
    appends via ``_prose_with_gate_notice`` and increments
    ``narration_overclaim_total``.
    """
    if not _narration_asserts_disposition(agent_text):
        return None
    if case.is_terminal:
        # The claim is true — a terminal transition executed (or the case was
        # already terminal). Nothing to correct.
        return None
    if gate_prose_appended:
        # A prose gate notice already frames the real (not-yet-terminal) state
        # below the LLM's reply; a second notice would be redundant.
        return None
    if open_terminal_offer(case):
        return _NARRATION_OVERCLAIM_NOTICE_PENDING
    return _NARRATION_OVERCLAIM_NOTICE


async def _finalize_turn(
    terminal, *, case_updated, metadata, plan, redaction_ctx, response_obj
):
    """Stamp the turn's activity clock, render the terminal summary if the case just went terminal, flatten the follow-ups and save the redaction registry.

    Persists nothing of the case (#1882). It used to save the case here (the
    engine's "Step 7"), before the reply was composed and before the service's
    own save, so a failure in between committed half a turn. The case now
    commits once, at the service; the summary row this renders goes into
    ``plan`` and commits with the terminal state.
    """
    case_updated.updated_at = datetime.now(UTC)
    case_updated.last_activity_at = datetime.now(UTC)

    # Step 7b: Auto-generate terminal summary synchronously on
    # terminal transition. The rendered summary (or skip / failure
    # note) is appended to the agent reply below so it appears in
    # chat at the moment of generation — consistent with the
    # explicit-confirmation path. `summary_failed` flags a render
    # failure so the ack-turn follow-ups can include the regen
    # affordance (G2).
    summary_payload: str | None = None
    summary_failed: bool = False
    if metadata.get("status_transitioned") and case_updated.state in (
        CaseState.RESOLVED,
        CaseState.CLOSED,
    ):
        summary_payload, summary_failed = await terminal.auto_generate_report(
            case_updated, plan=plan
        )

    logger.info(
        f"Turn {case_updated.current_turn} processed successfully. "
        f"Status: {case_updated.state}, "
        f"Progress made: {metadata.get('progress_made', False)}"
    )

    # Extract follow-up suggestions from LLM response
    follow_ups: list[dict[str, Any]] = []
    if (
        hasattr(response_obj, "suggested_follow_ups")
        and response_obj.suggested_follow_ups
    ):
        follow_ups = _flatten_follow_ups(response_obj.suggested_follow_ups, metadata)

    # Persist redaction registry for cross-turn consistency. Not part of the
    # turn's commit, and need not be: a pseudonym is a keyed function of the
    # value, so mappings left by a turn that then fails are a harmless
    # superset (class C, #1882). It never raises (``CaseRedactionContext``).
    await redaction_ctx.save()
    return follow_ups, summary_failed, summary_payload


async def _compose_turn_reply(
    llm_provider,
    report_service,
    repository,
    *,
    case_updated,
    follow_ups,
    metadata,
    plan,
    redaction_ctx,
    response_obj,
    stagnation_str,
    summary_failed,
    summary_payload,
    validation_repairs,
):
    """Compose the final agent-facing reply text, gate prose, follow-ups and telemetry for the turn."""
    response_synthesized = is_agent_response_synthesized(response_obj)
    agent_response_text = "" if response_synthesized else response_obj.agent_response

    # Gate prose for the offer the turn opened, or the request it made. Gate
    # PROSE is composed with (appended below) the LLM's reply via
    # _prose_with_gate_notice — never replacing the analysis the user asked
    # for. Gate SUGGESTIONS stay engine-owned replacements: the open offer's
    # confirmation pair, from the offer projection (``offer_projection.project_offers``),
    # here and again in ``engine_owned_affordances`` below.
    #
    # ``gate_prose_appended`` records whether one of the PROSE branches fired:
    # each frames the not-yet-terminal state below the LLM's reply, so the
    # INV-40 guard suppresses on it. The model's own proposal appends NO prose,
    # so the guard must still fire there (INV-40 — a proposed transition alone
    # does not contradict a "Case resolved." narration).
    gate_prose_appended = False
    # Set when Gate 1 composed its statement presentation, and checked
    # against the FINAL reply at the return boundary — see the counter there.
    _gate1_presentation: str | None = None
    projection = project_offers(case_updated)
    # The prose each engine opener publishes for its offer, in precedence
    # order: the re-check of a resolution request (step 0), then the engine's
    # own proposers. Every one says why the offer is on the table.
    _OFFER_PROSE = (
        "rca_infeasible_closure_message",
        "false_alarm_closure_message",
        "deferred_solution_gate_message",
        "resolution_ready_gate_message",
    )
    offer_prose = next(
        (metadata[key] for key in _OFFER_PROSE if metadata.get(key)), None
    )
    if metadata.get("resolution_ready_for_confirmation"):
        agent_response_text = _prose_with_gate_notice(
            agent_response_text,
            "Thanks for the additional details.\n\n"
            + _build_resolution_confirmation(case_updated),
        )
        follow_ups = projection.pair
        gate_prose_appended = True
    elif metadata.get("resolution_suggest_close"):
        # The request was not met: Close is offered instead.
        agent_response_text = _prose_with_gate_notice(
            agent_response_text,
            metadata["resolution_readiness_message"],
        )
        follow_ups = projection.pair
        gate_prose_appended = True
    elif metadata.get("resolution_needs_info_first_pass"):
        # The model proposed RESOLVED but readiness is NEEDS_INFO: a request,
        # not an offer (#2408). The readiness ask goes below the LLM's reply,
        # and the follow-ups are the model's own: no confirmation chips.
        agent_response_text = _prose_with_gate_notice(
            agent_response_text,
            metadata["resolution_needs_info_message"],
        )
        gate_prose_appended = True
    elif offer_prose is not None:
        # An engine-proposed offer (the rca-infeasible or false-alarm close,
        # the deferred disposition, the resolution backstop): its reason is
        # composed below the model's reply, because an offer the user did not
        # ask for and the model did not narrate is otherwise two unexplained
        # buttons (fm#1122, case_fa29e0023b85 turns 11-15).
        agent_response_text = _prose_with_gate_notice(agent_response_text, offer_prose)
        follow_ups = projection.pair
        gate_prose_appended = True

    # Engine-owned gate affordances. When a state-machine gate is
    # pending (Gate 1 — problem-statement confirmation; or an open
    # offer on the case's offer record), the engine
    # emits the canonical clickable affordance pair regardless of
    # LLM compliance with the prompt's suggestion-emission
    # directives. The consolidator is a single source of truth that
    # replaced the previously-scattered handshake-deferred / Gate 2
    # / Gate 3 branches. Gate 1 now fires on every Gate-1-pending
    # turn (not only the handshake-deferred recovery turn) — the
    # architectural completion that makes Gate 1 symmetric with
    # Gate 2 and Gate 3, and removes LLM compliance from the
    # correctness path. See INV-01, INV-19, INV-21.
    #
    # It also drives the mid-investigation correctives (code-guarded,
    # always on): the insufficient-evidence structured handoff (a
    # work-gated stall with no grounded cause), the restatement-held
    # handoff (#1195 — the same stall where the block is the cause's
    # phrasing rather than missing data) and the NOT_YET_PRODUCTIVE
    # pull-back (a persisted 0-hypothesis vacuum — #656 P3.1). All read a
    # FRESH grounding grade because this runs after
    # ``_apply_investigation_updates`` recomputed cause_state this turn
    # (the #593 re-derive-after-stamp ordering the plan requires).
    gate_result = engine_owned_affordances(case_updated)
    if gate_result is not None:
        gate_name, gate_affordances = gate_result
        # REPLACE the LLM's suggestions with the engine-owned gate
        # affordances. This is the engine↔LLM suggestion-ownership
        # boundary:
        #
        #   A suggestion answers "what is the user's next move?"
        #   - STATE-MACHINE moves (confirm/refine a gate, close,
        #     resolve) advance the case's formal lifecycle. The ENGINE
        #     owns them: only it knows the valid transitions, can
        #     attach deterministic ``intent``, and can guarantee the
        #     affordance is clickable every turn (INV-01).
        #   - CONTENT moves (share data, explore an angle, describe
        #     symptoms) advance the investigation's content. The LLM
        #     owns these — but only when NO gate is pending, in which
        #     case ``engine_owned_affordances`` returns None and the
        #     LLM's suggestions pass through untouched (above).
        #
        # When a gate IS pending the case is BLOCKED on a state-machine
        # decision, so the gate moves are the only meaningful next
        # moves — content moves are premature (you cannot gather
        # investigation data before the problem is even confirmed).
        # The engine therefore owns the whole list. A tangential user
        # question on a gate turn is answered in the agent's PROSE, not
        # via suggestions; the next-move affordances stay confirm/refine.
        #
        # The insufficient-evidence handoff replaces for a parallel
        # reason: on a work-gated stall the LLM's own suggestions are the
        # least trustworthy (this is exactly the turn a weak model
        # fabricates a cause or spins), so the engine overrides them with
        # honest keep-engaging moves. The model's *content* — what
        # specifically would decide it — still lands in the PROSE.
        #
        # We do NOT augment (append the LLM's suggestions). The LLM,
        # asked to confirm, naturally emits its OWN confirm/decline
        # suggestions ("Yes, that's correct. Let's investigate." / "No,
        # that's not quite right."), which carry no ``intent`` and would
        # render as duplicate, overlapping buttons beside the engine's
        # authoritative pair (observed on case_d22ebbd63784). Relevance
        # on a gate turn comes from the gate opening ONLY when it should
        # (intent detection — Answer First), not from mixing in the
        # LLM's premature/duplicate suggestions.
        # Gate 1 is the one gate whose affordances refer to a piece
        # of CASE TEXT, so it is the one gate that has to ship that
        # text with them. INV-26(b) already says the visible transcript
        # may not contradict the applied state_updates and enforces it
        # through ``_prose_with_gate_notice`` for every disposition
        # gate; Gate 1 predates that mechanism and was never brought
        # under it. It is now.
        #
        # Composed, never substituted: whatever the user actually asked
        # this turn stays above, and the statement lands below it. That
        # is the #430 boundary holding — the engine owns the gate's
        # SUGGESTIONS outright, and composes (does not replace) prose.
        #
        # ``gate_prose_appended`` is deliberately NOT set: this block
        # frames the INQUIRY problem, not a disposition, so it does not
        # contradict a "case resolved" over-claim. INV-40 must still be
        # free to fire on the same turn.
        if gate_name == "gate1":
            _gate1_presentation = _gate1_statement_presentation(case_updated)
            agent_response_text = _prose_with_gate_notice(
                agent_response_text, _gate1_presentation
            )
        elif gate_name == "statement_revision":
            # The revision handshake is Gate 1 inside INVESTIGATING, so it
            # ships its text with its buttons on the same terms: composed
            # below the reply, every pending turn.
            agent_response_text = _prose_with_gate_notice(
                agent_response_text, revision_presentation(case_updated)
            )

        follow_ups = gate_affordances
        if gate_name != "gate1":
            # Gate 1's is counted with its outcome, at the return boundary
            # (``_count_gate1_turn``), so INV-01's pair moves together.
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
        # Record the verification status on the turn when the handoff
        # fired. This turn-metadata copy is the return-boundary signal;
        # the durable reading lives on ``case.progress.verification_status``
        # (persisted each turn). The affordance-served metric above
        # already carries the firing count per gate.
        if gate_name in _GATE_VERIFICATION_STATUS:
            metadata["verification_status"] = _GATE_VERIFICATION_STATUS[gate_name].value

    # Closure-ack turn (LLM-driven path): when generation
    # succeeded, suggestions stay minimal — the rendered summary
    # is right above and a regen card next to it would be noise.
    # When generation failed, include the regen affordance so the
    # user can retry immediately (G2 — the "noise" guard doesn't
    # apply when there's no inline summary).
    if metadata.get("status_transitioned") and case_updated.state in (
        CaseState.RESOLVED,
        CaseState.CLOSED,
    ):
        remaining = await _remaining_regens_for(
            report_service,
            repository,
            case_updated,
            pending=plan.pending_reports(),
        )
        follow_ups = _select_ack_follow_ups(case_updated, summary_failed, remaining)

    # The model re-proposed a close the user declined, and step 2 refused it
    # (#1889). The close stays one step away: its card is APPENDED to the
    # follow-ups as settled above, never substituted for them — replacing the
    # model's suggestions with a lone close button would turn the refusal into
    # the re-ask it exists to stop. No terminal
    # check: nothing reaches the refusal on a terminal case (0a hands its
    # turns to the terminal path, and step 2's refusal sits behind the legality
    # check, which admits no target from a terminal state).
    declined_side = metadata.get("declined_close_card")
    if declined_side:
        follow_ups = [*follow_ups, declined_close_card(declined_side)]
    # The resolve side (#1895): the "Mark it resolved" chip is on screen on
    # EVERY turn a resolve decline stands, whatever the model did. The model
    # never proposes on a request (``RESOLVE_DECLINED_RULE``) and points a user
    # who changes their mind at this chip, so the chip cannot depend on a
    # refusal having happened. The projection's chip: the decline stands, no
    # offer is open, the problem is not on hold. A new confirmation (the offer
    # returns with its own pair) or an open offer leaves it off. Appended
    # once: any follow-up already carrying its label or its intent is dropped
    # first, so the turn shows exactly one.
    resolve_card = projection.reopen_chip
    if resolve_card is not None:
        follow_ups = [
            *(f for f in follow_ups if not _is_resolve_chip(f, resolve_card)),
            resolve_card,
        ]

    # Append the synthesized summary (or skip / failure note) so it
    # appears in chat at the moment of generation. The composed reply
    # is committed by the service, in the turn's one commit, from the
    # returned ``agent_response`` — turn_history records are frozen and
    # carry only a summary, never the chat text.
    if summary_payload:
        agent_response_text = f"{agent_response_text}\n\n{summary_payload}".strip()

    # INV-40 (§7.9): narration-truth coherence guard. The narration
    # channel (agent_response) is LLM free text and sits outside every
    # truth surface the §7.6 reconciliation lane reads — so an LLM that
    # narrates "Case resolved." on a case the engine holds at
    # INVESTIGATING (the #668 incident, 3/3 on long-context haiku)
    # delivers a false disposition claim the user acts on. Reconcile the
    # existing narrow completion-phrase scan against engine truth and,
    # when it over-claims, APPEND a corrective notice below the LLM's
    # prose (the INV-26 composition lane, never a substitution — the DF-4
    # lesson). This runs after the summary append above, so a genuine
    # terminal transition (state now RESOLVED/CLOSED) is excluded by
    # construction; the guard fires only on the truth-split.
    # ``gate_prose_appended`` suppresses the guard on the branches
    # that already appended a state-framing gate notice — but NOT on the
    # suggestions-only override branch, whose bare proposed_transition
    # leaves an over-claim uncontradicted (the guard's likeliest shape).
    # Scans what the MODEL wrote, not the composed turn. Gate prose is
    # engine-authored, and Gate 1's carries the user's own problem
    # statement verbatim — a statement reading "users report the case
    # resolved itself overnight" would otherwise trip the completion
    # scan and have the engine contradict its own presentation.
    _overclaim_notice = _narration_overclaim_notice(
        case_updated,
        response_obj.agent_response,
        gate_prose_appended=gate_prose_appended,
    )
    if _overclaim_notice is not None:
        agent_response_text = _prose_with_gate_notice(
            agent_response_text, _overclaim_notice
        )
        narration_overclaim_total.labels(
            provider=_resolve_chat_provider_name(llm_provider)
        ).inc()
        logger.warning(
            "narration_overclaim_corrected",
            extra={
                "case_id": case_updated.case_id,
                "turn": case_updated.current_turn,
                "state": case_updated.state.value,
            },
        )

    # The turn record (step 6) summarized the RAW LLM text; the gate,
    # summary, and INV-40 compositions above changed only the returned
    # reply. Re-record the summary channel when they diverge: the
    # next-turn prompt (context_builder) and the turn_outcome
    # heuristics read ``agent_response_summary``, so without this the
    # model is replayed its own uncorrected over-claim (the #668 loop
    # INV-40 exists to break) and terminal summaries vanish from
    # long-case state prompts. TurnProgress is frozen — replace the
    # record, never mutate; the turn's one commit, at the service,
    # persists it alongside the messages.
    # Nothing composed onto a synthesized placeholder: it IS the reply,
    # and stays flagged. Anything composed replaced it with engine
    # prose, which is a real answer and is not flagged.
    if response_synthesized and not agent_response_text.strip():
        agent_response_text = response_obj.agent_response
    else:
        response_synthesized = False
    if case_updated.turn_history and agent_response_text != response_obj.agent_response:
        case_updated.turn_history[-1] = case_updated.turn_history[-1].model_copy(
            update={
                "agent_response_summary": summarize_for_turn_record(
                    agent_response_text, 500
                ),
                # Re-derived with the text, never carried over: the
                # record step 6 wrote described the raw reply.
                "agent_response_synthesized": (not agent_response_text.strip()),
            }
        )

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
    # (INV-15 drift note). The scan reuses the module-level
    # _COMPLETION_PHRASES via _narration_asserts_disposition, so the
    # telemetry and the INV-40 guard share ONE scan implementation (not
    # just one phrase list) — no re-implemented any(...) to drift.
    # Capture LLM-vs-engine drift on the proposed-transition path.
    # When the LLM emits to_state=resolved on a thin case, the engine
    # pivots to closed (see _check_automatic_transitions). Recording
    # the pivot here lets us compare LLM intent against engine action
    # over time without diffing log lines.
    _llm_proposed = getattr(
        getattr(response_obj, "state_updates", None),
        "proposed_transition",
        None,
    )
    _llm_proposed_to_status = (
        getattr(_llm_proposed, "to_state", None) if _llm_proposed else None
    )
    _open_offer = open_terminal_offer(case_updated)
    _engine_to_status = _open_offer.kind.to_state if _open_offer else None
    # A proposal the engine's same-turn offer superseded was DROPPED, not
    # pivoted (#1885): the two are separate fields so the pivot rate keeps
    # meaning "the engine re-read the model's request".
    _transition_superseded = bool(metadata.get("transition_superseded_by_engine"))
    _transition_pivoted = bool(
        _llm_proposed_to_status
        and _engine_to_status
        and _llm_proposed_to_status != _engine_to_status
        and not _transition_superseded
    )
    logger.info(
        "transition_compliance",
        extra={
            "case_id": case_updated.case_id,
            "turn": case_updated.current_turn,
            "state": case_updated.state.value,
            "proposed_transition_emitted": bool(
                terminal_offer_opened_this_turn(case_updated)
            ),
            "llm_proposed_to_status": _llm_proposed_to_status,
            "engine_effective_to_status": _engine_to_status,
            "transition_pivoted": _transition_pivoted,
            "transition_superseded_by_engine": _transition_superseded,
            # The model's proposal re-asked a disposition the user declined
            # and was refused: ``false_alarm`` or ``deferred`` (#1889),
            # ``resolve`` (#1895), else None.
            "transition_refused_as_declined": (
                metadata.get("declined_close_card")
                or ("resolve" if metadata.get("declined_resolve_card") else None)
            ),
            "user_confirmed_investigation_emitted": bool(
                getattr(
                    getattr(response_obj, "state_updates", None),
                    "user_confirmed_investigation",
                    False,
                )
            ),
            # The model's own narration, for the same reason the INV-40
            # guard above reads it: attributing an engine-composed
            # phrase to the model corrupts the telemetry it feeds.
            "agent_response_contains_completion_phrase": (
                _narration_asserts_disposition(response_obj.agent_response)
            ),
            "status_transitioned": bool(metadata.get("status_transitioned")),
            # Readiness verdicts explain WHY a proposed transition did
            # not transition this turn (pending confirmation /
            # needs_info / pivot) — without them a pending handshake
            # reads as a silent gate refusal (#656 triage).
            "resolution_readiness_verdict": metadata.get(
                "resolution_readiness_verdict"
            ),
            "resolution_readiness_missing": metadata.get(
                "resolution_readiness_missing"
            ),
            "closure_readiness_verdict": metadata.get("closure_readiness_verdict"),
        },
    )

    # INV-01 outcome check. Counting at the composition site would be
    # a second rule-fire counter for one rule fire — the ratio against
    # the affordance counter would read 1.0 by construction, two
    # adjacent lines apart, and could not detect anything. Verified
    # HERE instead, against the text actually returned, so anything
    # that drops or mangles the block between composition and return
    # shows up as the gap the alert is written for.
    # The check reads the rendered, block-quoted presentation, so a
    # multi-line statement counts one for one; the helper is the one
    # ``_refuse_offer_click`` uses too.
    if _gate1_presentation is not None:
        _count_gate1_turn(case_updated, _gate1_presentation, agent_response_text)

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
            # Verification-status Phase 1: the insufficient-evidence
            # handoff records the status on the internal working dict;
            # surface it here so it crosses the return boundary (the
            # calibration eval / Phase-3 persistence read it). Absent
            # (None) on turns the handoff did not fire.
            "verification_status": metadata.get("verification_status"),
            # The KB entries this turn's prompt carried, as the prompt build
            # reported them (``TURN_METADATA_KB_PROMPTED``). Named here because
            # this return forwards a fixed key list; the service builds the
            # turn's ``sources`` from it and drops the raw entries.
            TURN_METADATA_KB_PROMPTED: metadata.get(TURN_METADATA_KB_PROMPTED, []),
            # The offer this turn made of system output typed into the
            # message (#1930); the service renders its three choices.
            TURN_METADATA_CHAT_DATA_OFFER: metadata.get(TURN_METADATA_CHAT_DATA_OFFER),
            "timestamp": datetime.now(UTC).isoformat(),
            # The turn's uploads, on the SAME footing as on the
            # deterministic branches (#1229). This return rebuilds
            # metadata from a fixed key list rather than forwarding the
            # working dict, so a key added to that dict does not reach a
            # caller unless it is named here — and the two upload keys
            # were not, which made an identical file visible on a gate
            # turn and invisible on an ordinary one. Spread rather than
            # ``.get()``-ed so the keys stay ABSENT on a turn with no
            # uploads, which is what every consumer expects and what the
            # deterministic branches do. The service persists this dict
            # onto the assistant ``case_messages`` row, so it is durable,
            # not merely returned.
            **{
                k: metadata[k]
                for k in ("files_uploaded", "novel_files_uploaded")
                if k in metadata
            },
            # #1451: the reply is a placeholder this engine wrote. The
            # service persists this dict onto the assistant row, which
            # is what tells every renderer not to quote it. Absent,
            # not False, on an answered turn — the same footing as the
            # service backstop that writes this key.
            **(
                {MESSAGE_METADATA_AGENT_SYNTHESIZED: True}
                if response_synthesized
                else {}
            ),
            # #1142 handoff. Four of the predicate's arms
            # ``check_if_progress_made`` scores — ``novel_evidence_added``,
            # ``novel_solutions_proposed``, ``status_transitioned``,
            # ``hypothesis_evidence_links_applied`` — live only on the
            # working dict above and are written nowhere, so
            # ``progress_made`` is currently recorded without the evidence
            # for WHY. Counted here, at the point of decision, and read by
            # the service one frame up.
            #
            # Underscore-prefixed and POPPED by the service before the
            # returned metadata is persisted onto the assistant
            # ``case_messages`` row: unlike the keys above this is
            # monitoring data, and that row is readable through the
            # transcript API.
            TELEMETRY_HANDOFF_KEY: {
                "path": TurnPath.LLM,
                "arms": collect_progress_arms(metadata),
                "gate_name": gate_result[0] if gate_result else None,
                "validation_repairs": len(validation_repairs),
                "repair_pattern": stagnation_str,
            },
        },
    }
