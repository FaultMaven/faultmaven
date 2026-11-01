"""A keyed KB upload's or conversion's receipt (#2433).

``kb_request_receipts`` holds one row per keyed request that published,
written in the transaction that publishes it: an upload's in the ``record``
that completes its knowledge item (#2396), a conversion's in
``_persist_job_rows`` with the job. So a receipt exists exactly when its
request published, and lives exactly as long as its subject (``ON DELETE
CASCADE`` from the item or the job). The route's half, which answers a retry
from it, is ``modules/knowledge/api/request_idempotency.py``.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

#: The two routes a receipt names, its ``route`` column.
ROUTE_UPLOAD = "upload"
ROUTE_CONVERT = "convert"

#: Both routes answer a publication 201.
CREATED = 201


@dataclass(frozen=True)
class KbReceiptKey:
    """What a keyed request's receipt is written and looked up under."""

    enterprise_id: str
    author_id: str
    route_name: str
    """``upload`` or ``convert``: the row's ``route``."""
    idempotency_key: str
    request_fingerprint: str


@dataclass(frozen=True)
class KbRequestReceipt:
    """A published request's answer, as stored."""

    request_fingerprint: str
    status_code: int
    response: Dict[str, Any]


@dataclass(frozen=True)
class KbReceiptWrite:
    """A conversion's receipt, written by ``_persist_job_rows``:
    ``build_response`` runs there, once the drafts' titles are scrubbed, so
    the stored body is the body the request returns."""

    key: KbReceiptKey
    build_response: Callable[[], Dict[str, Any]]


def add_receipt(
    session: Any,
    key: KbReceiptKey,
    *,
    response: Dict[str, Any],
    knowledge_item_id: Optional[str] = None,
    conversion_id: Optional[str] = None,
) -> None:
    """Add ``key``'s receipt to the publishing transaction. Never commits: a
    duplicate key raises ``IntegrityError`` at that transaction's flush."""
    # Lazy, as the knowledge service imports persistence: no import cycle.
    from faultmaven.infrastructure.persistence.models import KbRequestReceiptModel

    session.add(
        KbRequestReceiptModel(
            enterprise_id=key.enterprise_id,
            author_id=key.author_id,
            route=key.route_name,
            idempotency_key=key.idempotency_key,
            request_fingerprint=key.request_fingerprint,
            status_code=CREATED,
            response=response,
            knowledge_item_id=knowledge_item_id,
            conversion_id=conversion_id,
            created_at=datetime.now(timezone.utc),
        )
    )


async def read_receipt(
    db_session_factory: Any, key: KbReceiptKey
) -> Optional[KbRequestReceipt]:
    """``key``'s receipt, or ``None``. In a session of its own: never call it
    while a session of the same factory is open (a pool of one deadlocks)."""
    from faultmaven.infrastructure.persistence.models import KbRequestReceiptModel

    if not db_session_factory:
        return None
    async with db_session_factory() as session:
        row = await session.get(
            KbRequestReceiptModel,
            (key.enterprise_id, key.author_id, key.route_name, key.idempotency_key),
        )
        if row is None:
            return None
        return KbRequestReceipt(
            request_fingerprint=row.request_fingerprint,
            status_code=row.status_code,
            response=row.response,
        )
