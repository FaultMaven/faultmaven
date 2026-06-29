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
    evaluate_datum_against_differential,
)
from faultmaven.modules.case.contracts import EvidenceStance, NodeEvidenceLink

if TYPE_CHECKING:
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
