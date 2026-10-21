"""Applying the LLM's hypothesis actions, likelihood updates and evidence links onto the case's hypothesis set, via the shared HypothesisManager."""

import logging
from typing import Any

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    failed_fix_of,
    refuting_evidence_on_record,
)
from faultmaven.core.investigation.cause_assurance import (
    absence_row_link_refused,
    stance_declared_confident,
)
from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    settle_set_aside_link,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    hypothesis_refutation_refused_total,
)
from faultmaven.core.investigation.milestone_engine.chain_emission import (
    record_root_verdict,
)
from faultmaven.core.investigation.milestone_engine.turn_records import _resolve_id_ref
from faultmaven.modules.case.contracts import (
    Case,
    EvidenceStance,
    HypothesisState,
)

from .stage_gates import (
    _add_system_feedback,
)

logger = logging.getLogger(__name__)


#: ``MilestoneEngine._resolve_link_confidence``'s answer for a link that must
#: not be written (fm#1502). A sentinel, because ``None`` already means "keep
#: the stored value".
_PRUNE_LINK = object()


def _apply_hypothesis_action_intent(
    hypothesis_manager,
    case: "Case",
    intent_data: dict,
    user_message: str,
    metadata: dict[str, Any],
) -> None:
    """Apply an explicit user ``hypothesis_action`` intent
    (frontend/IntentResolver) — ``refute`` | ``validate`` | ``retire`` —
    BEFORE LLM processing, so the agent sees the updated state in its
    context and can acknowledge.

    Terminal immutability holds on EVERY write path, not just the LLM
    apply layer (#843): a hypothesis already ``REFUTED``/``RETIRED`` is out
    of the differential for good, and this path refuses all three actions
    against it, surfacing why via ``system_feedback``. The concrete
    corruption the guard prevents: retiring an already-REFUTED hypothesis
    would strand ``refutation_reason`` on ``state=RETIRED`` — a pair the
    domain model rejects — and because ``validate_assignment`` is off, the
    in-place write would succeed silently and only surface as a 500 at the
    next Case reconstruction, far from its cause.

    A ``refute`` is applied only when data on record backs it, the same rule
    as the model's refutation (``_apply_pending_refutations``, #1911): the
    click is the user's claim, not a disproof.

    On refusal the action is NOT marked applied
    (``hypothesis_action_applied`` stays unset).
    """
    hypothesis_id = intent_data.get("hypothesis_id")
    action = intent_data.get("action")  # validate | refute | retire

    if not (hypothesis_id and action and case.hypotheses):
        return
    hypothesis = case.hypotheses.get(hypothesis_id)

    if hypothesis and hypothesis.state.is_terminal:
        current_fb = metadata.get("system_feedback", "") or ""
        metadata["system_feedback"] = "\n".join(
            [
                current_fb,
                f"Hypothesis {hypothesis_id} is already "
                f"{hypothesis.state.value} (terminal) — it "
                f"cannot be {action}d. Open a NEW hypothesis "
                f"if that theory is back in play.",
            ]
        ).strip()
        logger.info(
            f"Hypothesis {hypothesis_id} {action} intent refused "
            f"for case {case.case_id}: state "
            f"{hypothesis.state.value} is terminal"
        )
    elif (
        hypothesis
        and action == "refute"
        and not any(_refutation_grounds(case, hypothesis))
    ):
        # The user's click is a claim, like their words: it refutes nothing
        # without data on record behind it (#1911). The hypothesis stays open
        # and the model is told what would settle it.
        hypothesis_refutation_refused_total.labels(source="user").inc()
        _add_system_feedback(
            metadata,
            f"The user's request to rule out hypothesis {hypothesis_id} was "
            f"refused: nothing on record refutes it, so it is still open. If a "
            f"reply told the user it was ruled out, correct that, name the check "
            f"whose output would exclude it, and say they can set it aside "
            f"instead.",
        )
        logger.info(
            "Hypothesis %s refute intent refused for case %s: nothing on "
            "record refutes it",
            hypothesis_id,
            case.case_id,
        )
    elif hypothesis:
        if action == "refute":
            evidence_ids, _failed_fix = _refutation_grounds(case, hypothesis)
            hypothesis_manager.refute_hypothesis(
                hypothesis=hypothesis,
                current_turn=case.current_turn,
                refuting_evidence_ids=evidence_ids,
                reason=user_message or "User refuted",
            )
        elif action == "validate":
            # #695 Defect A: a user "validate" intent records a
            # strong PRIOR, not a validation-by-assertion. The
            # single model derives VALIDATED from the chain root's
            # evidence (project_hypothesis_states_from_roots); a
            # bare assertion cannot mint it (the causal-node model
            # forbids validation by assertion). The user's
            # definitive confirmation is the RESOLVED handshake
            # (the confirm-stamp), not this mid-investigation
            # signal. Surface the new semantics so the affordance
            # does not read as a silent no-op.
            hypothesis.likelihood = 1.0
            hypothesis.last_updated_turn = case.current_turn
            # The user's explicit validation restarts the stagnation clock,
            # even when belief was already near 1.0: stagnation flags lines
            # the investigation is not moving, and the user has just named
            # this one as the line to pursue. Left unrecorded, a positive
            # counter from earlier turns made this a stagnant turn, so
            # housekeeping decayed the user's belief on the spot — or
            # anti-anchoring retired the hypothesis the user just affirmed.
            hypothesis.last_progress_at_turn = case.current_turn
            hypothesis.iterations_without_progress = 0
            current_fb = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = "\n".join(
                [
                    current_fb,
                    f"Recorded your strong belief in hypothesis "
                    f"{hypothesis_id}. It is marked validated once "
                    f"its cause chain is confirmed by evidence — "
                    f"link supporting evidence to its root to get "
                    f"there.",
                ]
            ).strip()
        elif action == "retire":
            hypothesis.state = HypothesisState.RETIRED
            # Bounded at the write, not left to the field validator: this is
            # the user's own message, and letting an over-long one raise here
            # would turn a retire intent into a failed turn.
            hypothesis.retirement_reason = (user_message or "User retired")[:200]
            hypothesis.last_updated_turn = case.current_turn

        metadata["hypothesis_action_applied"] = True
        logger.info(
            f"Hypothesis {hypothesis_id} {action}d via explicit intent "
            f"for case {case.case_id}"
        )
    else:
        logger.warning(f"Hypothesis {hypothesis_id} not found in case {case.case_id}")


