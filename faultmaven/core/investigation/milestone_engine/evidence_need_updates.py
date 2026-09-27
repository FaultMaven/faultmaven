"""Applying the engine's evidence-need updates (raised, satisfied, withdrawn) onto a case's outstanding EvidenceNeed rows."""

import logging
from datetime import UTC, datetime
from typing import Any

from faultmaven.core.investigation.lifecycle_metrics import (
    evidence_need_created_total,
    evidence_need_status_changed_total,
)
from faultmaven.core.investigation.milestone_engine.turn_records import _resolve_id_ref
from faultmaven.modules.case.contracts import (
    TERMINAL_HYPOTHESIS_STATES,
    Case,
    EvidenceNeed,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

logger = logging.getLogger(__name__)


def _apply_evidence_need_updates(
    case: Case,
    updates_list: list,
    metadata: dict[str, Any],
    current_turn: int,
) -> None:
    """Apply LLM-emitted ``evidence_need_updates`` to the case.

    Each ``EvidenceNeedUpdate`` either creates a new ``EvidenceNeed``
    (when ``need_id`` is None) or updates an existing one. Cross-
    emission ``new_index_N`` references are resolved against
    metadata-stored ID lists populated earlier in this same
    ``_apply_investigation_updates`` invocation:

    - ``motivating_hypothesis_ids`` → ``metadata["hypotheses_generated"]``
    - ``fulfilling_evidence_ids`` → ``metadata["evidence_added"]``
    - ``need_id`` → in-loop list of need IDs created earlier in
      this same ``updates_list``

    Redesign R5/§2: the former path-conditional causal-purpose ban is
    removed — causal-verification needs are allowed opportunistically
    during INVESTIGATING; the prompt (gated on cause uncertainty) decides
    when causal work runs, not an engine emission ban.

    See ``docs/architecture/investigation-engine/evidence-needs-design.md``
    §5.3 (out-of-order arrival).
    """
    # Ensure the metadata key exists before any append. The dict built
    # in ``_process_response_structured`` (the one threaded here via
    # ``_apply_investigation_updates``) does not seed
    # ``evidence_needs_updated``, unlike the parallel dict in
    # ``_process_turn_impl``. Without this, the first need created or
    # updated this turn raised ``KeyError`` and 500'd the whole turn.
    # The Phase-6 flatten seam already reads this key defensively
    # (``metadata.get("evidence_needs_updated", [])``).
    metadata.setdefault("evidence_needs_updated", [])
    # Same-turn need_id resolution: needs created earlier in this
    # same ``updates_list`` are tracked here so a later update with
    # ``need_id="new_index_0"`` can find them.
    needs_created_in_this_loop: list[str] = []

    for update in updates_list:
        # Resolve new_index_N references (same pattern as
        # hypothesis_evidence_links at line ~5927 / 5931).
        resolved_motivators = [
            _resolve_id_ref(
                hyp_ref,
                metadata.get("hyp_emit_order")
                or metadata.get("hypotheses_generated", []),
                "hyp",
            )
            for hyp_ref in (update.motivating_hypothesis_ids or [])
        ]
        resolved_fulfillments = [
            _resolve_id_ref(ev_ref, metadata.get("evidence_added", []), "ev")
            for ev_ref in (update.fulfilling_evidence_ids or [])
        ]
        resolved_need_id: str | None = None
        if update.need_id is not None:
            resolved_need_id = _resolve_id_ref(
                update.need_id, needs_created_in_this_loop, "eneed"
            )

        # Reference validation: dangling hypothesis IDs are dropped
        # (the link couldn't form anyway), and already-TERMINAL IDs
        # (REFUTED / RETIRED) are also dropped — a hypothesis already out
        # of the differential motivates nothing, so admitting it would
        # create a need the end-of-turn sweep immediately supersedes.
        # Rejecting it at the boundary keeps the churn (and the misleading
        # ask, for the turn it would live) out of the case entirely.
        # Dangling evidence IDs are dropped likewise.
        # These look like prompt-compliance issues, not lifecycle
        # errors, so they go to validation_repairs not system_feedback.
        dangling_hyp_ids = {
            h_id for h_id in resolved_motivators if h_id not in case.hypotheses
        }
        terminal_hyp_ids = {
            h_id
            for h_id in resolved_motivators
            if h_id in case.hypotheses
            and case.hypotheses[h_id].state in TERMINAL_HYPOTHESIS_STATES
        }
        valid_motivators = [
            h_id
            for h_id in resolved_motivators
            if h_id not in dangling_hyp_ids and h_id not in terminal_hyp_ids
        ]
        if dangling_hyp_ids:
            logger.warning(
                f"Dropped {len(dangling_hyp_ids)} dangling hypothesis "
                f"ID(s) on evidence_need_update for case {case.case_id}: "
                f"{dangling_hyp_ids}"
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Dropped {len(dangling_hyp_ids)} dangling hypothesis "
                f"ID(s) on evidence_need_update"
            )
        if terminal_hyp_ids:
            logger.warning(
                f"Dropped {len(terminal_hyp_ids)} terminal hypothesis "
                f"ID(s) on evidence_need_update for case {case.case_id}: "
                f"{terminal_hyp_ids}"
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Dropped {len(terminal_hyp_ids)} terminal hypothesis "
                f"ID(s) on evidence_need_update"
            )

        valid_ev_ids = {ev.evidence_id for ev in case.evidence}
        valid_fulfillments = [
            e_id for e_id in resolved_fulfillments if e_id in valid_ev_ids
        ]
        if len(valid_fulfillments) != len(resolved_fulfillments):
            dropped = set(resolved_fulfillments) - set(valid_fulfillments)
            logger.warning(
                f"Dropped {len(dropped)} dangling evidence ID(s) on "
                f"evidence_need_update for case {case.case_id}: {dropped}"
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Dropped {len(dropped)} dangling evidence ID(s) "
                f"on evidence_need_update"
            )

        # CREATE path (need_id is None)
        if resolved_need_id is None:
            # Reject causal-purpose creates with no valid motivator.
            # A causal need without any motivating hypothesis is the
            # exact orphan state §7.4's supersession rule was
            # designed to clean up — but the sweep keys off a
            # terminal hypothesis id, and a need born with no
            # motivator at all has none to key on, so it would
            # never be auto-cleaned. Per design §5.2,
            # causal needs are *motivated by hypotheses*; absent
            # motivators (whether the LLM omitted them or all
            # references filtered away as dangling/retired) makes
            # the emission malformed. Symptom needs are unaffected
            # — empty motivator list is their normal shape, they're
            # motivated by the problem statement.
            if (
                update.purpose == NeedPurpose.CAUSAL_VERIFICATION
                and not valid_motivators
            ):
                logger.warning(
                    f"Rejected causal-purpose evidence_need create on "
                    f"case {case.case_id}: no valid motivating "
                    f"hypothesis (omitted, or all references were "
                    f"dangling/retired). "
                    f"request_text={update.request_text[:80]!r}"
                )
                metadata.setdefault("validation_repairs", []).append(
                    "Rejected causal-purpose evidence_need create "
                    "(no valid motivating hypothesis)"
                )
                continue

            # FULFILLED→PARTIALLY_MET demotion when all referenced
            # fulfilling evidence IDs were dropped as dangling. The
            # schema's create-path rule rejects FULFILLED + empty
            # list at emission, but the apply-layer drop happens
            # after that check; constructing EvidenceNeed with
            # FULFILLED + [] would raise via the model_validator.
            # The rule lives on the model (single owner); this site owns
            # only the repair note.
            requested_status = update.state or NeedState.PENDING
            effective_superseded_reason = update.superseded_reason
            effective_status = EvidenceNeed.admissible_state(
                requested_status, valid_fulfillments
            )
            if effective_status != requested_status:
                metadata.setdefault("validation_repairs", []).append(
                    "Demoted FULFILLED→PARTIALLY_MET on evidence_need "
                    "create (all fulfilling_evidence_ids dropped as "
                    "dangling)"
                )
                effective_superseded_reason = None

            new_need = EvidenceNeed(
                case_id=case.case_id,
                purpose=update.purpose,
                request_text=update.request_text,
                rationale=update.rationale,
                # priority is Optional on EvidenceNeedUpdate (omitted on
                # the update path); on create, fall back to MEDIUM.
                priority=update.priority or NeedPriority.MEDIUM,
                state=effective_status,
                motivating_hypothesis_ids=valid_motivators,
                fulfilling_evidence_ids=valid_fulfillments,
                superseded_reason=effective_superseded_reason,
                # Opt-in obtainability (§5.3); the model validator coerces it
                # to UNKNOWN for symptom needs or terminal states.
                obtainability=getattr(update, "obtainability", None)
                or NeedObtainability.UNKNOWN,
                created_at_turn=current_turn,
            )
            case.evidence_needs.append(new_need)
            needs_created_in_this_loop.append(new_need.need_id)
            metadata["evidence_needs_updated"].append(new_need.need_id)
            try:
                evidence_need_created_total.labels(purpose=new_need.purpose.value).inc()
            except Exception:
                pass
            logger.info(
                f"Created EvidenceNeed {new_need.need_id} "
                f"(purpose={new_need.purpose.value}) on case {case.case_id}"
            )
            continue

        # UPDATE path (need_id is set)
        target = next(
            (n for n in case.evidence_needs if n.need_id == resolved_need_id),
            None,
        )
        if target is None:
            logger.warning(
                f"evidence_need_update references unknown need_id "
                f"{resolved_need_id!r} on case {case.case_id}; "
                f"dropping update"
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Dropped evidence_need_update for unknown need_id "
                f"{resolved_need_id!r}"
            )
            continue

        # Purpose is immutable on the update path. It is Optional on
        # EvidenceNeedUpdate and is normally OMITTED on update (None);
        # only warn when the LLM actually sent a *different* purpose.
        # (Guarding on ``is not None`` also avoids ``None.value`` here.)
        if update.purpose is not None and update.purpose != target.purpose:
            logger.warning(
                f"evidence_need_update attempted to flip purpose on "
                f"need {target.need_id} "
                f"({target.purpose.value} → {update.purpose.value}); "
                f"ignoring purpose change"
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Ignored purpose-change attempt on need {target.need_id}"
            )

        # SUPERSEDED is terminal — cannot resurrect via update.
        if target.state == NeedState.SUPERSEDED and update.state not in (
            None,
            NeedState.SUPERSEDED,
        ):
            logger.warning(
                f"evidence_need_update attempted to resurrect "
                f"SUPERSEDED need {target.need_id}; ignoring status "
                f"change. Emit a new need instead."
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Ignored resurrection attempt on SUPERSEDED need {target.need_id}"
            )
            continue

        # Merge lists (append-only). Dedup is handled by the
        # EvidenceNeed field validator at assignment time.
        prior_status = target.state
        target.motivating_hypothesis_ids = list(
            dict.fromkeys(list(target.motivating_hypothesis_ids) + valid_motivators)
        )
        target.fulfilling_evidence_ids = list(
            dict.fromkeys(list(target.fulfilling_evidence_ids) + valid_fulfillments)
        )
        # Revise-don't-clobber: request_text / rationale / priority are
        # Optional on the update path and are normally omitted on a
        # fulfill/status update. Only overwrite when the LLM actually
        # supplied a new value — None means "leave unchanged". Without
        # this guard a bare fulfill update would null out request_text /
        # rationale (silent corruption) and downgrade priority to the
        # field default.
        #
        # For the two text fields we guard on truthiness, not ``is not
        # None``: an explicit "" is treated as "leave unchanged" too.
        # request_text/rationale are min_length=1 on the domain model
        # (validate_assignment is off, so "" wouldn't raise here — it
        # would crash on the next repo round-trip), and blanking a
        # mandatory field is never a valid revision. This mirrors the
        # create validator, which rejects ``in (None, "")``.
        if update.request_text:
            target.request_text = update.request_text
        if update.rationale:
            target.rationale = update.rationale
        if update.priority is not None:
            target.priority = update.priority
        # FULFILLED→PARTIALLY_MET demotion when the post-merge
        # fulfilling list is still empty. ``validate_assignment``
        # is off on EvidenceNeed, so in-place mutation bypasses
        # ``_validate_state_consistency`` — without this guard a
        # bad LLM emission could leave the need in FULFILLED+[]
        # state that raises on next reconstruction. The rule lives on the
        # model (single owner); this site owns only the repair note.
        effective_status = EvidenceNeed.admissible_state(
            update.state, target.fulfilling_evidence_ids
        )
        if effective_status != update.state:
            metadata.setdefault("validation_repairs", []).append(
                f"Demoted FULFILLED→PARTIALLY_MET on need {target.need_id} "
                f"(all fulfilling_evidence_ids dropped as dangling)"
            )
        if effective_status is not None:
            target.state = effective_status
        if effective_status == NeedState.SUPERSEDED:
            target.superseded_reason = update.superseded_reason
        elif effective_status is not None and effective_status != NeedState.SUPERSEDED:
            # Clearing superseded_reason on non-SUPERSEDED transition
            target.superseded_reason = None
        # Obtainability (§5.3): opt-in model declaration, scoped to
        # causal_verification (symptom declarations are out of scope).
        # ``validate_assignment`` is off on EvidenceNeed, so the model
        # validator's auto-revoke does not fire on in-place mutation —
        # apply the same rule here: reset to UNKNOWN when the need reaches a
        # terminal state (the question is moot). The rollup only reads
        # outstanding causal needs, so this is belt-and-suspenders for a
        # clean record rather than the correctness guarantee.
        _declared_obtainability = getattr(update, "obtainability", None)
        if _declared_obtainability is not None and (
            target.purpose == NeedPurpose.CAUSAL_VERIFICATION
        ):
            target.obtainability = _declared_obtainability
        # Auto-revoke on terminal state (§5.3) — centralized invariant.
        target.revoke_obtainability_if_terminal()
        target.updated_at = datetime.now(UTC)
        if target.need_id not in metadata["evidence_needs_updated"]:
            metadata["evidence_needs_updated"].append(target.need_id)

        if effective_status is not None and effective_status != prior_status:
            try:
                evidence_need_status_changed_total.labels(
                    from_state=prior_status.value,
                    to_state=effective_status.value,
                ).inc()
            except Exception:
                pass
            logger.info(
                f"Need {target.need_id} status "
                f"{prior_status.value} → {effective_status.value} "
                f"on case {case.case_id}"
            )
