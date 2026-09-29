"""Automatic and user-confirmed case-stage transitions: detecting when a turn's updates cross a milestone gate and moving the case into INVESTIGATING."""

import logging
from typing import Any

from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _user_confirms_transition,
    _user_declines_transition,
    confirmation_token_class,
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
    _close_confirmation_suggestions,
)
from .terminal_replies import (
    _build_resolution_confirmation,
    _resolution_confirmation_suggestions,
)

logger = logging.getLogger(__name__)


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

        # Gap #6: Checkpoint before status change
        if self.deps.checkpoint_service:
            await self.deps.checkpoint_service.create_checkpoint(
                case,
                trigger="pre_case_action",
                metadata={
                    "from_state": case.state.value,
                    "to_state": "investigating",
                },
            )

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

        # Initialize problem verification with confirmed statement
        verification_kwargs = {
            "symptom_statement": case.description or "Unspecified issue",
            "severity": "MEDIUM",  # Default when unknown (valid value: CRITICAL|HIGH|MEDIUM|LOW)
        }

        # Hydrate from problem confirmation if available
        if case.inquiry.problem_confirmation:
            pc = case.inquiry.problem_confirmation
            if pc.severity_guess.upper() in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                verification_kwargs["severity"] = pc.severity_guess.upper()
            # else: keep default "MEDIUM" — severity_guess="unknown" is valid
            # for ProblemConfirmation but not for ProblemVerification

        # Hydrate from preliminary urgency if available
        if case.inquiry.preliminary_urgency:
            pu = case.inquiry.preliminary_urgency
            if pu.level:
                verification_kwargs["urgency_level"] = (
                    pu.level.lower()
                )  # Convert to lowercase for enum
                # If severity still at default (MEDIUM), use urgency level as severity (keep uppercase for severity)
                if (
                    verification_kwargs["severity"] == "MEDIUM"
                    and pu.level != UrgencyLevel.UNKNOWN
                ):
                    verification_kwargs["severity"] = (
                        pu.level.value.upper()
                    )  # Convert urgency level to uppercase for severity field
            # Bug fix: Transfer temporal_state from preliminary urgency
            # Without this, path selection receives Temporal:None and the
            # router falls back to the ROOT_CAUSE default (auto_selected=False)
            # rather than matching a definitive matrix row.
            if pu.is_ongoing:
                verification_kwargs["temporal_state"] = TemporalState.ONGOING
            else:
                verification_kwargs["temporal_state"] = TemporalState.HISTORICAL

        case.problem_verification = ProblemVerification(**verification_kwargs)

        # The INQUIRY → INVESTIGATING transition carries Gate 1
        # (problem-statement confirmation) only. There is no path fork
        # (redesign R5) — the investigation proceeds opportunistically.
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
        self, case: Case, metadata: dict[str, Any], user_message: str = ""
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

                if readiness.verdict == readiness.READY:
                    # Requirements met — clear needs_info, show confirmation
                    case.pending_transition["needs_info"] = False
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

                # Use the user_message parameter directly, not from metadata
                if _user_confirms_transition(user_message):
                    # Gap #6: Checkpoint before terminal transition
                    if self.deps.checkpoint_service:
                        to_state = case.pending_transition.get("to_state", "unknown")
                        await self.deps.checkpoint_service.create_checkpoint(
                            case,
                            trigger="pre_case_action",
                            metadata={
                                "from_state": case.state.value,
                                "to_state": to_state,
                            },
                        )
                    executed = confirm_pending_transition(case, case.user_id)
                    if executed:
                        metadata["status_transitioned"] = True
                        # Read onto this turn's record by ``_apply_turn_response``
                        # and counted after the save (#1748). This branch
                        # confirms on the typed text alone, so the text's token
                        # class is the channel.
                        metadata["terminal_confirmed_via"] = confirmation_token_class(
                            user_message
                        )
                    else:
                        # INV-37 resolve-preservation: the pending CLOSE pivoted
                        # to a RESOLVED proposal because the case became
                        # resolvable. Nothing terminal committed — surface the
                        # resolve confirmation (prose appended below the LLM's
                        # reply + the canonical resolve DECIDE pair) instead of
                        # closing. The pending_transition now targets "resolved".
                        metadata["close_pivoted_to_resolve"] = True
                        metadata["override_suggestions"] = (
                            _resolution_confirmation_suggestions()
                        )
                        metadata["closure_readiness_verdict"] = (
                            ClosureReadiness.SUGGEST_RESOLVE
                        )
                    return case
                elif _user_declines_transition(user_message):
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

            # Loop-bound (project-resolution-gate-stuck-loop): if the
            # handshake block above already pivoted this case to CLOSE this
            # turn — a repeated resolution NEEDS_INFO that re-asking cannot
            # satisfy (the user keeps confirming but no Solution is/can be
            # recorded) — do NOT let the LLM's same-turn ``proposed_transition``
            # re-arm RESOLVED and clobber that CLOSE via ``propose_transition``.
            # The LLM re-proposes RESOLVED every turn while the user confirms;
            # without this guard the CLOSE pivot is overwritten every turn and
            # the gate loops forever (Run 36, case_95d86b7daf8c). Honoring the
            # CLOSE pivot terminates the case cleanly (root cause preserved).
            if proposed and metadata.get("resolution_suggest_close"):
                logger.info(
                    f"Case {case.case_id}: honoring handshake CLOSE pivot — "
                    f"ignoring same-turn LLM proposed_transition="
                    f"{getattr(proposed, 'to_state', None)!r} so it does not "
                    f"clobber the escape from a repeated resolution NEEDS_INFO."
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
                        _resolution_confirmation_suggestions()
                    )
                else:  # closed
                    metadata["override_suggestions"] = _close_confirmation_suggestions()
                logger.info(
                    f"Agent proposed transition → {effective_to_status} "
                    f"(pending user confirmation)"
                )

        return case
