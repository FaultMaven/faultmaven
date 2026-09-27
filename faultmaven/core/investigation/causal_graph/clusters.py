from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    cached_content_tokens as _cached_content_tokens,
)
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.cause_assurance import (
    root_counterfactually_confirmed,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    causal_and_set_late_grouping_total,
)
from faultmaven.modules.case.contracts import CausalEdge, CausalNode, NodeState

from .projection import _standing_hypotheses
from .queries import _state, incoming_and_groups
from .similarity import _mutual_mirror

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)


# §7.1.2 MECE arbitration (#656): Jaccard at/above which two ROOT
# statements are ONE cause recorded twice (duplicate emission), not competing
# explanations. Same value as the other mirror bars but deliberately its own
# knob: root-statement identity is a different comparison from evidence
# independence or frame ownership, separately calibrated. Like every mirror
# bar it is LEXICAL: negation is stopworded, so opposite-polarity statements
# ("disk full" / "disk NOT full") read as one cause — an accepted limit of
# the token layer, shared with INV-27/INV-29 and pinned in the tests.
_ROOT_DISTINCT_JACCARD = 0.6


def _live_descendant_ids(
    node_id: str, adjacency: dict, nodes: dict[str, CausalNode]
) -> set[str]:
    """Nodes reachable from ``node_id`` along cause→effect edges WITHOUT
    passing through a REFUTED node. §7.1.2 reachability is deliberately
    liveness-aware, unlike the bearing-frame walk
    (``cause_assurance._chain_descendant_ids``, which renders a chain's
    recorded mechanism): a REFUTED rung is a BROKEN link — two validated
    roots joined only through a disproven intermediate are competing
    explanations, not one line of explanation, and merging them would mask a
    real contest. A refuted node is neither returned nor expanded (endpoints
    under comparison are VALIDATED/count-held, never refuted, so this only
    prunes intermediates). Iterative; a malformed cyclic graph terminates via
    the visited set."""
    descendants: set[str] = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, ()):
            if nxt in descendants or nxt == node_id:
                continue
            node = nodes.get(nxt)
            if node is not None and node.node_state == NodeState.REFUTED:
                continue
            descendants.add(nxt)
            frontier.append(nxt)
    return descendants


def _edge_adjacency(case: Case) -> dict:
    """cause_node_id → [effect ids], built once per traversal batch (the
    per-frontier full-edge-list rescan is what made the naive walk O(V·E))."""
    adjacency: dict[str, list[str]] = {}
    for edge in case.causal_edges or []:
        adjacency.setdefault(edge.cause_node_id, []).append(edge.effect_node_id)
    return adjacency


def _cluster_relations(case: Case, root_ids) -> tuple[list, dict]:
    """Shared §7.1.2 groundwork: (sorted in-graph members, live-descendant map)."""
    members = sorted(rid for rid in root_ids if rid in case.causal_nodes)
    adjacency = _edge_adjacency(case)
    desc = {
        rid: _live_descendant_ids(rid, adjacency, case.causal_nodes) for rid in members
    }
    return members, desc


# ``causal_edges.and_group`` is String(64); the field is an unconstrained
# Optional[str] at every application layer above it (CausalNodeToAdd,
# CausalEdgeToAdd, CausalEdge). Since #1096 the prompt asks for a group key
# whenever a cause needs two conditions, and it invites a DESCRIPTIVE one —
# "memory-exhaustion-requires-unbounded-cache-and-reduced-limit" is already 60
# characters. An over-long key inserts fine on SQLite and raises "value too
# long for type character varying(64)" on PostgreSQL, i.e. it would fail the
# case save in cloud only.
_AND_GROUP_MAX_LEN = 64


