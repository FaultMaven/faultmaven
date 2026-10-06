"""The statement-revision handshake: the problem is real, its confirmed
statement is not accurate, and the user re-confirms the evidence-based revision.

The handshake is Gate 1 again, inside INVESTIGATING. The engine presents the
revised statement on every pending turn (the user cannot confirm what they
cannot see — INV-01), the card's two buttons name the offer by the key of the
wording shown (#1812), and a typed answer is read by the same consent grammar
the disposition gate uses (``pending_gate_verdict``): a click or a bare "yes"
confirms, a decline returns the case to where it was, and anything substantive
is answered as an ordinary turn with the revision still standing.

Confirmation runs BEFORE the turn's LLM call (section 0b of the engine), so the
model works the turn on the revised statement. The commit writes the statement
into every store that holds it (``problem_status.commit_revision``), takes a
checkpoint, re-runs the KB pre-fetch on the new wording, and replays the cause
work staged while the revision waited — through the normal apply path, with the
evidence ids each staged turn resolved against. So the confirmation turn can
verify the revised problem, form its hypotheses, ground the chain and identify
the cause: the opportunistic flow holds across the handshake.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from faultmaven.core.investigation.problem_status import (
    commit_revision,
    current_statement,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.modules.case.contracts import CaseState, ProblemStatus

from .transition_consent import (
    TYPED_CONFIRMATION_LINE,
    offer_intent_fields,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

#: Set on a replayed bundle's metadata. The apply path's engine proposers
#: (deferred close, false-alarm close) stand down under it: an offer made inside
#: the replay would land on ``case.pending_transition`` with its card and its
#: same-turn guard left in the bundle's dict, and the confirmation turn's own
#: "yes" — consent to the revised STATEMENT — would then execute it. The live
#: turn that follows the replay runs the proposers with its own metadata, so an
#: offer the replayed state warrants is made there, with its card, unexecuted.
REPLAY_METADATA_KEY = "replaying_staged_cause_work"

#: The keys a replayed turn's apply step writes that the confirmation turn's
#: progress reading and turn record must see. The LLM's own response metadata
#: replaces the turn's dict wholesale (``metadata.update``), so the replay
#: records under ``statement_commit`` and these are merged back afterwards.
REPLAYED_KEYS = (
    "hypotheses_generated",
    "hypotheses_validated",
    "solutions_proposed",
    "novel_solutions_proposed",
    "milestones_completed",
    "validation_repairs",
)


def revision_pending(case: "Case") -> bool:
    """Whether a revised statement awaits the user's re-confirmation."""
    pv = case.problem_verification
    return (
        case.state == CaseState.INVESTIGATING
        and case.progress.problem_status == ProblemStatus.REVISION_PENDING
        and pv is not None
        and pv.pending_revision is not None
    )


def revision_offer(case: "Case") -> str | None:
    """The key of the revision offer standing on ``case``, or None."""
    if not revision_pending(case):
        return None
    return case.problem_verification.pending_revision.offer_key or None


def revision_presentation(case: "Case") -> str:
    """The engine's presentation of the standing revision: the statement the
    user confirmed, the revision the evidence calls for, and how to answer.
    Composed below the model's reply on every pending turn; asks nothing the
    user may already have answered."""
    pending = case.problem_verification.pending_revision
    confirmed = _quoted(current_statement(case))
    revised = _quoted(pending.text)
    basis = pending.basis.strip()
    return (
        "The evidence shows the problem differs from the statement you "
        f"confirmed.\n\nConfirmed:\n\n{confirmed}\n\nRevised, from the "
        f"evidence:\n\n{revised}\n\n"
        + (f"What differs: {basis}\n\n" if basis else "")
        + "Confirm the revision to continue on it, or tell me what to change.\n\n"
        + TYPED_CONFIRMATION_LINE
    )


def revision_confirmation_suggestions(case: "Case") -> list:
    """The confirm/refine pair for the standing revision, each naming its offer."""
    offer = offer_intent_fields(revision_offer(case), case=case, gate="revision")
    return [
        {
            "label": "Yes, use the revised statement",
            "action_type": "DECIDE",
            "payload": "Yes, the revised problem statement is right.",
            "body": "Continue the investigation on the revised problem statement.",
            "intent": {"type": "confirmation", "confirmation_value": True, **offer},
        },
        {
            "label": "No, let me clarify",
            "action_type": "DECIDE",
            "payload": "No — let me clarify the problem first.",
            "body": "Keep the confirmed statement and clarify what is wrong.",
            "intent": {"type": "confirmation", "confirmation_value": False, **offer},
        },
    ]


async def confirm_revision(
    responses: Any, deps: Any, case: "Case", metadata: dict
) -> None:
    """The user re-confirmed the revision: commit it and replay what was staged.

    ``responses`` is the engine's ``ResponseApplier``; the replay runs through
    its ``_apply_investigation_updates`` — the same path a live turn takes —
    one staged turn at a time, each with a fresh metadata seeded with the
    evidence ids that turn's refs resolve against.
    """
    if deps.checkpoint_service:
        await deps.checkpoint_service.create_checkpoint(
            case,
            trigger="pre_case_action",
            metadata={"action": "problem_statement_revised"},
        )
    pending = commit_revision(case)
    await responses.kb_prefetcher.prefetch_kb_context(case, pending.text, "symptom")

    replayed: dict[str, list] = {key: [] for key in REPLAYED_KEYS}
    for bundle in pending.staged:
        try:
            updates = (
                InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
                    bundle.updates
                )
            )
        except Exception as exc:  # noqa: BLE001 — a stale shape is dropped, not fatal
            logger.warning(
                "Case %s: staged cause work from turn %s no longer validates "
                "and was dropped: %s",
                case.case_id,
                bundle.turn,
                exc,
            )
            metadata.setdefault("validation_repairs", []).append(
                f"Staged cause work from turn {bundle.turn} could not be replayed"
            )
            continue
        bundle_metadata: dict[str, Any] = {
            "milestones_completed": [],
            "evidence_added": list(bundle.evidence_added),
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            REPLAY_METADATA_KEY: True,
        }
        await responses._apply_investigation_updates(case, updates, bundle_metadata)
        for key in REPLAYED_KEYS:
            replayed[key].extend(bundle_metadata.get(key, []))
        feedback = bundle_metadata.get("system_feedback")
        if feedback:
            _append_feedback(metadata, feedback)

    metadata["statement_commit"] = {
        "statement": pending.text,
        "replayed_turns": [bundle.turn for bundle in pending.staged],
        **replayed,
    }
    metadata["problem_status_changed"] = True
    metadata["revision_confirmed_this_turn"] = True
    logger.info(
        "Case %s: revised statement confirmed at turn %s; replayed %d staged turn(s)",
        case.case_id,
        case.current_turn,
        len(pending.staged),
    )


def merge_statement_commit(metadata: dict) -> None:
    """Fold the replay's results into the turn's metadata after the LLM's
    response metadata replaced it (``_apply_turn_response``), so the turn
    record and the progress reading count the hypotheses and solutions the
    replay produced."""
    commit = metadata.get("statement_commit")
    if not commit:
        return
    for key in REPLAYED_KEYS:
        values = commit.get(key) or []
        if values:
            existing = metadata.setdefault(key, [])
            existing[:0] = [v for v in values if v not in existing]
    metadata["problem_status_changed"] = True


def _quoted(text: str) -> str:
    text = (text or "").strip()
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _append_feedback(metadata: dict, message: str) -> None:
    current = metadata.get("system_feedback", "") or ""
    metadata["system_feedback"] = "\n".join(p for p in (current, message) if p).strip()
