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
into every store that holds it (``problem_status.commit_revision``), re-runs the
KB pre-fetch on the new wording, and replays the cause
work staged while the revision waited — through the normal apply path, with the
evidence ids each staged turn resolved against. So the confirmation turn can
verify the revised problem, form its hypotheses, ground the chain and identify
the cause: the opportunistic flow holds across the handshake.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from faultmaven.core.investigation.offers import (
    open_revision,
    terminal_offer_opened_this_turn,
)
from faultmaven.core.investigation.problem_status import (
    commit_revision,
    current_statement,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.modules.case.contracts import AnswerVia, CaseState, ProblemStatus

from .stage_gates import CARRIED_FIX_KEY, _add_system_feedback
from .transition_consent import (
    TYPED_CONFIRMATION_LINE,
)
from .turn_records import REFUSED_EVIDENCE_SLOT

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, PendingRevision

logger = logging.getLogger(__name__)

#: Set on a replayed bundle's metadata. The apply path's engine proposers
#: (deferred close, false-alarm close) stand down under it: an offer made inside
#: the replay would stand before the confirmation turn's own "yes" — consent to
#: the revised STATEMENT — had reached the gate. The live turn that follows the
#: replay runs the proposers with its own metadata, so an offer the replayed
#: state warrants is made there, with its card, unexecuted.
REPLAY_METADATA_KEY = "replaying_staged_cause_work"

#: One list for every bundle of a replay: the actions the replay has minted
#: from fix entries so far, which the stage gate weighs as fix bearers (#2105).
REPLAY_FIX_SLOTS_KEY = "replay_fix_slots"

#: The prose an offer made inside the replay writes beside it. Carried into the
#: turn's own metadata whenever the replay leaves an offer open, whichever
#: proposer made it, so the offer reaches the user with its reason; its card
#: comes from the offer projection at compose. The offer is opened on the
#: confirmation turn (``Offer.opened_at_turn``), so the gate, which answers
#: only an offer opened on an earlier turn, never executes it on the user's
#: "yes" to the revised statement (#722). The proposers in the apply path's
#: tail also stand down under ``REPLAY_METADATA_KEY``; this covers the ones
#: that do not (the stage-gate side effects) and any added later.
PROPOSAL_KEYS = (
    "rca_infeasible_closure_message",
    "deferred_solution_gate_message",
    "false_alarm_closure_message",
    "resolution_ready_gate_message",
    "resolution_readiness_verdict",
    "resolution_readiness_missing",
)

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
    """Whether a revised statement awaits the user's re-confirmation: a
    revision offer is open."""
    return (
        case.state == CaseState.INVESTIGATING
        and case.progress.problem_status == ProblemStatus.REVISION_PENDING
        and open_revision(case) is not None
    )


def revision_offer(case: "Case") -> str | None:
    """The ``proposal_id`` of the revision offer open on ``case``, or None."""
    if not revision_pending(case):
        return None
    return open_revision(case).proposal_id


def revision_presentation(case: "Case") -> str:
    """The engine's presentation of the standing revision: the statement the
    user confirmed, the revision the evidence calls for, and how to answer.
    Composed below the model's reply on every pending turn; asks nothing the
    user may already have answered."""
    pending = open_revision(case).revision
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


async def confirm_revision(
    responses: Any,
    case: "Case",
    metadata: dict,
    *,
    via: AnswerVia,
) -> None:
    """The user re-confirmed the revision: commit it and replay what was staged.

    ``responses`` is the engine's ``ResponseApplier``; the replay runs through
    its ``_apply_investigation_updates`` — the same path a live turn takes —
    one staged turn at a time, each with a fresh metadata seeded with the
    evidence ids that turn's refs resolve against. A bundle that carried a fix
    acceptance (``held_fix``) is handed to the stage gate, which registers it
    stamped at that turn and decides nothing again (#2105); a bundle that no
    longer validates is dropped with it.
    """
    pending = commit_revision(case, via=via)
    await responses.kb_prefetcher.prefetch_kb_context(case, pending.text, "symptom")

    replayed: dict[str, list] = {key: [] for key in REPLAYED_KEYS}
    fix_slots: list = []
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
            # The staged list is positional (``evidence_slots``); the rows that
            # exist are the ones the live turn created.
            "evidence_added": [
                ev_id
                for ev_id in bundle.evidence_added
                if ev_id != REFUSED_EVIDENCE_SLOT
            ],
            "evidence_slots": list(bundle.evidence_added),
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            REPLAY_METADATA_KEY: True,
            REPLAY_FIX_SLOTS_KEY: fix_slots,
        }
        if bundle.held_fix:
            bundle_metadata[CARRIED_FIX_KEY] = bundle
        await responses._apply_investigation_updates(case, updates, bundle_metadata)
        if terminal_offer_opened_this_turn(case) is not None:
            for key in PROPOSAL_KEYS:
                if key in bundle_metadata:
                    metadata[key] = bundle_metadata[key]
        for key in REPLAYED_KEYS:
            replayed[key].extend(bundle_metadata.get(key, []))
        feedback = bundle_metadata.get("system_feedback")
        if feedback:
            _add_system_feedback(metadata, feedback)

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


#: How the decline note names each kind of staged cause work.
_STAGED_WORK_NOUNS = (
    ("hypotheses_to_add", "hypothesis(es)"),
    ("causal_nodes_to_add", "causal node(s)"),
    ("causal_edges_to_add", "causal link(s)"),
    ("hypotheses_to_update", "refutation(s)"),
    ("solutions_to_add", "proposed solution(s)"),
    ("root_cause_conclusion", "root-cause conclusion"),
)


def staged_work_summary(pending: PendingRevision | None) -> str | None:
    """What a decline discards: the cause work staged on the revision, by kind
    — so the model knows to re-send what still holds once the problem is
    verified, rather than assume it was recorded. A fix execution the user
    reported, carried for the replay (``held_fix``), is named too: it is
    discarded with its bundle and must be registered again (#2105)."""
    if pending is None:
        return None
    counts: dict[str, int] = {}
    for bundle in pending.staged:
        for field, noun in _STAGED_WORK_NOUNS:
            value = bundle.updates.get(field)
            if value:
                counts[noun] = counts.get(noun, 0) + (
                    len(value) if isinstance(value, list) else 1
                )
    executions = sum(1 for bundle in pending.staged if bundle.held_fix)
    if executions:
        counts["reported fix execution(s)"] = executions
    if not counts:
        return None
    return ", ".join(f"{n} {noun}" for noun, n in counts.items())


def _quoted(text: str) -> str:
    text = (text or "").strip()
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))