def _normalize_and_group(and_group: object) -> str | None:
    """The ONE normalization of an AND-set key, applied where edges are written.

    - A blank key ("" or whitespace) names no group. The field is unconstrained
      end to end, so a model emitting ``and_group:""`` on independent
      alternatives would otherwise collapse them into one conjunction —
      silently strengthening the M7 gate and, since #1096, publishing "the
      cause required these conditions too" about causes that are alternatives.
    - Only a STRING names a group. The schema declares ``Optional[str]`` and
      Pydantic v2 does not coerce, so nothing else can arrive from the one
      production caller (``ingest_emitted_chain`` on schema-validated specs);
      the duck-typed caller that once handed numbers here — the KB cause
      seeder — is gone (fm#1295). A bool, a number or any other type names no
      group, matching the trust boundary rather than widening it.
    - An over-long key is folded to fit the column. A plain truncation would
      make two distinct long keys sharing a 64-char prefix into ONE group —
      the same silent M7 strengthening by another route — so the fold keeps a
      prefix and appends a digest of the WHOLE key. It is a pure function of
      that key, so the same logical group emitted on a later turn normalizes to
      the same token and the AND-set still forms.

    The key is an identity token, never rendered: ``validated_and_conjuncts``
    publishes the member nodes' statements, not the group name.
    """
    if not isinstance(and_group, str):
        return None
    and_group = and_group.strip()
    if not and_group:
        return None
    if len(and_group) <= _AND_GROUP_MAX_LEN:
        return and_group
    digest = hashlib.sha256(and_group.encode("utf-8")).hexdigest()[:8]
    return f"{and_group[: _AND_GROUP_MAX_LEN - 9]}-{digest}"


def _observe_late_grouping(case: Case, effect_id: str, and_group: str) -> None:
    """Record a grouping that arrived AFTER its members were already validated.

    Since #1096 a conjunction is not a MECE contest (§7.1.2), so a grouping token
    over two already-VALIDATED rivals dissolves an arbitration hold: it grants
    identification, publishes the conjunction and unblocks the confirm-stamp.
    That is the intended mechanism — the model authors causal structure
    everywhere else, and demanding M7 proof before honoring a grouping would
    recreate the deadlock the fix removes — but the merge is MONOTONE, so the
    grant is permanent and never re-examined, and a stuck contest is exactly
    the state a model has an incentive to escape.

    So the SEQUENCE is made observable, not refused. Fires only when this
    grouping completes an AND-set whose members were ALREADY validated: a
    conjunction modeled up front — the shape the prompt asks for — never
    reaches here, because its causes are still CANDIDATE when the edges are
    emitted. Non-zero is a population to audit, not a verdict: a genuine late
    recognition is indistinguishable from a hallucinated one at this point, and
    the engine is deliberately not the one guessing which it saw.
    """
    members = [
        e.cause_node_id
        for e in case.causal_edges
        if e.effect_node_id == effect_id and e.and_group == and_group
    ]
    validated = sorted(
        nid for nid in members if _state(nid, case.causal_nodes) == NodeState.VALIDATED
    )
    if len(validated) < 2:
        return
    causal_and_set_late_grouping_total.inc()
    logger.info(
        f"Late and_group {and_group!r} joined {len(validated)} already-validated "
        f"causes of {effect_id} for case {case.case_id}: they are now ONE "
        f"conjunctive cause and no longer contest each other",
        extra={
            "event": "causal_and_set_late_grouping",
            "case_id": case.case_id,
            "turn": case.current_turn,
            "effect_node_id": effect_id,
            "and_group": and_group,
            "validated_members": validated,
        },
    )


def _co_necessary_sets(edges: list[CausalEdge]) -> list[list[str]]:
    """The graph's AND-sets: each is the list of causes sharing one
    ``(effect_node_id, and_group)`` — co-necessary for that effect (M7).

    Only genuine sets (>1 member) are returned; a lone member names a
    conjunction of one, which is just an ordinary cause. Read through
    ``incoming_and_groups`` so the blank-key normalization ("" is not a group)
    keeps its single home — a legacy row with ``and_group=""`` must not fuse
    independent alternatives into one cause here any more than it may satisfy
    the M7 gate.
    """
    effects = {e.effect_node_id for e in edges or [] if e.and_group is not None}
    sets: list[list[str]] = []
    for effect_id in sorted(effects):
        for key, cause_ids in incoming_and_groups(effect_id, edges).items():
            if key is not None and len(cause_ids) > 1:
                sets.append(cause_ids)
    return sets


