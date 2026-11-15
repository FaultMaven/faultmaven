"""The narrators' one read of the case besides its record (#2811, ruled
2026-11-15): the immutable text of a row, a solution, a need or a causal node,
by the id the record selects (``CaseOutcome``). No predicate: which id to
state is the record's; this only fetches what it says."""

from typing import Any, Optional


def content(case: Any, id: str) -> Optional[str]:
    """The text ``id`` names on ``case``: an evidence row's summary, a
    solution's title, a need's request, a causal node's statement; None for an
    id the case does not hold."""
    for row in case.evidence:
        if row.evidence_id == id:
            return row.summary
    for solution in case.solutions:
        if solution.solution_id == id:
            return solution.title
    for need in case.evidence_needs:
        if need.need_id == id:
            return need.request_text
    node = case.causal_nodes.get(id)
    return node.statement if node is not None else None
