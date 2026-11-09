"""Applying a generated response to the case: structured updates, automatic transitions, progress scoring and validation."""

import logging
from typing import Any, NamedTuple, Optional

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
    _determine_turn_outcome,
    _perform_hypothesis_housekeeping,
    _resolve_id_ref,
)
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY, NoticeCode
from faultmaven.core.investigation.offers import lapse_offers
from faultmaven.core.investigation.outcome import record_case_outcome
from faultmaven.core.investigation.problem_status import problem_on_hold
from faultmaven.core.investigation.state_validator import ValidationSeverity
from faultmaven.core.investigation.working_conclusion_generator import (
    calculate_progress_metrics,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    HypothesisState,
    TurnOutcome,
)

from .affordances import engine_owned_affordances
from .cause_state import _recompute_assessment_state
from .progress import restatement_stall_notice, score_progress
from .response_application import ASSESSMENT_RECOMPUTED_KEY, FIX_CAUSE_AT_START_KEY
from .response_synthesis import is_agent_response_synthesized
from .stage_gates import (
    SYSTEM_NOTICE_KEYS,
    _add_system_feedback,
    _refresh_working_conclusion,
    fix_cause,
    merge_decisions,
    merge_system_notices,
    record_decision,
)
from .statement_revision import merge_statement_commit
from .terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _sweep_needs_for_terminal_hypotheses,
)
from .turn_narration import OUTCOME_WRITTEN_KEY

logger = logging.getLogger(__name__)


class TurnRecordInputs(NamedTuple):
    """What the turn record takes from the decision cycle; it is created in
    ``_finalize_turn``, after the narration step (#2636, A20)."""

    progress_metrics: Any
    stagnation_str: Optional[str]
    validation_repairs: list
    cause_at_start: Any


def _standing_hypothesis_ids(case) -> set[str]:
    return {
        h_id
        for h_id, h in (case.hypotheses or {}).items()
        if h.state != HypothesisState.RETIRED
    }


def _final_recompute(case, metadata: dict, llm_outcome) -> None:
    """The cycle's last recompute (#2636, Q8 (a)): the pool after
    housekeeping, the monitor's repair and the needs sweeps. Its notices are
    codes, so they deduplicate against step 1's. A ``root_cause_identified``
    this turn added and this recompute dropped is removed from the turn's
    milestones (the falling edge), and the turn's outcome is re-derived;
    ``progress_made`` and housekeeping's ``investigation_advanced`` keep the
    step-1 reading (A12)."""
    _recompute_assessment_state(
        case,
        exclusion_survivors=metadata.get("deductive_survivor_ids", frozenset()),
        rcc_authored_this_turn=False,
        metadata=metadata,
        provider_name=metadata[ASSESSMENT_RECOMPUTED_KEY],
    )
    milestones = metadata.get("milestones_completed") or []
    if (
        "root_cause_identified" in milestones
        and case.progress.cause_state != CauseState.IDENTIFIED
    ):
        milestones.remove("root_cause_identified")
        metadata["outcome"] = _determine_turn_outcome(case, metadata, llm_outcome)