def _apply_hypothesis_updates(
    hypothesis_manager,
    case: "Case",
    entries: list,
    metadata: dict[str, Any],
    current_turn: int,
    *,
    staging: bool = False,
) -> None:
    """Apply the LLM's per-turn hypothesis lifecycle updates
    (``state_updates.hypotheses_to_update``).

    Scoped to the DISCONFIRMATION signal — ``state=REFUTED`` with a
    ``refutation_reason``, the disproof that drives M6 demotion of a grounded
    cause — plus likelihood tracking. The schema and prompt have long emitted
    these, but the engine never applied them (no read of
    ``hypotheses_to_update`` anywhere); wired here.

    A refutation is not applied here. It is queued and judged by
    ``_apply_pending_refutations`` once the turn's links have landed, so a row
    linked REFUTES in the same emission can back it (#1911). While cause work
    is ``staging`` it is held on the pending revision instead
    (``ResponseApplier._stage_cause_work``), with the links that back it.

    Deliberately NOT applied in this slice: ``VALIDATED`` / ``RETIRED`` /
    ``ACTIVE`` / ``INCONCLUSIVE`` transitions. ``cause_state`` grounding is
    derived from the ``RootCauseConclusion``, not ``hypothesis.state``, so
    flipping state here would only perturb the ACTIVE-count derivation
    without grounding the cause; richer lifecycle wiring is a separate change.

    Guards:

    - **Terminal immutability.** ``REFUTED`` / ``RETIRED`` are terminal — the
      methodology forbids reviving a disproven/retired hypothesis (it would
      undo the very demotion M6 exists for), and a bare state-flip away from
      ``REFUTED`` would strand ``refutation_reason`` and fail the model's
      pair invariant on reload. A change request against a terminal
      hypothesis is refused and surfaced to the LLM via ``system_feedback``.
    - **Pair integrity.** ``state=REFUTED`` without a ``refutation_reason`` is
      refused (we do not record a disproof on no stated grounds) and surfaced
      as feedback; no likelihood from that same entry is applied (it was a
      refutation entry).
    - **Data behind it.** A stated reason is not a disproof. The refutation
      stands only on evidence on record or a failed fix
      (``_apply_pending_refutations``).

    Best-effort otherwise: an unknown id is logged and skipped, never raised;
    ``new_index_N`` placeholders resolve against hypotheses created this turn.
    Refutation goes through the canonical ``refute_hypothesis``; likelihood
    through ``update_hypothesis_likelihood`` (clamps, maintains the
    progress/decay counters).
    """
    if not entries:
        return
    metadata.setdefault("hypotheses_updated", [])
    feedback: list[str] = []

    # ONE entry per hypothesis. The ``Dict[str, HypothesisUpdate]`` this
    # replaced enforced that for free; a list does not, and everything below
    # assumes it (fm#1057). Two entries naming the same hypothesis would BOTH
    # be applied: the second likelihood update reads the value the first just
    # wrote, sees |delta| < 0.05 and charges ``iterations_without_progress``
    # on a turn that made progress, feeding the stagnation/deadlock repair
    # path; a repeated REFUTED tells the model its own accepted refutation
    # was rejected as "terminal". Last entry wins, which is what a duplicated
    # JSON object key did. Resolve FIRST, so an id and the ``new_index_N``
    # that points at the same hypothesis collapse together.
    resolved: dict[str, Any] = {}
    for upd in entries:
        resolved[
            _resolve_id_ref(
                upd.hypothesis_id,
                metadata.get("hyp_emit_order")
                or metadata.get("hypotheses_generated", []),
                "hyp",
            )
        ] = upd
    if len(resolved) < len(entries):
        logger.warning(
            "Case %s: hypotheses_to_update carried %d entries for %d "
            "hypotheses; kept the last per hypothesis.",
            case.case_id,
            len(entries),
            len(resolved),
        )

    for h_id, upd in resolved.items():
        raw_id = upd.hypothesis_id
        hypothesis = case.hypotheses.get(h_id)
        if hypothesis is None:
            logger.warning(
                f"Hypothesis update skipped: id '{h_id}' not found "
                f"(resolved from '{raw_id}'). "
                f"Available: {list(case.hypotheses.keys())}"
            )
            continue

        # Terminal states are immutable (see docstring).
        if hypothesis.state.is_terminal:
            if (
                upd.state and upd.state != hypothesis.state
            ) or upd.likelihood is not None:
                feedback.append(
                    f"Hypothesis {h_id} is {hypothesis.state.value} (terminal) "
                    f"— its state/likelihood cannot be changed. Open a NEW "
                    f"hypothesis if that theory is back in play."
                )
            continue

        # A REFUTED request is a refutation ENTRY: handle it and nothing else
        # (no likelihood from the same entry — it was a disconfirmation). It is
        # judged after this turn's links land (``_apply_pending_refutations``):
        # the refuting row may be linked in this same emission (#1911). While
        # cause work is staged it is held with the links that back it.
        if upd.state == HypothesisState.REFUTED:
            if upd.refutation_reason and upd.refutation_reason.strip():
                if not staging:
                    metadata.setdefault("pending_refutations", []).append(
                        (h_id, upd.refutation_reason)
                    )
            else:
                feedback.append(
                    f"Hypothesis {h_id}: state=REFUTED requires a "
                    f"refutation_reason (they travel as a pair); the "
                    f"refutation was not applied."
                )
            continue

        # Re-root request (chain mode): record the ref so the chain-emission
        # linking pass re-points this existing hypothesis onto the named chain
        # root, replacing any earlier root it carried. Applied there (not
        # here) because the target node is commonly emitted this same turn in
        # causal_nodes_to_add and must be ingested first.
        reroot = getattr(upd, "root_node_ref", None)
        if reroot:
            metadata.setdefault("hyp_root_refs", {})[h_id] = reroot
            metadata["hypotheses_updated"].append(h_id)
        record_root_verdict(metadata, h_id, upd)

        # Non-REFUTED state transitions are intentionally not applied here.
        # Likelihood updates are DEFERRED to after the same-turn
        # hypothesis_evidence_links pass (``_apply_deferred_likelihood_
        # updates``): the B1 evidence-free cap must judge the hypothesis
        # WITH the links this same emission carries — the prompt mandates
        # record → link → set-likelihood in one turn, and capping before
        # the link lands would gaslight a model that did exactly that.
        if upd.likelihood is not None:
            metadata.setdefault("deferred_likelihood_updates", []).append(
                (h_id, upd.likelihood)
            )
            if not reroot:
                metadata["hypotheses_updated"].append(h_id)

    if feedback:
        current = metadata.get("system_feedback", "") or ""
        metadata["system_feedback"] = "\n".join([current, *feedback]).strip()


