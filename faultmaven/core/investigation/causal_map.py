"""Deterministic causal-map rendering for terminal reports (pure, contracts-only).

Serializes the case's persisted causal graph into a fenced mermaid flowchart
for the terminal summaries. The LLM never authors the diagram: every node and
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

__all__ = ["render_causal_map"]

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

    Order matters: ampersands first, so the entities introduced for the
    angle brackets survive. Double quotes would terminate the label, so
    they become apostrophes.
    """
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.replace("&", "&amp;")
    text = text.replace('"', "'")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    # A backtick right after the opening quote flips mermaid into
    # markdown-string parsing; real node statements carry inline code
    # spans, so drop backticks rather than risk it.
    text = text.replace("`", "")
    if len(text) > MAX_LABEL_CHARS:
        text = text[: MAX_LABEL_CHARS - 1].rstrip() + "…"
    return text


def _node_line(mermaid_id: str, node: "CausalNode") -> str:
    label = _sanitize_label(node.statement)
    if node.node_type == NodeType.PROBLEM:
        # Stadium shape marks the problem anchor D; its identity is the
        # symptom itself, so no state glyph.
        return f'    {mermaid_id}(["{label}"])'
    glyph = _STATE_GLYPHS.get(node.node_state, _CANDIDATE_GLYPH)
    return f'    {mermaid_id}["{glyph} {label}"]'


def render_causal_map(case: "Case") -> Optional[str]:
    """Return a fenced ``mermaid`` block for the case's causal graph, or None.

    None means "no map would inform here": the cause is not established, the
    graph is trivially small or degenerately large, the problem anchor is
    missing, or too few edges survive dangling-reference checks. Callers
    treat None as "omit the section" — never as an error.
    """
    if grade_cause_assurance(case) not in (
        CauseAssuranceGrade.MECHANISTIC,
        CauseAssuranceGrade.CONFIRMED,
    ):
        return None

    nodes = list((case.causal_nodes or {}).values())
    edges = list(case.causal_edges or [])
    if not (MIN_NODES <= len(nodes) <= MAX_NODES) or len(edges) < MIN_EDGES:
        return None
    if not any(n.node_type == NodeType.PROBLEM for n in nodes):
        return None

    # Stable order: creation turn, then node id — deterministic across
    # dict-insertion order and regenerations.
    nodes.sort(key=lambda n: (n.generated_at_turn, n.node_id))
    by_id = {n.node_id: n for n in nodes}
    mermaid_ids = {n.node_id: f"n{i}" for i, n in enumerate(nodes, 1)}

    lines = ["flowchart LR"]
    for node in nodes:
        lines.append(_node_line(mermaid_ids[node.node_id], node))

    edge_lines: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for edge in sorted(edges, key=lambda e: (e.created_at_turn, e.edge_id)):
        pair = (edge.cause_node_id, edge.effect_node_id)
        if pair in seen_pairs:
            continue
        cause = by_id.get(edge.cause_node_id)
        if cause is None or edge.effect_node_id not in by_id:
            continue  # dangling reference — draw only what resolves
        seen_pairs.add(pair)
        # A solid arrow leaves an established cause; everything unproven
        # stays dotted so the drawing never over-claims (M4 in ink).
        arrow = "-->" if cause.node_state == NodeState.VALIDATED else "-.->"
        edge_lines.append(
            f"    {mermaid_ids[edge.cause_node_id]} {arrow} "
            f"{mermaid_ids[edge.effect_node_id]}"
        )

    if len(edge_lines) < MIN_EDGES:
        return None

    lines.extend(edge_lines)
    return "```mermaid\n" + "\n".join(lines) + "\n```"
