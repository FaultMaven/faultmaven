"""The offer record as the narrators read it (#2811 S2b-2d, ruling (a) of
2026-11-20).

The offer facts are not outcome facts: the offer record
(``case.offer_record``) stays their owner (``core/investigation/offers.py``
states its rules), so they are not on the outcome record. A narrator reads
them through ``OfferView``: typed, holding no text (ids, turns and answer
kinds only), built only by ``offers.offer_view`` from the case the narrator
renders. The prompt state reads it at most once per prompt assembly
(``NarrationFold.fresh``), the turn's narration where it reads an offer.
Pin 2 exempts it permanently, with its field set asserted, and it is not a
census row.
"""

from dataclasses import dataclass
from typing import Optional

from faultmaven.modules.case.contracts import AnswerKind


@dataclass(frozen=True)
class OfferView:
    """The case's offer facts at the moment read."""

    open_proposal_id: Optional[str]
    """The open offer's ``proposal_id``, or None when no offer is open."""
    answers: tuple[tuple[str, AnswerKind], ...]
    """Each recorded answer's ``(proposal_id, answer)``, in record order."""
    false_alarm_decline_turn: Optional[int]
    """The turn of the standing decline of a false-alarm close
    (``offers.standing_false_alarm_decline``)."""
    resolve_decline_turn: Optional[int]
    """The turn of the standing decline of a resolve offer
    (``offers.standing_resolve_decline``)."""

    def offer_stands(self, proposal_id: str) -> bool:
        """The offer ``proposal_id`` names is the open one."""
        return self.open_proposal_id is not None and (
            self.open_proposal_id == proposal_id
        )

    def answers_for(self, proposal_id: str) -> tuple[AnswerKind, ...]:
        """The answers recorded for ``proposal_id``, in record order."""
        return tuple(answer for pid, answer in self.answers if pid == proposal_id)

    def lapsed(self, proposal_id: str) -> bool:
        """The offer ``proposal_id`` names was answered LAPSED."""
        return AnswerKind.LAPSED in self.answers_for(proposal_id)
