"""The case module's own erasers for the case delete (#1016).

``CaseService.hard_delete_case`` runs an ``ICaseStoreEraser`` per store before
it deletes the row. The erasers of stores other modules own live in those
modules; these two belong here: the session pointer is written by this module,
and the stand-in for a store that failed to construct is the composition's
way of failing every delete closed.
"""

from faultmaven.exceptions import ServiceException
from faultmaven.models.interfaces import ISessionStore


def session_pointer_key(session_id: str) -> str:
    """The session store key naming the case a session is on (unprefixed)."""
    return f"session:{session_id}:current_case_id"


class SessionPointerEraser:
    """Deletes every session pointer that names the case. One SCAN of the
    keyspace per delete; each key is compare-and-deleted, so a pointer
    re-linked to another case meanwhile is kept."""

    store_name = "session_pointers"

    def __init__(self, session_store: ISessionStore):
        self.session_store = session_store

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        return await self.session_store.delete_keys_holding(
            session_pointer_key("*"), case_id
        )


class UnavailableStoreEraser:
    """Bound for a configured store whose service failed to construct, so
    every delete fails instead of skipping the store."""

    def __init__(self, store_name: str, reason: str):
        self.store_name = store_name
        self.reason = reason

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        raise ServiceException(
            f"Cannot clear {self.store_name} for case {case_id}: {self.reason}"
        )