def record_turn(
    case, metadata: dict, inputs: TurnRecordInputs, *, user_message, response_obj
) -> None:
    """Step 6: the turn record, after the narration step rendered the
    notices it copies (#2636)."""
    case.turn_history.append(
        _create_turn_record(
            turn_number=case.current_turn,
            cause_at_start=inputs.cause_at_start,
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
            system_notices=metadata.get("system_notices"),
            momentum=inputs.progress_metrics.investigation_momentum,
            blocked_reasons=inputs.progress_metrics.blocked_reasons,
            next_steps=inputs.progress_metrics.next_steps,
            repair_pattern=inputs.stagnation_str,
            # The state validator's repairs, then everything the turn's apply
            # steps recorded on ``metadata["validation_repairs"]`` — the
            # schema's confidence repairs (fm#1502) and the apply-time
            # rejections. The #1142 telemetry count stays the state
            # validator's alone, which is what that stream documents.
            validation_repairs=[
                *inputs.validation_repairs,
                *metadata.get("validation_repairs", []),
            ],
        )
    )


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
    # The apply step's proposers read an answer section 0b gave on this turn
    # off the case's offer record, not off this metadata.
    case_updated, response_metadata = await responses.process_response_structured(
        case,
        user_message,
        response_obj,
        attachments,
        upload_report,
    )
    # Merge response metadata with early metadata. The accumulators written
    # before the LLM call — a declined revision's note, a refused hypothesis
    # action, a replay's refusals, repairs and decision codes — are merged
    # with the response's own, never replaced by them: the turn record is the
    # only way the next turn learns of them.
    early = {key: metadata[key] for key in SYSTEM_NOTICE_KEYS if key in metadata}
    early_repairs = list(metadata.get("validation_repairs") or [])
    early_decisions = list(metadata.get(DECISIONS_KEY) or [])
    metadata.update(response_metadata)
    if early.get("system_notices"):
        for key in SYSTEM_NOTICE_KEYS:
            metadata.pop(key, None)
        metadata.update(early)
        merge_system_notices(metadata, response_metadata)
    if early_decisions:
        metadata[DECISIONS_KEY] = early_decisions
        merge_decisions(metadata, response_metadata)
    if early_repairs:
        live_repairs = list(metadata.get("validation_repairs") or [])
        metadata["validation_repairs"] = early_repairs + [
            r for r in live_repairs if r not in early_repairs
        ]
    # A revision confirmed this turn replayed its staged cause work BEFORE the
    # LLM call, into lists the update above just replaced. Fold them back, and
    # re-derive the outcome the response pipeline settled without them.
    state_updates = getattr(response_obj, "state_updates", None)
    llm_outcome = getattr(state_updates, "outcome", None) or TurnOutcome.CONVERSATION
    if metadata.get("statement_commit"):
        merge_statement_commit(metadata)
        metadata["outcome"] = _determine_turn_outcome(
            case_updated, metadata, llm_outcome
        )

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

    # 3b. The offer record's lapse rule, before step 4 reads it
    # (``offers.lapse_offers``). A decline given on THIS turn covers what the
    # turn just recorded (#1895), so step 2 refuses a same-turn resolve
    # proposal on the user's "not yet"; an older decline whose premise moved
    # (a new solution or confirmation) is stamped lapsed, and the backstop
    # re-offers on this turn.
    case_updated.offer_record = lapse_offers(case_updated)

    # 4. Check for automatic status transitions
    case_updated = await transitions.check_automatic_transitions(
        case_updated, metadata, user_message
    )

    # 4b. Re-score progress now that EVERY arm writer has run (#1270).
    #
    # ``status_transitioned`` is one of the arms
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
    # here and Step 5.8) reads ``metadata["system_notices"]``, the
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

    # The deciders after scoring (#2615 Q8 (a), #2636): housekeeping, the
    # stall counter, the progress monitor and the needs sweeps, then a final
    # recompute, so the backstop and the record read one pool. A hypothesis
    # one of them retires is a code in the decision log (A19).
    standing = _standing_hypothesis_ids(case_updated)

    # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
    # Paused while the case holds on the user or on new evidence (a revision
    # awaiting re-confirmation, a false alarm): stagnation, decay, anchoring
    # and age-out all read turns, and a hold turn is not a stagnant one.
    if not problem_on_hold(case_updated):
        _perform_hypothesis_housekeeping(
            hypothesis_manager,
            case_updated,
            metadata,
            investigation_advanced=metadata["progress_made"],
        )

    # Step 5.8: Update progress tracking (before stagnation check). Frozen
    # during a hold, or the turn the hold lifts would read as a stall.
    if metadata.get("progress_made", False):
        case_updated.turns_without_progress = 0
    elif not problem_on_hold(case_updated):
        case_updated.turns_without_progress += 1
        # A stall the next turn should break rather than restate (#1911).
        stall_notice = restatement_stall_notice(case_updated)
        if stall_notice:
            _add_system_feedback(metadata, stall_notice)

    # Step 5.9: Progress monitoring. No repair patterns during a hold: they
    # rewrite hypotheses (the deadlock repair retires INCONCLUSIVE ones) the
    # hold has frozen.
    progress_result = (
        None
        if problem_on_hold(case_updated)
        else progress_monitor.check_progress(case_updated)
    )
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
            _add_system_feedback(metadata, progress_result.prompt_injection)

        log_msg = (
            f"Progress transparency activated: pending milestone "
            f"'{progress_result.pending_milestone}'"
        )
        if progress_result.repair_type:
            log_msg += f", repair: {progress_result.repair_type.value}"
        logger.info(log_msg)
    for hypothesis_id in sorted(standing - _standing_hypothesis_ids(case_updated)):
        record_decision(
            metadata, NoticeCode.HYPOTHESIS_RETIRED, hypothesis_id=hypothesis_id
        )

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
    # persisted state, and before the final recompute and the backstop, which
    # read the need a pending check names.
    _sweep_needs_for_terminal_hypotheses(case_updated)

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

    # The final recompute (#2636, Q8 (a)): the pool after housekeeping and the
    # monitor's repair, read once more, so the backstop and the record read
    # one pool. Only where the apply step's dispatch ran the first.
    if metadata.get(ASSESSMENT_RECOMPUTED_KEY):
        _final_recompute(case_updated, metadata, llm_outcome)

    # 4c. Resolution backstop (INV-43). LAST of the openers, which is what
    # makes it a backstop rather than a fourth competing proposer: step 4 has
    # run the LLM's own ``proposed_transition`` and the deferred proposer ran
    # inside the apply step before it, so anything they opened is open on the
    # offer record and this bails on it. Only a resolution-READY case that
    # NOBODY offered reaches the proposal. After the final recompute, so it
    # reads the pool the record reads; it writes none of the arms
    # ``score_progress`` reads.
    _maybe_propose_confirmed_resolution(case_updated, metadata)

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

    if getattr(response_obj, "suggested_follow_ups", None):
        try:
            gate_pending = engine_owned_affordances(case_updated) is not None
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

    # The case outcome record (#2615 stage S1), folded over the prior one
    # after every decider of the cycle, the final recompute, the backstop and
    # the need linking included, and before the save. The narration step reads
    # it (``_finalize_turn``), and only then is the turn recorded (#2636).
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case_updated, at="cycle")
    cause_at_start = metadata.get(FIX_CAUSE_AT_START_KEY)
    return case_updated, TurnRecordInputs(
        progress_metrics=progress_metrics,
        stagnation_str=stagnation_str,
        validation_repairs=validation_repairs,
        # The cause a fix run in this turn acts on is the one the live
        # investigation apply took when it started (#2036 D2.3); an inquiry or
        # terminal turn runs none, and records it now.
        cause_at_start=(
            cause_at_start if cause_at_start is not None else fix_cause(case_updated)
        ),
    )
