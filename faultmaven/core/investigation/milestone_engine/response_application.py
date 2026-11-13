"""Applying a parsed structured LLM response onto a case: the INQUIRY and INVESTIGATING state-update paths that turn schema fields into case mutations."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from faultmaven.core.investigation.causal_graph.similarity import (
    find_duplicate_hypothesis,
)
from faultmaven.core.investigation.cause_assurance import (
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
)
from faultmaven.core.investigation.confidence_repair import short_repr
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
    record_root_verdict,
    resolve_root_ref,
)
from faultmaven.core.investigation.milestone_engine.evidence_need_updates import (
    _apply_evidence_need_updates,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_deferred_likelihood_updates,
    _apply_hypothesis_evidence_links,
    _apply_hypothesis_updates,
    _apply_pending_refutations,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    gate1_bare_consent,
    revision_offer_key,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    REFUSED_EVIDENCE_SLOT,
    _determine_turn_outcome,
    _report_turn_uploads,
    _resolve_id_ref,
)
from faultmaven.core.investigation.narration.codes import NoticeCode
from faultmaven.core.investigation.offers import answer_offer, open_terminal_offer
from faultmaven.core.investigation.problem_status import (
    cause_work_accepted,
    cause_work_staged,
    hold_premature_absence,
    invalidate_problem,
    invalidation_refusal,
    is_engine_false_alarm_close,
    is_false_alarm_close,
    pending_revision,
    propose_revision,
    record_verifying_rows,
    restore_premature_absence,
    revision_refusal,
    stage_cause_work,
    unverify_problem,
    verify_problem,
    withdraw_invalidation,
)
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    MilestoneUpdates,
    TerminalResponse,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_ROW_REFUSED_TOTAL,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    Case,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    FixCause,
    HypothesisState,
    InvestigationActionType,
    JournalEntry,
    KnowledgeMatch,
    NodeType,
    ProblemStatus,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionFeasible,
    SolutionType,
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
    is_identification_edge,
)
from .milestone_inference import (
    _apply_symptom_retraction,
    _evidence_coverage,
    _infer_milestones,
    _post_process_llm_response,
    _source_file_resolves,
    fix_acceptance_justification_error,
    validate_reasoning_first,
)
from .stage_gates import (
    CARRIED_FIX_KEY,
    FIX_JUSTIFICATION_KEY,
    FIXES_ACCEPTED_KEY,
    _add_system_feedback,
    _apply_stage_gate_signals,
    _case_has_symptom_evidence,
    _coerce_intervention_quadrant,
    _determine_action_type,
    _settled_likelihoods,
    _settled_working_conclusion,
    _supersede_pending_solution_offers,
    add_evidence_validation_feedback,
    fix_cause,
    fix_identity,
    licence_leg_ids,
    licence_subject,
    llm_claimable_milestones,
    record_decision,
    solution_licence_leg,
    standing_fix_action,
)
from .statement_revision import (
    REPLAY_FIX_SLOTS_KEY,
    REPLAY_METADATA_KEY,
    emission_turn,
)
from .terminal_proposals import (
    _maybe_propose_deferred_close,
    _maybe_propose_false_alarm_close,
)

logger = logging.getLogger(__name__)


#: M5's recovery when the cause holds but its why is open (#2036). The
#: conclusion backstop does not license a fix then, so the generic recovery
#: would send the model round a loop. At most 500 characters
#: (``ProposedAction.downgrade_reason``).
WHY_OPEN_DOWNGRADE_REASON = (
    "Downgraded from SOLUTION: when proposed, the cause had validated but its "
    "why was open (ROOT-CAUSE TEST, clause b), so a fix acting on it would only "
    "intercept a state that comes back. Ask for what produced the state, then "
    "re-root onto that cause or mark the root terminal (root_is_terminal true, "
    "citing the causal_evidence row in terminal_basis_evidence_ids) and "
    "re-propose the fix. To restore service now, propose it as a WORKAROUND "
    "mitigation."
)


#: The rows ``_reclassify_premature_causal_absence`` held this turn, which its
#: final call reports.
_PREMATURE_ABSENCE_KEY = "premature_absence_held"


def _reclassify_premature_causal_absence(
    case: Case, metadata: dict[str, Any], *, final: bool = False
) -> None:
    """A cause cannot have been eliminated before the problem it caused is
    verified: a causal_absence row then means "the problem is not there", which
    is symptom_absence. This turn's rows are held as symptom_absence rather
    than dropped (``problem_status.hold_premature_absence``) — new_index_N refs
    this turn index into ``evidence_slots``, and a dropped row would leave its
    ref naming nothing. Without this, one such row reads resolution-READY on a
    never-verified case (``assess_resolution_readiness``) and pivots a
    false-alarm close to RESOLVED (INV-37).

    Judged on the status the turn ends with (the ``final`` call, after step
    2c), never the status it began with or an optimistic claim 2b reverts: a
    row the call before 2c held is restored when the turn ends verified (a
    false-alarm finding withdrawn). A row held while a revised statement waits
    is judged again when the user answers it (#2550): restored when the answer
    leaves the problem verified (``problem_status.commit_revision``,
    ``_answer_pending``)."""
    created = set(metadata.get("evidence_added") or [])
    rows = [e for e in case.evidence if e.evidence_id in created]
    if cause_work_accepted(case):
        restore_premature_absence(rows)
        metadata.pop(_PREMATURE_ABSENCE_KEY, None)
        return
    held = metadata.setdefault(_PREMATURE_ABSENCE_KEY, [])
    for row in rows:
        if row.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:
            hold_premature_absence(row)
            held.append(row.evidence_id)
    if not final:
        return
    metadata.pop(_PREMATURE_ABSENCE_KEY)
    if not held:
        return
    metadata.setdefault("validation_repairs", []).append(
        f"{len(held)} causal_absence_evidence row(s) recorded before the problem "
        "was verified were recorded as symptom_absence_evidence"
    )
    _add_system_feedback(
        metadata,
        f"{len(held)} causal_absence_evidence row(s) arrived before the problem "
        "was verified and were recorded as symptom_absence_evidence: a cause can "
        "only be shown eliminated once the problem it caused is verified."
        + (
            " They count as causal_absence_evidence if the user's answer to the "
            "revised statement leaves the problem verified."
            if cause_work_staged(case)
            else ""
        ),
    )


def _record_rechecks(case: Case, items: list, slots: list[str]) -> None:
    """Store what each of this response's rows re-observes (#1906 D1), once
    every row has its slot, so a forward ``new_index_N`` resolves. A ref that
    names no row of the case, a refused slot, the row itself or an absence row
    (absence rows carry no links, so nothing binds through one) is stored
    unbound. ``slots`` is ``metadata["evidence_slots"]``, positional with
    ``items``."""
    rows = {e.evidence_id: e for e in case.evidence}
    for item, slot in zip(items, slots):
        row = rows.get(slot)
        if row is None or not getattr(item, "rechecks", None):
            continue
        target = _resolve_id_ref(item.rechecks, slots, "ev_")
        named = rows.get(target)
        bound = (
            named is not None
            and named is not row
            and named.category
            not in (
                EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            )
        )
        row.record_rechecks(target if bound else None, named=str(item.rechecks))


def _record_verifying_rows(case: Case, updates: Any, metadata: dict[str, Any]) -> None:
    """Step 2e: the rows that showed the problem (#1906 D1), once this turn's
    verification is final.

    On the verifying EDGE (the symptom claim survived step 2b and 2c): the
    claim's cited ``symptom_verification_evidence_ids``; when it cites none,
    the single symptom_evidence row the turn wrote, only when there is exactly
    one — the claim is checked by category (``validate_milestone_claims``), so
    reading more would re-import that proxy.

    On a later turn (ruled 2026-11-03): an explicit citation fills an EMPTY
    list, never a non-empty one, and with no fallback."""
    if case.progress.problem_status != ProblemStatus.VERIFIED:
        return
    pv = case.problem_verification
    m = getattr(updates, "milestones", None)
    slots = metadata.get("evidence_slots", [])
    cited = [
        _resolve_id_ref(ref, slots, "ev_")
        for ref in (getattr(m, "symptom_verification_evidence_ids", None) or [])
    ]
    if "symptom_verified" in metadata["milestones_completed"]:
        if not cited:
            written = set(metadata.get("evidence_added") or [])
            cited = [
                e.evidence_id
                for e in case.evidence
                if e.evidence_id in written
                and e.category == EvidenceCategory.SYMPTOM_EVIDENCE
            ]
            cited = cited if len(cited) == 1 else []
        record_verifying_rows(case, cited)
    elif pv is not None and not pv.verifying_evidence_ids and cited:
        record_verifying_rows(case, cited)


#: Each fix entry of this apply with a ``node_ref``, as ``(ref, solution_id,
#: action_id)`` (``action_id`` None when it names no SOLUTION's target).
FIX_NODE_REFS_KEY = "fix_node_refs"
#: ``fix_cause`` at the start of this apply (#2036 D2.3): the turn record's
#: ``cause_at_start`` on a live apply; a replayed bundle's own, not written back.
FIX_CAUSE_AT_START_KEY = "fix_cause_at_start"
#: Set where this apply ran the assessment recompute: the turn's final
#: recompute runs only then (#2636, ``turn_application._final_recompute``).
ASSESSMENT_RECOMPUTED_KEY = "assessment_recomputed"
#: ``cause_state`` captured before step 1's recompute (on a revision-confirm
#: turn, after the replay): the turn's identification edge is measured from it
#: (``ResponseApplier.recompute_assessment``).
CAUSE_STATE_AT_START_KEY = "cause_state_at_start"
#: The SOLUTIONs this apply minted, as ``(action_id, leg, subject)``: the leg
#: M5 licensed each on (``cause_identification_leg``) and what it read there
#: (``licence_subject``), taken at the mint.
FIXES_MINTED_KEY = "fixes_minted_this_apply"


def _record_licences(case: Case, metadata: dict[str, Any]) -> None:
    """Record, once, the licence of each SOLUTION this apply minted
    (``ProposedAction.licence_ids``): what M5 licensed it on at the mint,
    resolved to its root(s) after this apply's chain emission. A
    ``new_index_N`` the conclusion names resolves as chain emission resolves
    the conclusion's (``resolve_root_ref``: a ROOT, else none), so the
    licence reads what the stored conclusion names. Nothing is re-scored
    here. Never changed after."""
    created = metadata.get("chain_nodes_created") or []
    actions = {a.action_id: a for a in case.proposed_actions}
    for action_id, leg, subject in metadata.get(FIXES_MINTED_KEY) or []:
        action = actions.get(action_id)
        if action is None or action.action_type != InvestigationActionType.SOLUTION:
            continue
        if leg == "rcc" and (subject or (None, None))[1]:
            hypothesis_id, named = subject
            if named.startswith("new_index_"):
                subject = (hypothesis_id, resolve_root_ref(case, named, created))
        action.licence_ids, action.licence_in_words = licence_leg_ids(
            case, leg, subject
        )


def _run_turn_cause(case: Case, metadata: dict[str, Any], turn: int) -> FixCause | None:
    """The ``FixCause`` recorded for ``turn``, the turn a fix ran in: this
    apply's (``FIX_CAUSE_AT_START_KEY``) for its own turn, a replayed
    bundle's included; else the turn record's ``cause_at_start``; None when
    that turn has none."""
    if turn == emission_turn(case, metadata):
        return metadata.get(FIX_CAUSE_AT_START_KEY)
    record = next(
        (t for t in reversed(case.turn_history or []) if t.turn_number == turn), None
    )
    return getattr(record, "cause_at_start", None)


def _unnamed_target(
    cause: FixCause, action: ProposedAction, same_turn: bool
) -> list[str] | None:
    """An unnamed fix's target, from records only (#2036 D2.3): the run
    turn's identified or count-held roots; else, proposed before its run
    turn, its own licence, then the run turn's licence leg; proposed in it,
    the run turn's licence leg (what the user ran it against), then its own
    licence. None, with no roots, for a fix whose licence was never recorded
    (proposed before D2.3): it stays unkinded, read as before."""
    if cause.root_ids:
        return list(cause.root_ids)
    if action.licence_ids is None:
        return None
    first, then = (
        (cause.licence_ids, action.licence_ids)
        if same_turn
        else (action.licence_ids, cause.licence_ids)
    )
    return list(first) or list(then)


def _settle_fix_attempts(case: Case, metadata: dict[str, Any]) -> None:
    """What each fix acts on, read from records once per apply, right after
    chain emission and before the orphan pass and the cause recompute, so M6
    reads settled windows (#2036 D2.3).

    1. A ``node_ref`` this apply sent resolves through the turn's created
       nodes and sets its ``Solution.node_id``. On a SOLUTION not yet named or
       run it is the named target, never changed after; a ref naming no node
       leaves the fix unnamed, with one line.
    2. Each SOLUTION this apply minted records its licence
       (``_record_licences``).
    3. Each SOLUTION accepted in this apply is kinded against the cause
       recorded for the turn it ran in (``_run_turn_cause``): the acceptance's
       turn, or the one it is backdated to (#2427). No record, or an unnamed
       fix with no recorded licence and no root: unkinded, read as before
       D2.3. Unnamed, it takes ``_unnamed_target``, where
       "proposed in its run turn" means minted in this apply. A remediation
       when its targets meet that record's ``reference_ids`` or a ROOT its
       own licence names (an intermediate one never), else an interception;
       a target id no longer on the graph is then dropped, the kind kept.
       With no target and a licence naming its cause in words only:
       unkinded, and the model is asked for the cause's node (ruling 2).
       Nothing here reads the cause as this apply leaves it."""
    created = metadata.get("chain_nodes_created") or []
    actions = {a.action_id: a for a in case.proposed_actions}
    solutions = {sol.solution_id: sol for sol in case.solutions}
    for ref, solution_id, action_id in metadata.get(FIX_NODE_REFS_KEY) or []:
        node_id = _resolve_id_ref(ref, created, "cn_")
        if node_id not in case.causal_nodes:
            _add_system_feedback(
                metadata,
                f"SYSTEM: node_ref {ref} names no node on the causal graph, so "
                "that fix names no target: when the user runs it, it acts on the "
                "cause identified then. Name a cn_... id from the graph, or "
                "new_index_N for a node emitted this turn.",
            )
            continue
        if solution_id in solutions:
            solutions[solution_id].node_id = node_id
        action = actions.get(action_id)
        if action is not None and not action.target_named and action.fix_kind is None:
            action.target_ids, action.target_named = [node_id], True
    _record_licences(case, metadata)
    minted = {entry[0] for entry in metadata.get(FIXES_MINTED_KEY) or []}
    for action_id in metadata.get(FIXES_ACCEPTED_KEY) or []:
        action = actions.get(action_id)
        if action is None or action.fix_kind is not None:
            continue
        cause = _run_turn_cause(case, metadata, action.accepted_in_turn)
        if cause is None:
            continue
        same_turn = action_id in minted
        # Only a ROOT the licence names joins the reference: a fix at an
        # intermediate node is never a remediation.
        own = {
            node_id
            for node_id in action.licence_ids or ()
            if node_id in case.causal_nodes
            and case.causal_nodes[node_id].node_type == NodeType.ROOT
        }
        if not action.target_named:
            target = _unnamed_target(cause, action, same_turn)
            if target is None:
                continue
            if not target and (cause.in_words or action.licence_in_words):
                record_decision(
                    metadata, NoticeCode.FIX_UNTARGETED, action_id=action_id
                )
                continue
            action.target_ids = target
        action.fix_kind = (
            FIX_REMEDIATION
            if (set(cause.reference_ids) | own).intersection(action.target_ids)
            else FIX_INTERCEPTION
        )
        # After kinding, every id off the graph goes, whether pruned after
        # the run turn's record named it or left in an unkinded fix's targets
        # by pruning. The kind stands.
        action.target_ids = [t for t in action.target_ids if t in case.causal_nodes]


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
#: ``solution_accepted`` is not among them: the stage gate decides it on the
#: hold turn and carries its decision by fix identity (#2105). The disposition
#: is held by its value (#2100).
_STAGED_GATE_SIGNALS = (
    "mitigation_accepted",
    "mitigation_verified",
    "mitigation_disposition",
)
#: Every compliance signal, refused together on a problem found never present.
_GATE_SIGNALS = ("solution_accepted", *_STAGED_GATE_SIGNALS)

#: What a refused hypothesis's ``new_index_N`` slot resolves to: no hypothesis,
#: so a link or update naming it is skipped by its consumer's existence check.
_REFUSED_HYPOTHESIS_SLOT = "refused_before_verification"


class ResponseApplier:
    """Applies a structured LLM response's state updates onto a case, dispatching to the INQUIRY or INVESTIGATING update path by the case's current state."""

    def __init__(self, *, deps, kb_prefetcher) -> None:
        self.deps = deps
        self.kb_prefetcher = kb_prefetcher

    async def recompute_assessment(
        self, case: Case, metadata: dict[str, Any], *, final: bool = False
    ) -> None:
        """Recompute the engine-owned assessment and react to the identification
        edge: step 1's call, and the turn's final recompute (``final``, #2636).

        The edge is measured from the turn's start (``CAUSE_STATE_AT_START_KEY``),
        so ``root_cause_identified`` records the net start-to-end
        identification edge: a turn that dips at step 1 and recovers records
        none. The final recompute removes the milestone only on a falling edge
        across itself, so a replay's milestone, folded in by
        ``merge_statement_commit``, is kept, as ``main`` keeps it. The
        KB-remediation pre-fetch (INV-35; terminal recompute paths deliberately
        do not warm KB) fires at most once per turn, on the first recompute
        that crosses to IDENTIFIED from an unidentified start; step 1's can
        fire on a turn the final recompute then un-identifies. The final
        recompute also drops from ``hypotheses_validated`` what the cycle ends
        not VALIDATED and re-derives the outcome when the milestones moved;
        its flags replace step 1's, and it re-runs no per-evaluation counter or
        trace. This turn's deductive survivors feed proof-by-exclusion. The
        DF-6 provider (INV-39) is resolved off the engine's LLM provider, the
        router in a real deployment ("unknown" when a fixture omits it)."""
        start = metadata.setdefault(CAUSE_STATE_AT_START_KEY, case.progress.cause_state)
        metadata[ASSESSMENT_RECOMPUTED_KEY] = True
        milestones = metadata.setdefault("milestones_completed", [])
        milestones_before = list(milestones)
        before = _recompute_assessment_state(
            case,
            exclusion_survivors=metadata.get("deductive_survivor_ids", frozenset()),
            rcc_authored_this_turn=(
                not final and metadata.get("rcc_authored_this_turn", False)
            ),
            metadata=metadata,
            provider_name=_resolve_chat_provider_name(self.deps.llm_provider),
            final=final,
            edge_from=start,
        )
        after = case.progress.cause_state
        if final:
            identified = CauseState.IDENTIFIED
            if before == identified != after and "root_cause_identified" in milestones:
                milestones.remove("root_cause_identified")
            metadata["hypotheses_validated"] = [
                h_id
                for h_id in metadata.get("hypotheses_validated") or []
                if getattr(case.hypotheses.get(h_id), "state", None)
                == HypothesisState.VALIDATED
            ]
            if milestones != milestones_before:
                # The reported outcome is not read (``_determine_turn_outcome``).
                metadata["outcome"] = _determine_turn_outcome(
                    case, metadata, TurnOutcome.CONVERSATION
                )
            if not is_identification_edge(start, after):
                return
        query = _kb_prefetch_query_on_identification(
            before, after, case.root_cause_conclusion, case.working_conclusion
        )
        if query:
            await self.kb_prefetcher.prefetch_kb_context(case, query, "root_cause")

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
            "evidence_slots": [],
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
        repairs = getattr(response_obj, "_confidence_repairs", [])
        confidence_notes = [repair.note() for repair in repairs]
        if confidence_notes:
            metadata["validation_repairs"] = confidence_notes
        for repair in repairs:
            # The one dropped value the model is told about, as every ignored
            # declaration is (#2427).
            if repair.field == "accepted_fix_reported_in":
                _add_system_feedback(
                    metadata,
                    f"SYSTEM: accepted_fix_reported_in {short_repr(repair.raw)} "
                    "was ignored: it is this_message or previous_message; the "
                    "fix is dated to this turn.",
                )
            elif repair.field == "mitigation_disposition":
                _add_system_feedback(
                    metadata,
                    f"SYSTEM: mitigation_disposition {short_repr(repair.raw)} "
                    "was ignored: it is declared_stable or proceed_unmitigated.",
                )

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
        # Whether this response could justify a solution_accepted. The stage
        # gate decides whether its acceptance registers a fix, and asks for the
        # justification exactly then (#2105).
        metadata[FIX_JUSTIFICATION_KEY] = fix_acceptance_justification_error(
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
            add_evidence_validation_feedback(
                metadata, not_recorded=stripped, errors=validation_errors
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
        # This populates InquiryData.knowledge_matches.
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

    def _apply_verification_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        *,
        status_at_start: ProblemStatus,
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

        # Positional, holes kept: a refused row keeps its slot (#1930).
        created = metadata.get("evidence_slots", [])

        def _ids(refs) -> list[str]:
            return [_resolve_id_ref(r, created, "ev_") for r in (refs or [])]

        revision = (vu.revised_problem_statement or "").strip()
        invalidated = vu.problem_invalidated is True
        # The symptom claim verified it this turn, which contradicts a
        # false-alarm finding.
        verified_this_turn = (
            status_at_start != ProblemStatus.VERIFIED
            and case.progress.problem_status == ProblemStatus.VERIFIED
        )

        if vu.invalidation_withdrawn is True:
            if (vu.withdrawal_basis or "").strip() and withdraw_invalidation(
                case, basis=vu.withdrawal_basis
            ):
                self._withdraw_false_alarm_close(case, metadata)
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
                    if open_terminal_offer(case)
                    else invalidation_refusal(
                        case, _ids(vu.invalidation_evidence_ids), vu.invalidation_basis
                    )
                )
            )
            if refusal:
                record_decision(
                    metadata, NoticeCode.FALSE_ALARM_REFUSED, reason=refusal
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

        held = pending_revision(case)
        if revision and held is not None and revision == held.text.strip():
            # The wording already awaiting the user, sent again: nothing moves,
            # and it must not read as progress on a turn spent waiting.
            return

        if revision:
            key = revision_offer_key(revision)
            pending = open_terminal_offer(case)
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
                record_decision(metadata, NoticeCode.REVISION_REFUSED, reason=refusal)
                return
            if pending:
                self._withdraw_false_alarm_close(case, metadata)
            if "symptom_verified" in metadata["milestones_completed"]:
                # The cited claim verified the revised problem, so it is
                # granted on confirmation. A runbook fix the user confirmed
                # stands on its own: a decline returns to it (verified).
                unverify_problem(case, via="superseded_by_revision")
                metadata["milestones_completed"].remove("symptom_verified")
            propose_revision(
                case,
                text=revision,
                evidence_ids=_ids(vu.revision_evidence_ids),
                basis=vu.revision_basis,
                wording_key=key,
            )
            metadata["problem_status_changed"] = True
            metadata["revision_proposed_this_turn"] = True

    @staticmethod
    def _withdraw_false_alarm_close(case: Case, metadata: dict[str, Any]) -> None:
        """Take back an open false-alarm close, whoever opened it: the finding
        it rested on no longer stands, or a revision takes its place (INV-45).
        Withdrawn by the engine, not declined: nothing binds."""
        offer = open_terminal_offer(case)
        if is_false_alarm_close(offer):
            case.offer_record = answer_offer(
                case,
                proposal_id=offer.proposal_id,
                answer=AnswerKind.WITHDRAWN,
                via=AnswerVia.ENGINE,
            )

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
        # Refutations are cause work too: one stands only on data linked to
        # it, and the links are staged above, so it is held with them and
        # judged on the replay (#1911). Likelihood updates still apply live.
        refutations = [
            item.model_dump(mode="json")
            for item in getattr(updates, "hypotheses_to_update", None) or []
            if item.state == HypothesisState.REFUTED
            and (item.refutation_reason or "").strip()
        ]
        if refutations:
            subset["hypotheses_to_update"] = refutations
        # The same fix re-sent on a later hold turn is staged once.
        if "solutions_to_add" in subset:
            staged_before = {
                fix_identity(item.get("description"))
                for bundle in pending_revision(case).staged
                for item in bundle.updates.get("solutions_to_add", [])
            }
            fresh = [
                item
                for item in subset["solutions_to_add"]
                if fix_identity(item.get("description")) not in staged_before
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
                    if getattr(milestones, name, None) not in (None, False)
                ]
                claims.update({name: getattr(milestones, name) for name in held})
                if held:
                    metadata["staged_gate_signals"] = held
            if claims:
                subset["milestones"] = claims
        if not subset:
            return
        stage_cause_work(
            case, updates=subset, evidence_added=metadata.get("evidence_slots", [])
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
        status_at_start = case.progress.problem_status
        # The cause a fix run in this turn acts on (#2036 D2.3): the turn
        # record's ``cause_at_start`` on a live apply; a replayed bundle's own.
        metadata[FIX_CAUSE_AT_START_KEY] = fix_cause(case)
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
            _add_system_feedback(metadata, blocker_msg)
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
            # step (one response may carry SolutionToAdd + solution_accepted)
            # — they are applied by ``_apply_stage_gate_signals`` AFTER step
            # 5 below.
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
            # Evidence is submitted data (#1930): every EvidenceToAdd names
            # the file its extract came from (the schema requires it), and a
            # row whose file is not one of this case's uploads is refused
            # here. There is no turn-file fallback: guessing a file would
            # attribute the extract to data it may not come from.
            #
            # Redesign R5/§2: the former path-conditional causal_evidence ban
            # is removed. Whether RCA-side work runs is decided by the prompt
            # (gated on cause uncertainty), not by an engine emission ban —
            # causal_evidence is always allowed during INVESTIGATING.
            for ev_item in updates.evidence_to_add:
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
                # before it aborts the turn at save: refuse the row, never keep
                # it with no file.
                source_file_id = ev_item.source_file_id
                if not _source_file_resolves(case, source_file_id):
                    EVIDENCE_ROW_REFUSED_TOTAL.labels(reason="unresolved_file").inc()
                    metadata["evidence_slots"].append(REFUSED_EVIDENCE_SLOT)
                    continue
                source_type = EvidenceSourceType(ev_item.source_type.value)

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
                    # The ACTOR (ADR-020 D6): only the case's effective
                    # driver may submit a turn (``_verify_access_and_reserve``),
                    # so on this turn the driver is who supplied the evidence
                    # — not the creator, once the two differ.
                    collected_by=case.effective_driver_id,
                    collected_at_turn=case.current_turn,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                    coverage_start_ts=coverage_start,
                    coverage_end_ts=coverage_end,
                    coverage_source=coverage_source,
                )
                # #1136: does this row carry a datum the case did not already
                # hold? Computed BEFORE the append, or the row would match
                # itself. ``evidence_added`` keeps every minted id (milestone
                # attribution and coverage resolve against it, and
                # ``evidence_slots`` keeps their positions for ``new_index_N``
                # refs); only the progress signal narrows.
                restates = _restates_standing_evidence(ev_item, case)
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                metadata["evidence_slots"].append(ev.evidence_id)
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
            _record_rechecks(case, updates.evidence_to_add, metadata["evidence_slots"])

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

        # The verification is final for the symptom claim: a causal_absence row
        # recorded on a problem still unverified is held as symptom_absence
        # before step 2c reads the turn's rows (a false-alarm finding may cite
        # it). The call after 2c judges it on the status the turn ends with.
        _reclassify_premature_causal_absence(case, metadata)

        # 2c. What the evidence says about the statement itself: inaccurate
        # (a revision for the user to re-confirm) or never present (a false
        # alarm), or a false-alarm finding the user disputes. After 2b, so the
        # turn's own verification is final and its evidence ids resolvable.
        self._apply_verification_updates(
            case, updates, metadata, status_at_start=status_at_start
        )
        # And again for the status the turn ends with: a turn that ends verified
        # (a withdrawn finding) restores what the call above held; a revision
        # holds the rows until the user's answer judges them again.
        _reclassify_premature_causal_absence(case, metadata, final=True)

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
        # And the rows that showed the problem, on the same final status.
        _record_verifying_rows(case, updates, metadata)

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
                # Posited in its emission's turn (#2537); its progress stamps
                # stay this turn's: it could not be worked while staged.
                h.generated_at_turn = emission_turn(case, metadata)
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
                record_root_verdict(metadata, h.hypothesis_id, h_item)

        # 3b. Apply the LLM's hypothesis disconfirmation signal (state=REFUTED +
        # reason) and likelihood updates. Emitted by schema+prompt but never
        # applied before; connecting it lets M6 demotion fire on the LLM's own
        # refutation, not only on REFUTES evidence links. A refutation is only
        # queued here and judged after the link passes, on the data on record
        # (#1911). (Other state transitions are intentionally deferred — see
        # _apply_hypothesis_updates.)
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
                staging=staging,
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
        # The action each solutions_to_add entry minted or kept, in order:
        # what the stage gate resolves this response's ``new_index_N`` against;
        # and those minted from fix entries, which alone can bear a fix (#2105),
        # across every bundle of a replay.
        solution_slots: list[ProposedAction] = []
        fix_slots: list[ProposedAction] = metadata.get(REPLAY_FIX_SLOTS_KEY, [])
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
                # resolves to a real node on this case's graph, resolved again
                # after chain emission so a same-turn ``new_index_N`` lands
                # (``_settle_fix_attempts``, #2036 D2.3). Note: no forward-
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
                # The leg M5 licenses a SOLUTION on and what it reads there,
                # taken now; the settle resolves it to the fix's licence
                # after chain emission (#2036 D2.3).
                licence_leg = licence_read = None
                if action_type == InvestigationActionType.SOLUTION:
                    licence_leg = solution_licence_leg(
                        case,
                        working_conclusion=_settled_working_conclusion(case, metadata),
                    )
                    licence_read = licence_subject(
                        case, licence_leg, _settled_likelihoods(case, metadata)
                    )
                if (
                    action_type == InvestigationActionType.SOLUTION
                    and licence_leg is None
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
                        WHY_OPEN_DOWNGRADE_REASON
                        if case.progress.cause_identification_held_open
                        else (
                            "Downgraded from SOLUTION: when proposed, no root "
                            "cause was established; a permanent fix needs one, "
                            "and a diagnostic test is not a fix (M5). To register "
                            "a real fix, send in ONE response a "
                            "root_cause_conclusion backed by evidence and the "
                            "fix as a SolutionToAdd; the conclusion counts once "
                            "the symptom is verified and no rival cause is "
                            "contested. If the user already applied that fix, also "
                            "set solution_accepted, accepted_action_ref naming it. "
                            "Or propose a WORKAROUND mitigation."
                        )
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
                #
                # The same fix restated is not a new offer (#2105): it keeps
                # the standing action (the pending offer, or the latest fix
                # while its window has not failed), mints none and supersedes
                # nothing, so the fix the user ran never reads as superseded
                # and its copy never as a second fix. The Solution row above
                # is still minted (#1136).
                refs = metadata.setdefault(FIX_NODE_REFS_KEY, [])
                if action_type == InvestigationActionType.SOLUTION:
                    kept = standing_fix_action(case, s_item.description)
                    if kept is not None:
                        # A restatement may name an unnamed pending fix once;
                        # a name never changes (#2036 D2.3).
                        if node_ref:
                            named = kept.action_id if kept.state == "pending" else None
                            refs.append((node_ref, sol.solution_id, named))
                        solution_slots.append(kept)
                        continue
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
                solution_slots.append(proposed_action)
                if action_type == InvestigationActionType.SOLUTION:
                    metadata.setdefault(FIXES_MINTED_KEY, []).append(
                        (proposed_action.action_id, licence_leg, licence_read)
                    )
                if s_item.solution_type != SolutionType.WORKAROUND:
                    fix_slots.append(proposed_action)
                if node_ref:
                    named = (
                        proposed_action.action_id
                        if action_type == InvestigationActionType.SOLUTION
                        else None
                    )
                    refs.append((node_ref, sol.solution_id, named))

                # solution_proposed is DERIVED at the end-of-turn assessment
                # recompute from live SOLUTION offers (INV-32) — the former
                # 3F write-once set here is gone; this new pending offer
                # flips the indicator True in the same turn via the
                # derivation.

        # 5b. Stage-gate compliance signals (Framework §4.1) — AFTER the
        # solutions step so the guards see actions created this turn (one
        # response may carry SolutionToAdd + solution_accepted; see
        # _apply_stage_gate_signals). A replayed bundle
        # whose hold turn carried a fix acceptance runs the gate with no
        # milestones of its own (#2105).
        if updates.milestones or metadata.get(CARRIED_FIX_KEY) is not None:
            gate_signals = updates.milestones or MilestoneUpdates()
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
            refused_signals = [
                name for name in _GATE_SIGNALS if getattr(gate_signals, name, None)
            ]
            if (
                refused_signals
                and case.progress.problem_status == ProblemStatus.INVALIDATED
            ):
                # Nothing is accepted or verified against a symptom the
                # evidence showed was never present — and a verified mitigation
                # would bar the very finding it was recorded under. The ref
                # and the report turn go with the acceptance they qualify.
                gate_signals = gate_signals.model_copy(
                    update={name: None for name in refused_signals}
                    | {"accepted_action_ref": None, "accepted_fix_reported_in": None}
                )
                record_decision(
                    metadata,
                    NoticeCode.SIGNALS_ON_INVALIDATED_PROBLEM,
                    signals=refused_signals,
                )
            _apply_stage_gate_signals(
                case,
                gate_signals,
                user_message,
                metadata,
                proposals=solution_slots,
                fixes=fix_slots,
                held=(
                    list(getattr(updates, "solutions_to_add", None) or [])
                    if staging
                    else None
                ),
            )

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
        _settle_fix_attempts(case, metadata)
        # Orphan-chain resolution (B2c invariant: every chain explaining D is
        # attached to exactly one hypothesis). T1 re-attaches an unambiguous
        # double-representation in place; any ambiguous orphan is surfaced to
        # the LLM as a one-turn nudge (T2a) to re-root it or declare it
        # separate, rather than guessing.
        _nudge_ambiguous_orphan_chains(case, metadata)
        # One note for everything refused this turn (cause_work.py).
        report_refused_cause_work(metadata)

        # Refutations queued at step 3b, judged now that both link passes
        # (flat step 4 and the chain's node links) have landed: a refutation
        # stands only on data on record, which this turn may have linked
        # (#1911). Before the deferred likelihoods, which skip a hypothesis
        # this refutes, and before the recompute, which reads the states.
        _apply_pending_refutations(
            self.deps.hypothesis_manager, case, metadata, case.current_turn
        )

        # Deferred likelihood updates — applied AFTER both link passes (flat
        # step 4 AND chain emission above), so the B1 evidence-free cap judges
        # the hypothesis with everything this turn's emission grounded it on;
        # a chain-contract turn (record -> node-link -> set likelihood) must
        # not be capped and gaslit for links it did emit.
        _apply_deferred_likelihood_updates(
            self.deps.hypothesis_manager, case, metadata, case.current_turn
        )

        # Recompute engine-owned assessment vars (cause_state / solution_state)
        # now that this turn's hypotheses and solutions are applied (redesign
        # R1), and react to the identification edge.
        await self.recompute_assessment(case, metadata)

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
