"""Automatic and user-confirmed case-stage transitions: detecting when a turn's updates cross a milestone gate and moving the case into INVESTIGATING."""

import logging
from typing import Any

from faultmaven.core.investigation.offers import (
    remember_resolve_request,
    standing_deferred_decline,
)
from faultmaven.core.investigation.problem_status import (
    false_alarm_close_declined_at,
    record_confirmed_statement,
)
from faultmaven.core.investigation.terminal_transitions import open_disposition_offer
from faultmaven.modules.case.contracts import (
    Case,
    CaseAction,
    CaseState,
    InvestigationProgress,
    OfferBasis,
    OfferKind,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

from .stage_gates import _add_system_feedback
from .statement_revision import revision_pending
from .terminal_replies import _build_resolution_confirmation

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
    case: Case, *, closing: bool = False
) -> tuple[str, str] | None:
    """Whether a transition the model proposes now re-asks a close the user
    declined, as ``(side, feedback)``, or None (#1889).

    The one read for both declined engine closes:

    * ``false_alarm`` — the user declined closing on the standing false-alarm
      finding (whoever opened that close). Gated on the CASE, not on the
      proposal's target: on an invalidated case a model ``resolved`` pivots to
      a ``closed_false_alarm`` close (resolution readiness is SUGGEST_CLOSE),
      so a target gate lets the same question back in through ``resolved``.
    * ``deferred`` — the model's proposal stays a close (``closing``) and the
      user's decline of the engine's deferred close stands: no solution or
      confirmation it never saw has been recorded (ruling 3).

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
    if not closing:
        return None
    if standing_deferred_decline(case) is not None:
        return (
            "deferred",
            "TRANSITION NOT PROPOSED: the user declined closing this case with "
            "the solution documented (the deferred-implementation close), and "
            "no new solution or confirmation has been recorded since. "
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
    when a solution or a confirmation the decline never saw is recorded
    (``terminal_transitions.declined_resolve_entry``; elapsed turns, repeated
    declines and a shrinking set of rows move nothing, ruling 3). Until then
    the model's re-proposal is the question the user just answered.
    The model is not left as the only way back: the refused turn carries the
    "Mark it resolved" chip (the offer projection's ``reopen_chip``).
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

        User-Agent Handshake Transitions (terminal), on the case's offer
        record (``core/investigation/offers.py``):
        - step 0 re-checks a resolution request the previous turn remembered;
        - step 2 runs the model's ``proposed_transition`` through readiness and
          opens the offer it earns. Nothing terminal executes here: the user
          answers an offer on a later turn, at the engine's gate (0b).
        """
        old_status = case.state

        # 0. A resolution request the previous turn remembered (#2408). A
        # model ``resolved`` the case had not earned opened no offer: the
        # readiness ask went below its reply, with no chips. This turn is the
        # user's answer, read once: the request ends whatever the outcome.
        # Skipped when an offer is open (opening one ended the request).
        request = case.offer_record.resolve_request
        if request is not None and case.offer_record.open is None:
            self._recheck_resolve_request(case, metadata)

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
        response_obj = metadata.get("response_obj")
        if response_obj and hasattr(response_obj, "state_updates"):
            proposed = getattr(response_obj.state_updates, "proposed_transition", None)
            if proposed:
                self._apply_model_proposal(case, metadata, proposed)
        return case

    def _recheck_resolve_request(self, case: Case, metadata: dict[str, Any]) -> None:
        """Step 0: the previous turn's resolution request, read once (#2408).

        READY opens the resolve offer as the model's (it asked); under a
        standing resolve decline nothing opens, and the turn carries today's
        refusal feedback and the reopen chip. SUGGEST_CLOSE, or still
        NEEDS_INFO, opens the close offer (``needs_info_unmet``): the user was
        asked once and did not (or could not) supply it, so the case does not
        loop on the ask (#656).
        """
        from faultmaven.core.investigation.terminal_transitions import (
            assess_resolution_readiness,
        )

        case.offer_record = remember_resolve_request(case, None)
        readiness = assess_resolution_readiness(case)
        # Telemetry: transition_compliance carries the readiness verdict, so a
        # turn that offered nothing is self-explaining in logs (#656).
        metadata["resolution_readiness_verdict"] = readiness.verdict
        metadata["resolution_readiness_missing"] = readiness.missing
        if readiness.verdict == readiness.READY:
            record = open_disposition_offer(
                case,
                kind=OfferKind.RESOLVE,
                basis=OfferBasis.MODEL,
                summary=_build_resolution_confirmation(case),
            )
            if record is None:
                # A resolve decline binds it: the request turned READY on the
                # rows the user declined (#1895). Refused as step 2 refuses
                # the model's own re-proposal.
                reask = declined_resolution_reask(case)
                if reask is not None:
                    _add_system_feedback(metadata, reask)
                    metadata["declined_resolve_card"] = True
                return
            case.offer_record = record
            metadata["resolution_ready_for_confirmation"] = True
            logger.info(
                f"Case {case.case_id}: the resolution request is met — "
                f"presenting the confirmation"
            )
            return
        if readiness.verdict == readiness.SUGGEST_CLOSE:
            summary = readiness.message
        else:
            summary = (
                "I understand. Without confirmation that the root cause "
                "was **eliminated** (e.g. the original error is now "
                "absent after the fix), I can't mark this as "
                "**resolved** — a restored-but-stabilized or "
                "deferred-fix case isn't a resolution.\n\n"
                "You can **close** the case instead — this preserves "
                "the root cause analysis and the documented (or "
                "deferred) solution."
            )
        record = open_disposition_offer(
            case,
            kind=OfferKind.CLOSE,
            basis=OfferBasis.NEEDS_INFO_UNMET,
            summary=summary,
        )
        if record is None:
            return
        case.offer_record = record
        metadata["resolution_suggest_close"] = True
        metadata["resolution_readiness_message"] = summary
        logger.info(
            f"Case {case.case_id}: the resolution request was not met, "
            f"proposing Close (missing: {readiness.missing})"
        )

    def _apply_model_proposal(
        self, case: Case, metadata: dict[str, Any], proposed: Any
    ) -> None:
        """Step 2: the model's ``proposed_transition``, through readiness and the
        record. The engine builds the prompt and the reason; the model only
        names the target."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_closure_readiness,
            assess_resolution_readiness,
        )
        from faultmaven.modules.case.domain.models.lifecycle import (
            LEGAL_TRANSITIONS,
        )

        if revision_pending(case):
            # The case is waiting on the user's answer about the problem
            # itself; no transition may compete with that question. The model
            # cannot tell who asked for this one, so it is refused whoever
            # did — the user's own close (status menu, REST) cancels the
            # revision first and reaches its own path.
            _add_system_feedback(
                metadata,
                "TRANSITION NOT PROPOSED: a revised problem statement is "
                "awaiting the user's confirmation. Let them answer it first.",
            )
            return

        # Structural validation against the LEGALITY graph — which edges
        # exist, not which ones a user may pick. The LLM is not a user: it
        # proposes transitions the state machine permits, so
        # ``USER_SELECTABLE_ACTIONS`` would be the wrong bar here. The prompt
        # instructs the LLM on which edges exist; this is the safety net for
        # prompt non-compliance (e.g., an LLM emitting ``to_state="resolved"``
        # from INQUIRY, which is not a valid edge). Rejecting here prevents
        # downstream pivot logic from quietly converting an invalid emission
        # into a different transition the user never intended.
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
            return

        # Only a disposition is offered (ruling 2). INQUIRY → INVESTIGATING is
        # a legal edge, but it is Gate 1's: the user's confirmation of the
        # problem statement moves the case, never an offer the model makes
        # (#2420).
        if proposed.to_state not in ("resolved", "closed"):
            _add_system_feedback(
                metadata,
                "TRANSITION NOT PROPOSED: only resolved or closed is offered to "
                "the user. The investigation starts when the user confirms the "
                "problem statement.",
            )
            return

        # A declined close binds the model too (#1889). The user's "no" to
        # closing on a false-alarm finding binds while the finding stands,
        # whoever asked; any proposal the model makes on the case is the same
        # question again (a ``resolved`` pivots to the false-alarm close).
        # Checked after legality and before the one-slot rule so the refusal,
        # not a supersession, is what the turn reports.
        reask = declined_close_reask(case)
        if reask is not None:
            _refuse_declined_reask(metadata, *reask)
            logger.info(
                f"Case {case.case_id}: model proposed {proposed.to_state!r} on "
                f"a false-alarm finding whose close the user declined — refused."
            )
            return

        # One slot (#1885). An offer the engine opened earlier this turn —
        # the apply step's false-alarm and deferred proposers, the
        # rca-infeasible close, a replayed bundle's offer, step 0's re-check —
        # stands: the model's proposal does not replace it. The engine chose
        # its target from the same readiness the model's proposal would be run
        # through, a CLOSE still pivots to RESOLVED at confirm on a resolvable
        # case (INV-37), and the model can propose again once the user has
        # answered.
        engine_offer = case.offer_record.open
        if engine_offer is not None:
            logger.info(
                f"Case {case.case_id}: the engine opened a "
                f"{engine_offer.kind.value!r} offer this turn — ignoring the "
                f"model's proposed_transition={proposed.to_state!r}."
            )
            # Read by the turn's ``transition_compliance`` line: the model's
            # proposal was dropped, not pivoted.
            metadata["transition_superseded_by_engine"] = True
            _add_system_feedback(
                metadata,
                "TRANSITION NOT PROPOSED: the engine already offered the "
                f"user a {engine_offer.kind.to_state!r} transition this turn, "
                "and that offer is what they will answer. Do not re-propose a "
                "transition until they have answered it.",
            )
            return

        # Readiness decides what the proposal earns:
        #   resolved: SUGGEST_CLOSE → a close offer; NEEDS_INFO → a request,
        #             not an offer (no chips; ruling 1, #2408); READY → a
        #             resolve offer.
        #   closed:   SUGGEST_RESOLVE → a resolve offer (INV-37); otherwise a
        #             close offer with the summary of what was found.
        kind = OfferKind.CLOSE if proposed.to_state == "closed" else OfferKind.RESOLVE
        if proposed.to_state == "resolved":
            readiness = assess_resolution_readiness(case)
            metadata["resolution_readiness_verdict"] = readiness.verdict
            metadata["resolution_readiness_missing"] = readiness.missing
            if readiness.verdict == readiness.SUGGEST_CLOSE:
                kind = OfferKind.CLOSE
                summary = readiness.message
                logger.info(
                    f"Agent proposed RESOLVED but case {case.case_id} "
                    f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}); "
                    f"pivoting to CLOSED."
                )
            elif readiness.verdict == readiness.NEEDS_INFO:
                # A request, not an offer: the ask goes below the reply with
                # no chips, and the next turn re-checks it (step 0).
                case.offer_record = remember_resolve_request(case, readiness.missing)
                metadata["resolution_needs_info_first_pass"] = True
                metadata["resolution_needs_info_message"] = readiness.message
                logger.info(
                    f"Agent proposed RESOLVED but case {case.case_id} "
                    f"verdict=NEEDS_INFO (missing: {readiness.missing}); "
                    f"asking for it, no offer."
                )
                return
            else:
                summary = _build_resolution_confirmation(case)
        else:
            closure = assess_closure_readiness(case)
            metadata["closure_readiness_verdict"] = closure.verdict
            summary = closure.message
            if closure.verdict == closure.SUGGEST_RESOLVE:
                kind = OfferKind.RESOLVE
                logger.info(
                    f"Agent proposed CLOSED but case {case.case_id} "
                    f"verdict=SUGGEST_RESOLVE; pivoting to RESOLVED."
                )

        # The deferred side of #1889: a close while the user's decline of the
        # engine's deferred close stands. A ``resolved`` the readiness check
        # pivots to CLOSED is read the same way.
        if kind == OfferKind.CLOSE:
            reask = declined_close_reask(case, closing=True)
            if reask is not None:
                _refuse_declined_reask(metadata, *reask)
                logger.info(
                    f"Case {case.case_id}: model proposed CLOSED while the "
                    f"user's decline of the deferred close stands — refused."
                )
                return

        record = open_disposition_offer(
            case,
            kind=kind,
            basis=OfferBasis.MODEL,
            summary=summary,
            evidence_ids=getattr(proposed, "evidence_ids", None) or (),
        )
        if record is None:
            # The resolve side (#1895): the model's ``resolved`` on a READY
            # case, and its ``closed`` the closure check pivots to RESOLVED
            # (INV-37), are one question — the resolution the user declined.
            # While that decline stands, both are refused, with feedback and
            # the "Mark it resolved" chip on the turn. The user's own close
            # pick closes while the decline stands (#2360): this binds the
            # model, never the user.
            feedback = declined_resolution_reask(case)
            if feedback is not None:
                _add_system_feedback(metadata, feedback)
                # Read by ``transition_compliance``, which reports the refusal.
                metadata["declined_resolve_card"] = True
            logger.info(
                f"Case {case.case_id}: model proposed {proposed.to_state!r} "
                f"(effective {kind.value}) while a decline binds it — refused."
            )
            return
        case.offer_record = record
        logger.info(
            f"Agent proposed transition → {kind.to_state} (pending user confirmation)"
        )
