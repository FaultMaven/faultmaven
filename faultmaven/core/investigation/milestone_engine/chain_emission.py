"""Emitting an LLM-authored causal chain onto the case graph, and the garbage-collection and ambiguity-nudge passes that keep it consistent afterward."""

import logging
from typing import Any, Sequence

from faultmaven.core.investigation.causal_graph.ingestion import (
    chain_path_to_problem,
    ingest_emitted_chain,
    mirror_hypothesis_support_to_root_nodes,
)
from faultmaven.core.investigation.causal_graph.pruning import (
    attach_root,
    prune_abandoned_nodes,
    resolve_orphan_chains,
)
from faultmaven.core.investigation.cause_assurance import ENGINE_EVIDENCE_AUTHOR
from faultmaven.core.investigation.lifecycle_metrics import (
    hypothesis_root_adoption_refused_total,
)
from faultmaven.core.investigation.problem_status import (
    cause_work_accepted,
    cause_work_staged,
)
from faultmaven.modules.case.contracts import (
    Case,
    EvidenceCategory,
    NodeType,
)

from .cause_work import refuse_cause_work
from .stage_gates import (
    _add_system_feedback,
)
from .statement_revision import emission_turn
from .turn_records import _resolve_id_ref

logger = logging.getLogger(__name__)


def record_root_verdict(metadata: dict[str, Any], hyp_id: str, entry: Any) -> None:
    """Carry a hypothesis entry's why-stopping verdict (``root_is_terminal``)
    to chain emission, which records it on the hypothesis's root once that root
    is resolved: the root is commonly emitted, or re-rooted onto, in the same
    turn (#2036). The rows the verdict cites resolve here, through this turn's
    evidence slots, like every other cited id."""
    verdict = getattr(entry, "root_is_terminal", None)
    if not isinstance(verdict, bool):
        return
    slots = metadata.get("evidence_slots", [])
    cited = [
        _resolve_id_ref(ref, slots, "ev_")
        for ref in getattr(entry, "terminal_basis_evidence_ids", None) or []
    ]
    metadata.setdefault("hyp_root_verdicts", {})[hyp_id] = (verdict, cited)


def resolve_root_ref(case: Case, ref: str | None, created: Sequence[str]) -> str | None:
    """Resolve a root ref (a cn_ id, or ``new_index_N`` into ``created``, the
    emission's nodes) to a ROOT node id, or None.

    A hypothesis root must be a ROOT node (M1/M3) — refs that resolve to
    an intermediate or to the PROBLEM node D are rejected (the hypothesis
    stays flat). A conclusion's same-turn ``names_root_node_id`` resolves
    here too, and so does the licence a fix records from it (#2036 D2.3).
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

    These stamps are dated at the emission's turn (``emission_turn``), a
    replayed bundle's own, not its confirmation (#2537): the nodes and edges
    it ingests, the root attaches (``root_attached_turn``,
    ``root_first_attached_turn``) and the deepening's terminal record. The
    mirrored root links (``linked_at_turn``) and a recorded verdict's
    ``root_terminal_turn`` / ``root_terminal_refused_turn`` keep the current
    turn; no floor reads them.
    """
    nodes = list(getattr(updates, "causal_nodes_to_add", None) or [])
    edges = list(getattr(updates, "causal_edges_to_add", None) or [])
    deductive = list(getattr(updates, "deductive_validations", None) or [])
    node_links = list(getattr(updates, "node_evidence_links", None) or [])
    root_refs = dict(metadata.get("hyp_root_refs", {}))
    verdicts = dict(metadata.get("hyp_root_verdicts", {}))
    if cause_work_staged(case):
        # Held on the pending revision at step 2d, replayed on confirmation.
        # A re-root sent through hypotheses_to_update is not staged (updates
        # apply while a revision waits), so it is refused rather than lost
        # without a word: the model sends it again once the problem is
        # verified.
        nodes, edges, deductive, node_links = [], [], [], []
        if root_refs or verdicts:
            refuse_cause_work(
                case.case_id,
                metadata,
                kind="chain",
                what=(
                    f"{len(root_refs)} hypothesis root refs, {len(verdicts)} "
                    "root verdicts"
                ),
            )
            root_refs, verdicts = {}, {}
    elif (nodes or edges or deductive or root_refs or verdicts) and not (
        cause_work_accepted(case)
    ):
        refuse_cause_work(
            case.case_id,
            metadata,
            kind="chain",
            what=(
                f"a causal chain ({len(nodes)} nodes, {len(edges)} edges, "
                f"{len(deductive)} deductive validations, {len(root_refs)} "
                f"hypothesis root refs, {len(verdicts)} root verdicts)"
            ),
        )
        nodes, edges, deductive, root_refs, verdicts = [], [], [], {}, {}
    turn = emission_turn(case, metadata)
    created = ingest_emitted_chain(
        case,
        nodes,
        edges,
        node_links,
        turn,
        evidence_created_ids=metadata.get("evidence_slots", []),
        validation_repairs=metadata.setdefault("validation_repairs", []),
    )
    # What a same-turn fix's ``node_ref: new_index_N`` resolves against (#2036).
    metadata["chain_nodes_created"] = list(created)

    def _resolve_root(ref: str | None) -> str | None:
        return resolve_root_ref(case, ref, created)

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
        attach_root(case, hyp, root_id, new_path, turn)
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

    # The why-stopping verdict lands on the root its entry named. A verdict sent
    # with a re-root the engine declined or refused describes a root this
    # hypothesis does not have, so it is dropped rather than pinned on the old one.
    # A hypothesis still flat here may gain its root in the orphan pass
    # (``_nudge_ambiguous_orphan_chains``); its verdict waits for that.
    pending: dict[str, tuple] = {}
    for hyp_id, (verdict, cited) in verdicts.items():
        hyp = case.hypotheses.get(hyp_id)
        if hyp is None:
            continue
        if not hyp.root_node_id:
            pending[hyp_id] = (verdict, cited)
            continue
        if hyp_id in root_refs and _resolve_root(root_refs[hyp_id]) != hyp.root_node_id:
            continue
        _record_verdict_on_root(case, hyp, verdict, cited, metadata)
    metadata["hyp_root_verdicts_pending"] = pending

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


