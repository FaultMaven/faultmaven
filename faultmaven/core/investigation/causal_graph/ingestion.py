from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import absence_row_link_refused
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    settle_set_aside_link,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    causal_and_group_regroup_refused_total,
    hypothesis_support_mirrored_to_root_total,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    EvidenceCategory,
    EvidenceStance,
    NodeEvidenceLink,
    NodeType,
)

from .clusters import _normalize_and_group, _observe_late_grouping

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger("faultmaven.core.investigation.causal_graph")


# ---------------------------------------------------------------------------
# Graph anchoring + path walking (the PROBLEM node D + root->D paths)
# ---------------------------------------------------------------------------


def seed_problem_node(case: Case) -> CausalNode | None:
    """Return the case's single PROBLEM node ``D``, creating it from the
    confirmed problem statement when absent.

    ``D`` is engine-owned (deterministic), not LLM-emitted: it anchors every
    chain. Returns None when there is no problem statement to anchor on yet.
    Idempotent — at most one PROBLEM node per case.
    """
    problem_node = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem_node is not None:
        return problem_node
    pv = case.problem_verification
    statement = pv.symptom_statement if pv else None
    if not statement or not statement.strip():
        return None
    problem_node = CausalNode(
        statement=statement[:500],
        node_type=NodeType.PROBLEM,
        generated_at_turn=case.current_turn,
    )
    case.causal_nodes[problem_node.node_id] = problem_node
    return problem_node


def chain_path_to_problem(root_id: str, case: Case) -> list[str]:
    """Walk cause→effect edges from ``root_id`` down to the PROBLEM node ``D``,
    returning the ordered path ``[root_id, ..., d_id]`` (methodology: a
    ``Hypothesis`` is a root→D path).

    Breadth-first search for the shortest ``root → D`` route. A node may have
    several downstream edges (convergence, S2) and some branches dead-end; a
    greedy single-arrow walk would wrongly report an open chain when it picked a
    dead branch first, so the search explores all branches. Returns ``[]`` if no
    path reaches ``D`` (the chain is still open, or ``root_id`` *is* ``D`` — a
    root cause cannot be the symptom itself) — the caller then leaves
    ``root_node_id``/``path`` unset.
    """
    problem = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem is None or root_id not in case.causal_nodes:
        return []
    d_id = problem.node_id
    if root_id == d_id:
        return []  # the symptom is not its own root cause
    # Adjacency: cause -> [effects].
    out: dict[str, list[str]] = {}
    for e in case.causal_edges:
        out.setdefault(e.cause_node_id, []).append(e.effect_node_id)
    parent: dict[str, str | None] = {root_id: None}
    queue: deque[str] = deque([root_id])
    while queue:
        cur = queue.popleft()
        if cur == d_id:
            path: list[str] = []
            node: str | None = d_id
            while node is not None:
                path.append(node)
                node = parent[node]
            return list(reversed(path))
        for nxt in out.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return []  # open chain — no route to D


def _normalize_statement(s: str | None) -> str:
    """Canonical key for exact-match node identity (engine ingest dedup):
    whitespace-collapsed, lowercased, capped at the same 500 chars
    ``CausalNode`` stores — so both operands of a comparison normalize
    identically (no asymmetric truncation)."""
    return " ".join((s or "")[:500].split()).lower()


