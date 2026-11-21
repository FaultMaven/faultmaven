"""The offer record as the narrators read it (#2811 S2b-2d, ruling (a) of
2026-11-20).

The offer facts are not outcome facts: the offer record
(``case.offer_record``) stays their owner (``core/investigation/offers.py``
states its rules), so they are not on the outcome record. A narrator reads
them through ``OfferView``: typed, holding no text (ids and answer kinds, and
readers of turns), built in production code only by ``offers.offer_view``
from the case the narrator renders. The open offer and the recorded answers
are read when the view is built; each standing decline's turn is read only
when it is first asked, its value or its failure kept, so a narrator that
never asks for it never derives it. The prompt state builds one view per
prompt assembly (``NarrationFold.fresh``); the turn's narration builds one
where it reads an offer and never asks for a decline. Pin 2 exempts it
permanently, with its field set asserted, and it is not a census row.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from faultmaven.modules.case.contracts import AnswerKind


class _Once:
    """A reader asked at most once: its value, or its failure, is kept and
    returned, or raised, to every later asker."""

    def __init__(self, read: Callable[[], Optional[int]]) -> None:
        self._read: Optional[Callable[[], Optional[int]]] = read
        self._value: Optional[int] = None
        self._error: Optional[Exception] = None

    def __call__(self) -> Optional[int]:
        if self._read is not None:
            read, self._read = self._read, None
            try:
                self._value = read()
            except Exception as error:  # noqa: BLE001 — raised to each asker
                self._error = error
        if self._error is not None:
            raise self._error
        return self._value


@dataclass(frozen=True)
class OfferView:
    """The case's offer facts at the moment read."""

    open_proposal_id: Optional[str]
    """The open offer's ``proposal_id``, or None when no offer is open."""
    answers: tuple[tuple[str, AnswerKind], ...]
    """Each recorded answer's ``(proposal_id, answer)``, in record order."""
    _false_alarm_turn: Callable[[], Optional[int]]
    """Reads the turn of the standing decline of a false-alarm close
    (``offers.standing_false_alarm_decline``)."""
    _resolve_turn: Callable[[], Optional[int]]
    """Reads the turn of the standing decline of a resolve offer
    (``offers.standing_resolve_decline``)."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "_false_alarm_turn", _Once(self._false_alarm_turn))
        object.__setattr__(self, "_resolve_turn", _Once(self._resolve_turn))

    def false_alarm_decline_turn(self) -> Optional[int]:
        """The turn the false-alarm close was declined, while that decline
        stands; read on the first ask."""
        return self._false_alarm_turn()

    def resolve_decline_turn(self) -> Optional[int]:
        """The turn of the standing resolve decline; read on the first ask."""
        return self._resolve_turn()

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
