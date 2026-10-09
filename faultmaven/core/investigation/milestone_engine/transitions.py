"""Automatic and user-confirmed case-stage transitions: detecting when a turn's updates cross a milestone gate and moving the case into INVESTIGATING."""

import logging
from typing import Any

from faultmaven.core.investigation.milestone_engine.transition_consent import (
    pending_gate_verdict,
)
from faultmaven.core.investigation.problem_status import (
    false_alarm_close_declined_at,
    record_confirmed_statement,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseAction,
    CaseState,
    InvestigationProgress,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

from .stage_gates import (
    _add_system_feedback,
    _close_confirmation_suggestions,
)
from .statement_revision import revision_pending
from .terminal_replies import (
    _build_resolution_confirmation,
    _resolution_confirmation_suggestions,
)

logger = logging.getLogger(__name__)

#: How a refused re-proposal tells the model what binds it. Conditional, never
#: a flat ban: a model that obeys a ban never proposes, so a user who later
#: types "ok, close it" (on Slack, with no status menu) gets no refusal and no
#: card, and no way to close at all.
_DIRECTED_ONLY = (
    "Propose {what} only if the user directs it; the engine then attaches the "
    "Close action to your reply. Do not propose it unprompted."
)


def declined_close_reask(
    case: Case, *, closure_verdict: str | None = None
) -> tuple[str, str] | None:
    """Whether a transition the model proposes now re-asks a close the user
    declined, as ``(side, feedback)``, or None (#1889).

    The one read for both declined engine closes:

    * ``false_alarm`` — the user declined closing on the standing false-alarm
      finding (``ProblemInvalidation.close_declined_at_turn``, whoever opened
      that close). Gated on the CASE, not on the proposal's target: on an
      invalidated case a model ``resolved`` pivots to a ``closed_false_alarm``
      close (resolution readiness is SUGGEST_CLOSE), so a target gate lets the
      same question back in through ``resolved``.
    * ``deferred`` — the model's ``closed`` stays CLOSED under the closure
      verdict (``closure_verdict`` is passed only then) and the signature the
      engine's deferred close was declined against still holds. That close
      derives the same ``solution_deferred`` reason from the same state, so it
      is the question the user just answered.

    The RESOLVE side is ``declined_resolution_reask`` (#1895): its refusal
    carries the "Mark it resolved" chip rather than a close card.
    """
    declined_at = false_alarm_close_declined_at(case)
    if declined_at is not None:
        return (
            "false_alarm",
            "TRANSITION NOT PROPOSED: resolution is not eligible on a false "
            "alarm, and the user declined closing on this finding at turn "
            f"{declined_at}. " + _DIRECTED_ONLY.format(what="a close"),
        )
    if closure_verdict is None:
        return None
    from faultmaven.core.investigation.terminal_transitions import (
        covering_declined_entry,
    )

    if covering_declined_entry(case, closure_verdict):
        return (
            "deferred",
            "TRANSITION NOT PROPOSED: the user declined closing this case with "
            "the solution documented (the deferred-implementation close), and "
            "nothing that justified that offer has changed since. "
            + _DIRECTED_ONLY.format(what="that close"),
        )
    return None


#: The feedback when step 2 refuses a resolution the user declined (#1895).
#: The rule is ``RESOLVE_DECLINED_RULE``, the same text the prompt line states;
#: the chip is on this turn's reply, as on every turn the decline stands.
def _declined_resolve_feedback() -> str:
    from faultmaven.core.investigation.terminal_transitions import (
        RESOLVE_DECLINED_RULE,
    )

    return (
        "TRANSITION NOT PROPOSED: the user declined marking this case resolved, "
        "and no new confirmation that the fix held has been recorded since. "
        "The engine has attached its 'Mark it resolved' action to this reply. "
        + RESOLVE_DECLINED_RULE
    )


DECLINED_RESOLVE_FEEDBACK = _declined_resolve_feedback()


def declined_resolution_reask(case: Case) -> str | None:
    """The feedback refusing a resolution the model proposes while the user's
    decline of it stands, or None (#1895).

    A resolution is earned, never requested. After a decline it is due back
    when the state that earned it moves: a NEW confirmation that the fix held
    (``terminal_transitions.declined_resolve_entry``, whose covering rule
    ignores elapsed turns, repeated declines and a shrinking set of rows).
    Until then the model's re-proposal is the question the user just answered.
    The model is not left as the only way back: the refused turn carries the
    "Mark it resolved" chip (``stage_gates.declined_resolve_card``).
    """
    from faultmaven.core.investigation.terminal_transitions import (
        declined_resolve_entry,
    )

    if declined_resolve_entry(case) is None:
        return None
    return DECLINED_RESOLVE_FEEDBACK


def _refuse_declined_reask(metadata: dict[str, Any], side: str, feedback: str) -> None:
    """Refuse the model's re-proposal: the feedback for its next turn, and the
    close card appended to this turn's follow-ups (``declined_close_card``),
    so a user who did ask still has the close one step away."""
    _add_system_feedback(metadata, feedback)
    # Read at the end of the turn: the card is appended once the follow-up
    # list is settled, and ``transition_compliance`` reports the refusal.
    metadata["declined_close_card"] = side


class TransitionManager:
    """Detects and executes the case-state transitions a turn's updates make available, most centrally INQUIRY to INVESTIGATING."""

    def __init__(self, *, deps, kb_prefetcher) -> None:
        self.deps = deps
        self.kb_prefetcher = kb_prefetcher

    async def _transition_to_investigating(self, case: Case) -> None:
        """
        Transition case from INQUIRY to INVESTIGATING.

        This creates the initial investigation structures and copies the
        confirmed problem statement to the case description.

        Evidence lifecycle:
            - File uploads create only ``UploadedFile`` rows at intake; no
              Evidence is auto-created. Preprocessing artifacts (summary,
              structural_index, data_type, coverage_*) live on the file row.
            - During INQUIRY no Evidence rows exist — the
              ``InquiryStateUpdate`` schema does not carry ``evidence_to_add``
              and the engine does not synthesize Evidence on transition.
              The LLM reads files via ``<uploaded_file>`` context blocks.
            - Evidence is born during INVESTIGATING: the LLM extracts
              claim-anchored slices via ``evidence_to_add``, each carrying a
              category (the verification quartet: symptom / causal +
              symptom_absence / causal_absence) and a ``source_file_id``
              back to the originating file.
            - Milestones derive from evidence categories as those rows are
              created turn-by-turn, not retroactively at the transition.

        Reference: ``docs/architecture/investigation-engine/
        evidence-driven-investigation-framework.md`` §5.
        """
        logger.info(f"Transitioning case {case.case_id} to INVESTIGATING")

        # Copy confirmed problem statement to description BEFORE changing status
        # (Pydantic validation requires description to be set before INVESTIGATING status)
        if case.inquiry.proposed_problem_statement:
            case.description = case.inquiry.proposed_problem_statement
        elif not case.description:
            # Manual flow: user may transition before agent proposes a statement.
            # Use case title as fallback to satisfy Pydantic validation.
            case.description = case.title or "Investigation requested by user"

        # Change status (Pydantic validation happens here)
        case.state = CaseState.INVESTIGATING

        # Initialize investigation progress
        case.progress = InvestigationProgress()

        # Initialize problem verification with confirmed statement. Severity is
        # the user's own assessment from the problem confirmation or it is
        # absent: urgency is a different axis (business impact) and never
        # stands in for it.
        verification_kwargs = {
            "symptom_statement": case.description or "Unspecified issue",
        }

        # Hydrate from problem confirmation if available
        if case.inquiry.problem_confirmation:
            pc = case.inquiry.problem_confirmation
            if pc.severity_guess.upper() in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                verification_kwargs["severity"] = pc.severity_guess.upper()
            # else: severity_guess="unknown" is valid for ProblemConfirmation
            # and means not assessed; the record keeps severity None.

        # Hydrate from preliminary urgency if available
        if case.inquiry.preliminary_urgency:
            pu = case.inquiry.preliminary_urgency
            if pu.level:
                verification_kwargs["urgency_level"] = (
                    pu.level.lower()
                )  # Convert to lowercase for enum
            # Temporal state as reported with the preliminary urgency.
            if pu.is_ongoing:
                verification_kwargs["temporal_state"] = TemporalState.ONGOING
            else:
                verification_kwargs["temporal_state"] = TemporalState.HISTORICAL

        case.problem_verification = ProblemVerification(**verification_kwargs)
        # The statement's history opens with the one the user just confirmed.
        record_confirmed_statement(case)

        # The INQUIRY → INVESTIGATING transition carries Gate 1
        # (problem-statement confirmation) only; the investigation proceeds
        # opportunistically.
        logger.info(f"Case {case.case_id}: transitioning to INVESTIGATING")

        # Post-010: no retroactive milestone attribution at INQUIRY→
        # INVESTIGATING. INQUIRY no longer creates Evidence rows, so
        # there is no INQUIRY-phase evidence to back-fill milestones for.
        # KB pre-fetch: search for runbooks matching the confirmed problem.
        # Deterministic, code-level — not an LLM tool call decision.
        # Results are stored on the case and injected into context by
        # context_builder so the LLM sees relevant runbooks from turn 1.
        await self.kb_prefetcher.prefetch_kb_context(case, case.description, "symptom")

    async def check_automatic_transitions(
        self,
        case: Case,
        metadata: dict[str, Any],
        user_message: str = "",
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions (non-terminal):
        - INQUIRY -> INVESTIGATING when problem_statement_confirmed=True
          (Gate 1 — the single condition; see gate1_passed below)

        v3: INQUIRY -> RESOLVED edge removed. KB-driven cases route through
        INVESTIGATING via the KB-resolution milestone collapse — the
        structured attribution (RootCauseConclusion + Solution + gate
        milestones) is authored in one turn, but the RESOLVED disposition
        still requires the explicit confirm turn like every other terminal
        transition (#722) — see
        docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        §1.2 INVESTIGATING -> RESOLVED -> KB-Resolution Path.

        User-Agent Handshake Transitions (terminal):
        - INVESTIGATING -> RESOLVED requires ProposedTransition + user confirmation
        - Any -> CLOSED requires explicit user action

        ProposedTransition handling:
        - If the LLM response includes a proposed_transition, store it as pending
        - The transition is NOT executed until the user confirms in the next turn
        - If a pending_transition exists and user confirms, execute it
        """
        old_status = case.state

        # 0. Handle pending transition confirmation from previous turn
        # Skip confirmation check if we just proposed a transition this turn
        # (User-Agent Handshake). ``transition_proposed_this_turn`` is the ONE
        # flag every same-turn proposal site sets — the LLM-emit path (step 2
        # below), the rca_infeasible stage-gate side effect, and the deferred-
        # solution close — so a proposal can never be confirmed by the very
        # message that produced it (#722): the user must see the confirmation
        # prompt and answer on a LATER turn. The KB-resolution path is no
        # exception — its same-turn confirm collapse was removed (#722): the
        # user's "it worked" message is the solution-verification claim, not
        # consent to the irreversible RESOLVED transition.
        if hasattr(case, "pending_transition") and case.pending_transition:
            if metadata.get("transition_proposed_this_turn", False):
                logger.info(
                    "Skipping confirmation check - transition was just proposed this turn"
                )
            elif case.pending_transition.get("needs_info"):
                # User was told what's missing and has now responded.
                # Re-evaluate readiness: did the LLM actually capture root
                # cause / solution from what the user provided?
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_resolution_readiness,
                    cancel_pending_transition,
                    propose_transition,
                )

                readiness = assess_resolution_readiness(case)
                # Telemetry: transition_compliance carries the readiness
                # verdict so a pending-but-not-transitioned turn is
                # self-explaining in logs (#656 triage misread this as a
                # silent gate refusal).
                metadata["resolution_readiness_verdict"] = readiness.verdict
                metadata["resolution_readiness_missing"] = readiness.missing

                reask = (
                    declined_resolution_reask(case)
                    if readiness.verdict == readiness.READY
                    else None
                )
                if reask is not None:
                    # Nothing re-offers while a resolve decline stands (#1895).
                    # A ``needs_info`` resolve opened on a state the decline
                    # did not cover (a failed fix had disqualified its row)
                    # turns READY again on the SAME rows the user declined —
                    # a pruned refutation re-admits one — so promoting it here
                    # would re-ask the question they answered. Withdrawn
                    # instead, with the feedback and the chip, as step 2
                    # refuses the model's own re-proposal.
                    cancel_pending_transition(case)
                    _add_system_feedback(metadata, reask)
                    metadata["declined_resolve_card"] = True
                    logger.info(
                        f"Case {case.case_id}: needs_info resolve turned READY "
                        f"on a state the user's decline covers — withdrawn, "
                        f"not promoted."
                    )
                elif readiness.verdict == readiness.READY:
                    # Requirements met — the READY offer is a new offer, so it
                    # is re-proposed rather than flipped in place (#1812). A
                    # card shipped while the offer was unconfirmable names the
                    # needs_info offer's key; flipping ``needs_info`` kept that
                    # key and made the old card live consent to the ready
                    # offer. ``propose_transition`` builds a fresh dict, so the
                    # engine proposer's provenance and the cited evidence are
                    # carried across, as the INV-37 pivot carries the former.
                    prior = case.pending_transition
                    propose_transition(
                        case=case,
                        to_state="resolved",
                        summary=_build_resolution_confirmation(case),
                        evidence_ids=prior.get("evidence_ids"),
                    )
                    if "justifying_signature" in prior:
                        case.pending_transition["justifying_signature"] = prior[
                            "justifying_signature"
                        ]
                    metadata["transition_proposed_this_turn"] = True
                    metadata["resolution_ready_for_confirmation"] = True
                    logger.info(
                        f"Case {case.case_id}: needs_info resolved, "
                        f"requirements met — presenting confirmation"
                    )
                elif readiness.verdict == readiness.SUGGEST_CLOSE:
                    # Still fundamentally lacking — pivot to CLOSED. Propose
                    # the close transition (not just emit a suggestion) so
                    # the user's next positive confirmation actually fires.
                    # The earlier code only emitted the message and the
                    # close suggestions, with no pending transition for
                    # those suggestions to confirm — producing the stuck
                    # loop documented in project-resolution-gate-stuck-loop.
                    # closure_reason auto-derives via derive_closure_reason().
                    cancel_pending_transition(case)
                    propose_transition(
                        case=case,
                        to_state="closed",
                        summary=readiness.message,
                    )
                    metadata["transition_proposed_this_turn"] = True
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = readiness.message
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied, "
                        f"proposing Close (missing: {readiness.missing})"
                    )
                else:
                    # NEEDS_INFO still — user was asked once, didn't (or
                    # couldn't) provide. Don't loop asking again. Propose
                    # CLOSE so the user's next positive confirmation fires
                    # — the loop's root cause was emitting a close-
                    # suggestion with no pending transition to confirm.
                    cancel_pending_transition(case)
                    close_message = (
                        "I understand. Without confirmation that the root cause "
                        "was **eliminated** (e.g. the original error is now "
                        "absent after the fix), I can't mark this as "
                        "**resolved** — a restored-but-stabilized or "
                        "deferred-fix case isn't a resolution.\n\n"
                        "You can **close** the case instead — this preserves "
                        "the root cause analysis and the documented (or "
                        "deferred) solution."
                    )
                    propose_transition(
                        case=case,
                        to_state="closed",
                        summary=close_message,
                    )
                    metadata["transition_proposed_this_turn"] = True
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = close_message
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied after "
                        f"second ask, proposing Close "
                        f"(missing: {readiness.missing})"
                    )
            else:
                from faultmaven.core.investigation.terminal_transitions import (
                    ClosureReadiness,
                    cancel_pending_transition,
                    confirm_pending_transition,
                )

                # Use the user_message parameter directly, not from metadata.
                # The same reader as the engine's 0b gate, with no intent: only
                # a bare consent token executes (#1783), and anything else that
                # is not a decline leaves the proposal standing.
                verdict, _ = pending_gate_verdict(
                    user_message,
                    case.pending_transition.get("to_state"),
                    intent_value=None,
                    typed=True,
                )
                if verdict == "confirm":
                    executed = confirm_pending_transition(case, case.user_id)
                    if executed:
                        metadata["status_transitioned"] = True
                    else:
                        # INV-37 resolve-preservation: the pending CLOSE pivoted
                        # to a RESOLVED proposal because the case became
                        # resolvable. Nothing terminal committed — surface the
                        # resolve confirmation (prose appended below the LLM's
                        # reply + the canonical resolve DECIDE pair) instead of
                        # closing. The pending_transition now targets "resolved".
                        metadata["close_pivoted_to_resolve"] = True
                        metadata["override_suggestions"] = (
                            _resolution_confirmation_suggestions(case)
                        )
                        metadata["closure_readiness_verdict"] = (
                            ClosureReadiness.SUGGEST_RESOLVE
                        )
                    return case
                elif verdict == "decline":
                    cancel_pending_transition(case)
                    # Continue normal processing
                # else: user said something ambiguous, let LLM handle it

        # 1. INQUIRY transitions
        # v3: INQUIRY → RESOLVED edge removed. KB-driven cases route through
        # INVESTIGATING via the KB-resolution milestone collapse documented in
        # docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        # §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path. Confirming the
        # problem statement is mandatory even when a runbook applies cleanly.
        #
        # INV-19: INQUIRY → INVESTIGATING requires Gate 1 only (problem
        # statement confirmation). Gate 2 (path selection) is no longer a
        # transition gate — it fires later, inside INVESTIGATING, after
        # ``symptom_verified`` so the user sees the agent's data-inspection
        # work in the transcript before committing. (The recommendation
        # itself is still computed from user-claimed urgency; making the
        # recommendation evidence-derived is deferred follow-up.)
        if case.state == CaseState.INQUIRY:
            # Gate 1 is the problem-statement confirmation, so that is what
            # this reads. It used to be an OR across two fields that every
            # writer sets together — ``decided_to_investigate`` and
            # ``problem_statement_confirmed and problem_confirmation`` — which
            # made the second arm load-bearing only for cases carrying no
            # ``problem_confirmation`` and hid the fact that the first field
            # has no independent meaning. One condition, one gate (#1607).
            gate1_passed = case.inquiry.problem_statement_confirmed
            if gate1_passed:
                await self._transition_to_investigating(case)
                metadata["status_transitioned"] = True
                case.action_history.append(
                    CaseAction(
                        from_state=old_status,
                        to_state=CaseState.INVESTIGATING,
                        triggered_by="system",
                        reason="Problem statement confirmed",
                    )
                )
                return case

        # 2. Handle ProposedTransition from LLM response (User-Agent Handshake)
        # The LLM proposes a terminal transition; we store it pending.
        # Auto-transition on solution_verified is REMOVED — all terminal
        # transitions require explicit user confirmation.
        response_obj = metadata.get("response_obj")
        if response_obj and hasattr(response_obj, "state_updates"):
            proposed = getattr(response_obj.state_updates, "proposed_transition", None)
            if proposed and revision_pending(case):
                # The case is waiting on the user's answer about the problem
                # itself; no transition may compete with that question. The
                # model cannot tell who asked for this one, so it is refused
                # whoever did — the user's own close (status menu, REST)
                # cancels the revision first and reaches its own path.
                _add_system_feedback(
                    metadata,
                    "TRANSITION NOT PROPOSED: a revised problem statement is "
                    "awaiting the user's confirmation. Let them answer it first.",
                )
                proposed = None
            if proposed:
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_closure_readiness,
                    assess_resolution_readiness,
                    propose_transition,
                )
                from faultmaven.modules.case.domain.models.lifecycle import (
                    LEGAL_TRANSITIONS,
                )

                # Structural validation against the LEGALITY graph — which
                # edges exist, not which ones a user may pick. The LLM is not
                # a user: it proposes transitions the state machine permits,
                # so ``USER_SELECTABLE_ACTIONS`` would be the wrong bar here.
                # (In practice INQUIRY → INVESTIGATING never arrives as a
                # ``proposed_transition`` — Gate 1 performs it — so the two
                # graphs would accept the same emissions today. The right one
                # is named anyway, so a future edge cannot silently inherit
                # the wrong rule.) The prompt instructs the LLM on
                # which edges exist; this is the safety net for prompt
                # non-compliance (e.g., an LLM emitting ``to_state="resolved"``
                # from INQUIRY, which is not a valid edge — INQUIRY can
                # only transition to INVESTIGATING or CLOSED). Rejecting
                # here prevents downstream pivot logic from accepting an
                # invalid emission and quietly converting it into a
                # different transition the user never intended.
                valid_targets = {s.value for s in LEGAL_TRANSITIONS.get(case.state, [])}
                if proposed.to_state not in valid_targets:
                    logger.warning(
                        f"Rejected proposed_transition for case {case.case_id}: "
                        f"to_state={proposed.to_state!r} is not a valid edge "
                        f"from {case.state.value!r}. "
                        f"Valid targets: {sorted(valid_targets)}."
                    )
                    current_feedback = metadata.get("system_feedback") or ""
                    valid_list = (
                        ", ".join(f"{t!r}" for t in sorted(valid_targets))
                        or "(none — case is terminal)"
                    )
                    metadata["system_feedback"] = (
                        f"{current_feedback}\n"
                        "INVALID TRANSITION ERROR: You emitted "
                        f"``proposed_transition.to_state={proposed.to_state!r}`` "
                        f"from case.state={case.state.value!r}, which is "
                        f"not a valid edge in the case action graph. "
                        f"Valid targets from {case.state.value!r}: "
                        f"{valid_list}. "
                        "Per the lifecycle: from INQUIRY only CLOSED is a "
                        "valid proposed_transition (resolution requires "
                        "investigation work first — there is no "
                        "INQUIRY → RESOLVED edge). Do not re-emit this "
                        "transition; emit only valid edges."
                    ).strip()
                    metadata.setdefault("validation_repairs", []).append(
                        f"Rejected proposed_transition.to_state="
                        f"{proposed.to_state!r} from {case.state.value!r}"
                    )
                    # Skip downstream proposal processing.
                    proposed = None

            # A declined close binds the model too (#1889). The user's "no" to
            # closing on a false-alarm finding is recorded on the finding,
            # whoever asked; while it stands, any proposal the model makes on
            # the case is the same question again (a ``resolved`` pivots to
            # the false-alarm close). Checked after legality and before the
            # same-turn rule so the refusal, not a supersession, is what the
            # turn reports.
            if proposed:
                reask = declined_close_reask(case)
                if reask is not None:
                    _refuse_declined_reask(metadata, *reask)
                    logger.info(
                        f"Case {case.case_id}: model proposed "
                        f"{proposed.to_state!r} on a false-alarm finding whose "
                        f"close the user declined — refused."
                    )
                    proposed = None

            # The engine's same-turn offer stands (#1885). Every engine opener
            # that runs before this point — the apply step's false-alarm and
            # deferred-disposition proposers, the rca_infeasible stage-gate
            # close, a staged-work replay's offer, and step 0's needs_info
            # re-proposals above — leaves its offer on ``pending_transition``
            # and sets ``transition_proposed_this_turn``. The model's proposal
            # must not replace it: ``propose_transition`` builds a fresh dict,
            # so it would erase the offer's provenance (``justifying_signature``,
            # which a deferred or resolve decline is recorded against and which
            # lets a revision withdraw the engine's false-alarm close, INV-45; a
            # false-alarm decline is recorded on the finding, #1889) and, on a
            # different target, put an offer in front of the user the engine's
            # own readiness reading did not make. A DIFFERENT target loses too: the engine
            # chose its target from the same readiness the model's proposal
            # would be run through, a CLOSE still pivots to RESOLVED at confirm
            # on a resolvable case (INV-37), and the model can propose again
            # once the user has answered. One rule, so the escape from a
            # repeated resolution NEEDS_INFO (step 0's CLOSE pivot, which the
            # model used to re-arm to RESOLVED every turn: Run 36,
            # case_95d86b7daf8c) is one instance of it rather than its own
            # guard.
            #
            # The ``pending_transition`` conjunct is defensive: every writer of
            # the flag leaves its offer standing, so today the flag alone would
            # do. It keeps a flag that outlived its offer (an offer withdrawn
            # later in the same turn) from silently swallowing the model's
            # proposal with nothing in front of the user.
            if (
                proposed
                and metadata.get("transition_proposed_this_turn")
                and getattr(case, "pending_transition", None)
            ):
                engine_to = case.pending_transition.get("to_state")
                logger.info(
                    f"Case {case.case_id}: the engine opened a {engine_to!r} "
                    f"handshake this turn — ignoring same-turn LLM "
                    f"proposed_transition={getattr(proposed, 'to_state', None)!r}."
                )
                # Read by the turn's ``transition_compliance`` line: the
                # model's proposal was dropped, not pivoted.
                metadata["transition_superseded_by_engine"] = True
                _add_system_feedback(
                    metadata,
                    "TRANSITION NOT PROPOSED: the engine already offered the "
                    f"user a {engine_to!r} transition this turn, and that offer "
                    "is what they will answer. Do not re-propose a transition "
                    "until they have answered it.",
                )
                proposed = None

            if proposed:
                # The LLM emits only to_state (and optional evidence_ids).
                # Engine handles everything else: closure_reason is derived
                # inside propose_transition; summary is built programmatically
                # via the same helpers every opener uses, so they produce
                # identical confirmation prompts.
                #
                # When the LLM proposes RESOLVED, run the same readiness
                # check every other opener uses, so the user sees a
                # coherent prompt + suggestion pair:
                #   SUGGEST_CLOSE → pivot to CLOSED (close suggestion pair)
                #   NEEDS_INFO    → keep RESOLVED but flag needs_info; the
                #                   response builder overrides agent_response
                #                   with the readiness message
                #   READY         → propose RESOLVED with confirmation prompt
                # When the LLM proposes CLOSED, symmetric pivot:
                #   SUGGEST_RESOLVE → pivot to RESOLVED (case has root cause
                #                     + solution; closing would discard the
                #                     resolution attribution)
                #   HAS_SUBSTANCE / TRIVIAL → propose CLOSED with summary
                effective_to_status = proposed.to_state
                needs_info_message: str | None = None
                # The closure verdict this proposal was read under, when the
                # closed branch below read one.
                closing_verdict: str | None = None

                if proposed.to_state == "resolved":
                    readiness = assess_resolution_readiness(case)
                    metadata["resolution_readiness_verdict"] = readiness.verdict
                    metadata["resolution_readiness_missing"] = readiness.missing
                    if readiness.verdict == readiness.SUGGEST_CLOSE:
                        effective_to_status = "closed"
                        summary = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}); "
                            f"pivoting to CLOSED."
                        )
                    elif readiness.verdict == readiness.NEEDS_INFO:
                        summary = readiness.message
                        needs_info_message = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=NEEDS_INFO (missing: {readiness.missing}); "
                            f"keeping RESOLVED intent with needs_info flag."
                        )
                    else:
                        summary = _build_resolution_confirmation(case)
                else:  # closed
                    closure = assess_closure_readiness(case)
                    closing_verdict = closure.verdict
                    metadata["closure_readiness_verdict"] = closure.verdict
                    if closure.verdict == closure.SUGGEST_RESOLVE:
                        effective_to_status = "resolved"
                        summary = closure.message
                        logger.info(
                            f"Agent proposed CLOSED but case {case.case_id} "
                            f"verdict=SUGGEST_RESOLVE (case has root cause "
                            f"+ solution); pivoting to RESOLVED."
                        )
                    else:
                        summary = closure.message

                # The deferred side of #1889: a close at the state the user
                # already declined the engine's deferred close against. Read
                # where the closed branch read the closure verdict and kept
                # CLOSED. A ``resolved`` the resolution check pivots to CLOSED
                # cannot land on such a state: the deferred proposer offers
                # only on a validated cause with a fix on record, where
                # resolution readiness is NEEDS_INFO, and a cause that falls
                # moves the signature's leg.
                if effective_to_status == "closed" and closing_verdict is not None:
                    reask = declined_close_reask(case, closure_verdict=closing_verdict)
                    if reask is not None:
                        _refuse_declined_reask(metadata, *reask)
                        logger.info(
                            f"Case {case.case_id}: model proposed CLOSED at "
                            f"the state the user declined the deferred close "
                            f"against — refused."
                        )
                        return case

                # The resolve side (#1895): the model's ``resolved`` on a
                # READY case, and its ``closed`` the closure check pivots to
                # RESOLVED (INV-37), are one question — the resolution the
                # user declined. While that decline stands (no confirmation
                # recorded since), both are refused, with feedback and the
                # "Mark it resolved" chip on the turn. The user's own close
                # pick still pivots (``_close_on_explicit_intent``): this
                # binds the model, never the user.
                model_resolves_ready = (
                    proposed.to_state == "resolved"
                    and effective_to_status == "resolved"
                    and needs_info_message is None
                )
                model_close_pivots = (
                    proposed.to_state == "closed" and effective_to_status == "resolved"
                )
                if model_resolves_ready or model_close_pivots:
                    feedback = declined_resolution_reask(case)
                    if feedback is not None:
                        _add_system_feedback(metadata, feedback)
                        # Read by ``transition_compliance``, which reports the
                        # refusal. The chip itself is on every turn the
                        # decline stands (``turn_completion``), this one
                        # included.
                        metadata["declined_resolve_card"] = True
                        logger.info(
                            f"Case {case.case_id}: model proposed "
                            f"{proposed.to_state!r} (effective RESOLVED) while "
                            f"the user's decline of the resolution stands — "
                            f"refused."
                        )
                        return case

                propose_transition(
                    case=case,
                    to_state=effective_to_status,
                    summary=summary,
                    evidence_ids=getattr(proposed, "evidence_ids", None),
                )
                if needs_info_message is not None:
                    case.pending_transition["needs_info"] = True
                    # The response builder reads this to override the LLM's
                    # agent_response with the readiness message, matching the
                    # readiness gate's first-pass behavior.
                    metadata["resolution_needs_info_first_pass"] = True
                    metadata["resolution_needs_info_message"] = needs_info_message
                metadata["transition_proposed_this_turn"] = True
                # Override LLM-emitted suggestions with the canonical
                # confirm/decline pair, so every opener produces the same
                # structured DECIDE confirmation UX — this branch, the engine's
                # INV-43 backstop, and a CLOSE pick from the menu. The
                # response builder consumes metadata["override_suggestions"]
                # at the final assembly point.
                if effective_to_status == "resolved":
                    metadata["override_suggestions"] = (
                        _resolution_confirmation_suggestions(case)
                    )
                else:  # closed
                    metadata["override_suggestions"] = _close_confirmation_suggestions(
                        case
                    )
                logger.info(
                    f"Agent proposed transition → {effective_to_status} "
                    f"(pending user confirmation)"
                )

        return case
