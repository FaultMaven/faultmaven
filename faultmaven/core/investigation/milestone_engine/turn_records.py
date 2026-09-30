"""Building and finishing a turn's TurnProgress record: outcome, progress scoring, upload reporting, summarization and the recency checks a turn record depends on."""

import logging
from datetime import UTC, datetime
from typing import Any

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
)
from faultmaven.core.investigation.causal_graph.queries import is_chain_root_validated
from faultmaven.core.investigation.causal_graph.support import (
    support_count_held_root_ids,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    evidence_need_id_dropped_total,
)
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.core.investigation.verification_status import is_stalled
from faultmaven.modules.case.contracts import (
    Case,
    HypothesisState,
    InvestigationMomentum,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)

from .progress import (
    record_promptless_turn,
    score_progress,
    summarize_for_turn_record,
)
from .stage_gates import (
    _normalise_id_ref,
)

logger = logging.getLogger(__name__)


# Anti-anchoring acts at most once per this many turns (a marker on
# progress.last_anti_anchoring_turn records when it last fired). With a value of
# 2 the intervention skips the single turn immediately after it fires, then may
# act again — enough to avoid per-turn churn without going dormant.
_ANTI_ANCHORING_COOLDOWN_TURNS = 2


def _determine_turn_outcome(
    case: Case, metadata: dict[str, Any], reported_outcome: TurnOutcome
) -> TurnOutcome:
    """
    Determine turn outcome classification (Bug #8).
    Checked AFTER milestone detection and evidence processing.
    """
    from faultmaven.core.investigation.turn_outcome import determine_turn_outcome

    return determine_turn_outcome(
        case=case,
        progress_made=metadata.get("progress_made", False),
        milestones_completed=metadata.get("milestones_completed", []),
        evidence_added=metadata.get("evidence_added", []),
        hypotheses_generated=len(metadata.get("hypotheses_generated", [])),
        solutions_proposed=len(metadata.get("solutions_proposed", [])),
    )


def _create_turn_record(
    turn_number: int,
    milestones_completed: list[str],
    evidence_added: list[str],
    hypotheses_generated: list[str],
    hypotheses_validated: list[str],
    solutions_proposed: list[str],
    progress_made: bool,
    outcome: TurnOutcome,
    user_message: str,
    agent_response: str,
    system_feedback: str | None = None,
    momentum: InvestigationMomentum | None = None,
    blocked_reasons: list[str] | None = None,
    next_steps: list[str] | None = None,
    repair_pattern: str | None = None,
    validation_repairs: list[str] | None = None,
    agent_response_synthesized: bool = False,
) -> TurnProgress:
    """Create turn progress record."""
    # Multiple backstops (path-conditional emission rejection, milestone
    # ordering, data-quality blockers, prompt-injection alerts, etc.)
    # all append to ``metadata["system_feedback"]`` independently. A
    # single turn can fire 4+ backstops (e.g., LLM emits root_cause
    # milestone + causal_evidence + hypotheses_to_add + solutions_to_add
    # in a pre_path_investigating state), pushing the accumulated text
    # past ``TurnProgress.system_feedback``'s 1000-char Pydantic cap and
    # crashing the turn save. Truncate at the chokepoint so every
    # accumulation path is covered without per-call edits.
    if system_feedback and len(system_feedback) > 1000:
        system_feedback = system_feedback[:980] + "\n... [truncated]"
    return TurnProgress(
        turn_number=turn_number,
        timestamp=datetime.now(UTC),
        milestones_completed=milestones_completed,
        evidence_added=evidence_added,
        hypotheses_generated=hypotheses_generated,
        hypotheses_validated=hypotheses_validated,
        solutions_proposed=solutions_proposed,
        progress_made=progress_made,
        outcome=outcome,
        user_message_summary=summarize_for_turn_record(user_message, 200),
        agent_response_summary=summarize_for_turn_record(agent_response, 500),
        agent_response_synthesized=agent_response_synthesized,
        system_feedback=system_feedback,
        momentum=momentum,
        blocked_reasons=blocked_reasons or [],
        next_steps=next_steps or [],
        repair_pattern=repair_pattern,
        validation_repairs=validation_repairs or [],
    )


