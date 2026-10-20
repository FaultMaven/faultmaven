"""A case's hard delete (#1016): what it cleared, as its audit row records it.

``DELETE /cases/{id}`` clears every store that holds the case's data before
the row, and fails rather than deleting the row when one cannot be cleared.
These are the values that travel from the service to the repository's one
transaction: the row, its share rows and the ``case_deleted`` audit row.
"""

import json
from dataclasses import dataclass, field
from typing import Dict, Tuple

#: Content-bearing stores the delete does NOT clear, each an expiring cache
#: that cannot be looked up by case (ruled 2026-10-19). Named in every audit
#: row so the record says what the delete did not reach.
NOT_CLEARED: Dict[str, str] = {
    "idempotency_replay_cache": (
        "Redis replay bodies of non-turn writes (api/middleware/idempotency.py), "
        "keyed by request hash; expire after 1 h"
    ),
    "llm_response_cache": (
        "in-process LLM response cache (infrastructure/llm/cache.py), per "
        "process; armed only by the structured-output provider override"
    ),
}


@dataclass(frozen=True)
class CaseDeletion:
    """One hard delete of a case, as its ``case_deleted`` audit row records it.

    ``cleared`` maps each store the delete cleared to the number of items it
    removed (0 when there was nothing to remove). ``not_configured`` names a
    store this deployment does not have, so nothing in it can hold the case.

    The audit row's ``not_cleared`` list holds content-bearing stores only.
    Left out on purpose: TTL claims and locks that carry no content
    (``turn-inflight:…`` claims, ``turn_idempotency.py``;
    ``lock:report_generation:case:…``, ``report_lock_manager.py``) and audit
    trails that must outlive the case (``operator_access_audit``,
    ``operator_access_grants.target_case_id``). Opik traces are #2102.
    """

    case_id: str
    enterprise_id: str
    actor_user_id: str | None
    cleared: Dict[str, int] = field(default_factory=dict)
    not_configured: Tuple[str, ...] = ()

    def audit_details(self, shares_removed: int) -> str:
        """The audit row's ``details`` JSON. ``shares_removed`` is the number
        of ``resource_shares`` rows the delete's own transaction removed."""
        return json.dumps(
            {
                "cleared": {**self.cleared, "resource_shares": shares_removed},
                "not_configured": list(self.not_configured),
                "not_cleared": NOT_CLEARED,
            }
        )
