"""Sweep ``knowledge_items`` rows whose authored ingest never completed (#2394).

An authored ingest (``KnowledgeService.ingest_runbook`` without ``shipped``)
inserts its row with ``ingest_complete`` false and sets it true once its vectors
are written. No KB door serves an incomplete row's vectors (every door admits a
chunk only when its row is complete and published, #2397), so such a row is
garbage, never a leak: one whose ingest failed and whose row cleanup failed
too, or whose INSERT committed after the ingest raised. This sweep, where armed, removes it.

A row is swept once its ``created_at`` is older than
``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS`` (default 24); a younger one may be an
ingest still running. The bound affects only liveness: an ingest whose row was
swept fails its guarded flip (``ingest_complete`` is set only on the row
carrying that ingest's stamp) and raises; the vectors it wrote stay unadmitted.

Per row: the row's DELETE, guarded on its stamp and on ``ingest_complete``
false, uncommitted; one vector delete by parent id; commit. A collector, not a
writer, so it deletes vectors and keeps its row until they are gone: a failed
vector delete rolls the row back for the next run, and a row
whose guarded delete matches nothing is counted ``spared``. No share is deleted:
an authored row gets its share in the transaction that completes it.

``bootstrap_kb`` calls this before ``KbPack.load``, so it runs whether or not a
pack loads.

Modes, ``kb_orphan_sweep``'s gate: the web startup passes ``armed``; ``kb_seed``
passes :func:`resolve_incomplete_sweep_mode`, report-only until an operator arms
it (``KB_INCOMPLETE_INGEST_SWEEP_ENABLED`` and a run that is not a dry run;
``--dry-run`` / ``--no-dry-run`` override ``KB_INCOMPLETE_INGEST_SWEEP_DRY_RUN``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    SWEEP_OFF,
    SWEEP_REPORT_ONLY,
    read_sweep_gate,
)

logger = logging.getLogger(__name__)

#: The ``DatabaseSettings`` field holding the age bound.
MAX_AGE_FIELD = "kb_incomplete_ingest_max_age_hours"


@dataclass
class IncompleteSweepResult:
    """What the sweep found, deleted and left."""

    #: ``off`` / ``report_only`` / ``armed``.
    mode: str = SWEEP_REPORT_ONLY
    #: The age bound this run applied.
    max_age_hours: float = 0.0
    #: Incomplete rows past the bound: what an armed run deletes.
    would_delete: List[str] = field(default_factory=list)
    #: Rows deleted with their vectors (armed runs only).
    deleted: List[str] = field(default_factory=list)
    #: Rows whose guarded delete matched nothing: completed, deleted or
    #: re-created under the id since the read. Nothing was deleted for them.
    spared: int = 0
    #: Rows whose removal raised: left for the next run.
    unremoved: int = 0
    #: Why the sweep stopped early, when it did.
    skipped: Optional[str] = None

    def summary(self) -> Dict[str, Any]:
        """The job-result shape."""
        return {
            "mode": self.mode,
            "skipped": self.skipped,
            "max_age_hours": self.max_age_hours,
            "would_delete": len(self.would_delete),
            "deleted": len(self.deleted),
            "spared": self.spared,
            "unremoved": self.unremoved,
            "would_delete_documents": list(self.would_delete),
        }


def resolve_incomplete_sweep_mode(dry_run: Optional[bool] = None) -> str:
    """``kb_seed``'s mode: :func:`read_sweep_gate` over the
    ``KB_INCOMPLETE_INGEST_SWEEP_*`` settings, ``dry_run`` the run's override."""
    return read_sweep_gate(
        "kb_incomplete_ingest_sweep_enabled",
        "kb_incomplete_ingest_sweep_dry_run",
        dry_run,
    )


def _resolve_max_age() -> timedelta:
    """``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS``; the field's own default when
    settings cannot be read."""
    from faultmaven.config.settings import DatabaseSettings, get_settings

    try:
        hours = getattr(get_settings().database, MAX_AGE_FIELD)
    except Exception as exc:  # settings unavailable in some test contexts
        logger.debug(f"KB incomplete-ingest sweep: settings unreadable ({exc})")
        hours = DatabaseSettings.model_fields[MAX_AGE_FIELD].default
    return timedelta(hours=hours)


async def sweep_incomplete_ingests(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    *,
    mode: str,
    max_age: Optional[timedelta] = None,
    now: Optional[datetime] = None,
) -> IncompleteSweepResult:
    """Find the incomplete rows older than ``max_age`` (default: the setting)
    at ``now`` (default: the clock) and, when ``mode`` is ``armed``, delete
    each with its vectors. Never raises."""
    max_age = _resolve_max_age() if max_age is None else max_age
    now = datetime.now(timezone.utc) if now is None else now
    result = IncompleteSweepResult(
        mode=mode, max_age_hours=max_age.total_seconds() / 3600
    )
    if mode == SWEEP_OFF:
        result.skipped = "disabled"
        return result
    store = getattr(knowledge_service, "_vector_store", None)
    if store is None:
        result.skipped = "no_vector_store"
        return result
    try:
        stale = await _read_stale(db_session_factory, now - max_age)
    except Exception as exc:
        logger.warning(f"KB incomplete-ingest sweep skipped: {exc}")
        result.skipped = "error"
        return result
    result.would_delete = [item_id for item_id, _stamp in stale]
    if mode != SWEEP_ARMED:
        if stale:
            logger.warning(
                "KB incomplete-ingest sweep [report-only]: an armed run would "
                f"delete {len(stale)} row(s) whose ingest never completed, with "
                f"their vectors: {result.would_delete}. Nothing was deleted."
            )
        return result
    for item_id, stamp in stale:
        try:
            if await _remove(store, db_session_factory, item_id, stamp):
                result.deleted.append(item_id)
            else:
                result.spared += 1
        except Exception as exc:
            result.unremoved += 1
            logger.warning(
                f"KB incomplete-ingest sweep: removing {item_id} failed ({exc}); "
                "its row is kept for the next run."
            )
    if result.deleted:
        logger.info(
            f"KB incomplete-ingest sweep: deleted {len(result.deleted)} row(s) "
            f"whose ingest never completed, with their vectors: {result.deleted}"
        )
    return result


async def _read_stale(
    db_session_factory: Callable[[], Awaitable[Any]], cutoff: datetime
) -> List[Tuple[str, datetime]]:
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        _as_utc,
    )

    # ``_as_utc``: the rule of the ``created_at`` guard in
    # ``DatabaseKnowledgeItemRepository.delete``, a naive value read as UTC.
    query = (
        select(KnowledgeItemModel.item_id, KnowledgeItemModel.created_at)
        .where(
            KnowledgeItemModel.ingest_complete == False,  # noqa: E712
            KnowledgeItemModel.created_at < _as_utc(cutoff),
        )
        .order_by(KnowledgeItemModel.created_at)
    )
    async with db_session_factory() as session:
        return [(item_id, stamp) for item_id, stamp in (await session.execute(query))]


async def _remove(
    store: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    item_id: str,
    stamp: datetime,
) -> bool:
    """True when the row and its vectors went; False when the guarded delete
    matched nothing (the row completed, went or was re-created since it was
    read). Raises with the row change rolled back."""
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    async with db_session_factory() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        if not await repo.delete(
            item_id, commit=False, created_at=stamp, only_incomplete=True
        ):
            await repo.rollback()
            return False
        try:
            await store.delete_documents_by_parent_id(item_id, retries=0)
        except Exception:
            await repo.rollback()
            raise
        await repo.commit()
        return True
