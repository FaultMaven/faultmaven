"""Emitting an LLM-authored causal chain onto the case graph, and the garbage-collection and ambiguity-nudge passes that keep it consistent afterward."""

import logging
from typing import Any

from faultmaven.core.investigation.causal_graph.ingestion import (
    chain_path_to_problem,
    ingest_emitted_chain,
    mirror_hypothesis_support_to_root_nodes,
)
from faultmaven.core.investigation.causal_graph.pruning import (
    prune_abandoned_nodes,
    resolve_orphan_chains,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    hypothesis_root_adoption_refused_total,
)
from faultmaven.core.investigation.problem_status import (
    cause_work_accepted,
    cause_work_staged,
)
from faultmaven.modules.case.contracts import (
    Case,
    NodeType,
)

from .cause_work import refuse_cause_work
from .stage_gates import (
    _add_system_feedback,
)

logger = logging.getLogger(__name__)


def _apply_chain_emission(
    case: "Case",
    updates: Any,
    metadata: dict[str, Any],
) -> None:
    """Ingest the LLM's emitted causal chain and link new hypotheses to their
    roots (the emitted chain is the sole source of the causal graph; the
    transitional flag and flat->chain bridge were removed).

    Lazy backward expansion (methodology §5/S3): build the graph from the
    emitted nodes/edges/node-evidence, then set ``root_node_id``/``path`` on
    each hypothesis whose spec carried a ``root_node_ref``. A hypothesis the
    LLM never links stays flat (``root_node_id`` is None) — the graph is
    emission-only, so there is no projection floor.

    Best-effort: an unresolvable ``root_node_ref`` leaves the hypothesis flat
    rather than raising. ``path`` may be ``[]`` when the chain has not yet
    reached ``D`` (still being expanded); the model permits ``root_node_id``
    set with an empty path.

    Chain STRUCTURE — new nodes, edges, deductive validations, and pointing a
    hypothesis at a root (``root_node_ref``, which can re-root a standing
    hypothesis and prune its old chain) — is cause work, accepted only on a
    verified problem (``cause_work_accepted``, read at the status the turn ends
    with, so a turn that verifies the symptom can emit its chain). Evidence
    links onto nodes that already stand follow the rule for links onto
    standing hypotheses and still apply; a link naming a refused same-turn
    node resolves to nothing and is skipped.
    """
    nodes = list(getattr(updates, "causal_nodes_to_add", None) or [])
    edges = list(getattr(updates, "causal_edges_to_add", None) or [])
    deductive = list(getattr(updates, "deductive_validations", None) or [])
    node_links = list(getattr(updates, "node_evidence_links", None) or [])
    root_refs = dict(metadata.get("hyp_root_refs", {}))
    if cause_work_staged(case):
        # Held on the pending revision at step 2d, replayed on confirmation.
        # A re-root sent through hypotheses_to_update is not staged (updates
        # apply while a revision waits), so it is refused rather than lost
        # without a word: the model sends it again once the problem is
        # verified.
        nodes, edges, deductive, node_links = [], [], [], []
        if root_refs:
            refuse_cause_work(
                case.case_id,
                metadata,
                kind="chain",
                what=f"{len(root_refs)} hypothesis root refs",
            )
            root_refs = {}
    elif (nodes or edges or deductive or root_refs) and not cause_work_accepted(case):
        refuse_cause_work(
            case.case_id,
            metadata,
            kind="chain",
            what=(
                f"a causal chain ({len(nodes)} nodes, {len(edges)} edges, "
                f"{len(deductive)} deductive validations, {len(root_refs)} "
                "hypothesis root refs)"
            ),
        )
        nodes, edges, deductive, root_refs = [], [], [], {}
    created = ingest_emitted_chain(
        case,
        nodes,
        edges,
        node_links,
        case.current_turn,
        evidence_created_ids=metadata.get("evidence_added", []),
        validation_repairs=metadata.setdefault("validation_repairs", []),
    )

    def _resolve_root(ref: str | None) -> str | None:
        """Resolve a root_node_ref to a ROOT node id, or None.

        A hypothesis root must be a ROOT node (M1/M3) — refs that resolve to
        an intermediate or to the PROBLEM node D are rejected (the hypothesis
        stays flat).
        """
        if not ref:
            return None
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            node_id = created[idx] if 0 <= idx < len(created) else None
        else:
            node_id = ref if ref in case.causal_nodes else None
        node = case.causal_nodes.get(node_id) if node_id else None
        return node_id if node is not None and node.node_type == NodeType.ROOT else None

    # Link each hypothesis to its chain root via the explicit
    # hyp_id -> root_node_ref map (recorded at creation, or on a re-root
    # update when the LLM elaborates a previously-posited hypothesis into a
    # real chain). Re-rooting abandons the hypothesis's old chain; collect any
    # of its now-dead nodes so the elaborated chain does not co-exist with the
    # abandoned degenerate stub for the same cause (the double-representation /
    # orphan-chain divergence).
    def _other_owner(hyp_id: str, root_id: str):
        """The OTHER hypothesis currently rooted at ``root_id``, if any."""
        return next(
            (
                h
                for h in case.hypotheses.values()
                if h.hypothesis_id != hyp_id and h.root_node_id == root_id
            ),
            None,
        )

    def _attach(hyp, root_id: str) -> list | None:
        """Point ``hyp`` at ``root_id``. Returns the path it abandoned (``[]``
        when it abandoned nothing), or None when the move was declined.

        On a RE-ROOT (the hypothesis already had a root) only move it once
        the new chain actually reaches D. Abandoning a working [root, D]
        link for an empty path would strand the hypothesis: the graph is
        emission-only (no projection floor), so nothing would restore the
        link this turn. At creation (no prior root) an empty path is fine —
        there was no link to lose.
        """
        old_root = hyp.root_node_id
        old_path = hyp.path or []
        new_path = chain_path_to_problem(root_id, case)
        if old_root and old_root != root_id and not new_path:
            return None
        hyp.root_node_id = root_id
        hyp.path = new_path
        return old_path if (old_root and old_root != root_id) else []

    # One cause, one chain (M3/§7.8.1): a chain root belongs to exactly ONE
    # hypothesis. A ref naming a root ANOTHER hypothesis owns is REFUSED —
    # adopting a foreign chain silently re-labels this hypothesis's cause with
    # the owner's statement, and everything derived from the root afterwards
    # (the mirrored support, the node state, the VALIDATED projection back onto
    # the hypothesis, the report's causal map) then speaks about a cause this
    # hypothesis never claimed. Observed live (fm#1091): a cache-exhaustion
    # hypothesis adopted the root of a REFUTED runner-out-of-memory hypothesis,
    # and the resolution summary drew that refuted statement as the validated
    # cause of the problem while the real cause appeared nowhere in the map.
    #
    # Contested refs are settled in a SECOND pass, because the batch is applied
    # in emission order (adds before re-roots) and a root that is owned when we
    # first read it may be FREED by a re-root later in the same batch — the
    # hand-off shape, where the owner deepens onto a new root and the old one
    # becomes the new hypothesis's cause. Judging on first read would refuse a
    # hand-off the model expressed correctly, AND then GC the very chain it
    # handed over.
    abandoned: list[list] = []
    contested: list[tuple[str, str]] = []
    for hyp_id, ref in root_refs.items():
        root_id = _resolve_root(ref)
        hyp = case.hypotheses.get(hyp_id)
        if root_id is None or hyp is None:
            continue
        if _other_owner(hyp_id, root_id) is not None:
            contested.append((hyp_id, root_id))
            continue
        freed = _attach(hyp, root_id)
        if freed:
            abandoned.append(freed)

    for hyp_id, root_id in contested:
        hyp = case.hypotheses.get(hyp_id)
        if hyp is None:
            continue
        owner = _other_owner(hyp_id, root_id)
        if owner is None:  # freed by a re-root above — the hand-off, honored
            freed = _attach(hyp, root_id)
            if freed:
                abandoned.append(freed)
            continue
        hypothesis_root_adoption_refused_total.inc()
        _add_system_feedback(
            metadata,
            f"Hypothesis {hyp_id} was NOT anchored to node {root_id}: "
            f"that node is already the chain root of {owner.hypothesis_id} "
            f"('{(owner.statement or '')[:80]}'). One cause = one chain. "
            f"Emit a NEW root node stating THIS hypothesis's own cause and "
            f"point its root_node_ref at it — or, if the two are the same "
            f"cause, update {owner.hypothesis_id} instead of keeping both.",
        )
        logger.info(
            "Refused hypothesis root adoption (fm#1091): %s -> %s owned by %s",
            hyp_id,
            root_id,
            owner.hypothesis_id,
        )

    # GC runs only once every move is settled: a chain abandoned by a re-root
    # may have been ADOPTED by a hand-off in the second pass, and
    # prune_abandoned_nodes drops only what no hypothesis still references.
    for old_path in abandoned:
        prune_abandoned_nodes(case, old_path)

    # B1 (#695): mirror each hypothesis's flat causal SUPPORTS links onto its
    # (now-linked) chain ROOT node. The flat hypothesis_evidence and
    # causal_node_evidence axes are disjoint, so grounding the LLM recorded
    # only on the hypothesis left its root node with zero causal support and
    # uncertifiable. Runs AFTER root_node_id is assigned above and BEFORE the
    # derive_node_states recompute; provides candidate links only (the
    # independence/restatement/AND-gate filters still decide validation).
    mirror_hypothesis_support_to_root_nodes(case, case.current_turn)

    # B2 (#695): resolve the RCC's names_root_node_id placeholder. When the
    # LLM names its cause's root as a same-turn new_index_N ref, ingest
    # resolved that ref for nodes/evidence/hypotheses but not for the RCC —
    # it persisted as the placeholder, and Tier-1 RCC->hypothesis attribution
    # (link_llm_rcc_to_cause) could never match a real cn_ id, so
    # validated_hypothesis_id stayed null. Resolve it here against the same
    # `created` list, on the AUTHORING turn only (the placeholder indexes
    # THIS turn's emission; a prior-turn placeholder would mis-resolve). A
    # ref that resolves to a non-root / unknown node becomes None — an honest
    # "unnamed" that falls through to the Tier-2 lexical fallback.
    if metadata.get("rcc_authored_this_turn") and case.root_cause_conclusion:
        named = getattr(case.root_cause_conclusion, "names_root_node_id", None)
        if named and named.startswith("new_index_"):
            case.root_cause_conclusion.names_root_node_id = _resolve_root(named)

    # Deductive validation (§7.1.1): resolve the ROOT survivors the LLM
    # certified as the sole survivor of an EXHAUSTIVE differential. The
    # resolved id set is the exhaustiveness assertion (guard #1 — the one the
    # engine cannot compute); it is stashed for the assessment recompute, which
    # runs ``validate_by_exclusion`` AFTER ``derive_node_states`` has settled the
    # siblings' states so the "all-but-survivor absolutely refuted" guard can be
    # checked. ``_resolve_root`` enforces ROOT-only (a survivor must be a root
    # cause); an unresolvable/non-root ref is silently dropped.
    survivor_ids: set[str] = set()
    for dv in deductive:
        root_id = _resolve_root(getattr(dv, "survivor_node_ref", None))
        if root_id is not None:
            survivor_ids.add(root_id)
    if survivor_ids:
        metadata["deductive_survivor_ids"] = survivor_ids


def _nudge_ambiguous_orphan_chains(case: "Case", metadata: dict[str, Any]) -> None:
    """Run the orphan-chain resolution post-pass. ``resolve_orphan_chains``
    re-attaches any UNAMBIGUOUS double-representation in place (T1); for the
    ambiguous remainder it returns the orphan + its candidate hypotheses,
    which we surface to the LLM next turn via ``system_feedback`` (T2a) so it
    re-roots or declares the chain separate — the engine does not guess."""
    ambiguous = resolve_orphan_chains(case)
    if not ambiguous:
        return
    lines = [
        "Unlinked causal chain(s) may restate an existing hypothesis. If a "
        "chain and a hypothesis are the SAME cause, re-root the hypothesis "
        "onto the chain (set its root_node_ref); if they are different "
        "causes, keep them separate:"
    ]
    for orphan in ambiguous[:3]:
        cands = "; ".join(orphan["candidate_hypotheses"][:2])
        lines.append(
            f"- chain root '{orphan['statement'][:80]}' ~ hypothesis '{cands[:120]}'"
        )
    current = metadata.get("system_feedback", "") or ""
    metadata["system_feedback"] = "\n".join([current, *lines]).strip()