def _distinct_cause_partition(case: Case, root_ids) -> tuple[list, dict]:
    """One relations pass behind §7.1.2: (clusters, live-descendant map).
    Shared by ``distinct_cause_clusters`` and ``sole_cluster_origin`` so the
    stamp's origin pick reads the SAME reachability its cluster count was
    built from (recomputing relations per consumer is both wasted work and a
    divergence seam)."""
    members, desc = _cluster_relations(case, root_ids)
    if not members:
        return [], desc
    tokens = {
        rid: _cached_content_tokens(case.causal_nodes[rid].statement or "")
        for rid in members
    }

    parent = {rid: rid for rid in members}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            # Deterministic: the lexically-smaller representative wins.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    # CO-NECESSITY (M7) — an AND-set is a CONJUNCTION, not a differential.
    # S2's "at most one root can be the cause" holds between OR-alternatives;
    # co-necessary causes are the explicit counterexample, and their
    # simultaneous validation is the correct end state rather than a coherence
    # violation. Without this the engine punished exactly the shape the prompt
    # asks for on a two-condition cause (#1096): two validated conjuncts read as
    # a MECE contest, which holds identification (no cause_state=IDENTIFIED, so
    # no M5 solution license), asserts no conclusion at all, and refuses the
    # resolution confirm-stamp — leaving the case unable to reach CONFIRMED. The
    # exclusion lane already draws this line (``_survivor_or_sets`` builds its
    # differential from ``and_group is None`` edges only); §7.1.2 was the lane
    # that missed it.
    for and_set in _co_necessary_sets(case.causal_edges):
        in_scope = sorted(rid for rid in and_set if rid in parent)
        for other in in_scope[1:]:
            _union(in_scope[0], other)

    for i, a in enumerate(members):
        for b in members[i + 1 :]:
            if (
                b in desc[a]
                or a in desc[b]
                or _mutual_mirror(tokens[a], tokens[b], _ROOT_DISTINCT_JACCARD)
            ):
                _union(a, b)

    clusters: dict[str, set[str]] = {}
    for rid in members:
        clusters.setdefault(_find(rid), set()).add(rid)
    return [clusters[key] for key in sorted(clusters)], desc


def distinct_cause_clusters(case: Case, root_ids) -> list[set[str]]:
    """Cluster ROOT node ids into DISTINCT-cause groups (§7.1.2, MECE
    arbitration). Two roots are the SAME cause — one cluster — when:

    - their statements are MUTUAL mirrors (``_ROOT_DISTINCT_JACCARD``): the
      duplicate-emission shape, one cause recorded as two nodes (the model
      re-stated an existing root instead of referencing its ``cn_`` id); or
    - one lies on the other's LIVE causal path (``_live_descendant_ids``,
      either direction): a DEEPENED chain — "log rotation broken" → "disk
      full" is one line of explanation at two depths, not a differential (S2
      competition is between ORIGINS, not between a cause and its own
      consequence). A path through a REFUTED rung does NOT connect: the link
      is disproven, so the endpoints are genuine competitors; or
    - they are CO-NECESSARY — members of one AND-set (M7), sharing an
      ``(effect, and_group)``. A conjunction is ONE cause carrying two
      conditions, so both conjuncts standing validated is the correct end
      state, not the "several simultaneously-proven exclusive causes" a MECE
      hold exists to catch (#1096). Strictly a merge of the CONJUNCTS: an
      AND-set beside an independent alternative is still a real differential,
      and union-find keeps those in separate clusters.

    Grouping is the transitive closure (connected components) of those
    relations, iterated in sorted-id order so the result is order-invariant
    across dict/DB orderings. A root whose statement yields no content tokens
    merges with nothing on the mirror relation — conservative by design: an
    unjudgeable statement stays a DISTINCT cause, and the safe direction under
    NO-INCORRECT-CONCLUSION is holding identification, never concluding on an
    arbitrary pick.
    """
    return _distinct_cause_partition(case, root_ids)[0]


