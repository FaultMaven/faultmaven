from datetime import (
    UTC,
    datetime,
)
from typing import (
    Any,
    Optional,
)

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)


def check_if_progress_made(metadata: dict[str, Any]) -> bool:
    """Whether the investigation ADVANCED this turn — the sole writer of
    ``turns_without_progress``, and therefore of every stall net downstream.

    The distinction this draws is *advancement*, not *activity* (#1136). The
    predicate used to accept any touched artifact, on the reasoning that "a
    skilled troubleshooter gathering information IS making progress". That is
    true of gathering something NEW and false of restating what the case
    already holds — and because the LLM restates constantly while it waits for
    the user (re-proposing the standing fix, re-quoting the same log lines),
    the counter reset almost every turn. It reached the ``EXHAUSTION_*``
    thresholds on 8 of 103 real cases past the turn floor, so ``is_stalled``,
    ``is_progress_stalled``, ``INSUFFICIENT_EVIDENCE``, ``TREATMENT_BLOCKED``,
    the exhaustion detector and the LOW/BLOCKED momentum bands were all
    effectively unreachable together.

    Each arm is therefore keyed to something the case did not already have:

    - ``novel_*`` rather than the raw ``evidence_added`` / ``solutions_proposed``
      / ``files_uploaded`` lists. Those keep every minted id — positional
      ``new_index_N`` resolution, milestone attribution and the turn record all
      depend on them — so the narrowing lives here, in the progress reading,
      not in what gets written. See ``_restates_standing_solution`` /
      ``_restates_standing_evidence`` for the per-arm bars.
    - ``DATA_PROVIDED`` is **dropped** as a separate arm. It is set from
      ``evidence_added`` (``turn_outcome.determine_turn_outcome``), so keeping
      it would readmit through the outcome label exactly the duplicate rows the
      ``novel_evidence_added`` key exists to exclude. Genuinely new evidence
      still lands via that key; nothing else was ever reaching this arm.
    - ``DATA_REQUESTED`` stays, and is now structural — a NEW outstanding
      ``EvidenceNeed`` raised this turn, not a keyword scan of the previous
      turn's prose (``turn_outcome._new_data_request_raised``). Re-asking for
      data the case is already waiting on no longer counts, which is the
      behaviour a parked investigation actually exhibits.
    - ``HYPOTHESIS_TESTED`` stays as-is: it reads ``tested_at ==
      current_turn``, already state-backed and already per-turn.

    Note this makes the counter honest for its OTHER readers too, all of which
    were reading the same inflated signal: ``progress_monitor``'s exhaustion
    detector, the LOW/BLOCKED momentum bands in
    ``working_conclusion_generator``, the "M turns since last progress" line in
    ``prompts/context_builder``, and the ``evidence_need_surfacing`` page
    cursor (which now rotates on genuinely barren turns, as it was meant to).
    """
    # Structural progress: an artifact the case did not already hold.
    structural_keys = [
        "milestones_completed",
        "novel_evidence_added",
        "hypotheses_generated",
        "hypotheses_validated",
        "novel_solutions_proposed",
        "novel_files_uploaded",
    ]
    for key in structural_keys:
        if metadata.get(key):
            return True

    if metadata.get("status_transitioned"):
        return True

    # Investigative progress: active diagnostic behaviors
    outcome = metadata.get("outcome")
    if outcome in (
        TurnOutcome.DATA_REQUESTED,
        TurnOutcome.HYPOTHESIS_TESTED,
    ):
        return True

    # A NEW or materially revised evidence link counts as progress. The
    # caller gates this counter on what ``link_evidence`` reports, so a
    # re-emitted standing link never reaches here (#1136) — linking storage
    # is an upsert, so counting per call was the same restatement leak the
    # ``novel_*`` keys close on the other arms.
    if metadata.get("hypothesis_evidence_links_applied"):
        return True

    return False


def confirmed_transition_arms(
    case: "Case", executed: bool, confirmed_via: TerminalConfirmedVia
) -> dict[str, Any]:
    """Arms for a deterministic branch that just confirmed a terminal proposal.

    TWO branches used to confirm a standing terminal proposal without an LLM
        call — the step-0b pending-transition short-circuit and the 0c
        status-transition dropdown — and they hand-wrote this answer differently
        for the SAME state change: 0c passed
        ``milestones_completed=["solution_verified"]`` and 0b passed none, so a
        consumer counting gate completions off the #1142 stream mis-counted by
        which UI affordance the user happened to use.

        The resolve arm of 0c went when RESOLVED left the menu, so there is ONE
        caller now and the disagreement is structurally impossible rather than
        merely reconciled. This stays as the single definition of the arms — the
        close path still reaches it, and a second confirm branch would otherwise
        start the divergence over.

        Derived from what actually happened rather than from which branch is asking:

        * ``status_transitioned`` is ``executed`` — the value
          ``confirm_pending_transition`` RETURNED, not an assumption. It returns
          ``False`` when a pending CLOSE pivots to a RESOLVED proposal, in which
          case nothing terminal committed and the arm would be a lie.
        * ``solution_verified`` is claimed only for a RESOLVED landing. It is a
          resolution milestone, so asserting it on a CLOSED confirmation — which
          0b also serves — would manufacture a gate completion the case never had.
        * ``terminal_confirmed_via`` is ``confirmed_via`` only when ``executed``: the
          turn record names how the user confirmed only for a transition that
          committed, so the first later message can be counted against it (#1748).
    """
    transitioned = bool(executed)
    resolved = transitioned and case.state == CaseState.RESOLVED
    return {
        "status_transitioned": transitioned,
        "milestones_completed": ["solution_verified"] if resolved else [],
        "terminal_confirmed_via": confirmed_via if transitioned else None,
    }


