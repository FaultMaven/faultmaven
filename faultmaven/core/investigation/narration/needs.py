"""The needs pool as the narrators read it (#2811 S2b-1b-i, ruling 2 of
2026-11-17).

A need's state is the pool's own stored state, never an outcome fact
(``EvidenceNeed``), so it is not on the record. A narrator reads it through
``NeedsView``: typed, holding no text, built once per block from the case the
record is folded from. Pin 2 exempts it permanently, with its field set
asserted, and it is not a census row. A need's text is ``content(id)``.
"""

from dataclasses import dataclass

from faultmaven.core.investigation.evidence_need_surfacing import is_ask_exhausted
from faultmaven.modules.case.contracts import NeedPurpose, NeedState

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
    outstanding: bool
    exhausted: bool
    """The engine has stopped putting it to the user (``is_ask_exhausted``)."""


@dataclass(frozen=True)
class NeedsView:
    """The case's needs, in pool order."""

    needs: tuple[NeedView, ...] = ()

    @classmethod
    def of(cls, case) -> "NeedsView":
        return cls(
            tuple(
                NeedView(
                    need_id=n.need_id,
                    purpose=n.purpose,
                    state=n.state,
                    outstanding=n.is_outstanding,
                    exhausted=is_ask_exhausted(n, case.current_turn),
                )
                for n in case.evidence_needs
            )
        )
