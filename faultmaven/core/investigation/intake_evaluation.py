"""Process-owned intake-evaluation step (the supply half of the collect→validate
loop).

On each data submission, every new datum is run through the deterministic
evaluator (``differential_intake.evaluate_datum_against_differential`` — matcher
body) and each returned ``StanceVerdict`` is recorded as a typed
``NodeEvidenceLink`` on the relevant cause's ROOT node, carrying the verdict's
provenance. ``derive_node_states`` then counts those links — so cause validation
becomes a function of deterministic, authority-grounded judgments rather than raw
LLM-asserted stances.

OWNERSHIP / SEAM
----------------
This module is process-owned. It depends on two matcher-owned operations,
injected so the two sides build in parallel against the stub:

  - ``evaluate`` — ``evaluate_datum_against_differential`` (matcher body; the stub
    returns ``[]`` so this loop is inert until it lands).
  - ``resolve_root`` — ``(case, record, *, may_instantiate) -> Optional[str]``:
    the cause's ROOT node id, instantiating the chain (lazy promotion, via the
    matcher's ``instantiate_cause_chain`` + root resolution) when
    ``may_instantiate`` and it does not yet exist; ``None`` if it cannot be
    resolved/instantiated. This is the one NEW seam symbol the build surfaced — it
    is matcher-owned (only the matcher knows the CauseRecord→node mapping) and is
    flagged for ratification; injecting it here keeps it off the shared seam file
    until then.

Lazy promotion is on a SUPPORTS only (``may_instantiate=True``); a REFUTES verdict
for a cause that was never promoted has no node to attach to and is skipped —
we do not instantiate a chain just to mark it refuted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Protocol

from faultmaven.core.investigation.differential_intake import (
    StanceVerdict,
    assemble_active_causes,
    evaluate_datum_against_differential,
)
from faultmaven.modules.case.contracts import EvidenceStance, NodeEvidenceLink

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from faultmaven.core.investigation.cause_schemas import CauseRecord
    from faultmaven.modules.case.contracts import Case, Evidence


class ActiveCauseLike(Protocol):
    """What the intake step consumes from each differential candidate — the
    matcher's ``ActiveCause`` satisfies this (``candidate_id`` + ``record``)."""

    candidate_id: str
    record: "CauseRecord"


# (case, record, *, may_instantiate) -> root node id or None
RootResolver = Callable[..., Optional[str]]


def _attach_verdict_link(
    node, evidence: "Evidence", verdict: StanceVerdict, current_turn: int
) -> bool:
    """Attach a ``NodeEvidenceLink`` for ``verdict`` to ``node``; return True if a
    link was added. Idempotent: a link for the same (evidence, stance) is not
    duplicated, so re-evaluating the same datum across turns does not inflate the
    tally."""
    for link in node.evidence_links:
        if link.evidence_id == evidence.evidence_id and link.stance == verdict.stance:
            return False
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=evidence.evidence_id,
            stance=verdict.stance,
            reasoning=f"deterministic intake ({verdict.provenance}): predicate "
            f"{verdict.predicate} fired",
            provenance=verdict.provenance,
            linked_at_turn=current_turn,
        )
    )
    return True


def run_intake_evaluation(
    case: "Case",
    new_evidence: "list[Evidence]",
    active_causes: "list[ActiveCauseLike]",
    current_turn: int,
    *,
    resolve_root: RootResolver,
    evaluate: Callable[..., list[StanceVerdict]] = evaluate_datum_against_differential,
) -> list[StanceVerdict]:
    """Evaluate each new datum against the differential and record the resulting
    verdicts as node-evidence links (with provenance).

    Returns the verdicts that were recorded (for turn-progress accounting). A
    verdict is recorded iff its candidate is known, its root node resolves
    (instantiated on a first SUPPORTS), and the link is not a duplicate.
    """
    records_by_id = {ac.candidate_id: ac.record for ac in active_causes}
    recorded: list[StanceVerdict] = []

    for evidence in new_evidence:
        for verdict in evaluate(
            evidence=evidence, active_causes=active_causes, case=case
        ):
            record = records_by_id.get(verdict.cause_id)
            if record is None:
                continue  # verdict for a candidate not in the differential
            root_id = resolve_root(
                case,
                record,
                may_instantiate=verdict.stance == EvidenceStance.SUPPORTS,
            )
            if root_id is None:
                continue  # REFUTES on an un-promoted cause, or no instantiable root
            node = case.causal_nodes.get(root_id)
            if node is None:
                continue
            if _attach_verdict_link(node, evidence, verdict, current_turn):
                recorded.append(verdict)

    return recorded


async def run_differential_intake_turn(
    case: "Case",
    new_evidence: "list[Evidence]",
    current_turn: int,
    *,
    runbook_ids: "list[str]",
    resolve_causes: "Callable[[str], Awaitable[list[dict] | None]]",
    build_records: "Callable[[str, list[dict]], list[CauseRecord]]",
    resolve_root: RootResolver,
    evaluate: Callable[..., list[StanceVerdict]] = evaluate_datum_against_differential,
) -> list[StanceVerdict]:
    """Per-turn driver: rebuild the standing differential from its persisted
    runbook source and validate this turn's new data against it.

    The matcher establishes WHICH runbook(s) are the differential once per case
    (one-shot); this runs EVERY turn so each newly submitted datum is checked
    against the candidates' predicates — that is what keeps a confident-but-wrong
    LLM from driving an unsupported cause to IDENTIFIED on a later turn. The
    candidates are re-resolved each turn (``resolve_causes`` is a cheap DB read,
    not an LLM call), so nothing heavyweight is persisted — only the runbook
    id(s).

    No new data, or no runbook source yet (the matcher has not fired), ⇒ a no-op
    returning ``[]`` — the loop is inert until the differential is established.
    """
    if not new_evidence or not runbook_ids:
        return []
    matched: list[tuple[str, list[CauseRecord]]] = []
    for runbook_id in runbook_ids:
        raw = await resolve_causes(runbook_id)
        if not raw:
            continue
        try:
            # ``build_records`` is tolerant per entry (a malformed cause is logged
            # and skipped, not raised) — so this only guards a wholesale failure.
            records = build_records(runbook_id, raw)
        except Exception:  # noqa: BLE001 — a prior must never break the turn
            continue
        if records:
            matched.append((runbook_id, records))

    active_causes = assemble_active_causes(matched)
    if not active_causes:
        return []
    return run_intake_evaluation(
        case,
        new_evidence,
        active_causes,
        current_turn,
        resolve_root=resolve_root,
        evaluate=evaluate,
    )