def score_progress(metadata: dict[str, Any]) -> bool:
    """Score ``progress_made`` onto *metadata*, **monotonically** (#1270).

    The one place the write is DEFINED, and every caller routes through it: the
    generation path scores once at step 4b (after ``_check_automatic_transitions``
    has written the ``status_transitioned`` arm), the ten deterministic branches
    score through ``_finish_deterministic_turn``, and the service's consumed-turn
    backstop scores for the three routes that never reach Step 6. A copy of this
    expression spelled out at any of them means a refinement here silently skips
    that path -- which is the divergence-between-copies failure this function
    exists to prevent, so the count is deliberately not restated as a number
    that can rot.

    Monotone because :func:`check_if_progress_made` reads the nine ARMS and
    never the ``progress_made`` key. A plain
    ``metadata["progress_made"] = check_if_progress_made(metadata)`` therefore
    DESTROYS a ``True`` an earlier writer put on the dict, and the generation
    path has such a writer: ``_apply_stage_gate_side_effects`` sets
    ``progress_made=True`` beside ``compliance_detected`` on a stage-gate
    compliance turn, before the first score. That write is redundant TODAY (the
    side effects run only when a gate landed in ``milestones_completed``, so
    that arm co-fires and the predicate returns ``True`` anyway) — which is
    exactly why a silent clobber there would go unnoticed until a compliance
    path stopped co-firing an arm. This makes the clobber unreachable rather
    than merely improbable.

    Safe to call more than once per turn: that is the point. A later call is a
    strict refinement of an earlier one rather than a re-decision, which is what
    lets the generation path take a provisional reading and then correct it once
    the last arm exists.
    """
    scored = bool(metadata.get("progress_made")) or check_if_progress_made(metadata)
    metadata["progress_made"] = scored
    return scored


def summarize_for_turn_record(text: Optional[str], max_length: int) -> str:
    """Bound a message for a ``TurnProgress`` summary field."""
    text = text or ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def record_promptless_turn(
    case: Case,
    *,
    user_message: Optional[str],
    agent_response: Optional[str],
    progress_made: bool,
    milestones_completed: Optional[list[str]] = None,
    outcome: TurnOutcome = TurnOutcome.CONVERSATION,
    agent_response_synthesized: bool = False,
    terminal_confirmed_via: Optional[TerminalConfirmedVia] = None,
) -> None:
    """Record the ``TurnProgress`` of a turn that built no prompt (#1688).

    The one builder for both writers of such a record: the engine's
    deterministic branches (``_finish_deterministic_turn``) and the service's
    consumed-turn backstop (``_backfill_consumed_turn``, #1264). They were two
    near-identical constructions, and #1267 gave only the service's copy the
    rule below, so a pending-gate click buried the notice a greeting would have
    carried.

    **``system_feedback`` is FORWARDED from the previous turn.** It is addressed
    to the next prompt, and the prompt reads it positionally, from
    ``turn_history[-1]`` (``context_builder.system_feedback_block``). A turn
    that built no prompt has not consumed it, so a record written here with no
    feedback would hide the notice from the next prompt that is built.
    Forwarding cannot deliver twice: the generation path records only the
    feedback its own turn produced. A forwarded copy is marked
    ``system_feedback_forwarded``, so the prompt can say which turn the notice
    came from instead of calling it the previous turn's.

    **Except on a terminal case.** No prompt renders feedback once a case is
    closed or resolved (``TERMINAL_TEMPLATE`` has no slot, and terminal states
    have no outgoing transitions), so forwarding there would only copy a dead
    notice onto every later record. Read from the case state rather than taken
    from the caller, so a new call site cannot get it wrong.

    The stall accounting is one-directional: progress RESETS
    ``turns_without_progress`` and nothing here ever increments it (see
    ``_finish_deterministic_turn``).
    """
    previous = case.turn_history[-1] if case.turn_history else None
    forwarded = (
        previous.system_feedback
        if previous is not None and not case.is_terminal
        else None
    )
    case.turn_history.append(
        TurnProgress(
            turn_number=case.current_turn,
            timestamp=datetime.now(UTC),
            milestones_completed=list(milestones_completed or []),
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=progress_made,
            outcome=outcome,
            user_message_summary=summarize_for_turn_record(user_message, 200),
            agent_response_summary=summarize_for_turn_record(agent_response, 500),
            agent_response_synthesized=agent_response_synthesized,
            terminal_confirmed_via=terminal_confirmed_via,
            system_feedback=forwarded,
            system_feedback_forwarded=bool(forwarded),
        )
    )
    if progress_made:
        case.turns_without_progress = 0