def _record_verdict_on_root(
    case: "Case", hyp: Any, verdict: bool, cited: list, metadata: dict[str, Any]
) -> None:
    """Record ``verdict`` on ``hyp``'s root. A ``True`` verdict stands only on
    a cited row that qualifies (``_terminal_basis``, D6); its new rows extend a
    ``True`` that already stands, and one that adds none leaves it as it is
    (any rows it cited that do not qualify are named).
    One citing none on a root without a standing ``True`` is refused with a
    feedback line, marked on the node (``root_terminal_refused_turn``, read by
    the graph block's note), and the root's why stays open."""
    node = case.causal_nodes.get(hyp.root_node_id or "")
    if node is None or node.node_type != NodeType.ROOT:
        return
    if not verdict:
        node.record_root_terminal(False, None, case.current_turn)
        return
    kept, refused = _terminal_basis(case, node, cited)
    standing = (
        list(node.metadata.get("root_terminal_evidence_ids") or [])
        if node.root_terminal is True
        else []
    )
    if kept or standing:
        added = [ref for ref in kept if ref not in standing]
        if added or not standing:
            node.record_root_terminal(True, standing + added, case.current_turn)
        if refused and not kept:
            _add_system_feedback(
                metadata,
                f"Root {node.node_id}: not qualifying: {'; '.join(refused)} "
                "(the root's verdict stands on its cited rows).",
            )
        return
    node.metadata["root_terminal_refused_turn"] = case.current_turn
    _add_system_feedback(
        metadata,
        f"Root {node.node_id} was NOT marked terminal: root_is_terminal true "
        "must cite, in terminal_basis_evidence_ids, a causal_evidence row "
        f"recorded since this root was posited (turn "
        f"{node.root_first_attached_turn}) showing that nothing controllable "
        "produces its state (how it was issued or set, its lifetime, that "
        "nothing renews it). "
        + (f"Not qualifying: {'; '.join(refused)}. " if refused else "None cited. ")
        + "Its why stays open.",
    )


def _terminal_basis(case: "Case", node: Any, cited: list) -> tuple[list, list]:
    """The rows a ``True`` verdict on ``node`` cites, split into those that
    qualify and why each other does not. A row qualifies when it is a
    non-engine causal_evidence row recorded (``collected_at_turn``, not the
    upload's date) at or after the root was first posited, which a staged
    turn replayed after its revision is confirmed dates at its own turn
    (``emission_turn``, #2537), so the same emission counts. This is a floor:
    it cannot judge what the row says, so a row showing only that the state
    exists still qualifies."""
    rows = {e.evidence_id: e for e in case.evidence}
    floor = node.root_first_attached_turn
    kept, refused = [], []
    for ref in dict.fromkeys(cited):
        row = rows.get(ref)
        if row is None:
            refused.append(f"{ref} (not on record)")
        elif row.category != EvidenceCategory.CAUSAL_EVIDENCE:
            refused.append(f"{ref} ({row.category.value}, not causal_evidence)")
        elif row.collected_by == ENGINE_EVIDENCE_AUTHOR:
            refused.append(f"{ref} (recorded by the engine)")
        elif row.collected_at_turn < floor:
            refused.append(
                f"{ref} (recorded at turn {row.collected_at_turn}, before the "
                "root was posited)"
            )
        else:
            kept.append(ref)
    return kept, refused


def _nudge_ambiguous_orphan_chains(case: "Case", metadata: dict[str, Any]) -> None:
    """Run the orphan-chain resolution post-pass. ``resolve_orphan_chains``
    re-attaches any UNAMBIGUOUS double-representation in place (T1); for the
    ambiguous remainder it returns the orphan + its candidate hypotheses,
    which we surface to the LLM next turn via ``system_feedback`` (T2a) so it
    re-roots or declares the chain separate — the engine does not guess.

    A verdict sent with a hypothesis that was still flat at chain emission is
    recorded on the root T1 gave it (#2036)."""
    ambiguous = resolve_orphan_chains(case, turn=emission_turn(case, metadata))
    for hyp_id, (verdict, cited) in (
        metadata.pop("hyp_root_verdicts_pending", None) or {}
    ).items():
        hyp = case.hypotheses.get(hyp_id)
        if hyp is not None and hyp.root_node_id:
            _record_verdict_on_root(case, hyp, verdict, cited, metadata)
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
