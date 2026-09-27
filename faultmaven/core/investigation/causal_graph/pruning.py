from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import NodeType

from .ingestion import chain_path_to_problem
from .similarity import (
    RESTATEMENT_AMBIGUOUS,
    RESTATEMENT_STRONG,
    _substantive_overlap,
    restatement_score,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis


def _referenced_node_ids(case: Case) -> set[str]:
    """Every node id that lies on some hypothesis path or is a hypothesis root —
    the single definition of "load-bearing" used by both the GC and the
    orphan-resolution post-pass."""
    referenced: set[str] = set()
    for h in case.hypotheses.values():
        if h.root_node_id:
            referenced.add(h.root_node_id)
        referenced.update(h.path or [])
    return referenced


def prune_abandoned_nodes(case: Case, abandoned_node_ids: list[str]) -> None:
    """Drop the nodes of a chain abandoned by a hypothesis re-root, but only the
    ones now dead — referenced by no hypothesis (as a root or on a path). The
    PROBLEM node D is never collected (it anchors every chain). Edges are pruned
    to those whose endpoints both survive, so a surviving node's connectivity is
    never severed. No-op for any node still load-bearing for another hypothesis.
    """
    referenced = _referenced_node_ids(case)
    for node_id in abandoned_node_ids:
        node = case.causal_nodes.get(node_id)
        if node is None or node.node_type == NodeType.PROBLEM:
            continue  # keep D; skip already-gone
        if node_id in referenced:
            continue  # still load-bearing for some hypothesis
        case.causal_nodes.pop(node_id, None)
    case.causal_edges[:] = [
        e
        for e in case.causal_edges
        if e.cause_node_id in case.causal_nodes
        and e.effect_node_id in case.causal_nodes
    ]


def _hypothesis_lacks_real_chain(hyp: "Hypothesis") -> bool:
    """True when the hypothesis is flat or carries only a degenerate stub (a
    2-node root->D path). Re-attaching only such a hypothesis avoids clobbering
    one that already owns a real multi-rung chain — that case is a genuine
    separate representation, left for an LLM nudge instead."""
    return not hyp.path or len(hyp.path) <= 2


def resolve_orphan_chains(case: Case) -> list[dict]:
    """Resolve emitted chains the LLM left unlinked (the invariant: every chain
    explaining D attaches to exactly one hypothesis). Run AFTER chain ingest.

    For each ORPHAN root (a ROOT node on no hypothesis path, anchoring a chain
    that reaches D), score its statement against every hypothesis:

    - **T1 — deterministic re-attach.** Exactly one hypothesis restates it
      ``>= RESTATEMENT_STRONG`` with no other ``>= RESTATEMENT_AMBIGUOUS``, and
      that hypothesis lacks a real chain of its own: re-root it onto the orphan
      chain and GC its abandoned stub. Mutates the graph in place.
    - **T2a — ambiguous.** The orphan restates a hypothesis but not
      unambiguously (best in ``[AMBIGUOUS, STRONG)``, or two-plus hypotheses
      ``>= AMBIGUOUS``, or the sole strong match already owns a real chain):
      returned for the caller to surface as a one-turn LLM nudge. NOT
      auto-resolved.
    - **benign.** Matches no hypothesis (best ``< AMBIGUOUS``): left as a
      standalone candidate root ("an unexplained candidate root is fine").

    Returns the ambiguous orphans (each ``{root_id, statement,
    candidate_hypotheses}``) for T2a; T1 re-attachments are applied in place.
    """
    if not any(n.node_type == NodeType.PROBLEM for n in case.causal_nodes.values()):
        return []

    # ``referenced`` is the set of load-bearing nodes; it only changes when a T1
    # re-attach below mutates a hypothesis path, so compute it once and refresh
    # it only after an actual re-attach (not every iteration). Snapshot the
    # orphan-root candidates up front; a prior re-attach may GC or adopt a later
    # candidate, so re-check existence/membership per root.
    referenced = _referenced_node_ids(case)
    orphan_root_ids = [
        nid
        for nid, n in case.causal_nodes.items()
        if n.node_type == NodeType.ROOT and nid not in referenced
    ]

    # Hypotheses already re-rooted in THIS pass — excluded from later scoring so
    # one orphan cannot re-attach a hypothesis a previous orphan already took
    # (no churn), and an already-taken hypothesis cannot inflate another orphan's
    # ambiguity count and wrongly downgrade its clean match to a nudge.
    adopted: set[str] = set()
    ambiguous: list[dict] = []
    for root_id in orphan_root_ids:
        node = case.causal_nodes.get(root_id)
        if node is None or root_id in referenced:
            continue  # GC'd or adopted onto a path by a prior re-attach
        path = chain_path_to_problem(root_id, case)
        if not path:
            continue  # open chain not yet anchored to D — leave it

        scored = [
            (s, h)
            for h in case.hypotheses.values()
            if h.hypothesis_id not in adopted
            and (s := restatement_score(node.statement, h.statement))
            >= RESTATEMENT_AMBIGUOUS
        ]
        if not scored:
            continue  # benign standalone candidate root

        # T1: exactly one hypothesis matches (so the sole match is also the only
        # one >= STRONG, since STRONG > AMBIGUOUS), it owns no real chain, and the
        # overlap is substantive (not a single-word containment artifact).
        if (
            len(scored) == 1
            and scored[0][0] >= RESTATEMENT_STRONG
            and _hypothesis_lacks_real_chain(scored[0][1])
            and _substantive_overlap(node.statement, scored[0][1].statement)
        ):
            hyp = scored[0][1]
            old_path = hyp.path or []
            hyp.root_node_id = root_id
            hyp.path = path
            prune_abandoned_nodes(case, old_path)
            adopted.add(hyp.hypothesis_id)
            referenced = _referenced_node_ids(case)  # graph changed — refresh
            continue

        # T2a: matched but ambiguous (or the strong match already owns a chain).
        ambiguous.append(
            {
                "root_id": root_id,
                "statement": node.statement,
                "candidate_hypotheses": [
                    h.statement
                    for _, h in sorted(scored, key=lambda sh: sh[0], reverse=True)
                ],
            }
        )
    return ambiguous
