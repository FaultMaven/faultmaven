"""Applying a generated response to the case: structured updates, automatic transitions, progress scoring and validation."""

import logging

from faultmaven.core.investigation.evidence_need_linking import (
    link_evidence_suggestions_to_needs,
    suggestions_are_engine_replaced,
    sweep_silent_inferred_needs,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    evidence_suggestion_unlinked_total,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _create_turn_record,
    _perform_hypothesis_housekeeping,
    _resolve_id_ref,
)
from faultmaven.core.investigation.state_validator import ValidationSeverity
from faultmaven.core.investigation.working_conclusion_generator import (
    calculate_progress_metrics,
)
from faultmaven.modules.case.contracts import TurnOutcome

from .affordances import engine_owned_affordances
from .progress import score_progress
from .response_synthesis import is_agent_response_synthesized
from .stage_gates import _refresh_working_conclusion
from .terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _sweep_needs_for_terminal_hypotheses,
)

logger = logging.getLogger(__name__)


async def _apply_turn_response(
    hypothesis_manager,
    progress_monitor,
    state_validator,
    responses,
    transitions,
    *,
    attachments,
    case,
    metadata,
    response_obj,
    upload_report,
    user_message,
):
    """Apply the generated response to the case: structured updates, automatic transitions, progress scoring and validation."""
    case_updated, response_metadata = await responses.process_response_structured(
        case, user_message, response_obj, attachments, upload_report
    )
    # Merge response metadata with early metadata (which may have transition_proposed_this_turn)
    metadata.update(response_metadata)

    # 4a. Stage-gate compliance is now handled via LLM milestone output
    # (Framework §4.1). The LLM sets stage-gate milestones in its
    # structured response; side effects are applied in
    # _apply_investigation_updates → _apply_stage_gate_side_effects.

    # Phase 1: No-Op Detection happens at 4b below, not here. This is
    # where the reading USED to be taken, five lines before
    # ``_check_automatic_transitions`` wrote the arm it scores (#1270).
    #
    # The provisional reading that briefly stood here is gone rather than
    # kept: nothing between this point and 4b reads ``progress_made``
    # (``_check_automatic_transitions`` writes ``status_transitioned``
    # and the readiness verdicts, and reads neither), so it decided
    # nothing — while leaving a value on the shared dict that is
    # provisional BY DESIGN, which is the read-before-write hazard this
    # fix exists to remove, re-armed for whatever gets inserted here
    # next. One decision point per turn.
    #
    # This is not the "move the block" the issue warned against: the
    # block's other statements stay where they are, and deleting a dead
    # call touches less than keeping it.
    # Outcome is already set by _process_response_structured (default) or applied updates (LLM choice)

    # 4. Check for automatic status transitions
    case_updated = await transitions.check_automatic_transitions(
        case_updated, metadata, user_message
    )

    # 4b. Re-score progress now that EVERY arm writer has run (#1270).
    #
    # ``status_transitioned`` is one of the nine arms
    # ``check_if_progress_made`` scores, and ``_check_automatic_transitions``
    # is its only writer on this path — five lines AFTER the read above.
    # So an automatic INQUIRY→INVESTIGATING transition never counted as
    # progress, and ``turns_without_progress`` climbed through a turn
    # that demonstrably advanced the case. Measured on the local corpus
    # before this fix: 158 of 170 cases with an observable
    # INQUIRY→INVESTIGATING turn scored that turn ``progress_made=False``,
    # every visible arm empty. The 12 exceptions all carried an upload,
    # so ``novel_files_uploaded`` — written back at Step 0 — fired for
    # them independently.
    #
    # HERE rather than by moving the read: this is the first point at
    # which every arm writer has run, and it precedes every reader —
    # the stall counter at Step 5.8, the turn record at Step 6, and the
    # #1142 telemetry handoff all re-read ``metadata`` rather than a
    # local. ``_check_automatic_transitions`` never reads
    # ``progress_made``, so scoring after it is not circular, and
    # ``_perform_hypothesis_housekeeping`` (the only other thing between
    # here and Step 5.8) reads ``metadata["system_feedback"]``, the
    # ``case.turns_without_progress`` ATTRIBUTE, which Step 5.8 updates
    # afterwards either way — so anti-anchoring sees the same value it
    # saw before — and ``metadata["progress_made"]``, which decides
    # whether the turn ages ignored priors. That last read is why
    # housekeeping must stay AFTER this call.
    #
    # The invariant this restores is the one the deterministic path
    # already states: every arm is written before the read. See
    # ``_finish_deterministic_turn``.
    score_progress(metadata)

    # 4c. Resolution backstop (INV-43). LAST of the openers, which is
    # what makes it a backstop rather than a fourth competing proposer:
    # step 4 has just run the LLM's own ``proposed_transition`` and the
    # deferred proposer ran inside the apply step before it, so anything
    # they opened is standing on ``pending_transition`` and this bails on
    # it. Only a resolution-READY case that NOBODY offered reaches the
    # proposal. Placed after 4b rather than before it because the offer
    # is engine action, not case progress — it writes none of the arms
    # ``score_progress`` reads.
    _maybe_propose_confirmed_resolution(case_updated, metadata)

    # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
    # This happens after transitions but before recording the turn
    _perform_hypothesis_housekeeping(
        hypothesis_manager,
        case_updated,
        metadata,
        investigation_advanced=metadata["progress_made"],
    )

    # Step 5.5: Calculate progress metrics
    progress_metrics = calculate_progress_metrics(
        case=case_updated, current_turn=case_updated.current_turn
    )
    metadata["momentum"] = progress_metrics.investigation_momentum
    metadata["blocked_reasons"] = progress_metrics.blocked_reasons
    metadata["next_steps"] = progress_metrics.next_steps

    # Step 5.6: Generate working conclusion EVERY turn during INVESTIGATING
    # Gap #7: Working Conclusion Every Turn
    # Reference: Prompt Engineering Guide Section 11.7
    # Why: Provides consistent context tracking, prevents "lost context" issues
    _refresh_working_conclusion(case_updated)
    if case_updated.working_conclusion is not None:
        logger.debug(
            "Working conclusion updated: likelihood="
            f"{case_updated.working_conclusion.likelihood:.2f}"
        )

    # Step 5.7: Validate state consistency
    is_valid, validation_issues = state_validator.is_valid(case_updated)
    validation_repairs: list[str] = []
    if validation_issues:
        # Log validation issues and collect repairs
        for issue in validation_issues:
            if issue.severity == ValidationSeverity.ERROR:
                logger.warning(
                    f"State validation error: {issue.code} - {issue.message}"
                )
                if issue.suggested_fix:
                    validation_repairs.append(f"{issue.code}: {issue.suggested_fix}")
            elif issue.severity == ValidationSeverity.WARNING:
                logger.debug(
                    f"State validation warning: {issue.code} - {issue.message}"
                )
        metadata["validation_issues"] = [
            {"code": i.code, "message": i.message, "severity": i.severity.value}
            for i in validation_issues
        ]

    # Step 5.8: Update progress tracking (before stagnation check)
    if metadata.get("progress_made", False):
        case_updated.turns_without_progress = 0
    else:
        case_updated.turns_without_progress += 1

    # Step 5.9: Progress monitoring (before recording turn)
    # Check if transparent mode should activate and/or repair
    # patterns are detected. Replaces the old stagnation detector.
    progress_result = progress_monitor.check_progress(case_updated)
    stagnation_str: str | None = None
    if progress_result:
        # Record repair pattern if detected
        if progress_result.repair_type:
            stagnation_str = progress_result.repair_type.value
            metadata["stagnation_type"] = progress_result.repair_type.value
            metadata["breakout_action"] = progress_result.repair_action

        metadata["progress_transparent"] = True
        metadata["pending_milestone"] = progress_result.pending_milestone
        metadata["milestone_description"] = progress_result.milestone_description

        # Store prompt injection in system_feedback for next turn
        if progress_result.prompt_injection:
            current_feedback = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = (
                f"{current_feedback}\n{progress_result.prompt_injection}".strip()
            )

        log_msg = (
            f"Progress transparency activated: pending milestone "
            f"'{progress_result.pending_milestone}'"
        )
        if progress_result.repair_type:
            log_msg += f", repair: {progress_result.repair_type.value}"
        logger.info(log_msg)

    # Step 6: Record turn progress
    turn_record = _create_turn_record(
        turn_number=case_updated.current_turn,
        milestones_completed=metadata.get("milestones_completed", []),
        evidence_added=metadata.get("evidence_added", []),
        hypotheses_generated=metadata.get("hypotheses_generated", []),
        hypotheses_validated=metadata.get("hypotheses_validated", []),
        solutions_proposed=metadata.get("solutions_proposed", []),
        progress_made=metadata.get("progress_made", False),
        outcome=metadata.get("outcome", TurnOutcome.CONVERSATION),
        user_message=user_message,
        agent_response=response_obj.agent_response,
        agent_response_synthesized=(
            is_agent_response_synthesized(response_obj)
            or not response_obj.agent_response.strip()
        ),
        system_feedback=metadata.get("system_feedback"),
        momentum=progress_metrics.investigation_momentum,
        blocked_reasons=progress_metrics.blocked_reasons,
        next_steps=progress_metrics.next_steps,
        repair_pattern=stagnation_str,
        # The state validator's repairs, then everything the turn's
        # apply steps recorded on ``metadata["validation_repairs"]`` —
        # the schema's confidence repairs (fm#1502) and the apply-time
        # rejections. The latter were appended to that key and read by
        # nothing, so no turn record carried them; this is where the
        # channel lands. The #1142 telemetry count below stays the state
        # validator's alone, which is what that stream documents.
        validation_repairs=[
            *validation_repairs,
            *metadata.get("validation_repairs", []),
        ],
    )
    case_updated.turn_history.append(turn_record)

    # Evidence-needs Phase 3: run the supersession rule for
    # causal-purpose needs anchored to any TERMINAL hypothesis. Covers
    # every terminal write path without threading ``case`` through
    # their APIs:
    #   - hypothesis_manager.py (low-confidence retirement)
    #   - hypothesis_manager.py (anchoring-prevention retirement)
    #   - hypothesis_manager.py (``refute_hypothesis``)
    #   - progress_monitor.py (INCONCLUSIVE → RETIRED)
    #   - milestone_engine/engine.py (LLM-emitted refutation / retirement)
    #
    # The FULL terminal set is swept, not a newly-terminal diff. The
    # helper is idempotent — it removes the id from every motivating
    # list on the first pass, so later sweeps hit its ``continue`` and
    # change nothing — which makes the steady-state cost a no-op and
    # removes the need for a pre-turn snapshot. The diff form could
    # only ever supersede needs whose motivator turned terminal in the
    # same turn, so a need already carrying a terminal id (a motivator
    # that went terminal before this rule existed, or one left in the
    # list beside a still-active motivator) stayed PENDING for the life
    # of the case with nothing able to clear it. Sweeping everything
    # self-heals those instead of requiring a backfill.
    #
    # Runs BEFORE save() so the supersession lands in the same turn's
    # persisted state.
    _sweep_needs_for_terminal_hypotheses(case_updated)

    # #1079: give every EVIDENCE suggestion a need to hang on, and
    # record the ask on it. Both anti-nagging mechanisms (the
    # obtainability wall and mention decay) act on an EvidenceNeed, so
    # an ask with no need behind it is one neither can ever see — which
    # is how the same request survived ten consecutive turns against a
    # user declining it six times.
    #
    # Placed here for two ordering reasons: BEFORE save() so created
    # needs and the recorded turn persist with the rest of the turn,
    # and BEFORE _flatten_follow_ups (below) so the wire response
    # carries the IDs assigned here. After the terminal sweep, so a need
    # superseded this turn is not a match candidate.
    # Skipped when the engine is going to REPLACE these suggestions
    # further down (gate affordances, the resolution/close prose
    # branches, the closure ack). Those turns never render the model's
    # EVIDENCE asks, and recording an ask the user never saw would decay
    # it toward "stop surfacing" for the wrong reason.
    # GC for engine-inferred needs. They are the orphan shape the
    # terminal-hypothesis sweep above cannot reach (no motivator to key
    # off) — the same shape ``_apply_evidence_need_updates`` refuses to
    # let the MODEL create, for that exact reason. Run before linking so
    # an ask repeated THIS turn is refreshed rather than swept a moment
    # early, and unconditionally (not inside the suggestion branch) so a
    # case that stops emitting suggestions still gets its pool cleaned.
    try:
        sweep_silent_inferred_needs(case_updated, case_updated.current_turn)
    except Exception as sweep_err:  # noqa: BLE001
        logger.warning(
            "Inferred-need sweep failed on case %s: %s",
            case_updated.case_id,
            sweep_err,
        )

    if getattr(response_obj, "suggested_follow_ups", None):
        try:
            gate_pending = engine_owned_affordances(case_updated, metadata) is not None
            if suggestions_are_engine_replaced(case_updated, metadata, gate_pending):
                logger.debug(
                    "Skipping evidence-need linking on case %s turn %s: "
                    "the engine replaces this turn's suggestions",
                    case_updated.case_id,
                    case_updated.current_turn,
                )
            else:
                link_evidence_suggestions_to_needs(
                    case_updated,
                    response_obj.suggested_follow_ups,
                    metadata,
                    case_updated.current_turn,
                    _resolve_id_ref,
                )
        except Exception as link_err:  # noqa: BLE001
            # Never fail a turn over suggestion bookkeeping — the reply
            # is still correct without the linkage, just un-countable.
            # Counted, not merely logged: a systematic failure here
            # turns the whole fix off, and a flat created/matched rate
            # reads identically to a model that started declaring its
            # own needs.
            logger.warning(
                "Evidence-need linking failed on case %s: %s",
                case_updated.case_id,
                link_err,
            )
            try:
                evidence_suggestion_unlinked_total.labels(resolution="error").inc()
            except Exception:
                pass
    return case_updated, stagnation_str, validation_repairs
