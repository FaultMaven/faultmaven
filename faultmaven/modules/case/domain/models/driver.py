"""The case driver (ADR-020): who holds a case's investigation writes.

A case has a CREATOR (``cases.user_id``) and a DRIVER (``cases.driver_id``,
NULL meaning the creator drives; the effective driver is
``Case.effective_driver_id``). These are the values that travel with a change
of driver: why it changed, and the audit row every change writes.
"""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CaseDriverChangeReason(str, Enum):
    """Why a case's driver changed (ADR-020 D4, the audit row's ``reason``).

    ``REASSIGNED`` is the one deliberate change (``PUT /cases/{id}/driver``).
    The rest are RELEASES (ADR-020 D3): an operation that would leave the
    driver unable to read the case hands it back to the creator first.
    """

    REASSIGNED = "reassigned"
    UNSHARED = "unshared"
    LEFT_TEAM = "left_team"
    DEACTIVATED = "deactivated"
    REANCHORED = "reanchored"
    CREATOR_REASSIGNED = "creator_reassigned"


@dataclass(frozen=True)
class CaseDriverChange:
    """One change of a case's driver, as its ``case_driver_changed`` audit row
    records it.

    ``from_driver_id`` and ``to_driver_id`` are EFFECTIVE drivers (the creator
    when the stored column is NULL), so the row reads as who handed the case
    to whom. ``actor_user_id`` is the authenticated principal that caused the
    change, or ``None`` for an operator CLI, which has none to name — the
    convention ``fm-reassign-cases`` set for ``case_reassigned``.
    """

    case_id: str
    enterprise_id: str
    from_driver_id: Optional[str]
    to_driver_id: Optional[str]
    reason: CaseDriverChangeReason
    actor_user_id: Optional[str]

    def audit_details(self) -> str:
        """The audit row's ``details`` JSON."""
        return json.dumps(
            {
                "from_driver_id": self.from_driver_id,
                "to_driver_id": self.to_driver_id,
                "reason": self.reason.value,
            }
        )


@dataclass(frozen=True)
class DrivenCase:
    """A case some account drives by assignment (``driver_id`` is set).

    The light row a release reads: enough to decide whether the driver would
    lose read access, without hydrating the whole case.
    """

    case_id: str
    enterprise_id: str
    creator_id: Optional[str]
    driver_id: str