def _refutation_grounds(case: "Case", hypothesis) -> tuple[list[str], bool]:
    """What backs a refutation of ``hypothesis`` on record (#1911): the
    refuting evidence ids (``refuting_evidence_on_record``), and whether a
    failed fix of it is recorded (``failed_fix_of``). Neither means the
    refutation would be an argument with no data behind it."""
    evidence_ids = refuting_evidence_on_record(case, hypothesis)
    if evidence_ids:
        return evidence_ids, False
    return [], failed_fix_of(case, hypothesis) is not None


def _has_hedged_refutes(hypothesis) -> bool:
    """The hypothesis carries a self-hedged REFUTES link: the reason a
    refutation the model did link evidence for can still be refused."""
    return any(
        link.stance == EvidenceStance.REFUTES and not stance_declared_confident(link)
        for link in hypothesis.evidence_links
    )


def _apply_pending_refutations(
    hypothesis_manager,
    case: "Case",
    metadata: dict[str, Any],
    current_turn: int,
) -> None:
    """Apply the refutations ``_apply_hypothesis_updates`` queued, each only
    when data on record backs it (#1911, owner ruling 2026-10-10).

    Runs after both link passes (the flat ``hypothesis_evidence_links`` and the
    chain's ``node_evidence_links``), so a turn can add a competitor, link the
    row that excludes it and refute it at once, through ``new_index_N`` refs.

    A refutation stands on an evidence row linked REFUTES to the hypothesis or
    its chain, or on a recorded failed fix of it (the one disproof with no row
    of its own to link). The backing rows are passed to ``refute_hypothesis``.
    Otherwise it is refused: the hypothesis stays open, the refusal is counted,
    and ``system_feedback`` tells the model what would back it. The stated
    reason alone is an argument, and an argument that does not exclude the
    competitor reads exactly like one that does.
    """
    pending = metadata.pop("pending_refutations", None)
    if not pending:
        return
    for h_id, reason in pending:
        hypothesis = case.hypotheses.get(h_id)
        if hypothesis is None or hypothesis.state.is_terminal:
            continue
        evidence_ids, failed_fix = _refutation_grounds(case, hypothesis)
        if evidence_ids or failed_fix:
            hypothesis_manager.refute_hypothesis(
                hypothesis=hypothesis,
                current_turn=current_turn,
                refuting_evidence_ids=evidence_ids,
                reason=reason,
            )
            metadata.setdefault("hypotheses_updated", []).append(h_id)
            continue
        # Refused: the soft transition the link pass held now applies, so the
        # hypothesis ends where its links alone would have left it.
        hypothesis_manager.check_state_transition(hypothesis, current_turn)
        hypothesis_refutation_refused_total.labels(source="model").inc()
        logger.info(
            "Case %s: refutation of %s refused, nothing on record refutes it",
            case.case_id,
            h_id,
        )
        hedged = (
            " Its REFUTES links are self-hedged (stance_confidence below 0.6), "
            "which is not a disproof."
            if _has_hedged_refutes(hypothesis)
            else ""
        )
        where = (
            "Its links lowered its likelihood enough to set it aside (RETIRED), "
            "which is not a disproof."
            if hypothesis.state == HypothesisState.RETIRED
            else "It stays open."
        )
        _add_system_feedback(
            metadata,
            f"Hypothesis {h_id} was NOT refuted: nothing on record refutes it."
            f"{hedged} A refutation stands only on an evidence row from submitted "
            f"data linked with stance REFUTES to the hypothesis "
            f"(hypothesis_evidence_links) or to a node of its chain "
            f"(node_evidence_links), this turn or earlier, or on a recorded "
            f"failed fix of it. {where} Do not tell the user it is ruled out: "
            f"ask for the check that would settle it, or set state=RETIRED if "
            f"you are setting it aside without a disproof.",
        )