def _origin_of(cluster: set, desc: dict) -> str:
    """The ORIGIN of one distinct-cause cluster — the node the confirm-stamp
    cites (§7.1.2: on a deepened line the gone⇒gone confirmation is asserted
    of the origin, not its consequence): the member with the MOST live
    in-cluster descendants, lexical id on ties.

    That single sort IS the whole policy — no "has no member-ancestor"
    pre-filter is needed, because on a DAG a live member-ancestor strictly
    dominates: if ``r`` live-reaches member ``m`` then ``desc(r) ⊇ desc(m) ∪
    {m}`` (members are never REFUTED, so paths extend through them), giving
    ``r`` a strictly higher count. An edge-less duplicate of a consequence has
    zero descendants and never outranks the line's head; pure duplicates all
    count zero and tie to stable id order (same statement — any is faithful).
    A malformed cycle degenerates to the same id-order tie-break.
    """
    return sorted(cluster, key=lambda m: (-len(desc[m] & cluster), m))[0]


def sole_cluster_origin(case: Case, root_ids) -> "tuple[str, set] | None":
    """§7.1.2 arbitration entry point for the confirm-stamp, in ONE relations
    pass: ``None`` unless ``root_ids`` collapse to exactly one distinct cause
    (an unarbitrated MECE violation — the engine never guesses which cause the
    fix removed); otherwise ``(origin_id, member_ids)`` — the node to cite and
    the full cluster the stamp's idempotence and refutation-window checks must
    range over (a confirmation or failed-fix refute anywhere in the cluster
    belongs to the CAUSE)."""
    clusters, desc = _distinct_cause_partition(case, root_ids)
    if len(clusters) != 1:
        return None
    cluster = clusters[0]
    return _origin_of(cluster, desc), set(cluster)


def mece_contested_root_ids(case: Case) -> set:
    """§7.1.2 MECE arbitration (#656): the VALIDATED standing-chain roots
    that stand as simultaneously-validated DISTINCT causes — a coherence
    violation under S2 (roots are mutually-exclusive origins; at most one can
    be the cause, so "several are simultaneously proven" means the evidence has
    not discriminated yet). Returns the union of the contested roots' ids, or
    empty when identification is uncontested.

    - Only roots of STANDING hypotheses count — the same population that
      grounds ``cause_state=IDENTIFIED`` (``any_chain_root_validated``) and the
      same standing preference the confirm-stamp applies: an orphan validated
      node whose hypothesis decayed never contests the standing cause.
    - Duplicates, same-LIVE-causal-line roots and CO-NECESSARY conjuncts
      collapse first (``distinct_cause_clusters``): a duplicate emission is not
      a differential, and holding on one would deadlock — no evidence can ever
      discriminate a statement from its own restatement (NO-COLLAPSE); an M7
      AND-set is one cause carrying two conditions, and no evidence can
      discriminate between conditions the graph holds as both required.
    - A counterfactually CONFIRMED root (M2 top grade, engine-only producer)
      settles the contest outright: the gone⇒gone confirmation IS the
      discrimination, so validated siblings never hold a proven cause hostage.
      On a REOPENED case whose old confirmation has gone stale (the problem
      recurred), this deliberately still settles: recurrence is discharged by
      the failed-fix machinery (M6 demotes the confirmed root on
      disconfirmation), not by re-litigating the confirmation here —
      conclusion refresh/retraction is tracked on #656.

    This predicate HOLDS case-level identification only (the mirror of the
    §7.1.1 exclusion collapse: forward validation concludes only when the
    differential has collapsed to one cause). Node states are untouched —
    each root's VALIDATED standing is ruled by its own evidence (§7.1 entry-bar
    lesson: re-adjudicating settled nodes is what causes flap).
    """
    nodes = case.causal_nodes
    root_ids = {
        h.root_node_id
        for h in _standing_hypotheses(case)
        if h.root_node_id and _state(h.root_node_id, nodes) == NodeState.VALIDATED
    }
    if len(root_ids) < 2:
        return set()
    # Cluster BEFORE the confirmation scan: the common >=2-roots shape is a
    # duplicate emission, which collapses to one cluster with no evidence-map
    # build at all.
    clusters = distinct_cause_clusters(case, root_ids)
    if len(clusters) < 2:
        return set()
    cat_by_id = _evidence_category_map(case)
    if any(root_counterfactually_confirmed(nodes[rid], cat_by_id) for rid in root_ids):
        return set()
    # The clusters partition root_ids (all in-graph — a dangling root_node_id
    # never reads VALIDATED), so the contested union IS the root set.
    return set(root_ids)