def _report_turn_uploads(
    case: Case,
    attachments: list[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """The turn's uploads, as the two metadata keys that report them.

    Thin bind of ``turn_uploads.report_turn_uploads`` to this ``case``. The
    derivation is a free function because ``InvestigationService`` needs the
    same reading for the two SERVICE-routed handlers that never reach the
    engine (#1229) — one derivation, not a copy per caller.

    Called once per turn from ``_process_turn_impl``, ABOVE the path fork,
    so the reading and its two degradation warnings reach the deterministic
    early-return branches and the terminal short-circuit as well as the
    generation path.
    """
    return report_turn_uploads(case.case_id, case.current_turn, attachments)


def _finish_deterministic_turn(
    case: Case,
    user_message: str,
    agent_response: str,
    upload_report: dict[str, list[str]],
    *,
    milestones_completed: list[str] | None = None,
    progress_made: bool = False,
    status_transitioned: bool = False,
    terminal_confirmed_via: TerminalConfirmedVia | None = None,
) -> dict[str, Any]:
    """Close out a deterministic early-return turn: ONE progress decision,
    applied to all three surfaces that report it (#1229).

    The deterministic branches (the pending resolve/close gate, and the
    CLOSE status-transition handler — the resolve one went with the menu
    entry) answer without an LLM call. They
    used to record a hardcoded ``progress_made=False`` ``TurnProgress`` in
    one place and build a hand-written metadata dict in another, and
    neither consulted the turn's uploads. This is both, from one reading,
    so the stored turn-history entry, the returned metadata and the case's
    stall counter cannot disagree about the same turn.

    Must be called BEFORE the branch's ``repository.save(case)`` — the
    counter it writes is part of what that save persists. Every call site
    follows the ``metadata = self._finish_deterministic_turn(...)`` →
    ``save`` → ``return`` shape for that reason. (Recording a
    ``TurnProgress`` at all is load-bearing on its own: without one the
    turn_history validator rejects the case on its next load, because a
    deterministic branch still consumes a turn number.)

    **A genuinely novel upload counts as progress here.** The reading is
    ``check_if_progress_made`` itself, not a copy of one arm of it, so a
    progress arm added there in future lands on these paths too rather than
    on the generation path alone. Its ``novel_files_uploaded`` arm is what
    fires for an upload: ``check_if_progress_made`` defines progress as
    *advancement, not activity* — "an artifact the case did not already
    have" — and a file that survived content-hash dedup is exactly that.
    Nothing about a gate turn makes that untrue: whether the user accepted
    a mitigation is orthogonal to whether new data arrived.

    The accounting stays **one-directional**: progress RESETS
    ``turns_without_progress``, and nothing here ever increments it. That
    asymmetry is deliberate, and it is also what these paths already did —
    measured, not assumed: the increment at Step 5.8 sits inside the
    generation block, so a deterministic branch never reached it and the
    counter was FROZEN, not advanced. (#1229 reported it as incrementing;
    it does not.) Both arms therefore err the same way — against a stall
    net firing on a turn the engine did no investigative work on.

    Nothing releases a pending gate on ``turns_without_progress``, so
    resetting it cannot change how long one stands: a pending terminal
    proposal stands until the user answers it (a click, a bare consent token
    or a decline), or sends a turn the gate never consumes — one carrying an
    upload, or a non-answer over 40 characters or containing "?" — which the
    gate's own escape lane withdraws it for. Everything else the gate answers
    with the proposal's buttons, every time and never recording a refusal: a
    consent-shaped reply that is not bare and that ``is_substantive_reply``
    does not call substantive, a reply whose text and minted intent disagree,
    a minted confirmation on text that is not bare, and a short (at most 40
    characters) question-free non-answer (#1783).
    """
    metadata: dict[str, Any] = {
        "turn_number": case.current_turn,
        "milestones_completed": milestones_completed or [],
        "progress_made": progress_made,
    }
    if status_transitioned:
        metadata["status_transitioned"] = status_transitioned
    # Upload keys before the progress read: ``check_if_progress_made``
    # scores ``novel_files_uploaded`` off this same dict.
    metadata.update(upload_report)
    # The SHARED monotone write, not a fourth copy of it (#1270). ``metadata
    # ["progress_made"]`` is already seeded with the caller's ``progress_made``
    # above, and ``score_progress`` is ``seeded or predicate(...)`` -- the same
    # expression this line used to spell out. Spelling it out again meant a
    # refinement to ``score_progress`` silently skipped all ten deterministic
    # branches, which is the divergence-between-copies failure the rest of
    # this work exists to close.
    score_progress(metadata)

    # The shared prompt-less record, which also forwards the previous
    # turn's ``system_feedback``: none of these branches builds a prompt
    # (#1688).
    record_promptless_turn(
        case,
        user_message=user_message,
        agent_response=agent_response,
        progress_made=metadata["progress_made"],
        milestones_completed=metadata["milestones_completed"],
        terminal_confirmed_via=terminal_confirmed_via,
    )
    # #1142: the same handoff the generation path builds, so a deterministic
    # turn is a ROW in the stream rather than a gap. A gap is worse than an
    # uninteresting row: streaks computed over the stream silently shorten,
    # and a correct multi-turn confirmation handshake — which is exactly
    # what these branches serve — would read as an engine-dry run.
    metadata[TELEMETRY_HANDOFF_KEY] = {
        "path": TurnPath.DETERMINISTIC,
        "arms": collect_progress_arms(metadata),
        "gate_name": None,
        # Carried in the handoff rather than written onto ``metadata``: the
        # TurnProgress these branches record is CONVERSATION, but the
        # returned dict is persisted onto the assistant message row and
        # adding a key there is a wire-visible change this does not need.
        "outcome": TurnOutcome.CONVERSATION,
    }
    return metadata


def _perform_hypothesis_housekeeping(
    hypothesis_manager,
    case: Case,
    metadata: dict[str, Any],
    *,
    investigation_advanced: bool,
) -> None:
    """Apply confidence decay and anchoring detection.

    ``investigation_advanced`` is the turn's final ``progress_made``, so the
    turn path calls this after ``score_progress``; it is required, so no
    caller can run the age sweep on a turn it has not judged.
    ``case.turns_without_progress`` is read before Step 5.8 updates it — as
    of the previous turn — so the stall arm below engages one turn after the
    exhaustion detector sees the stall, the direction that errs toward not
    counting.
    """
    active_hypotheses = [
        h for h in case.hypotheses.values() if h.state == HypothesisState.ACTIVE
    ]

    if not active_hypotheses:
        return

    # Whether this turn counts toward an ignored prior's stagnation. It is
    # judged forwards — does the turn make that stagnation more evident?
    # When the investigation advanced on something else and passed the
    # prior over, yes. When nothing advanced — the turn only waited on the
    # user, or restated what the case holds — one such turn says nothing
    # new. A run of them does: once the case has stalled (``is_stalled``,
    # the EXHAUSTED time thresholds) the wait is itself the evidence, and
    # the priors must go on aging so the exhaustion handoff, which needs
    # spent hypotheses, can still be reached.
    turn_counts = investigation_advanced or is_stalled(case)
    # A prior the investigation has just asked to test is not being passed
    # over: while a recent, model-authored request it motivated is still
    # outstanding, the turn does not count against it.
    awaited = _hypotheses_awaiting_recent_evidence(case, _ANTI_ANCHORING_COOLDOWN_TURNS)

    # 1. Apply confidence decay to stagnant hypotheses
    for h in active_hypotheses:
        # Age-based stagnation sweep (#713): a prior no turn ever touches
        # keeps iterations_without_progress=0, so decay/anchoring would never
        # act on it. Advance the stagnation counter for one that has gone
        # stagnant-by-age (provenance-blind) so an IGNORED prior decays and
        # can trip anchoring the same as a repeatedly-tested one — never
        # validating or concluding, only lowering belief over time. A
        # hypothesis that causal evidence supports is not aged (#1678).
        hypothesis_manager.advance_stagnation_if_ignored(
            h,
            case.current_turn,
            case,
            turn_counts=turn_counts and h.hypothesis_id not in awaited,
        )
        # One decay step if THIS turn left the hypothesis stagnant (touched
        # without progress); an untouched turn does not decay it.
        hypothesis_manager.apply_likelihood_decay(h, case.current_turn)

    # 2. Detect anchoring and add system feedback if necessary
    is_anchored, reason, hypothesis_ids = hypothesis_manager.detect_anchoring(
        active_hypotheses, case.current_turn
    )

    # 3. Age-out: an ignored prior past the stagnation horizon and below the
    # retirement threshold soft-retires. Anti-anchoring retires only on
    # fixation, which a lone stalled prior beside a healthy leader is not, so
    # without this it sat ACTIVE at the decay floor. Whatever anchoring
    # flagged is left to the intervention below, which also tells the LLM to
    # broaden the differential. Same stand-down and root protections as the
    # intervention; runs here, ahead of the intervention's early returns.
    # Only on a turn that counts: a turn that says nothing new about a
    # prior does not end it either.
    if turn_counts and not _awaiting_recent_evidence(
        case, _ANTI_ANCHORING_COOLDOWN_TURNS
    ):
        flagged = set(hypothesis_ids) if is_anchored else set()
        count_held = support_count_held_root_ids(case)
        for h in active_hypotheses:
            if (
                h.hypothesis_id not in flagged
                and not is_chain_root_validated(h, case.causal_nodes)
                and h.root_node_id not in count_held
            ):
                hypothesis_manager.retire_if_aged_out(h, case, case.current_turn)

    if is_anchored:
        logger.warning(f"Anchoring detected for case {case.case_id}: {reason}")
        # Anti-anchoring intervenes only on a GENUINE stall:
        #  - Stand down while the investigation RECENTLY asked for data that is
        #    still outstanding — it is waiting on the user, not fixated. Bounded
        #    to recent asks so a single stale, never-answered need cannot
        #    permanently disable the mechanism.
        #  - Cooldown: act at most once per `_ANTI_ANCHORING_COOLDOWN_TURNS`,
        #    read from the explicit `last_anti_anchoring_turn` marker so the
        #    cooldown holds even on a turn that happens to retire nothing.
        if _awaiting_recent_evidence(case, _ANTI_ANCHORING_COOLDOWN_TURNS):
            return
        if (
            case.current_turn - case.progress.last_anti_anchoring_turn
            < _ANTI_ANCHORING_COOLDOWN_TURNS
        ):
            return

        # Engine action (not merely a prompt nudge): retire the STALLED
        # hypotheses the detector flagged so the differential actually
        # diversifies. Exclude any flagged hypothesis whose chain root is
        # validated — it is grounding the cause, and retiring it for "anchoring"
        # would discard the answer. Same protection for a COUNT-HELD root
        # (§7.1/INV-29: really causally supported, blocked only by the
        # independent-support bar) — pre-INV-29 that root would have been
        # VALIDATED and protected; the raised bar must not feed the true
        # cause to the anchoring retirer while it waits for its second
        # observation.
        count_held = support_count_held_root_ids(case)
        targets = [
            hid
            for hid in hypothesis_ids
            if hid in case.hypotheses
            and not is_chain_root_validated(case.hypotheses[hid], case.causal_nodes)
            and case.hypotheses[hid].root_node_id not in count_held
        ]
        retired = hypothesis_manager.force_alternative_generation(
            targets, active_hypotheses, case.current_turn, case
        )
        # Record that the intervention fired THIS turn — drives the cooldown
        # regardless of how many hypotheses were eligible to retire.
        case.progress.last_anti_anchoring_turn = case.current_turn

        # Tell the LLM to broaden the differential. State the retirement only
        # when one happened, so the message never claims "retired 0".
        retired_note = (
            f"Retired {len(retired)} stalled hypothesis(es). " if retired else ""
        )
        anchoring_msg = (
            f"CRITICAL: {reason}. {retired_note}Broaden the differential — "
            "propose alternative hypotheses from different root-cause categories."
        )
        current_feedback = metadata.get("system_feedback", "")
        metadata["system_feedback"] = (
            (current_feedback + "\n" + anchoring_msg)
            if current_feedback
            else anchoring_msg
        )


def _awaiting_recent_evidence(case: Case, within_turns: int) -> bool:
    """True if the investigation RECENTLY (within ``within_turns``) asked for
    data that is still outstanding.

    A fresh, still-outstanding ask means the agent is waiting on the user —
    progress, not fixation — so anti-anchoring stands down. Bounding it to
    recent asks ensures a single stale need the user never answers cannot
    permanently disable anti-anchoring for the rest of the case.

    ENGINE-INFERRED needs are excluded (#1079). Those are minted by
    ``evidence_need_linking`` from any EVIDENCE suggestion the model did not
    declare a need for — which, on a fixated case, is most turns. Counting
    them would stamp a fresh ``created_at_turn`` every turn and hold the
    stand-down open forever, destroying the bound the paragraph above
    promises and disabling anti-anchoring exactly when a stuck investigation
    needs it. The signal this reads is the model's DELIBERATE demand, so it
    reads only the needs the model authored.
    """
    return any(
        n.is_outstanding
        and not n.engine_inferred
        and case.current_turn - n.created_at_turn < within_turns
        for n in (case.evidence_needs or [])
    )


def _hypotheses_awaiting_recent_evidence(case: Case, within_turns: int) -> set:
    """Ids of hypotheses that motivate a recent, still-outstanding request
    for data — the per-hypothesis form of ``_awaiting_recent_evidence``, with
    the same bound and the same exclusion of engine-inferred needs."""
    return {
        hypothesis_id
        for n in (case.evidence_needs or [])
        if n.is_outstanding
        and not n.engine_inferred
        and case.current_turn - n.created_at_turn < within_turns
        for hypothesis_id in n.motivating_hypothesis_ids
    }


def _resolve_id_ref(ref: str, created_ids: list[str], prefix: str) -> str:
    """Resolve ``new_index_N`` to the actual ID from ``created_ids``,
    or return ``ref`` unchanged.

    **Contract (load-bearing across all callers — Phase 3 apply-layer
    for hypothesis/evidence refs, Phase 6 for need refs):** callers
    detect unresolved placeholders by checking
    ``ref.startswith("new_index_")`` on the return value. The
    function returns the input unchanged when ``N`` is out of range
    or malformed, never raises — graceful degradation. A "did this
    resolve?" probe at the caller is the canonical pattern; do not
    switch this to ``Optional[str]`` without auditing every caller.

    The ref is normalised first: surrounding whitespace and one pair of
    square brackets are stripped. The prompt renders ids as ``[hyp_...]`` /
    ``[ev_...]`` and a model that echoes the brackets otherwise misses the
    lookup and has its link or update dropped with only a log line
    (#1116 review). Applies to every prefix, real ids and placeholders
    alike.
    """
    ref = _normalise_id_ref(ref)
    if ref and ref.startswith("new_index_"):
        try:
            idx_str = ref.replace("new_index_", "")
            idx = int(idx_str)
            if 0 <= idx < len(created_ids):
                return created_ids[idx]
        except (ValueError, IndexError):
            pass
    return ref


def _flatten_follow_ups(
    follow_ups: list,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten LLM-emitted ``SuggestedFollowUp`` objects into the
    dict shape the API response carries.

    Phase 6 of the evidence-needs rollout: resolves
    ``evidence_need_id`` ``new_index_N`` placeholders against
    ``metadata["evidence_needs_updated"]`` so the wire-level field
    always carries a real ``eneed_xxxxxxxxxxxx`` ID. Unresolvable
    refs are dropped silently (graceful degradation — matches the
    apply-layer pattern for dangling motivator/evidence IDs).
    """
    out: list[dict[str, Any]] = []
    for f in follow_ups:
        suggestion: dict[str, Any] = {
            "label": f.label,
            "action_type": f.action_type,
        }
        if f.payload:
            suggestion["payload"] = f.payload
        if f.body:
            suggestion["body"] = f.body
        if f.hints:
            suggestion["hints"] = f.hints
        if getattr(f, "evidence_need_id", None):
            created_ids = metadata.get("evidence_needs_updated", [])
            resolved = _resolve_id_ref(
                f.evidence_need_id,
                created_ids,
                "eneed",
            )
            if resolved.startswith("new_index_"):
                drop_reason = (
                    "missing_metadata"
                    if "evidence_needs_updated" not in metadata
                    else "out_of_range"
                )
                logger.warning(
                    f"Dropped unresolvable evidence_need_id "
                    f"{f.evidence_need_id!r} on a SuggestedFollowUp "
                    f"(reason={drop_reason}; "
                    f"evidence_needs_updated len={len(created_ids)})"
                )
                try:
                    evidence_need_id_dropped_total.labels(reason=drop_reason).inc()
                except Exception:
                    pass
            else:
                suggestion["evidence_need_id"] = resolved
        out.append(suggestion)
    return out