def _apply_deferred_likelihood_updates(
    hypothesis_manager,
    case: "Case",
    metadata: dict[str, Any],
    current_turn: int,
) -> None:
    """Apply the likelihood updates stashed by ``_apply_hypothesis_updates``
    — AFTER the same-turn ``hypothesis_evidence_links`` pass, so the B1
    evidence-free cap sees the links this emission carried. The mutator
    caps an evidence-free (or hedged-links-only) update at the prior bar;
    when it does, tell the LLM WHY its number was not applied — the
    recovery is to record the observation as evidence and link it with a
    confident stance, not to re-assert a larger number."""
    deferred = metadata.pop("deferred_likelihood_updates", None)
    if not deferred:
        return
    feedback: list[str] = []
    for h_id, likelihood in deferred:
        hypothesis = case.hypotheses.get(h_id)
        if hypothesis is None:
            continue
        # Re-check terminal immutability HERE, not only at stash time:
        # the links pass between stash and apply can retire this same
        # hypothesis (REFUTES links push likelihood under 0.3 ->
        # check_state_transition), and the refutation pass can refute it;
        # applying the stale number would resurrect a terminal hypothesis's
        # likelihood.
        if hypothesis.state.is_terminal:
            feedback.append(
                f"Hypothesis {h_id}: likelihood update not applied — the "
                f"hypothesis became {hypothesis.state.value} this turn "
                f"(terminal states are immutable)."
            )
            continue
        hypothesis_manager.update_hypothesis_likelihood(
            hypothesis,
            likelihood,
            current_turn,
            reason="LLM hypothesis update",
            case=case,  # chain-axis grounding visible to the B1 cap
        )
        if hypothesis.likelihood < min(1.0, likelihood) - 1e-9:
            feedback.append(
                f"Hypothesis {h_id}: likelihood capped at "
                f"{hypothesis.likelihood:.2f} — a hypothesis with no "
                f"confident supporting evidence links is a prior, not a "
                f"conclusion. Record the observation as evidence and "
                f"link it (hypothesis_evidence_links) to raise belief."
            )
    if feedback:
        current = metadata.get("system_feedback", "") or ""
        metadata["system_feedback"] = "\n".join([current, *feedback]).strip()


