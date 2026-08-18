"""Deterministic causal-map rendering for terminal reports (pure, contracts-only).

Serializes the case's persisted causal graph into a fenced mermaid flowchart
for the resolution summary. The LLM never authors the diagram: every node and
edge here already passed the engine's ingestion/validation gates before being
persisted, so the map can only assert structure the investigation actually
established — the same derive-don't-trust stance as the rest of the engine
lane. Output is a pure function of the graph rows, so it is stable across
regenerations and pinnable in tests.

The map renders only when it would inform: the cause must be established
(assurance at MECHANISTIC or above, the same recomputed-from-graph grade the
resolution summary's assurance note uses), and the graph must be neither
trivial (a two-box arrow says nothing a sentence doesn't) nor too dense to
read. Anything else returns None and the report simply has no map section.

AND-groups (M7 co-necessity) are not visually distinguished from OR
alternatives in v1 — each edge is still a true causal link, so the drawing
under-specifies rather than misstates.

Lives beside ``cause_assurance`` for the same reason that module gives: it
needs nothing from ``causal_graph``, only the case contracts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from faultmaven.core.investigation.cause_assurance import grade_cause_assurance
from faultmaven.modules.case.contracts import (
    CauseAssuranceGrade,
    NodeState,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode

__all__ = ["CAUSAL_MAP_LEGEND", "render_causal_map"]

# The human-readable key for the symbols this module draws. Owned here,
# beside the glyph table and arrow rule it describes, so the diagram and
# its legend cannot drift apart; the report renders it verbatim.
CAUSAL_MAP_LEGEND = (
    "_From the investigation's causal analysis: ✓ validated · "
    "○ not established (candidate or inconclusive) · ✗ refuted. "
    "Solid arrows lead from validated causes._"
)

# Below MIN the map is a restated sentence; above MAX it is an unreadable
# hairball (dev-DB terminal graphs top out around 15 nodes).
MIN_NODES = 3
MIN_EDGES = 2
MAX_NODES = 30

# Wide enough that real node statements (sentence-length; checked against
# dev-DB graphs) survive whole; mermaid wraps long labels, so width is
# bounded by the renderer, not the cut.
MAX_LABEL_CHARS = 110

_STATE_GLYPHS = {
    NodeState.VALIDATED: "✓",
    NodeState.REFUTED: "✗",
}
_CANDIDATE_GLYPH = "○"  # candidate / anything neither validated nor refuted

_WHITESPACE = re.compile(r"\s+")


def _sanitize_label(text: str) -> str:
    """Make a node statement safe inside a double-quoted mermaid label.

    Truncation runs on the raw text, BEFORE escaping, so the cap measures
    what the author wrote (escaping inflates '&' to 5 chars) and the cut
    can never bisect an inserted entity into visible garbage.

    Mermaid decodes two escape syntaxes inside labels — HTML entities and
    its own ``#code;`` form — so '&'/'<'/'>' become entities and '#'
    becomes ``#35;`` (a literal '#'), keeping accidental sequences like
    '#123;' in a statement from being decoded into other characters.
    Double quotes would terminate the label (they become apostrophes);
    a backtick right after the opening quote flips mermaid into
    markdown-string parsing, so backticks are dropped.
    """
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.replace('"', "'").replace("`", "")
    if len(text) > MAX_LABEL_CHARS:
        text = text[: MAX_LABEL_CHARS - 1].rstrip() + "…"
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("#", "#35;")
    return text


def _node_line(mermaid_id: str, node: "CausalNode") -> Optional[str]:
    """One mermaid node line, or None when the statement sanitizes away."""
    label = _sanitize_label(node.statement)
    if not label:
        return None
    if node.node_type == NodeType.PROBLEM:
        # Stadium shape marks the problem anchor D; its identity is the
        # symptom itself, so no state glyph.
        return f'    {mermaid_id}(["{label}"])'
    glyph = _STATE_GLYPHS.get(node.node_state, _CANDIDATE_GLYPH)
    return f'    {mermaid_id}["{glyph} {label}"]'


def render_causal_map(case: "Case") -> Optional[str]:
    """Return a fenced ``mermaid`` block for the case's causal graph, or None.

    The map draws exactly the subgraph that explains the problem: after
    resolving edges (dedup, dangling references dropped), only nodes with a
    rendered path INTO the problem anchor D survive — orphan boxes and
    chains that never arrive at the symptom are pruned rather than drawn
    floating. The size gates count that pruned subgraph, and the validated
    root must be part of it, or the map cannot show what the section claims
    (how the established cause produced the problem).

    None means "no map would inform here": the cause is not established,
    the explanatory subgraph is trivially small or unreadably large, the
    validated cause's chain does not reach D, or a node's statement
    sanitizes to an empty label. Callers treat None as "omit the section" —
    never as an error.
    """
    if grade_cause_assurance(case) not in (
        CauseAssuranceGrade.MECHANISTIC,
        CauseAssuranceGrade.CONFIRMED,
    ):
        return None

    nodes = list((case.causal_nodes or {}).values())
    node_ids = {n.node_id for n in nodes}
    problem_ids = {n.node_id for n in nodes if n.node_type == NodeType.PROBLEM}
    if not problem_ids:
        return None

    # Resolve edges first: dedup on (cause, effect), drop dangling refs.
    seen_pairs: set[tuple[str, str]] = set()
    resolved = []
    for edge in sorted(
        case.causal_edges or [], key=lambda e: (e.created_at_turn, e.edge_id)
    ):
        pair = (edge.cause_node_id, edge.effect_node_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        if edge.cause_node_id in node_ids and edge.effect_node_id in node_ids:
            resolved.append(edge)

    # Keep only the ancestry of D over rendered edges: nodes with a path
    # into the symptom. Everything else is noise on this picture.
    causes_of: dict[str, list[str]] = {}
    for edge in resolved:
        causes_of.setdefault(edge.effect_node_id, []).append(edge.cause_node_id)
    kept: set[str] = set(problem_ids)
    frontier = list(problem_ids)
    while frontier:
        for cause_id in causes_of.get(frontier.pop(), []):
            if cause_id not in kept:
                kept.add(cause_id)
                frontier.append(cause_id)

    kept_nodes = sorted(
        (n for n in nodes if n.node_id in kept),
        # Stable order: creation turn, then node id — deterministic across
        # dict-insertion order and regenerations.
        key=lambda n: (n.generated_at_turn, n.node_id),
    )
    kept_edges = [
        e for e in resolved if e.cause_node_id in kept and e.effect_node_id in kept
    ]
    if not (MIN_NODES <= len(kept_nodes) <= MAX_NODES):
        return None
    if len(kept_edges) < MIN_EDGES:
        return None
    # The section claims to show how the established cause produced the
    # problem — if no validated root sits on a chain into D, it cannot.
    validated_ids = {
        n.node_id for n in kept_nodes if n.node_state == NodeState.VALIDATED
    }
    if not any(
        n.node_type == NodeType.ROOT and n.node_id in validated_ids for n in kept_nodes
    ):
        return None

    mermaid_ids = {n.node_id: f"n{i}" for i, n in enumerate(kept_nodes, 1)}
    lines = ["flowchart LR"]
    for node in kept_nodes:
        node_line = _node_line(mermaid_ids[node.node_id], node)
        if node_line is None:
            return None  # statement sanitized to nothing — omit, don't degrade
        lines.append(node_line)

    for edge in kept_edges:
        # A solid arrow leaves an established cause; everything unproven
        # stays dotted so the drawing never over-claims (M4 in ink).
        arrow = "-->" if edge.cause_node_id in validated_ids else "-.->"
        lines.append(
            f"    {mermaid_ids[edge.cause_node_id]} {arrow} "
            f"{mermaid_ids[edge.effect_node_id]}"
        )

    return "```mermaid\n" + "\n".join(lines) + "\n```"
