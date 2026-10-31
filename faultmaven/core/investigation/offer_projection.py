"""The projection of the offer record onto what the user is shown (#2379).

Pure, and the only place an affordance is derived from the record
(``core/investigation/offers.py`` states the record's rules):
``disposition_eligibility``, written on every save
(``CaseRepository.prepare_for_save``), and the reply's chips — the open
offer's confirmation pair and, with nothing open, the "Mark it resolved" chip
while a resolve decline stands and the problem is not on hold.

A module of its own because the eligibility and the chip read the readiness
assessments in ``terminal_transitions``, which reads the record: kept out of
``offers.py``, the record's rules import nothing that imports them.
"""

from functools import cached_property
from typing import TYPE_CHECKING, Optional

from faultmaven.core.investigation.offers import confirmation_pair
from faultmaven.core.investigation.problem_status import problem_on_hold
from faultmaven.core.investigation.terminal_transitions import (
    declined_resolve_entry,
    derive_disposition_eligibility,
    resolve_reopen_key,
)
from faultmaven.modules.case.contracts import DispositionEligibility

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

#: The "Mark it resolved" chip (#1895): label, payload, body.
DECLINED_RESOLVE_CARD_LABEL = "Mark it resolved"
_DECLINED_RESOLVE_CARD_PAYLOAD = "Mark this case resolved."
_DECLINED_RESOLVE_CARD_BODY = (
    "Re-open the resolution the agent proposed. You confirm on the next step."
)


def _reopen_chip(case: "Case") -> Optional[dict]:
    """The chip that keeps a declined resolution one step away (#1895), or
    None.

    RESOLVED is earned, never requested: it is not on the status menu, and a
    user's request never becomes the confirmation row that earns it. So once
    the user has declined the resolution, neither the engine nor the model
    offers it again until a solution or confirmation the decline never saw is
    recorded. A user who changes their mind meanwhile needs a deterministic way
    back on every client, Slack included (which renders only the server's
    suggestions): this chip re-presents the offer the user declined.

    Its intent is the published ``QueryIntent`` shape ``{"type":
    "status_transition", "to_state": "resolved", "proposal_id": <reopen
    key>}``, which every client forwards verbatim. The reopen key
    (``terminal_transitions.resolve_reopen_key``) names the newest standing
    decline; the service boundary and the engine's guard admit the chip only
    while that decline stands (``resolve_reopen_admitted``), and the engine
    then OPENS a resolve offer the user confirms (INV-03). It names no open
    offer. Not built while any offer is open (it re-opens a declined offer, it
    does not compete with a live one) or while the problem is on hold (a
    revision awaiting the user, a false alarm).
    """
    if case.offer_record.open is not None or problem_on_hold(case):
        return None
    entry = declined_resolve_entry(case)
    if entry is None:
        return None
    return {
        "label": DECLINED_RESOLVE_CARD_LABEL,
        "action_type": "DECIDE",
        "payload": _DECLINED_RESOLVE_CARD_PAYLOAD,
        "body": _DECLINED_RESOLVE_CARD_BODY,
        "intent": {
            "type": "status_transition",
            "to_state": "resolved",
            "proposal_id": resolve_reopen_key(entry),
        },
    }


class OfferProjection:
    """Every affordance the record implies, derived on demand from one case:
    ``disposition_eligibility``, the open offer's confirmation ``pair`` and the
    ``reopen_chip``."""

    def __init__(self, case: "Case") -> None:
        self._case = case

    @cached_property
    def disposition_eligibility(self) -> DispositionEligibility:
        return derive_disposition_eligibility(self._case)

    @cached_property
    def pair(self) -> list[dict]:
        """The open offer's confirmation pair, each card naming it by its
        ``proposal_id``; empty when nothing is open."""
        return confirmation_pair(self._case.offer_record.open)

    @cached_property
    def reopen_chip(self) -> Optional[dict]:
        """The "Mark it resolved" chip: with nothing open, while a resolve
        decline stands and the problem is not on hold; else None."""
        return _reopen_chip(self._case)


def project_offers(case: "Case") -> OfferProjection:
    """The one projection of the record onto what the user is shown."""
    return OfferProjection(case)