def _apply_hypothesis_evidence_links(
    hypothesis_manager,
    case: Case,
    links: list,
    metadata: dict[str, Any],
) -> None:
    """Apply LLM-emitted ``hypothesis_evidence_links`` to the case.

    Linking is best-effort: the LLM may reference hypothesis or
    evidence IDs that don't resolve (timing issue), so failed links
    are logged and skipped. A link on a **terminal** hypothesis
    (``REFUTED``/``RETIRED``) is refused and surfaced via
    ``system_feedback``, mirroring ``_apply_hypothesis_updates``: the
    prompt renders refuted hypotheses WITH their ids (so the model does
    not re-create them), and without this guard one SUPPORTS link lifted a
    refuted hypothesis from 0.0 to 0.35 and reset its progress counter
    (#1116 review). The emitted stance is carried through
    verbatim — NEUTRAL links attach without any likelihood effect
    (#514) — EXCEPT on ``causal_absence`` rows, which carry no
    model-authored stance at all (the M2 trust boundary, #987; see
    ``cause_assurance.absence_row_link_refused``).

    This is the FLAT belief axis. It shares that boundary verbatim with the
    chain axis in ``causal_graph.ingest_emitted_chain`` because the
    invariant belongs to the evidence ROW, not to the link target: a
    REFUTES on a success-confirmation absence row reaches
    ``cause_refuted_by_evidence`` → ``_hypothesis_disconfirmed`` → M6 from here just as
    it reached ``derive_node_states`` from there, so guarding one axis only
    would leave the #987 cascade one stance choice away.
    """
    pending_ids = {h_id for h_id, _reason in metadata.get("pending_refutations", [])}
    for link in links:
        # Resolve partial IDs like 'new_index_0' to actual IDs if we just created them
        h_id = _resolve_id_ref(
            link.hypothesis_id_ref,
            metadata.get("hyp_emit_order") or metadata.get("hypotheses_generated", []),
            "hyp",
        )
        e_id = _resolve_id_ref(
            link.evidence_id_ref, metadata.get("evidence_slots", []), "ev"
        )

        # Check existence
        if h_id not in case.hypotheses:
            # Hypothesis ID validation failed - log warning but don't add to system_feedback
            logger.warning(
                f"Hypothesis-evidence link skipped: Hypothesis ID '{h_id}' not found "
                f"(resolved from '{link.hypothesis_id_ref}'). "
                f"Available hypotheses: {list(case.hypotheses.keys())}, "
                f"Hypotheses added this turn: {metadata.get('hypotheses_generated', [])}"
            )
            continue

        # Terminal states are immutable on every write path (see docstring).
        hypothesis = case.hypotheses[h_id]
        if hypothesis.state.is_terminal:
            logger.warning(
                f"Hypothesis-evidence link refused: hypothesis '{h_id}' is "
                f"{hypothesis.state.value} (terminal)."
            )
            _add_system_feedback(
                metadata,
                f"Hypothesis {h_id} is {hypothesis.state.value} (terminal) "
                f"— evidence cannot be linked to it. Open a NEW hypothesis "
                f"if that theory is back in play.",
            )
            continue

        # Check evidence existence (scan list)
        ev_row = next((e for e in case.evidence if e.evidence_id == e_id), None)
        ev_exists = ev_row is not None
        if not ev_exists:
            # Evidence reference failed to resolve
            # This is only a problem if LLM tried to link evidence but used wrong format/ID
            # It's acceptable if no evidence exists (e.g., user_text message)

            # Build diagnostic info
            evidence_this_turn = metadata.get("evidence_added", [])
            all_evidence_ids = [e.evidence_id for e in case.evidence]

            logger.warning(
                f"Hypothesis-evidence link validation failed: "
                f"Cannot resolve reference '{link.evidence_id_ref}' to evidence ID '{e_id}'. "
                f"Evidence created this turn: {evidence_this_turn}. "
                f"Recent evidence IDs: {all_evidence_ids[-5:] if len(all_evidence_ids) > 5 else all_evidence_ids}. "
                f"Note: This is expected if no evidence was created (user_text messages)."
            )
            continue

        # M2 trust boundary (#987), category-gated — the SAME predicate the
        # chain axis applies, so the two entry points cannot drift.
        if absence_row_link_refused(
            getattr(ev_row, "category", None),
            link.stance,
            axis="hypothesis",
            evidence_id=e_id,
            node_or_hypothesis_id=h_id,
            case_id=case.case_id,
            turn=case.current_turn,
        ):
            continue

        # A confidence the schema SET ASIDE as out of range (fm#1502) is
        # decided here, where "re-emitted" is knowable: storage is an upsert
        # by evidence_id, and only the stored link says whether this one
        # re-states the same claim.
        stored = next(
            (el for el in hypothesis.evidence_links if el.evidence_id == e_id),
            None,
        )
        stance_confidence = _resolve_link_confidence(
            link,
            stored_stance=stored.stance if stored is not None else None,
            where=f"{h_id}<-{e_id}",
            metadata=metadata,
        )
        if stance_confidence is _PRUNE_LINK:
            continue

        # Counts only a NEW or materially revised link (#1136). Storage is an
        # upsert by evidence_id, so counting every call let a model re-emitting
        # the same link each turn hold ``turns_without_progress`` at 0 forever —
        # the same restatement leak the ``novel_*`` keys close on the other
        # arms. ``link_evidence`` decides, because only it holds both the prior
        # link and the new one.
        # A hypothesis refuted in this same emission keeps its state until the
        # refutation is judged: its own REFUTES link would otherwise retire it
        # as low-confidence first, and the refutation would find it terminal
        # (#1911). ``_apply_pending_refutations`` settles the held transition.
        if hypothesis_manager.link_evidence(
            case.hypotheses[h_id],
            e_id,
            link.stance,
            case.current_turn,
            reasoning=link.reasoning,
            stance_confidence=stance_confidence,
            hold_state=h_id in pending_ids,
        ):
            metadata["hypothesis_evidence_links_applied"] = (
                metadata.get("hypothesis_evidence_links_applied", 0) + 1
            )


