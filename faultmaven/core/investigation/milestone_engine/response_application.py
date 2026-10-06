"""Applying a parsed structured LLM response onto a case: the INQUIRY and INVESTIGATING state-update paths that turn schema fields into case mutations."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from faultmaven.core.investigation.causal_graph.similarity import (
    find_duplicate_hypothesis,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    hypothesis_dedup_skipped_total,
    inquiry_classified_without_statement_total,
    inquiry_handshake_deferred_total,
)
from faultmaven.core.investigation.milestone_engine.cause_work import (
    refuse_cause_work,
    report_refused_cause_work,
)
from faultmaven.core.investigation.milestone_engine.chain_emission import (
    _apply_chain_emission,
    _nudge_ambiguous_orphan_chains,
)
from faultmaven.core.investigation.milestone_engine.evidence_need_updates import (
    _apply_evidence_need_updates,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_deferred_likelihood_updates,
    _apply_hypothesis_evidence_links,
    _apply_hypothesis_updates,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    gate1_bare_consent,
    revision_offer_key,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _determine_turn_outcome,
    _report_turn_uploads,
    _resolve_id_ref,
)
from faultmaven.core.investigation.problem_status import (
    cause_work_accepted,
    cause_work_staged,
    invalidate_problem,
    invalidation_refusal,
    propose_revision,
    revision_refusal,
    stage_cause_work,
    unverify_problem,
    verify_problem,
    withdraw_invalidation,
)
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
)
from faultmaven.core.investigation.terminal_transitions import (
    cancel_pending_transition,
)
from faultmaven.modules.case.contracts import (
    Case,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    InvestigationActionType,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    ProblemStatus,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionFeasible,
    TurnOutcome,
)

from .affordances import (
    _restates_standing_evidence,
    _restates_standing_solution,
    gate1_statement_is_confirmable,
)
from .cause_state import (
    _kb_prefetch_query_on_identification,
    _recompute_assessment_state,
    _resolve_chat_provider_name,
)
from .milestone_inference import (
    _apply_symptom_retraction,
    _evidence_coverage,
    _infer_milestones,
    _post_process_llm_response,
    _resolve_evidence_source,
    validate_reasoning_first,
)
from .response_synthesis import _note_engine_disposition_withdrawn
from .stage_gates import (
    _add_system_feedback,
    _apply_stage_gate_signals,
    _case_has_symptom_evidence,
    _coerce_intervention_quadrant,
    _determine_action_type,
    _settled_working_conclusion,
    _solution_cause_validated,
    _supersede_pending_solution_offers,
    llm_claimable_milestones,
)
from .statement_revision import REPLAY_METADATA_KEY
from .terminal_proposals import (
    _maybe_propose_deferred_close,
    _maybe_propose_false_alarm_close,
    is_engine_false_alarm_close,
)

logger = logging.getLogger(__name__)

#: The state-update fields that are cause work. While a revised statement
#: awaits the user's re-confirmation they are staged on it rather than applied
#: (``problem_status.cause_work_staged``), and replayed through this same apply
#: path when the user confirms. ``milestones`` contributes only its two cause
#: claims (``_STAGED_MILESTONE_FIELDS``).
_STAGED_FIELDS = (
    "hypotheses_to_add",
    "hypothesis_evidence_links",
    "causal_nodes_to_add",
    "causal_edges_to_add",
    "node_evidence_links",
    "deductive_validations",
    "root_cause_conclusion",
    "solutions_to_add",
)
_STAGED_MILESTONE_FIELDS = ("root_cause_likelihood", "root_cause_method")
#: Compliance signals that name the turn's own proposals. When a staging turn
#: holds its solutions, these are held with them — replayed after them, so they
#: register against the actions the replay creates — rather than rejected now
#: as "not registered" against a proposal that is merely waiting.
_STAGED_GATE_SIGNALS = (
    "solution_accepted",
    "mitigation_accepted",
    "mitigation_verified",
)

#: What a refused hypothesis's ``new_index_N`` slot resolves to: no hypothesis,
#: so a link or update naming it is skipped by its consumer's existence check.
_REFUSED_HYPOTHESIS_SLOT = "refused_before_verification"


class ResponseApplier:
    """Applies a structured LLM response's state updates onto a case, dispatching to the INQUIRY or INVESTIGATING update path by the case's current state."""

    def __init__(self, *, deps, kb_prefetcher) -> None:
        self.deps = deps
        self.kb_prefetcher = kb_prefetcher

    async def process_response_structured(
        self,
        case: Case,
        user_message: str,
        response_obj: BaseInteractionResponse,
        attachments: list[dict[str, Any]] | None = None,
        upload_report: dict[str, list[str]] | None = None,
    ) -> tuple[Case, dict[str, Any]]:
        """Process structured response and update case state.

        ``upload_report`` is the turn's already-derived upload reading (see
        ``_report_turn_uploads``). ``_process_turn_impl`` derives it once, for
        EVERY path, and hands it down so the derivation — and its warnings —
        happen exactly once per turn (#1229). Callers that don't have one (the
        direct-call tests) pass ``attachments`` and the reading is derived here.
        """

        # NOTE: Validation moved AFTER post-processing to allow fallback evidence creation
        # See line 1500 for actual validation

        # Initialize metadata for this response processing
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            "outcome": TurnOutcome.CONVERSATION,
        }
        metadata.update(
            upload_report
            if upload_report is not None
            else _report_turn_uploads(case, attachments)
        )
        # What validation had to do to the model's confidence values (fm#1502).
        # Seeded here, before any apply step appends its own repairs, because
        # this is the first point with both the accepted response and the
        # turn's metadata in hand; ingest adds its link decisions to the same
        # list.
        confidence_notes = [
            repair.note() for repair in getattr(response_obj, "_confidence_repairs", [])
        ]
        if confidence_notes:
            metadata["validation_repairs"] = confidence_notes

        # POST-PROCESSING: Apply LLM failure mitigation (Pattern-based fallback)
        # This repairs LLM classification failures before applying state updates
        # Reference: docs/working/LLM-FAILURE-MITIGATION-STRATEGY.md
        logger.debug(
            f"Post-processing LLM response: response_type={type(response_obj).__name__}, "
            f"has_state_updates={hasattr(response_obj, 'state_updates')}, "
            f"state_updates_exists={response_obj.state_updates is not None if hasattr(response_obj, 'state_updates') else False}"
        )
        if isinstance(response_obj, (InquiryResponse,)) or (
            hasattr(response_obj, "state_updates") and response_obj.state_updates
        ):
            # Apply post-processing to repair state_updates
            logger.debug(
                f"Applying post-processing to state_updates with user_message preview: {user_message[:100]}..."
            )
            response_obj.state_updates = _post_process_llm_response(
                updates=response_obj.state_updates,
                user_message=user_message,
                case=case,
            )
            # None-safe logging
            evidence_list = getattr(response_obj.state_updates, "evidence_to_add", [])
            evidence_count = len(evidence_list) if evidence_list is not None else 0
            logger.debug(
                f"Post-processing complete, evidence_to_add count: {evidence_count}"
            )

        # Validate reasoning-first requirement (AFTER post-processing to allow fallback evidence creation)
        is_valid, validation_errors, offending_milestones = validate_reasoning_first(
            response_obj, case
        )
        if not is_valid:
            error_msg = "Reasoning validation failed:\n" + "\n".join(validation_errors)
            logger.warning(
                f"Reasoning validation failed for case {case.case_id}: {error_msg}"
            )
            # Degrade gracefully: strip ONLY the milestones that actually failed
            # validation, preserving co-emitted valid ones. A single unjustified
            # milestone (e.g. a reflexive root_cause_identified) must NOT wipe a
            # validated mitigation/solution gate emitted the same turn — that
            # all-or-nothing wipe was the S1 trap mechanism (redesign §1.1, §5).
            milestones = getattr(
                getattr(response_obj, "state_updates", None), "milestones", None
            )
            stripped: list[str] = []
            if milestones and offending_milestones:
                for field_name in offending_milestones:
                    if hasattr(milestones, field_name):
                        setattr(milestones, field_name, None)
                        stripped.append(field_name)
                # Drop only the stripped milestones' justifications; keep the rest.
                # ``milestone_justifications`` is a model now, so clearing a
                # justification is setting its field to None rather than popping
                # a key — ``as_dict()`` then omits it, which is what "dropped"
                # meant when this was a dict (fm#1057).
                ir = getattr(response_obj, "evidence_trail", None)
                justifications = getattr(ir, "milestone_justifications", None)
                if justifications is not None:
                    for field_name in stripped:
                        if field_name in type(justifications).model_fields:
                            setattr(justifications, field_name, None)
            logger.info(
                f"Surgically stripped {stripped or 'no'} milestone(s) for case "
                f"{case.case_id}; preserved the rest. Continuing with response."
            )
            # Tell the model, or it re-claims the same milestone unjustified
            # and is stripped again (fm#1677). The turn record truncates
            # feedback from the tail; the strip is the turn's first writer, and
            # prepending keeps this at the head should that order change.
            not_recorded = (
                f"Milestones {sorted(stripped)} were NOT recorded this turn. "
                if stripped
                else ""
            )
            _add_system_feedback(
                metadata,
                f"EVIDENCE VALIDATION: {not_recorded}" + " ".join(validation_errors),
                prepend=True,
            )

        # Dispatch based on response type
        if isinstance(response_obj, InquiryResponse):
            await self._apply_inquiry_updates(
                case, response_obj.state_updates, metadata, user_message
            )
        elif isinstance(response_obj, TerminalResponse):
            # Terminal updates typically just documentation, no deep state change
            pass
        else:
            # Investigation updates (Verification, Hypothesis, Resolution, General)
            # All check 'state_updates' which matches InvestigationStateUpdate structure
            await self._apply_investigation_updates(
                case,
                response_obj.state_updates,
                metadata,
                response_obj,
                user_message,
            )

        # Store response_obj in metadata so _check_automatic_transitions can
        # access ProposedTransition for the User-Agent Handshake flow
        metadata["response_obj"] = response_obj

        return case, metadata

    async def _apply_inquiry_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        user_message: str = "",
    ) -> None:
        """Apply updates during INQUIRY phase."""
        # Capture pre-turn state for the same-turn-confirmation guard
        # applied later in this method. The design requires the user to
        # confirm a problem statement that was presented on a PRIOR turn —
        # never one that was first written this turn. The INQUIRY_TEMPLATE
        # instructs the LLM accordingly ("Never set user_confirmed_-
        # investigation=True on the same turn you first present the
        # problem statement"), but LLMs are stochastic and the rule was
        # observed to be violated on first-turn cases with explicit
        # "please investigate" phrasing. This local makes the invariant
        # enforceable independently of prompt compliance.
        #
        # The TEXT, not a boolean, because the write guard below reads it to
        # decide whether a statement the user could have SEEN already stood.
        _statement_at_turn_start = case.inquiry.proposed_problem_statement

        # Consent binds to the wording the user was SHOWN, so a revision that
        # arrives on a consent turn is not applied. Two shapes reach here:
        #
        #   - The DECIDE click. Section 0c has already set
        #     ``problem_statement_confirmed`` earlier in THIS turn, and the
        #     turn still renders an InquiryResponse. Without this guard the
        #     LLM's same-turn rewording replaced the statement after consent
        #     and ``_transition_to_investigating`` copied the new text into
        #     ``case.description`` — framing the investigation on wording the
        #     user never saw, which is the hole this whole change exists to
        #     close, left open on the click path.
        #   - The LLM path relaying a plain "yes" while re-emitting the field
        #     with cosmetic edits. Refusing the consent there would be worse
        #     than useless: the engine re-presents the reword, the user says
        #     yes again, the model rewords again, and the case never leaves
        #     INQUIRY. Dropping the reword instead commits the consent against
        #     the text it was actually given for.
        #
        # A FIRST write arriving with consent is still applied — nothing stood
        # for the user to have seen, so there is no reword to protect, and the
        # statement must persist for the next turn to present it. The consent
        # itself is refused below, by ``gate1_statement_is_confirmable``.
        #
        # The LLM's flag is consent only when the turn's typed text is one bare
        # consent token (#1794, ruling (a)): ``screened``. The write guard keys
        # on the SCREENED consent, computed before the write, so a refused turn
        # keeps the user's correction: "yes but it's the primary too" commits
        # nothing, and must not lose the revision it carries either.
        _llm_flag = bool(getattr(updates, "user_confirmed_investigation", False))
        screened = _llm_flag and gate1_bare_consent(user_message)
        _consent_on_this_turn = screened or case.inquiry.problem_statement_confirmed
        _statement_stood = bool((_statement_at_turn_start or "").strip())
        if updates.proposed_problem_statement and not (
            _consent_on_this_turn and _statement_stood
        ):
            case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

        # Convert and store problem_confirmation from LLM schema to domain model
        if updates.problem_confirmation:
            from faultmaven.modules.case.domain.models.problem import (
                ProblemConfirmation as DomainProblemConfirmation,
            )

            case.inquiry.problem_confirmation = DomainProblemConfirmation(
                problem_type=updates.problem_confirmation.problem_type,
                severity_guess=updates.problem_confirmation.severity_guess,
            )

        # Convert and store preliminary_urgency from LLM schema to domain model
        if updates.preliminary_urgency:
            from faultmaven.modules.case.domain.models.problem import (
                PreliminaryUrgency as DomainPreliminaryUrgency,
            )
            from faultmaven.modules.case.domain.models.problem import UrgencyLevel

            case.inquiry.preliminary_urgency = DomainPreliminaryUrgency(
                level=UrgencyLevel(
                    updates.preliminary_urgency.level.lower()
                ),  # Convert uppercase to lowercase enum
                is_ongoing=getattr(updates.preliminary_urgency, "is_ongoing", False),
                is_incident_report=getattr(
                    updates.preliminary_urgency, "is_incident_report", False
                ),
                impact_assessment=updates.preliminary_urgency.impact_assessment,
                assessed_at_turn=case.current_turn,  # Use current turn number
            )

        # ``proposed_problem_statement`` has exactly ONE writer: the block
        # above, where the LLM sets it deliberately. A second writer used to
        # sit here and promote ``problem_confirmation.preliminary_guidance``
        # into the statement whenever none existed yet. That field carried no
        # description on the LLM-facing schema and was named nowhere in the
        # INQUIRY prompt, so a model filled it from its name alone — with
        # guidance. The guidance then became the problem statement, and on
        # confirmation became ``case.description`` and the frame for the whole
        # investigation. Removed together with the field (#1606); a statement
        # is now only ever what the model deliberately wrote as one.
        #
        # What the promotion hid is now COUNTED rather than papered over: a
        # turn that classified the problem but proposed nothing leaves Gate 1
        # shut, so a user confirmation on it commits nothing.
        if updates.problem_confirmation and not case.inquiry.proposed_problem_statement:
            inquiry_classified_without_statement_total.inc()

        # Two-Step Confirmation (Design Doc Section 1.2)
        #
        # The design requires explicit user confirmation before INQUIRY → INVESTIGATING.
        # Auto-confirm is NOT used — even for CRITICAL/HIGH urgency issues.
        #
        # Flow:
        #   Turn N: User reports incident → Agent presents problem statement + asks "Is this accurate?"
        #   Turn N+1: User confirms ("Yes") → LLM sets user_confirmed_investigation=True → transition fires
        #
        # This block handles two scenarios:
        # (a) LLM signals user confirmation via user_confirmed_investigation=True
        # (b) Logging for informational/urgent cases (no auto-transition)
        _is_incident = updates.preliminary_urgency and getattr(
            updates.preliminary_urgency, "is_incident_report", False
        )

        # Check if LLM detected user confirmation of the problem statement.
        # Same-turn-confirmation guard: a statement must have stood BEFORE
        # this turn, or the LLM is writing it and confirming it in one shot,
        # which collapses the User-Agent Handshake. Binding consent to the
        # wording the user SAW is handled on the write side above. See
        # ``gate1_statement_is_confirmable`` and the captured
        # _statement_at_turn_start at the top of this method. The flag commits
        # only when ``screened`` (#1794): a click commits at section 0c, before
        # this runs, and so arrives here already confirmed.
        if (
            screened
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and gate1_statement_is_confirmable(_statement_at_turn_start)
        ):
            case.inquiry.problem_statement_confirmed = True
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            logger.info(
                f"User confirmed problem statement — transitioning to INVESTIGATING. "
                f"statement='{case.inquiry.proposed_problem_statement[:80]}...'"
            )
        elif (
            _llm_flag
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and gate1_statement_is_confirmable(_statement_at_turn_start)
        ):
            # The flag on a confirmable statement, but the typed text is not
            # one bare consent token (#1794): "yes but it's the primary too",
            # "ok, don't start yet", the card's own payload typed out. The flag
            # stays an honest reading; the engine commits nothing, Gate 1 stays
            # pending and composes its card. Gated on ``not
            # problem_statement_confirmed`` above, exactly: a click-committed
            # Gate 1 hands the LLM the card's payload text, which is not bare,
            # and that turn refused nothing.
            inquiry_handshake_deferred_total.labels(reason="not_bare").inc()
            logger.info(
                f"Gate 1 not committed for case {case.case_id}: the LLM read a "
                f"confirmation, but the typed text is not one bare consent token "
                f"(#1794)",
                extra={"case_id": case.case_id, "turn": case.current_turn},
            )
        elif (
            _llm_flag
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and not gate1_statement_is_confirmable(_statement_at_turn_start)
        ):
            # LLM tried to set the problem statement AND confirm investigation
            # in the same turn — design forbids this (the user must see the
            # statement first, then confirm on a subsequent turn). Refuse
            # the transition; Gate 1 stays pending, so the engine composes the
            # statement into the very next turn and re-offers the pair.
            #
            # There is no recovery FLAG any more. ``handshake_deferred_at_turn``
            # existed to tell the following turn to re-present — a narrow proxy
            # for "the user has not seen this statement", carried because
            # presentation was the LLM's job and it had to be told. Presentation
            # is now the engine's, and it happens on EVERY Gate-1-pending turn,
            # so the recovery the flag arranged is the ordinary path (#1607).
            inquiry_handshake_deferred_total.labels(reason="same_turn").inc()
            logger.warning(
                f"Same-turn-confirmation guard rejected INQUIRY→INVESTIGATING "
                f"for case {case.case_id}: LLM emitted "
                f"user_confirmed_investigation=True on the same turn that "
                f"first set proposed_problem_statement. Deferring to next turn.",
                extra={
                    "case_id": case.case_id,
                    "turn": case.current_turn,
                    "statement_preview": case.inquiry.proposed_problem_statement[:80],
                },
            )
        elif (
            updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not _is_incident
        ):
            # LLM flagged HIGH urgency but did NOT mark as incident report.
            # This typically means the user asked an informational/how-to question
            # about a topic that involves failures (e.g., "How do I check logs of a
            # restarting pod?"). Stay in INQUIRY.
            logger.info(
                f"Urgent signals detected but is_incident_report=False — "
                f"treating as informational query, staying in INQUIRY. "
                f"level={updates.preliminary_urgency.level}, "
                f"problem_type={updates.problem_confirmation.problem_type if updates.problem_confirmation else 'unknown'}"
            )
        elif (
            _is_incident
            and updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not case.inquiry.problem_statement_confirmed
        ):
            # Urgent incident detected — agent should present problem statement
            # and ask for confirmation in its response. Transition will happen on
            # the NEXT turn when user confirms.
            logger.info(
                f"Urgent incident detected ({updates.preliminary_urgency.level} + ongoing). "
                f"Agent will present problem statement for user confirmation. "
                f"has_statement={bool(case.inquiry.proposed_problem_statement)}"
            )

        # Store KB match on case when LLM identifies one (Gap #5a)
        # This populates InquiryData.knowledge_matches so we can validate
        # confidence thresholds when knowledge_resolution arrives (possibly in a later turn)
        if updates.knowledge_match:
            km = updates.knowledge_match
            case.inquiry.knowledge_matches.append(
                KnowledgeMatch(
                    match_id=km.match_type
                    + "_"
                    + str(len(case.inquiry.knowledge_matches)),
                    match_type=km.match_type,
                    relevance_score=km.match_likelihood,
                    summary=km.match_summary,
                    potential_solution=km.suggested_solution,
                )
            )
            logger.info(
                f"KB match stored: type={km.match_type}, "
                f"likelihood={km.match_likelihood:.2f}, "
                f"summary={km.match_summary[:80]}"
            )

        # Check for KB Resolution
        if updates.knowledge_resolution:
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=updates.knowledge_resolution.match_id,
                match_type=updates.knowledge_resolution.match_type,
                solution_applied=updates.knowledge_resolution.solution_applied,
                user_confirmation=updates.knowledge_resolution.user_confirmation,
            )
            # v3: knowledge_resolution received during INQUIRY is stored
            # for visibility but is NOT a transition trigger. The LLM
            # should emit knowledge_resolution during INVESTIGATING (when
            # the user confirms a runbook fix worked), not INQUIRY.
            logger.warning(
                "Case %s: knowledge_resolution emitted during INQUIRY; "
                "v3 expects this during INVESTIGATING (after problem confirmation). "
                "Storing for audit but not transitioning.",
                case.case_id,
            )

    def _apply_verification_updates(
        self, case: Case, updates: Any, metadata: dict[str, Any]
    ) -> None:
        """Step 2c: the statement is inaccurate, the problem never existed, or
        the user disputes a false-alarm finding.

        Each proposal passes its guard in ``problem_status`` or is refused with
        a note to the model. Two shapes are contradictions and refuse
        everything they carry: a revision and a false-alarm finding together,
        and a false-alarm finding on the turn that verified the symptom. A
        revision on the turn that verified the symptom wins: the verification
        was of the revised problem, so it is granted when the user confirms.
        """
        vu = getattr(updates, "verification_updates", None)
        if vu is None:
            return
        pv = case.problem_verification
        if pv is not None and vu.rca_infeasible is not None:
            pv.rca_infeasible = bool(vu.rca_infeasible)
            pv.rca_infeasible_rationale = vu.rca_infeasible_rationale
        if pv is None:
            return

        created = metadata.get("evidence_added", [])

        def _ids(refs) -> list[str]:
            return [_resolve_id_ref(r, created, "ev_") for r in (refs or [])]

        revision = (vu.revised_problem_statement or "").strip()
        invalidated = vu.problem_invalidated is True
        verified_this_turn = "symptom_verified" in metadata["milestones_completed"]

        if vu.invalidation_withdrawn is True:
            if (vu.withdrawal_basis or "").strip() and withdraw_invalidation(
                case, basis=vu.withdrawal_basis
            ):
                self._withdraw_engine_false_alarm_close(case, metadata)
                metadata["problem_status_changed"] = True
            else:
                _add_system_feedback(
                    metadata,
                    "FALSE-ALARM WITHDRAWAL NOT ACCEPTED: "
                    + (
                        "no false-alarm finding stands."
                        if case.progress.problem_status.value != "invalidated"
                        else "say what the user said that disputes the finding "
                        "(withdrawal_basis)."
                    ),
                )

        if revision and invalidated:
            _add_system_feedback(
                metadata,
                "STATEMENT REVISION AND FALSE-ALARM FINDING NOT ACCEPTED: they "
                "contradict each other — a revision says a problem exists, a "
                "false alarm says none does. Send the one the evidence supports.",
            )
            return

        if invalidated:
            refusal = (
                "the same response verified the symptom"
                if verified_this_turn
                else (
                    "a transition is awaiting the user's answer"
                    if case.pending_transition
                    else invalidation_refusal(
                        case, _ids(vu.invalidation_evidence_ids), vu.invalidation_basis
                    )
                )
            )
            if refusal:
                _add_system_feedback(
                    metadata, f"FALSE-ALARM FINDING NOT ACCEPTED: {refusal}."
                )
                return
            invalidate_problem(
                case,
                evidence_ids=_ids(vu.invalidation_evidence_ids),
                basis=vu.invalidation_basis,
            )
            metadata["problem_status_changed"] = True
            metadata["problem_invalidated_this_turn"] = True
            return

        pending_revision = pv.pending_revision
        if (
            revision
            and pending_revision is not None
            and revision == pending_revision.text.strip()
        ):
            # The wording already awaiting the user, sent again: nothing moves,
            # and it must not read as progress on a turn spent waiting.
            return

        if revision:
            key = revision_offer_key(revision)
            pending = case.pending_transition
            refusal = (
                "a transition is awaiting the user's answer"
                if pending and not is_engine_false_alarm_close(pending)
                else revision_refusal(
                    case,
                    revision,
                    _ids(vu.revision_evidence_ids),
                    vu.revision_basis,
                    key,
                )
            )
            if refusal:
                _add_system_feedback(
                    metadata, f"STATEMENT REVISION NOT ACCEPTED: {refusal}."
                )
                return
            if pending:
                self._withdraw_engine_false_alarm_close(case, metadata)
            if verified_this_turn:
                unverify_problem(case, via="superseded_by_revision")
                metadata["milestones_completed"].remove("symptom_verified")
            propose_revision(
                case,
                text=revision,
                evidence_ids=_ids(vu.revision_evidence_ids),
                basis=vu.revision_basis,
                offer_key=key,
            )
            metadata["problem_status_changed"] = True
            metadata["revision_proposed_this_turn"] = True

    @staticmethod
    def _withdraw_engine_false_alarm_close(
        case: Case, metadata: dict[str, Any]
    ) -> None:
        """Take back the engine's own false-alarm close offer: the finding it
        rested on no longer stands. The offer is withdrawn, not declined —
        nothing is recorded against it."""
        pending = case.pending_transition
        if pending and is_engine_false_alarm_close(pending):
            _note_engine_disposition_withdrawn(case, metadata)
            cancel_pending_transition(case)

    @staticmethod
    def _stage_cause_work(case: Case, updates: Any, metadata: dict[str, Any]) -> None:
        """Step 2d: hold this turn's cause work on the pending revision."""
        subset: dict[str, Any] = {}
        for name in _STAGED_FIELDS:
            value = getattr(updates, name, None)
            if value:
                subset[name] = (
                    [item.model_dump(mode="json") for item in value]
                    if isinstance(value, list)
                    else value.model_dump(mode="json")
                )
        # The same fix re-sent on a later hold turn is staged once.
        if "solutions_to_add" in subset:
            staged_before = {
                (item.get("description") or "").strip().casefold()
                for bundle in case.problem_verification.pending_revision.staged
                for item in bundle.updates.get("solutions_to_add", [])
            }
            fresh = [
                item
                for item in subset["solutions_to_add"]
                if (item.get("description") or "").strip().casefold()
                not in staged_before
            ]
            if fresh:
                subset["solutions_to_add"] = fresh
            else:
                del subset["solutions_to_add"]
        milestones = getattr(updates, "milestones", None)
        if milestones is not None:
            claims = {
                name: getattr(milestones, name, None)
                for name in _STAGED_MILESTONE_FIELDS
                if getattr(milestones, name, None) is not None
            }
            if getattr(updates, "solutions_to_add", None):
                held = [
                    name
                    for name in _STAGED_GATE_SIGNALS
                    if getattr(milestones, name, None) is True
                ]
                claims.update({name: True for name in held})
                if held:
                    metadata["staged_gate_signals"] = held
            if claims:
                subset["milestones"] = claims
        if not subset:
            return
        stage_cause_work(
            case, updates=subset, evidence_added=metadata.get("evidence_added", [])
        )
        metadata["cause_work_staged"] = sorted(subset)
        logger.info(
            "Case %s: staged %s pending re-confirmation of the revised statement",
            case.case_id,
            sorted(subset),
        )

    def _apply_cause_claims(
        self, case: Case, updates: Any, metadata: dict[str, Any]
    ) -> None:
        """Step 2c: the root-cause conclusion and the likelihood / method the
        LLM attaches to it.

        These are cause claims, so they need a verified problem
        (``cause_work_accepted``). Read after step 2b, the status is the one
        the turn ends with. A refused claim is not stored and the model is
        told why: the evidence behind it is already on the case, so nothing
        is lost by concluding again once the symptom is verified.

        A likelihood of 0 is no claim: it is what a provider that fills every
        optional field sends when the model has no cause in mind. The
        IDENTIFIED floor for likelihood and method is the recompute's
        (``cause_state``), not this step's.
        """
        rcc = getattr(updates, "root_cause_conclusion", None)
        m = getattr(updates, "milestones", None)
        likelihood = getattr(m, "root_cause_likelihood", None) if m else None
        method = getattr(m, "root_cause_method", None) if m else None
        claimed = [
            name
            for name, present in (
                ("a root_cause_conclusion", bool(rcc)),
                ("a root_cause_likelihood", bool(likelihood)),
                ("a root_cause_method", bool(method)),
            )
            if present
        ]
        if not claimed:
            return

        if not cause_work_accepted(case):
            refuse_cause_work(
                case.case_id, metadata, kind="conclusion", what=", ".join(claimed)
            )
            return

        p = case.progress
        if rcc:
            metadata["rcc_authored_this_turn"] = True
            case.root_cause_conclusion = RootCauseConclusion(
                root_cause=rcc.root_cause,
                mechanism=rcc.mechanism,
                evidence_basis=rcc.evidence_ids,
                likelihood=rcc.likelihood,
                confidence_level=ConfidenceLevel.from_score(rcc.likelihood),
                # INV-35: attribution hint; the chain nodes/hypotheses this turn
                # are ingested later (_apply_chain_emission), so the engine
                # resolves this to validated_hypothesis_id at cause-state recompute
                # (link_llm_rcc_to_cause tier 1), not here.
                names_root_node_id=getattr(rcc, "names_root_node_id", None),
            )
        if likelihood:
            p.root_cause_likelihood = likelihood
        if method:
            # The schema's Literal admits only valid methods.
            p.root_cause_method = method

    async def _apply_investigation_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        response_obj: Any | None = None,
        user_message: str = "",
    ) -> None:
        """Apply updates during INVESTIGATING phase."""
        # 0. Check for Proactive Blocker Detection — surface as system feedback
        if hasattr(updates, "missing_critical_data") and updates.missing_critical_data:
            blocker = updates.missing_critical_data
            blocker_msg = (
                f"DATA QUALITY ISSUE: {blocker.description}. "
                f"Expected: {blocker.what_was_expected}. Found: {blocker.what_was_found}. "
                f"Impact: {blocker.impact}."
            )
            if blocker.suggested_alternatives:
                blocker_msg += (
                    f" Alternatives: {', '.join(blocker.suggested_alternatives)}"
                )
            current_feedback = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = f"{current_feedback}\n{blocker_msg}".strip()
            metadata["data_blocker_detected"] = True
            logger.warning(f"Case {case.case_id} data blocker: {blocker.description}")

        # Track evidence quality issues (non-blocking)
        if (
            hasattr(updates, "evidence_quality_issues")
            and updates.evidence_quality_issues
        ):
            for issue in updates.evidence_quality_issues:
                logger.info(
                    f"Evidence quality issue detected: {issue.evidence_id} - {issue.issue_type} ({issue.severity})"
                )
                # Could store these in case metadata for future reference
                metadata.setdefault("evidence_quality_issues", []).append(
                    {
                        "evidence_id": issue.evidence_id,
                        "issue_type": issue.issue_type,
                        "severity": issue.severity,
                    }
                )

        # 1b. v3 KB-Resolution signal: milestone collapse (state authoring
        # only). When the user confirms a runbook fix worked, the LLM emits
        # `knowledge_resolution` alongside `root_cause_conclusion`,
        # `solutions_to_add`, and the gate milestones (`solution_accepted`)
        # — INVESTIGATING's structured state is authored in this one turn.
        # The RESOLVED disposition is NOT collapsed (#722): the user's "it
        # worked" is the solution-verification claim (FM trusts it), not
        # consent to the irreversible terminal transition — that consent
        # comes from the explicit confirm turn of the standard
        # ProposedTransition handshake. `KnowledgeResolution` (including
        # `user_confirmation`) is an attribution/audit record, not consent.
        # See investigation-lifecycle-logic.md §1.2 →
        # "KB-Resolution Path (Milestone-Collapse Variant)".
        if hasattr(updates, "knowledge_resolution") and updates.knowledge_resolution:
            kr = updates.knowledge_resolution
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=kr.match_id,
                match_type=kr.match_type,
                solution_applied=kr.solution_applied,
                user_confirmation=kr.user_confirmation,
            )
            # A runbook matched against the reported symptom, and the user
            # confirmed its fix worked — the symptom is, by construction, verified.
            # Establish the cause-identification anchor so the milestone
            # collapse's RootCauseConclusion is honored by the M5 / readiness
            # gates (which require a verified symptom for the RCC signal).
            # Not appended to ``milestones_completed``: that list is reviewed
            # against cited evidence at step 2b, and this verification rests
            # on the user's confirmation, not on a citation.
            verify_problem(case, via="knowledge_resolution")
            logger.info(
                "Case %s: knowledge_resolution signalled during INVESTIGATING; "
                "match_id=%s, type=%s. Standard ProposedTransition handshake handles disposition.",
                case.case_id,
                kr.match_id,
                kr.match_type,
            )

        # 1. Update Milestones
        # NOTE: solution_verified is excluded — it requires the User-Agent
        # Handshake via ProposedTransition (see terminal_transitions.py).
        if updates.milestones:
            m = updates.milestones
            p = case.progress
            # Only set to True (never revert).
            #
            # STAGE-GATE SIGNALS ARE NOT APPLIED HERE. ``solution_accepted``
            # and the mitigation pair are compliance signals whose guards
            # must see the ProposedActions created by THIS turn's solutions
            # step (the prompt's KB-resolution flow mandates SolutionToAdd +
            # solution_accepted in one response) — they are applied by
            # ``_apply_stage_gate_signals`` AFTER step 5 below.
            # The only progress indicator the LLM claims is the symptom.
            # cause_state is engine-derived from a validated, uncontested chain
            # root at the recompute (§9.2 / INV-35); solution_proposed is
            # engine-derived from live SOLUTION offers (INV-32);
            # solution_verified requires the User-Agent Handshake. The
            # milestone is recorded on the edge only.
            if m.symptom_verified and verify_problem(case, via="symptom_claim"):
                metadata["milestones_completed"].append("symptom_verified")

            _apply_symptom_retraction(case, m, response_obj, metadata)

            if getattr(m, "solution_feasible", None) is not None:
                p.solution_feasible = SolutionFeasible(m.solution_feasible)

            # root_cause_likelihood / root_cause_method are cause claims: they
            # are applied at step 2c, once this turn's verification is final.
            # KB-remediation pre-fetch is triggered on the cause_state→IDENTIFIED
            # edge AFTER the end-of-turn chain recompute (INV-35) — cause_state is
            # engine-derived there, not from any milestone applied in this block.
            # See the prefetch beside _recompute_assessment_state below.

        # 2. Add Evidence
        # Post-010: every Evidence row comes from the LLM declaring an
        # `evidence_to_add` entry on this turn. Files uploaded earlier in
        # the turn live on `uploaded_files` only — they become Evidence
        # only when the LLM extracts a claim-relevant slice and records
        # it here.
        has_attr = hasattr(updates, "evidence_to_add")
        evidence_list = getattr(updates, "evidence_to_add", None) if has_attr else None
        evidence_count = len(evidence_list) if evidence_list else 0
        logger.info(
            f"Evidence creation check: "
            f"hasattr(updates, 'evidence_to_add')={has_attr}, "
            f"evidence_to_add={evidence_list}, "
            f"count={evidence_count}"
        )

        if hasattr(updates, "evidence_to_add") and updates.evidence_to_add:
            # Post-010: source_file_id is declared by the LLM directly on
            # EvidenceToAdd. The Pydantic ``_source_file_required_unless_user_description``
            # validator on EvidenceToAdd has already enforced the
            # ``evidence_source_invariant``: by the time we get here,
            # ``ev_item.source_file_id is None`` implies
            # ``source_type == USER_DESCRIPTION``. We pass the value
            # through unchanged — no turn-file fallback, because that
            # would silently mis-attribute a chat-extracted USER_DESCRIPTION
            # quote to whatever file happens to be in the same turn.
            #
            # Redesign R5/§2: the former path-conditional causal_evidence ban
            # is removed. Whether RCA-side work runs is decided by the prompt
            # (gated on cause uncertainty), not by an engine emission ban —
            # causal_evidence is always allowed during INVESTIGATING.
            for ev_item in updates.evidence_to_add:
                # A cause cannot have been eliminated before the problem it
                # caused is verified: a causal_absence row then means "the
                # problem is not there", which is symptom_absence. Reclassified
                # rather than dropped — ``evidence_added`` is positional, and
                # new_index_N refs this turn index into it. Without this, one
                # such row reads resolution-READY on a never-verified case
                # (``assess_resolution_readiness``) and pivots a false-alarm
                # close to RESOLVED (INV-37).
                if (
                    ev_item.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
                    and not cause_work_accepted(case)
                ):
                    ev_item.category = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
                    metadata.setdefault("validation_repairs", []).append(
                        "causal_absence_evidence recorded before the problem was "
                        "verified was recorded as symptom_absence_evidence"
                    )
                    _add_system_feedback(
                        metadata,
                        "A causal_absence_evidence row arrived before the problem "
                        "was verified and was recorded as symptom_absence_evidence: "
                        "a cause can only be shown eliminated once the problem it "
                        "caused is verified.",
                    )
                # Infer milestone attribution (Tier 2 + Tier 3)
                milestones_completed_this_turn = metadata.get(
                    "milestones_completed", []
                )
                if ev_item.advances_milestones is not None:
                    advances_milestones = ev_item.advances_milestones
                else:
                    advances_milestones = _infer_milestones(
                        ev_item.category, milestones_completed_this_turn
                    )

                # Guard a hallucinated/stale source_file_id (FK to uploaded_files)
                # before it aborts the turn at save.
                source_file_id, source_type = _resolve_evidence_source(
                    case, ev_item.source_file_id, ev_item.source_type
                )

                coverage_start, coverage_end, coverage_source = _evidence_coverage(
                    case, source_file_id, ev_item.extract
                )
                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    extract=ev_item.extract,
                    category=ev_item.category,
                    source_type=source_type,
                    source_file_id=source_file_id,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                    coverage_start_ts=coverage_start,
                    coverage_end_ts=coverage_end,
                    coverage_source=coverage_source,
                )
                # #1136: does this row carry a datum the case did not already
                # hold? Computed BEFORE the append, or the row would match
                # itself. ``evidence_added`` keeps every minted id (positional
                # ``new_index_N`` refs, milestone attribution and coverage all
                # resolve against it); only the progress signal narrows.
                restates = _restates_standing_evidence(ev_item, case)
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                if not restates:
                    metadata.setdefault("novel_evidence_added", []).append(
                        ev.evidence_id
                    )
                logger.info(
                    f"Created evidence: {ev.evidence_id} | "
                    f"category={ev.category.value}, source_type={ev.source_type.value}, "
                    f"source_file_id={ev.source_file_id}, "
                    f"summary='{ev.summary[:80]}...'"
                )

        # 2b. Validate Milestone Claims Against Cited Evidence
        # Milestones are applied optimistically from LLM output (step 1 above),
        # then validated here. Invalid claims are REVERTED to prevent milestones
        # advancing without supporting evidence.
        # Only names the LLM can CLAIM are reviewable. ``root_cause_identified``
        # is engine-derived (INV-35) and is appended by the recompute later in
        # this method (#1284), so today it is not present here anyway. Filtering
        # explicitly rather than relying on that ordering: the review expects
        # ">=2 CAUSAL_EVIDENCE rows" for that name, which plenty of genuine
        # identification turns lack, so if the recompute were ever hoisted above
        # this step the engine's own derivation would be silently deleted from
        # the turn and the transparency light would go back on with nothing
        # failing. The exclusion makes that reordering harmless instead.
        reviewable = llm_claimable_milestones(metadata["milestones_completed"])
        if reviewable:
            from faultmaven.core.investigation.evidence_processor import (
                validate_milestone_claims,
            )

            reasoning = getattr(response_obj, "evidence_trail", None)
            validation_results = validate_milestone_claims(case, reviewable, reasoning)
            for result in validation_results:
                if not result.is_valid:
                    # Revert the milestone — evidence doesn't support the claim.
                    # Only the symptom claim can be here: step 1 records no
                    # other milestone, and the stage gates apply at step 5b.
                    if result.milestone == "symptom_verified":
                        unverify_problem(case, via="unsupported_claim")
                    if result.milestone in metadata["milestones_completed"]:
                        metadata["milestones_completed"].remove(result.milestone)
                    logger.warning(
                        f"Milestone '{result.milestone}' REVERTED: claimed with insufficient evidence "
                        f"({result.cited_count}/{result.expected_min} required). "
                        f"Warnings: {result.warnings}"
                    )
                    metadata.setdefault("milestone_validation_warnings", []).extend(
                        result.warnings
                    )

        # 2c. What the evidence says about the statement itself: inaccurate
        # (a revision for the user to re-confirm) or never present (a false
        # alarm), or a false-alarm finding the user disputes. After 2b, so the
        # turn's own verification is final and its evidence ids resolvable.
        self._apply_verification_updates(case, updates, metadata)

        # 2d. While a revision awaits re-confirmation, this turn's cause work
        # is held on it, with the evidence ids its refs resolve against, and
        # replayed through this same path when the user confirms. The cause
        # steps below then skip it.
        staging = cause_work_staged(case)
        if staging:
            self._stage_cause_work(case, updates, metadata)

        # 2e. Cause claims — the root-cause conclusion and the likelihood /
        # method the LLM attaches to it. Cause work is accepted only on a
        # verified problem (``cause_work_accepted``), and this is the first
        # point where the turn's verification is final: step 1 applied the
        # claim and step 2b reverted it if the cited evidence did not hold.
        # So a turn that verifies the symptom AND concludes the cause lands
        # both. Nothing between step 1 and here reads these fields.
        if not staging:
            self._apply_cause_claims(case, updates, metadata)

        # 3. Add/Update Hypotheses
        #
        # Hypotheses are formed only on a verified problem. ``cause_work_accepted``
        # reads the status the turn ENDS with — step 1 applied this turn's
        # symptom claim and step 2b reverted it if the cited evidence did not
        # hold — so a turn that verifies the symptom and proposes its cause
        # mints the hypotheses ACTIVE on the spot (the opportunistic flow).
        # On an unverified problem they are refused, not queued: the evidence
        # behind them is already recorded, and the pool evaluation links it to
        # the hypotheses once they form.
        #
        # ``hyp_emit_order`` is the positional list ``new_index_N`` refs resolve
        # against (INV-36). It mirrors ``hypotheses_generated`` per item EXCEPT
        # that a dedup skip records the CANONICAL existing id instead of a new
        # one, so downstream refs (evidence links, updates, need motivators)
        # that target a skipped duplicate resolve to the kept hypothesis rather
        # than shifting onto the wrong sibling. ``hypotheses_generated`` stays
        # truly-new so telemetry / turn-outcome progress do not count a dedup as
        # generation (a skip is not diagnostic progress — the DF-6 exhaustion
        # signal). A refused emission records nothing, so a ref to it resolves
        # to nothing and the link that names it is skipped.
        emit_order: list[str] = metadata.setdefault("hyp_emit_order", [])
        hypotheses_in = list(getattr(updates, "hypotheses_to_add", None) or [])
        if staging:
            hypotheses_in = []  # held at step 2d
        elif hypotheses_in and not cause_work_accepted(case):
            # Refused, not queued. Positions are kept so a ``new_index_N`` ref
            # this turn still means what the model meant: an item that restates
            # a hypothesis already standing (a standing one survives a
            # retraction) maps onto it, exactly as the dedup below would, and
            # links onto it still apply; any other slot resolves to nothing,
            # so a link naming it is skipped.
            refused = 0
            for h_item in hypotheses_in:
                dup_id = find_duplicate_hypothesis(h_item.statement, case)
                if dup_id is not None:
                    emit_order.append(dup_id)
                else:
                    emit_order.append(_REFUSED_HYPOTHESIS_SLOT)
                    refused += 1
            if refused:
                refuse_cause_work(
                    case.case_id,
                    metadata,
                    kind="hypothesis",
                    what=f"{refused} hypothesis(es)",
                    count=refused,
                )
            hypotheses_in = []
        if hypotheses_in:
            for h_item in hypotheses_in:
                # INV-36: a statement that duplicates a standing (non-terminal)
                # hypothesis is not minted a second time — duplicates spuriously
                # re-satisfy the ≥2-active work gate, corrupting the axis that
                # separates INSUFFICIENT_EVIDENCE from NOT_YET_PRODUCTIVE.
                # Terminal (refuted/retired) causes are NOT dedup targets, so a
                # revival re-enters the differential. Same-batch duplicates are
                # caught for free: a sibling minted earlier this turn is already
                # in ``case.hypotheses`` by the time the next item is checked.
                dup_id = find_duplicate_hypothesis(h_item.statement, case)
                if dup_id is not None:
                    emit_order.append(dup_id)
                    hypothesis_dedup_skipped_total.inc()
                    existing = case.hypotheses.get(dup_id)
                    _add_system_feedback(
                        metadata,
                        f"Hypothesis '{h_item.statement[:80]}' duplicates "
                        f"standing hypothesis {dup_id}"
                        + (
                            f" ('{existing.statement[:80]}')"
                            if existing is not None
                            else ""
                        )
                        + " and was not re-added. To revise it, update the "
                        "existing hypothesis (hypotheses_to_update) with new "
                        "evidence rather than restating it.",
                    )
                    logger.info(
                        "Deduped hypothesis (INV-36): '%s' matches standing %s",
                        h_item.statement[:60],
                        dup_id,
                    )
                    # A chain the LLM emitted for the duplicate is left to the
                    # orphan-chain post-pass (``resolve_orphan_chains``), which
                    # re-attaches it to a FLAT standing hypothesis under its own
                    # anti-clobber guard (``_hypothesis_lacks_real_chain``).
                    # Re-rooting the canonical here would BYPASS that guard and
                    # could GC a validated hypothesis's existing chain.
                    continue
                h = self.deps.hypothesis_manager.create_hypothesis(
                    statement=h_item.statement,
                    category=h_item.category,
                    initial_likelihood=h_item.likelihood,
                    current_turn=case.current_turn,
                )
                case.hypotheses[h.hypothesis_id] = h
                metadata["hypotheses_generated"].append(h.hypothesis_id)
                emit_order.append(h.hypothesis_id)
                # Record this hypothesis's chain-root ref keyed by its id, so
                # chain linking (when enabled) needs no positional zip against
                # the spec list — robust to any future skip/dedup here.
                if getattr(h_item, "root_node_ref", None):
                    metadata.setdefault("hyp_root_refs", {})[
                        h.hypothesis_id
                    ] = h_item.root_node_ref

        # 3b. Apply the LLM's hypothesis disconfirmation signal (state=REFUTED +
        # reason) and likelihood updates. Emitted by schema+prompt but never
        # applied before; connecting it lets M6 demotion fire on the LLM's own
        # refutation, not only on REFUTES evidence links. (Other state
        # transitions are intentionally deferred — see _apply_hypothesis_updates.)
        false_alarm = case.progress.problem_status == ProblemStatus.INVALIDATED
        if getattr(updates, "hypotheses_to_update", None) and false_alarm:
            # Nothing to refute or support against: the problem never existed.
            _add_system_feedback(
                metadata,
                "HYPOTHESIS UPDATES NOT ACCEPTED: the reported problem was found "
                "not present, so there is no cause to update hypotheses about.",
            )
        elif getattr(updates, "hypotheses_to_update", None):
            _apply_hypothesis_updates(
                self.deps.hypothesis_manager,
                case,
                updates.hypotheses_to_update,
                metadata,
                case.current_turn,
            )

        # 4. Link Evidence (Partial Application Check)
        # Note: Hypothesis-evidence linking is best-effort. The LLM may reference
        # evidence IDs that don't exist yet (timing issue), so we silently skip failed links.
        if getattr(updates, "hypothesis_evidence_links", None) and false_alarm:
            _add_system_feedback(
                metadata,
                "HYPOTHESIS EVIDENCE LINKS NOT ACCEPTED: the reported problem "
                "was found not present, so there is no cause to weigh evidence "
                "against.",
            )
        elif (
            not staging
            and hasattr(updates, "hypothesis_evidence_links")
            and updates.hypothesis_evidence_links
        ):
            _apply_hypothesis_evidence_links(
                self.deps.hypothesis_manager,
                case,
                updates.hypothesis_evidence_links,
                metadata,
            )

        # (Deferred likelihood updates are applied AFTER chain emission —
        # see the call beside _apply_chain_emission below: the B1 cap must
        # judge the hypothesis WITH the links this same turn carried on BOTH
        # axes, flat hypothesis_evidence_links AND chain node_evidence_links.)

        # 4b. Evidence Needs (Phase 3 of evidence-needs rollout)
        # Process LLM-emitted ``evidence_need_updates``. Runs AFTER
        # evidence_to_add (so ``metadata["evidence_added"]`` is populated
        # for ``new_index_N`` resolution on ``fulfilling_evidence_ids``)
        # and AFTER hypotheses_to_add (so ``metadata["hypotheses_generated"]``
        # is populated for ``new_index_N`` resolution on
        # ``motivating_hypothesis_ids``). Symptom-purpose needs are
        # always allowed; causal-purpose needs are rejected by the
        # path-conditional emission backstop (parallels the
        # causal_evidence rejection at lines ~5758+).
        if hasattr(updates, "evidence_need_updates") and updates.evidence_need_updates:
            _apply_evidence_need_updates(
                case=case,
                updates_list=updates.evidence_need_updates,
                metadata=metadata,
                current_turn=case.current_turn,
            )

        # 5. Solutions
        #
        # Redesign R5/§2: the former pre-path solutions ban is removed. There
        # is no path commit gate; solution/workaround proposals are allowed
        # opportunistically during INVESTIGATING.
        if getattr(updates, "solutions_to_add", None) and false_alarm:
            _add_system_feedback(
                metadata,
                "SOLUTIONS NOT ACCEPTED: the reported problem was found not "
                "present, so there is nothing to fix or mitigate.",
            )
        if (
            not staging
            and not false_alarm
            and hasattr(updates, "solutions_to_add")
            and updates.solutions_to_add
        ):
            for s_item in updates.solutions_to_add:
                # R9: causal-graph linkage carried by the emission (optional;
                # honor-or-reject). ``quadrant`` is recorded as DATA — the M5
                # downgrade below is unchanged. ``node_ref`` is kept only when it
                # resolves to a real node on this case's graph. Note: no forward-
                # looking "verification" is written to ``verification_method`` here
                # — that field means *how the fix WAS verified* (past tense, read by
                # the resolution report + resolution-confirmation gate), so writing
                # a proposed check into it would claim a verification that never
                # happened. The runbook's verification prose reaches the LLM via RAG.
                node_ref = getattr(s_item, "node_ref", None)
                node_id = node_ref if node_ref in case.causal_nodes else None
                sol = Solution(
                    solution_id=f"sol_{uuid4().hex[:12]}",
                    solution_type=s_item.solution_type,
                    title=f"Solution: {s_item.solution_type}",
                    immediate_action=s_item.description,
                    commands=s_item.commands or [],
                    risks=[s_item.risks] if s_item.risks else [],
                    node_id=node_id,
                    quadrant=_coerce_intervention_quadrant(
                        getattr(s_item, "quadrant", None)
                    ),
                    proposed_at=datetime.now(UTC),
                )
                # #1136: as for evidence above — computed BEFORE the append so
                # the row cannot match itself. ``solutions_proposed`` keeps every
                # minted id; only the progress signal narrows to NEW offers.
                restates = _restates_standing_solution(s_item, case)
                case.solutions.append(sol)
                metadata["solutions_proposed"].append(sol.solution_id)
                if not restates:
                    metadata.setdefault("novel_solutions_proposed", []).append(
                        sol.solution_id
                    )

                # Gap 0: Create ProposedAction for compliance detection chain
                action_type = _determine_action_type(case, s_item.solution_type)
                downgrade_reason: str | None = None

                # 3C / M5: Solution-validation gate — a SOLUTION (permanent fix)
                # requires the cause to be mechanistically validated, i.e.
                # cause_state == IDENTIFIED (some chain's root validated by
                # evidence — methodology M5 / §9.2). Proposing a permanent
                # remediation before the root is validated is the premature-fix /
                # diagnostic-test-recorded-as-a-solution failure M5 forbids.
                # Downgrade to DIAGNOSTIC and tell the LLM how to recover. This
                # subsumes the prior weaker "≥1 hypothesis" check (IDENTIFIED
                # implies hypotheses). Mitigation (WORKAROUND) is exempt by
                # design — it precedes a known root and is gated on symptom
                # evidence by 3D instead. Graceful denial (no stall): the flow
                # continues as DIAGNOSTIC; the LLM grounds the root and
                # re-proposes, or proposes a mitigation.
                if (
                    action_type == InvestigationActionType.SOLUTION
                    and not _solution_cause_validated(
                        case,
                        working_conclusion=_settled_working_conclusion(case, metadata),
                    )
                ):
                    logger.warning(
                        f"Downgrading SOLUTION to DIAGNOSTIC for case {case.case_id}: "
                        f"cause_state={case.progress.cause_state.value}, "
                        f"rcc={'set' if case.root_cause_conclusion else 'none'} "
                        f"(M5 — a permanent fix requires an established root cause)"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC
                    # Rendered on every turn this action stays pending, so it
                    # states what was true WHEN it was proposed, never the
                    # current state, and gives a recovery that works either
                    # way: an RCC licenses the fix once the symptom is verified
                    # and no rival cause is contested, whatever the other legs
                    # say (fm#1679). ``ProposedAction.downgrade_reason`` holds
                    # at most 500 characters.
                    downgrade_reason = (
                        "Downgraded from SOLUTION: when proposed, no root cause "
                        "was established; a permanent fix needs one, and a "
                        "diagnostic test is not a fix (M5). To register a real "
                        "fix, send in ONE response a root_cause_conclusion "
                        "backed by evidence and the fix as a SolutionToAdd; the "
                        "conclusion counts once the symptom is verified and no "
                        "rival cause is contested. If the user already applied "
                        "that fix, also set solution_accepted from their report "
                        "rather than asking again. Or propose a WORKAROUND "
                        "mitigation."
                    )

                # 3D: Symptom-evidence gate — MITIGATION requires at least one
                # SYMPTOM_EVIDENCE row on the case. The mitigation must target
                # an observed failure, not an unverified user claim. If no
                # SYMPTOM_EVIDENCE exists, downgrade to DIAGNOSTIC so the
                # mitigation milestone cannot fire on an ungrounded proposal.
                # The LLM receives the downgrade_reason in next-turn context
                # and can recover by gathering symptom data and re-proposing.
                # See Behavioral Rule 2 (Evidence-Grounded) and
                # investigation-lifecycle-logic.md §2.3 (minimum-evidence
                # discipline).
                #
                # Scope of this gate (what it does NOT do): the action's
                # ``description`` and ``commands`` are preserved verbatim
                # below — only ``action_type`` is rewritten. The user sees
                # the original proposal in the chat and may execute it.
                # The gate prevents the engine from REGISTERING the
                # mitigation (firing ``mitigation_accepted`` on the user's
                # subsequent compliance), not from the mitigation HAPPENING
                # in the user's environment. If the user runs the action
                # anyway, the LLM next turn sees both the downgrade_reason
                # and the user's report; the recovery is to file
                # retrospective SYMPTOM_EVIDENCE (from pre-mitigation logs
                # or the user's account of what changed) and then re-propose.
                # Forward-only semantics: an ungrounded mitigation that
                # quietly executes does not register; the case stays in
                # DIAGNOSIS until grounding catches up — which is the
                # correct outcome under "valid results when possible, no
                # false progress otherwise."
                if (
                    action_type == InvestigationActionType.MITIGATION
                    and not _case_has_symptom_evidence(case)
                ):
                    logger.warning(
                        f"Downgrading MITIGATION to DIAGNOSTIC for case {case.case_id}: "
                        f"no SYMPTOM_EVIDENCE exists yet"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC
                    downgrade_reason = (
                        "Your previous MITIGATION proposal was downgraded to "
                        "DIAGNOSTIC because no SYMPTOM_EVIDENCE existed on "
                        "the case. A mitigation must target an observed "
                        "failure, not an unverified user claim. Inspect the "
                        "case data (pod logs / status / metrics / config "
                        "snapshot), file SYMPTOM_EVIDENCE for what you find, "
                        "then re-propose the mitigation grounded in that "
                        "evidence."
                    )

                # INV-32 (#656 DF-3): a NEW permanent-fix offer replaces any
                # standing pending one — the newest proposal is THE offer
                # (the context builder and compliance detection already key
                # on the most recent pending action; without supersession the
                # stale siblings linger pending forever and keep the derived
                # solution_proposed latched). Runs BEFORE the append so the
                # new offer never supersedes itself.
                if action_type == InvestigationActionType.SOLUTION:
                    _supersede_pending_solution_offers(case, reason="reproposal")

                proposed_action = ProposedAction(
                    case_id=case.case_id,
                    action_type=action_type,
                    description=s_item.description,
                    commands=s_item.commands or [],
                    proposed_in_turn=case.current_turn,
                    downgrade_reason=downgrade_reason,
                )
                case.proposed_actions.append(proposed_action)

                # solution_proposed is DERIVED at the end-of-turn assessment
                # recompute from live SOLUTION offers (INV-32) — the former
                # 3F write-once set here is gone; this new pending offer
                # flips the indicator True in the same turn via the
                # derivation.

        # 5b. Stage-gate compliance signals (Framework §4.1) — AFTER the
        # solutions step so the guards see actions created this turn (the
        # prompt's KB-resolution flow emits SolutionToAdd + solution_accepted
        # in ONE response; see _apply_stage_gate_signals).
        if updates.milestones:
            gate_signals = updates.milestones
            held = metadata.get("staged_gate_signals")
            if held:
                # Held with the solutions they accept (step 2d).
                gate_signals = gate_signals.model_copy(
                    update={name: None for name in held}
                )
                _add_system_feedback(
                    metadata,
                    f"{', '.join(held)} and the solutions this response proposed "
                    "are held until the user confirms the revised problem "
                    "statement, then applied under the same gates as any "
                    "proposal (a permanent fix still needs an established "
                    "cause). Do not re-propose them.",
                )
            _apply_stage_gate_signals(case, gate_signals, user_message, metadata)

        # 6. Journal Entries (append-only investigation memory)
        if hasattr(updates, "journal_entries") and updates.journal_entries:
            for je_item in updates.journal_entries:
                entry = JournalEntry(
                    turn=case.current_turn,
                    entry_type=je_item.entry_type,
                    content=je_item.content[:200],
                    evidence_id=je_item.evidence_id,
                    hypothesis_id=je_item.hypothesis_id,
                )
                case.investigation_journal.append(entry)
            logger.info(
                f"Case {case.case_id}: added {len(updates.journal_entries)} journal entries "
                f"(total: {len(case.investigation_journal)})"
            )

        # Populate the causal graph from the LLM's emitted chain (lazy backward
        # expansion), then resolve any chain the LLM left unlinked. The graph is
        # always populated from the emitted chain; cause_state/M6 derive from the
        # real emitted chains. (cause_state derivation never reads the graph for
        # truth — see _recompute_assessment_state.)
        _apply_chain_emission(case, updates, metadata)
        # Orphan-chain resolution (B2c invariant: every chain explaining D is
        # attached to exactly one hypothesis). T1 re-attaches an unambiguous
        # double-representation in place; any ambiguous orphan is surfaced to
        # the LLM as a one-turn nudge (T2a) to re-root it or declare it
        # separate, rather than guessing.
        _nudge_ambiguous_orphan_chains(case, metadata)
        # One note for everything refused this turn (cause_work.py).
        report_refused_cause_work(metadata)

        # Deferred likelihood updates — applied AFTER both link passes (flat
        # step 4 AND chain emission above), so the B1 evidence-free cap judges
        # the hypothesis with everything this turn's emission grounded it on;
        # a chain-contract turn (record -> node-link -> set likelihood) must
        # not be capped and gaslit for links it did emit.
        _apply_deferred_likelihood_updates(
            self.deps.hypothesis_manager, case, metadata, case.current_turn
        )

        # Recompute engine-owned assessment vars (cause_state / solution_state)
        # now that this turn's hypotheses and solutions are applied (redesign R1).
        # Pass this turn's LLM-certified deductive survivors (resolved in
        # _apply_chain_emission) so proof-by-exclusion can stamp them post-derive.
        # Provider identity for the DF-6 provider-floor metric (INV-39), passed
        # explicitly (not smuggled through the shared metadata dict). Resolved via
        # the helper because self.llm_provider is the LLMRouter in the real
        # deployment (no provider_name) — the helper reads the configured chat
        # provider off it; a partially constructed engine (some fixtures omit
        # llm_provider) degrades to "unknown" rather than raising.
        prior_cause_state = _recompute_assessment_state(
            case,
            exclusion_survivors=metadata.get("deductive_survivor_ids", frozenset()),
            rcc_authored_this_turn=metadata.get("rcc_authored_this_turn", False),
            metadata=metadata,
            provider_name=_resolve_chat_provider_name(self.deps.llm_provider),
        )

        # KB-remediation pre-fetch on the cause_state→IDENTIFIED edge (INV-35):
        # cause_state is engine-derived above, so this warm-up fires the turn it
        # newly crosses to IDENTIFIED (in-flight diagnosis — terminal recompute
        # paths deliberately do not warm KB, the fix has already happened).
        _kb_query = _kb_prefetch_query_on_identification(
            prior_cause_state,
            case.progress.cause_state,
            case.root_cause_conclusion,
            case.working_conclusion,
        )
        if _kb_query:
            await self.kb_prefetcher.prefetch_kb_context(case, _kb_query, "root_cause")

        # Deferred-implementation disposition: if the fix is known but can't be
        # applied this session, propose CLOSE-with-documented-solution (§3.1 row 3).
        # Not inside a staged-work replay: there the offer would be executed by
        # the user's "yes" to the revised statement (REPLAY_METADATA_KEY). The
        # live turn that follows the replay runs these with its own metadata.
        if not metadata.get(REPLAY_METADATA_KEY):
            _maybe_propose_deferred_close(case, metadata)
            # False alarm: offer the close the finding calls for (once).
            _maybe_propose_false_alarm_close(case, metadata)

        # Bug #4: Evidence-Milestone Linking (Moved here to ensure evidence exists)
        # LLM-claimed milestones only. This runs AFTER the assessment recompute,
        # so the turn's list may also carry the engine's own
        # ``root_cause_identified`` (#1284) — and the attribution is blanket, so
        # it would stamp that onto every evidence row added this turn regardless
        # of category, including SYMPTOM and DOCUMENT rows. That is exactly the
        # attribution INV-35 removed from CATEGORY_MILESTONE_MAP: identification
        # is earned by the causal chain, not by whatever arrived the same turn.
        attributable = llm_claimable_milestones(metadata["milestones_completed"])
        if attributable and metadata["evidence_added"]:
            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    ev.advances_milestones.extend(attributable)

        # Bug #8: Robust Turn Outcome Determination
        metadata["outcome"] = _determine_turn_outcome(case, metadata, updates.outcome)
