"""Collect superseded KB chunks: a generation no row serves (#2388).

Every chunk id ends in the ``vector_generation`` of the row it was written for
(``{parent}_chunk_{i}_{generation}``), and a KB door serves a chunk only while
its row still carries that generation. No writer deletes a chunk: a pack
re-ingest, a content update and a failed write each leave the chunks of a
generation no row holds. They are never served; this sweep removes them.

A chunk is SUPERSEDED when its parent has a complete row that this pass's read
sees, and the generation its id names is absent (written before #2388) or
differs from that row's. It is deleted once it is older than the age floor,
``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS``: a younger one may be a writer's that is
about to put its generation on the row (a pack ingest writes its vectors
before its row; an update its chunks before its UPDATE). A chunk whose parent
has no row is the built-in reconcile's or the authored-orphan sweep's, and one
whose row is incomplete is the incomplete-ingest sweep's: this sweep leaves
both. The served generation is never touched, so there is no document cap:
superseded chunks are the normal product of every pack update.

**Reads.** Id-only: ``list_all_chunk_ids_by_parent``, the rows'
``(item_id, vector_generation)``, and :func:`young_chunk_ids`, one filtered,
id-only read of the chunks younger than the floor. Every listed id not in it is
old or was written before ``indexed_at`` existed: ChromaDB's ``$gte`` skips a
chunk without the key. **Deletes.** By the observed ids, a page at a time
(``delete_observed_chunk_ids``); a page that raises is ``unremoved`` and left
for the next pass.

**Its one false positive** is a second database on the same ChromaDB, which
holds the same built-in ids under other generations: a wrongful delete there
removes a live document's served chunks. Precondition for arming, as for the
authored-orphan sweep: **one ChromaDB per database.** A row RLS hides can only
spare a chunk, never condemn one.

**Gate**, the incomplete-ingest sweep's (no knobs of its own):
:func:`web_startup_generation_mode` on the standalone web startup, which
deletes only on a local ``PersistentClient`` and is report-only under an
external ``CHROMADB_URL``; ``kb_seed`` passes
``resolve_incomplete_sweep_mode``, report-only until an operator arms it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from sqlalchemy import select

from faultmaven.bootstrap.kb_incomplete_sweep import resolve_max_age
from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    SWEEP_OFF,
    SWEEP_REPORT_ONLY,
)
from faultmaven.infrastructure.knowledge.admission import chunk_generation_from_id

logger = logging.getLogger(__name__)

#: Chunk ids per delete call, under ChromaDB's per-call batch limit.
DELETE_PAGE = 1000


@dataclass
class GenerationSweepResult:
    """What the sweep found, deleted and left. Counts are chunks."""

    #: ``off`` / ``report_only`` / ``armed``.
    mode: str = SWEEP_REPORT_ONLY
    #: Superseded chunks past the floor: what an armed run deletes.
    would_delete: int = 0
    #: The parents those chunks belong to.
    documents: List[str] = field(default_factory=list)
    #: Chunks deleted (armed runs only).
    deleted: int = 0
    #: Chunks whose delete raised: left for the next run.
    unremoved: int = 0
    #: Why the sweep did not run, when it did not.
    skipped: Optional[str] = None

    def summary(self) -> Dict[str, Any]:
        """The job-result shape."""
        return {
            "mode": self.mode,
            "skipped": self.skipped,
            "would_delete": self.would_delete,
            "deleted": self.deleted,
            "unremoved": self.unremoved,
            "would_delete_documents": list(self.documents),
        }


def web_startup_generation_mode() -> str:
    """The web startup's mode: ``armed`` on a local ``PersistentClient``,
    report-only under an external ``CHROMADB_URL`` (the predicate
    ``fm-reset-kb`` dispatches on) or when settings cannot be read."""
    from faultmaven.config.deployment_coherence import is_external_chroma_configured
    from faultmaven.config.settings import get_settings

    try:
        external = is_external_chroma_configured(get_settings())
    except Exception as exc:  # settings unavailable in some test contexts
        logger.debug(f"KB generation sweep: settings unreadable ({exc})")
        return SWEEP_REPORT_ONLY
    return SWEEP_REPORT_ONLY if external else SWEEP_ARMED


async def young_chunk_ids(
    store: Any, max_age: timedelta, now: Optional[datetime] = None
) -> Set[str]:
    """The chunk ids written less than ``max_age`` before ``now``: one
    filtered, id-only read (``indexed_at >= cutoff``). Raises what the store
    raises: an unknown age is never read as old."""
    now = datetime.now(timezone.utc) if now is None else now
    cutoff = int((now - max_age).timestamp())
    young = await store.list_chunk_ids_by_parent({"indexed_at": {"$gte": cutoff}})
    return {chunk_id for ids in young.values() for chunk_id in ids}


async def sweep_superseded_generations(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    *,
    mode: str,
    max_age: Optional[timedelta] = None,
    now: Optional[datetime] = None,
) -> GenerationSweepResult:
    """Find the superseded chunks older than ``max_age`` (default: the
    setting) at ``now`` and, when ``mode`` is ``armed``, delete them. Never
    raises."""
    result = GenerationSweepResult(mode=mode)
    if mode == SWEEP_OFF:
        result.skipped = "disabled"
        return result
    store = getattr(knowledge_service, "_vector_store", None)
    if store is None:
        result.skipped = "no_vector_store"
        return result
    try:
        listing = await store.list_all_chunk_ids_by_parent()
        served = await _read_served_generations(db_session_factory)
        young = await young_chunk_ids(
            store, resolve_max_age() if max_age is None else max_age, now
        )
    except Exception as exc:
        logger.warning(f"KB generation sweep skipped: {exc}")
        result.skipped = "error"
        return result

    superseded: List[str] = []
    for parent, chunk_ids in sorted(listing.items()):
        generation = served.get(parent)
        if generation is None:
            continue  # no complete row: another collector's
        stale = [
            chunk_id
            for chunk_id in chunk_ids
            if chunk_generation_from_id(chunk_id) != generation
            and chunk_id not in young
        ]
        if stale:
            superseded.extend(stale)
            result.documents.append(parent)
    result.would_delete = len(superseded)
    if mode != SWEEP_ARMED:
        if superseded:
            logger.warning(
                "KB generation sweep [report-only]: an armed run would delete "
                f"{len(superseded)} superseded chunk(s) of "
                f"{len(result.documents)} document(s). Nothing was deleted."
            )
        return result
    for start in range(0, len(superseded), DELETE_PAGE):
        page = superseded[start : start + DELETE_PAGE]
        try:
            result.deleted += len(await store.delete_observed_chunk_ids(page))
        except Exception as exc:
            result.unremoved += len(page)
            logger.warning(
                f"KB generation sweep: deleting {len(page)} superseded chunk(s) "
                f"raised ({exc}); they stay until the next run."
            )
    if result.deleted:
        logger.info(
            f"KB generation sweep: deleted {result.deleted} superseded chunk(s) "
            f"of {len(result.documents)} document(s)."
        )
    return result


async def _read_served_generations(
    db_session_factory: Callable[[], Awaitable[Any]],
) -> Dict[str, str]:
    """Each complete row's id and generation, as this session reads them."""
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    query = select(
        KnowledgeItemModel.item_id, KnowledgeItemModel.vector_generation
    ).where(
        KnowledgeItemModel.ingest_complete == True
    )  # noqa: E712
    async with db_session_factory() as session:
        return {
            item_id: generation for item_id, generation in await session.execute(query)
        }