def ingest_emitted_chain(
    case: Case,
    nodes_to_add: list,
    edges_to_add: list,
    node_evidence: list,
    current_turn: int,
    evidence_created_ids: list | None = None,
    validation_repairs: list[str] | None = None,
) -> list[str | None]:
    """Build the causal graph from a turn's LLM-emitted chain fragments (lazy
    backward expansion, methodology §5/S3). Pure: no I/O, no LLM.

    The sole source of the causal graph (the transitional bridge was removed in
    PR B2c). Each item is a duck-typed schema object:

    - ``nodes_to_add`` — ``statement``, ``node_type``, optional ``produces``
      (the node it directly causes: an existing id, ``'D'``, or ``'new_index_N'``
      into this same list) and ``and_group``.
    - ``edges_to_add`` — explicit ``cause``/``effect`` refs (+ ``and_group``,
      ``reasoning``) for convergence (S2) beyond a node's own ``produces``.
    - ``node_evidence`` — ``node_ref``, ``evidence_id``/``evidence_id_ref``,
      ``stance``, ``reasoning``, ``stance_confidence``. The evidence ref may be a
      real ``ev_...`` id or ``'new_index_N'`` referencing evidence created this
      turn (resolved via ``evidence_created_ids``).

    ``evidence_created_ids`` are the evidence ids added earlier this turn (the
    caller's ``metadata['evidence_added']``), against which ``new_index_N``
    evidence refs resolve — without it, same-turn rung evidence is dropped.

    ``validation_repairs``, when given, receives one line per out-of-range
    ``stance_confidence`` decided here (fm#1502) — the caller passes the turn's
    ``metadata['validation_repairs']``. Each decision is also counted on
    ``faultmaven_schema_field_repairs_total``, with or without it.

    Returns the created node ids in emission order (``None`` for any skipped
    node, so ``new_index_N`` indices stay aligned), so the caller can resolve
    ``new_index_N`` references (e.g. linking a hypothesis to its root node)
    against them. Best-effort and
    never raises: unresolvable refs, unknown evidence, and malformed nodes
    (empty statement, or a type other than root/intermediate — ``D`` is
    engine-seeded, never emitted) are skipped; ``D`` is seeded if a problem
    statement exists, otherwise ingestion is a no-op.
    """
    evidence_created_ids = evidence_created_ids or []
    problem = seed_problem_node(case)
    if problem is None:
        return []
    d_id = problem.node_id

    # Pass 1: create the nodes; record ids in order for new_index_N resolution.
    # A skipped node holds None so later indices still line up.
    #
    # Identity reconciliation (engine-derive lane): if an emitted statement EXACTLY
    # restates an existing same-type node, REUSE that node instead of minting a
    # duplicate — so its id flows into edges, evidence, and the hypothesis root_ref,
    # keeping one cause on one node. The LLM re-asserts a standing cause on later
    # turns; prevention is the chain rendered in <causal_graph> (which it is told to
    # reference), and this is the safe backstop for a verbatim re-emit it does
    # anyway. Exact normalized match ONLY: a fuzzy threshold cannot separate a true
    # duplicate from a distinct OR-sibling differing in one parameter (the
    # over-merge trap), so paraphrases are deliberately NOT merged here. Reuse
    # covers intra-turn repeats too (earlier nodes are already in causal_nodes).
    # Index existing nodes by (type, normalized statement) ONCE — so the per-spec
    # dedup is an O(1) dict lookup, not a full re-scan + re-normalization of every
    # node on every spec (this path runs each DIAGNOSIS turn and the graph grows
    # over a case). Newly-minted nodes are added to the index so intra-turn
    # verbatim repeats also dedup; first id wins (the canonical node). Skip the
    # build entirely on the common no-emission turn — nothing to dedup against.
    canonical_by_key: dict[tuple, str] = {}
    if nodes_to_add:
        for nid, n in case.causal_nodes.items():
            canonical_by_key.setdefault(
                (n.node_type, _normalize_statement(n.statement)), nid
            )

    created: list[str | None] = []
    for spec in nodes_to_add:
        statement = (getattr(spec, "statement", None) or "").strip()
        node_type = getattr(spec, "node_type", None)
        if not statement or node_type not in (
            NodeType.ROOT,
            NodeType.INTERMEDIATE,
        ):
            # Empty statement (CausalNode rejects it) or a non-{root,intermediate}
            # type (a second PROBLEM node would violate the one-D-per-case index).
            created.append(None)
            continue
        # Identity reconciliation (engine-derive lane): an emitted statement that
        # EXACTLY restates an existing same-type node REUSES it rather than minting
        # a duplicate — so its id flows into edges, evidence, and the hypothesis
        # root_ref, keeping one cause on one node. The LLM re-asserts a standing
        # cause on later turns; prevention is the chain rendered in <causal_graph>
        # (which it is told to reference), and this is the safe backstop for a
        # verbatim re-emit. Exact normalized match ONLY: a fuzzy threshold cannot
        # separate a true duplicate from a distinct OR-sibling differing in one
        # parameter (the over-merge trap), so paraphrases are NOT merged here.
        key = (node_type, _normalize_statement(statement))
        canonical = canonical_by_key.get(key)
        if canonical is not None:
            created.append(canonical)  # reuse the canonical node, no duplicate
            continue
        node = CausalNode(
            statement=statement[:500],
            node_type=node_type,
            generated_at_turn=current_turn,
        )
        case.causal_nodes[node.node_id] = node
        canonical_by_key[key] = node.node_id
        created.append(node.node_id)

    def _resolve(ref: str | None) -> str | None:
        if not ref:
            return None
        if ref == "D":
            return d_id
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return created[idx] if 0 <= idx < len(created) else None
        return ref if ref in case.causal_nodes else None

    def _add_edge(cause_id, effect_id, and_group, reasoning):
        if not cause_id or not effect_id or cause_id == effect_id:
            return
        # An AND-set key is a GROUPING token — see _normalize_and_group for what
        # is folded away and why.
        and_group = _normalize_and_group(and_group)
        if cause_id not in case.causal_nodes or effect_id not in case.causal_nodes:
            return
        existing = next(
            (
                e
                for e in case.causal_edges
                if e.cause_node_id == cause_id and e.effect_node_id == effect_id
            ),
            None,
        )
        if existing is not None:
            # Idempotent on the EDGE — but an existing edge may still GAIN a
            # group. Co-necessity is usually recognized after the fact: the
            # model proposes A and B as independent candidates, and only later
            # sees that the problem needed both. Expressing that means
            # re-emitting the edges with a shared and_group, and a flat
            # "already exists" drop left the AND-set half-formed (one member
            # carrying the key), so no conjunction ever existed to render —
            # the #1096 factor loss, through a second door. Monotone by design:
            # an edge may go None -> group, never group -> other group (a
            # silent regrouping of a standing conjunction) and never
            # group -> None (a later ungrouped restatement is not a
            # retraction; treating it as one would make the published
            # conjunction flicker turn to turn).
            if existing.and_group is None and and_group is not None:
                existing.and_group = and_group
                _observe_late_grouping(case, effect_id, and_group)
            elif and_group != existing.and_group and existing.and_group is not None:
                # Refused, and the model gets no witness of it from here
                # (ingest is pure and has no system_feedback channel), so the
                # refusal is recorded where an operator can see it — the model
                # is now reasoning over a grouping the graph does not have.
                attempt = "ungroup" if and_group is None else "regroup"
                causal_and_group_regroup_refused_total.labels(attempt=attempt).inc()
                logger.info(
                    f"Refused an and_group {attempt} on edge "
                    f"{cause_id}->{effect_id} for case {case.case_id}: "
                    f"{existing.and_group!r} stands (the merge is monotone)",
                    extra={
                        "event": "causal_and_group_regroup_refused",
                        "case_id": case.case_id,
                        "turn": current_turn,
                        "attempt": attempt,
                        "standing_group": existing.and_group,
                        "emitted_group": and_group,
                    },
                )
            return
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=cause_id,
                effect_node_id=effect_id,
                and_group=and_group,
                reasoning=reasoning,
                created_at_turn=current_turn,
            )
        )

    # Pass 2: edges from each node's `produces`, then explicit edges.
    for i, spec in enumerate(nodes_to_add):
        produces = getattr(spec, "produces", None)
        if produces:
            _add_edge(
                created[i], _resolve(produces), getattr(spec, "and_group", None), None
            )
    for e in edges_to_add:
        _add_edge(
            _resolve(getattr(e, "cause", None)),
            _resolve(getattr(e, "effect", None)),
            getattr(e, "and_group", None),
            getattr(e, "reasoning", None),
        )

    # Node-targeted evidence (rung-level stance).
    existing_ev = {ev.evidence_id for ev in case.evidence}
    cat_by_id = _evidence_category_map(case)

    def _resolve_ev(ref: str | None) -> str | None:
        # Evidence ref: a real ev_ id, or 'new_index_N' into this turn's evidence.
        if not ref:
            return None
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return (
                evidence_created_ids[idx]
                if 0 <= idx < len(evidence_created_ids)
                else None
            )
        return ref

    for link in node_evidence:
        nid = _resolve(getattr(link, "node_ref", None))
        node = case.causal_nodes.get(nid) if nid else None
        ev_id = _resolve_ev(
            getattr(link, "evidence_id", None) or getattr(link, "evidence_id_ref", None)
        )
        stance = getattr(link, "stance", None)
        if node is None or ev_id not in existing_ev or stance is None:
            continue
        # M2 trust boundary (#987), category-gated: NO model-authored stance on
        # a causal_absence row is accepted — absence rows are stand-alone audit
        # records and every counterfactual link on one is engine-minted. The
        # rule, its rationale, and its metering live in ONE place
        # (``cause_assurance.absence_row_link_refused``), shared verbatim with
        # the flat hypothesis axis in
        # ``milestone_engine._apply_hypothesis_evidence_links`` — enforcing it
        # here alone would leave a boundary a single stance choice routes
        # around (the #987 second finding).
        if absence_row_link_refused(
            cat_by_id.get(ev_id),
            stance,
            axis="node",
            evidence_id=ev_id,
            node_or_hypothesis_id=nid,
            case_id=getattr(case, "case_id", None),
            turn=current_turn,
        ):
            continue
        # Upsert by (node, evidence) — matching the junction table's ON
        # CONFLICT DO UPDATE. A re-emission is a genuine re-assessment (a
        # raised ``stance_confidence`` after corroboration, a stance flip);
        # first-write-wins would freeze a link's confidence forever, which is
        # load-bearing now that the §7.1 filter reads it — the model's only
        # escape would be minting a duplicate evidence row that the
        # independence mirror then collapses.
        #
        # Engine verdicts on absence rows are safe from LLM overwrite by
        # CONSTRUCTION now, not by a clause here: the category gate above
        # refuses every model-authored link on a causal_absence row before
        # this point, so neither a create nor an in-place overwrite of the
        # confirm-stamp's SUPPORTS or M6's REFUTES can be reached from an
        # emission.
        #
        # An OMITTED stance_confidence (schema default None) means "full
        # confidence" on a NEW link but "keep the existing value" on an
        # upsert — otherwise a routine graph re-listing that omits the field
        # would silently PROMOTE a previously deliberate hedge (e.g. 0.5) to
        # 1.0 and pull it into the §7.1 causal tally.
        emitted_confidence = getattr(link, "stance_confidence", None)
        existing_idx = next(
            (i for i, el in enumerate(node.evidence_links) if el.evidence_id == ev_id),
            None,
        )
        # A value the schema SET ASIDE as out of range (fm#1502) is decided
        # here, the only point that knows new from re-emitted: a re-emission of
        # the same claim (same evidence, same stance) keeps its stored value
        # (the ``None`` branch below); a new link — or a stance FLIP, whose
        # stored value is confidence in the other claim — is rescaled or
        # coerced when it can be and otherwise NOT written, which leaves any
        # stored link as it was. The ``1.0`` a new link's absence means must
        # not stand in for garbage, and neither may the stored confidence of the
        # opposite stance: either would manufacture grounding (or a decisive
        # disconfirmation) nobody asserted.
        settled = settle_set_aside_link(
            link,
            stored_stance=(
                node.evidence_links[existing_idx].stance
                if existing_idx is not None
                else None
            ),
            where=f"{nid}<-{ev_id}",
            notes=validation_repairs,
        )
        if settled is not None:
            action, emitted_confidence = settled
            if action is ConfidenceAction.PRUNED:
                continue
        if emitted_confidence is None:
            if existing_idx is not None:
                emitted_confidence = node.evidence_links[existing_idx].stance_confidence
            else:
                emitted_confidence = 1.0
        fresh = NodeEvidenceLink(
            evidence_id=ev_id,
            stance=stance,
            reasoning=getattr(link, "reasoning", None) or "node evidence",
            stance_confidence=emitted_confidence,
        )
        if existing_idx is None:
            node.evidence_links.append(fresh)
        else:
            node.evidence_links[existing_idx] = fresh

    return created


