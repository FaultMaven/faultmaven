"""Pure serializers for the dev-only debug endpoints.

Extracted from the ``/debug/cases/{id}/causal-graph`` route so the payload shape
is unit-testable without booting the app (the route stays a thin wrapper that
adds I/O + a timestamp). The fm-sre-simulator's chain probe consumes this exact
shape per turn to validate 2D-hypothesis chains and the runbook Cause matcher.
"""

from __future__ import annotations

from typing import Any


def _dump(obj: Any) -> Any:
    """Best-effort JSON-mode model dump; never raises on a stray object."""
    try:
        return obj.model_dump(mode="json")
    except Exception:  # pragma: no cover - defensive
        return str(obj)


def build_causal_graph_debug_payload(case: Any) -> dict:
    """Serialize a case's causal graph + hypothesis-chain wiring.

    Pure (no I/O, no clock) so the route can frame it with a timestamp and the
    unit tests can assert the shape directly. Returns the persisted causal DAG
    (nodes/edges), each hypothesis's chain link (``root_node_id`` / ``path``) and
    its ``rationale`` — the runbook Cause matcher's firing signal, a
    marker-prefixed rationale ("Instantiated from runbook …") that distinguishes a
    matcher-seeded hypothesis from an LLM-emitted one — the engine-derived
    ``cause_state``, and the root-cause conclusion.
    """
    problem_node_id = next(
        (
            nid
            for nid, n in case.causal_nodes.items()
            if getattr(n, "node_type", None)
            and getattr(n.node_type, "value", n.node_type) == "problem"
        ),
        None,
    )

    hypotheses = {}
    for hid, h in case.hypotheses.items():
        state = getattr(h, "state", None)
        hypotheses[hid] = {
            "statement": getattr(h, "statement", None),
            "category": getattr(getattr(h, "category", None), "value", None),
            "state": getattr(state, "value", state),
            "likelihood": getattr(h, "likelihood", None),
            "initial_likelihood": getattr(h, "initial_likelihood", None),
            "root_node_id": getattr(h, "root_node_id", None),
            "path": getattr(h, "path", None),
            "generated_at_turn": getattr(h, "generated_at_turn", None),
            # The runbook Cause matcher's firing signal: its seeded hypotheses
            # carry a marker-prefixed rationale (RUNBOOK_MATCH_RATIONALE_PREFIX,
            # "Instantiated from runbook …"). Exposing it lets the sim probe
            # distinguish a matcher-seeded hypothesis from an LLM-emitted one
            # (matcher firing, false-match, and capped-prior soundness checks).
            "rationale": getattr(h, "rationale", None),
        }

    progress = getattr(case, "progress", None)
    cause_state = getattr(progress, "cause_state", None) if progress else None

    return {
        "case_id": getattr(case, "case_id", None),
        "current_turn": getattr(case, "current_turn", None),
        "cause_state": getattr(cause_state, "value", cause_state),
        "problem_node_id": problem_node_id,
        "causal_nodes": {nid: _dump(n) for nid, n in case.causal_nodes.items()},
        "causal_edges": [_dump(e) for e in case.causal_edges],
        "hypotheses": hypotheses,
        "root_cause_conclusion": (
            _dump(case.root_cause_conclusion) if case.root_cause_conclusion else None
        ),
    }
