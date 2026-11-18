"""The needs pool as the narrators read it (#2811 S2b-1b-i, ruling 2 of
2026-11-17).

A need's state is the pool's own stored state, never an outcome fact
(``EvidenceNeed``), so it is not on the record. A narrator reads it through
``NeedsView``: typed, holding no text, built at most once per prompt assembly
from the case the record is folded from (``NarrationFold.needs``) and shared by
the ``<fix_verification>`` and ``<evidence_needs>`` blocks (S2b-1b-ii). Pin 2
exempts it permanently, with its field set asserted, and it is not a census
row. A need's text is ``content(id)``.
"""

from dataclasses import dataclass
from typing import Optional

from faultmaven.core.investigation.evidence_need_surfacing import (
    is_ask_exhausted,
    select_surfaced_causal_needs,
)
from faultmaven.modules.case.contracts import (
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

#: Per-need truncation cap for ``request_text``. The model attribute is
#: capped at 500 chars by the schema, but in the rendered block we keep
#: things scannable. The full text is preserved in the DB and surfaces
#: via the EVIDENCE-suggestion side (Phase 6).
_REQUEST_TEXT_RENDER_CAP = 120


def _truncate_request_text(text: str) -> str:
    """Truncate ``request_text`` for rendering only — full text stays in
    the DB. Adds a single-char ellipsis when truncation actually
    occurs so the LLM knows the surfaced line is partial."""
    if len(text) <= _REQUEST_TEXT_RENDER_CAP:
        return text
    return text[: _REQUEST_TEXT_RENDER_CAP - 1].rstrip() + "…"


@dataclass(frozen=True)
class NeedView:
    """One need of the pool, as it stands at the current turn."""

    need_id: str
    purpose: NeedPurpose
    state: NeedState
    priority: NeedPriority
    obtainability: NeedObtainability
    motivating_hypothesis_ids: tuple[str, ...]
    outstanding: bool
    exhausted: bool
    """The engine has stopped putting it to the user (``is_ask_exhausted``)."""
    surfaced: bool
    """One of the causal asks the surface cap shows this turn
    (``select_surfaced_causal_needs``, which rotates on
    ``turns_without_progress``)."""
    times_surfaced: int
    last_surfaced_turn: Optional[int]


@dataclass(frozen=True)
class NeedsView:
    """The case's needs, in pool order."""

    needs: tuple[NeedView, ...] = ()

    @classmethod
    def of(cls, case) -> "NeedsView":
        surfaced = {n.need_id for n in select_surfaced_causal_needs(case)}
        return cls(
            tuple(
                NeedView(
                    need_id=n.need_id,
                    purpose=n.purpose,
                    state=n.state,
                    priority=n.priority,
                    obtainability=n.obtainability,
                    motivating_hypothesis_ids=tuple(n.motivating_hypothesis_ids),
                    outstanding=n.is_outstanding,
                    exhausted=is_ask_exhausted(n, case.current_turn),
                    surfaced=n.need_id in surfaced,
                    times_surfaced=n.times_surfaced,
                    last_surfaced_turn=n.last_surfaced_turn,
                )
                for n in case.evidence_needs
            )
        )