def _resolve_link_confidence(
    link: Any,
    *,
    stored_stance: Any,
    where: str,
    metadata: dict[str, Any],
) -> Any:
    """The ``stance_confidence`` to store for a hypothesis link.

    Returns a number, ``None`` (keep the stored value — ``link_evidence``
    applies it), or ``_PRUNE_LINK`` when the link must not be written.

    Only a value the schema SET ASIDE as out of range is decided here
    (fm#1502): a re-emission of the same claim (same evidence, same stance)
    keeps the stored value; a new link, or a stance flip, is rescaled or
    coerced when it can be and otherwise pruned — leaving any stored link
    as it was. The schema's ``1.0`` default must not stand in, and nor may
    the stored confidence of the opposite stance: on REFUTES the first is a
    decisive disconfirmation nobody asserted, and on SUPPORTS the second is
    grounding nobody asserted.

    An OMITTED (or strict-mode ``null``) confidence follows the same
    new-versus-re-emitted rule, per the 2026-09-24 ruling: on a re-emission
    of the same claim it keeps the stored value — the schema's ``1.0``
    default used to overwrite a stored hedge whenever a routine re-listing
    left the field out, "the exact defect the node path documents
    avoiding" — and on a new link or a stance flip it is full confidence,
    the default, as before. A conforming value is the link's own, as before.

    Duck-typed like the rest of this apply path: a link that is not a
    Pydantic model has no fields-set record, so its ``stance_confidence``
    is read as given.
    """
    settled = settle_set_aside_link(
        link,
        stored_stance=stored_stance,
        where=where,
        notes=metadata.setdefault("validation_repairs", []),
    )
    if settled is None:
        fields_set = getattr(link, "model_fields_set", None)
        omitted = isinstance(fields_set, (set, frozenset)) and (
            "stance_confidence" not in fields_set
        )
        if omitted and stored_stance is not None and stored_stance == link.stance:
            return None  # a true re-emission: the stored value stands
        return link.stance_confidence
    action, value = settled
    if action is ConfidenceAction.PRUNED:
        return _PRUNE_LINK
    return value  # None when dropped: the stored value stands