def mirror_hypothesis_support_to_root_nodes(case: Case, current_turn: int) -> int:
    """#695 B1 — surface a hypothesis's flat causal SUPPORTS links onto its chain
    ROOT node, so the node axis (the sole source of truth for
    ``derive_node_states`` / ``grade_cause_assurance`` / the runbook gate) sees
    the grounding the LLM recorded on the hypothesis. Returns the number of node
    links created. Pure: no I/O, no LLM.

    The flat ``hypothesis_evidence`` axis and the ``causal_node_evidence`` axis
    are disjoint channels — a SUPPORTS link on a hypothesis is not mirrored onto
    that hypothesis's root node — so a hypothesis grounded purely on the flat
    axis leaves its root node with ZERO causal support and can never reach the
    ROOT validation bar (``ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN`` independent
    causal supports). This closes that gap.

    Trust boundary (identical to ``ingest_emitted_chain``): mirror ONLY
      * links whose evidence row is ``CAUSAL_EVIDENCE`` — the only category the
        ROOT bar counts. Symptom rows bear on ``D``, not the cause; and
        ``CAUSAL_ABSENCE`` rows are deliberately excluded, so this can never
        create the absence-SUPPORTS link that would satisfy the engine-reserved
        counterfactual CONFIRMATION mint;
      * ``SUPPORTS`` stance (grounding, not refutation);
      * onto a real ROOT node the hypothesis is attached to.
    It NEVER overwrites an existing node link — the LLM's explicit
    ``node_evidence`` emission (or a prior mirror) wins; it only FILLS a missing
    one, so it is idempotent across turns and repairs historical gaps on replay.
    The mirrored link is a CANDIDATE only: ``derive_node_states`` still applies
    the independence / restatement guard / M7 AND-gate, so mirroring cannot
    manufacture a validation the evidence does not independently support.
    """
    if not case.hypotheses or not case.causal_nodes:
        return 0
    cat_by_id = _evidence_category_map(case)
    mirrored = 0
    for hyp in case.hypotheses.values():
        root_id = getattr(hyp, "root_node_id", None)
        if not root_id:
            continue
        node = case.causal_nodes.get(root_id)
        if node is None or node.node_type != NodeType.ROOT:
            continue
        existing_ev_ids = {el.evidence_id for el in node.evidence_links}
        for link in hyp.evidence_links:
            if link.stance != EvidenceStance.SUPPORTS:
                continue
            ev_id = link.evidence_id
            if cat_by_id.get(ev_id) != EvidenceCategory.CAUSAL_EVIDENCE:
                continue
            if ev_id in existing_ev_ids:
                continue  # explicit node emission (or a prior mirror) wins
            node.evidence_links.append(
                NodeEvidenceLink(
                    evidence_id=ev_id,
                    stance=EvidenceStance.SUPPORTS,
                    reasoning=link.reasoning or "mirrored from hypothesis support",
                    stance_confidence=link.stance_confidence,
                    linked_at_turn=current_turn,
                )
            )
            existing_ev_ids.add(ev_id)
            mirrored += 1
    if mirrored:
        hypothesis_support_mirrored_to_root_total.inc(mirrored)
    return mirrored
