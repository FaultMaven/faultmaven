"""Knowledge Base Bootstrap — atomic, idempotent ingestion of the shipped KB pack.

Runs at application startup after the DI container has wired up the
``KnowledgeService``. Loads the **KB pack** (a self-contained bundle of shipped
runbooks + build-time chunk vectors — see ``faultmaven/bootstrap/kb_pack.py``)
and ensures every runbook in it has a row in ``knowledge_items`` and its chunks
in ChromaDB. Because the pack ships pre-chunked and pre-embedded, ingestion is
pure SQL + vector writes — **no chunking and no embedding model at startup**, so
it runs in seconds instead of the ~tens of minutes that on-pod CPU embedding of
~1244 chunks would take (the rollout-timeout bug; see
``docs/working/ANALYSIS-kb-ingestion-perf.md``).

The pack location is configurable (``KB_PACK_DIR``): empty → the baseline pack
bundled in the image at ``resources/knowledge/pack``; an override points at an
external, replaceable pack so the KB can be updated offline WITHOUT rebuilding
the app image (local bind-mounts a dir; cloud has an init container populate it).

Design Notes
============

Why a separate flow from ``conversion_drafts``?
    Pack runbooks are already authored, validated, and scored by build time.
    Requiring a dashboard "Activate" click per shipped runbook is ceremony for
    content the platform vendor (or admin) already approved. ``conversion_drafts``
    is reserved for case-generated / document-converted drafts that need review.

Atomicity
    Each runbook's ingestion is all-or-nothing:
      1. Compute the deterministic ``item_id`` (carried by the pack).
      2. Check ``knowledge_items`` — if present AND content hash matches, skip.
      3. Call ``KnowledgeService.ingest_runbook(prechunked=...)`` — SQL row
         first, ChromaDB chunks (pack vectors) second.
      4. Validate ``chunks_created > 0``; on failure, surface loudly and clean
         up — never leave half-state.

Idempotency
    Re-running is safe. Unchanged runbooks (content-hash match) are skipped;
    changed ones are re-ingested (delete + re-create). Runbooks removed from the
    pack are pruned from ``knowledge_items`` + ChromaDB (orphan prune below).
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)


class BootstrapResult:
    """Per-run summary. Logged at INFO; returned for tests."""

    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.skipped_unchanged: list[str] = []
        self.failed: list[tuple[str, str]] = []  # (relpath, reason)
        self.pruned: list[str] = []  # orphaned built-in item_ids removed

    def __repr__(self) -> str:
        return (
            f"BootstrapResult(ingested={len(self.ingested)}, "
            f"skipped_unchanged={len(self.skipped_unchanged)}, "
            f"failed={len(self.failed)}, "
            f"pruned={len(self.pruned)})"
        )


async def bootstrap_kb(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    organization_id: str,
    project_root: Optional[Path] = None,
    pack_dir: Optional[Path] = None,
) -> BootstrapResult:
    """Run the KB ingestion bootstrap from the shipped pack.

    Args:
        knowledge_service: The wired-up KnowledgeService (post-DI).
        db_session_factory: Async session factory (``get_db_session``).
        organization_id: Default org for the global-scope runbooks.
        project_root: Override the project root (test injection).
        pack_dir: Explicit pack directory (test injection). When None, resolves
            from ``KB_PACK_DIR`` settings, falling back to the bundled baseline.

    Returns:
        BootstrapResult — per-runbook outcomes (counts + failure reasons).

    Failure handling:
        Individual runbook failures are logged and recorded but do NOT abort the
        bootstrap — one bad runbook shouldn't block the others.
    """
    from faultmaven.bootstrap.data_init import get_project_root
    from faultmaven.bootstrap.kb_pack import KbPack, resolve_pack_dir

    result = BootstrapResult()
    root = project_root or get_project_root()

    if pack_dir is None:
        configured = ""
        try:
            from faultmaven.config.settings import get_settings

            configured = get_settings().database.kb_pack_dir
        except Exception as exc:  # settings unavailable in some test contexts
            logger.debug(f"Could not read KB_PACK_DIR from settings: {exc}")
        pack_dir = resolve_pack_dir(root, configured)

    pack = KbPack.load(Path(pack_dir))
    if pack is None:
        logger.warning(
            "KB bootstrap: no pack loaded from %s — knowledge base will be "
            "empty until a valid pack is present.",
            pack_dir,
        )
        return result

    logger.info(
        "KB bootstrap: ingesting %d runbook(s) from pack %s (version=%s)",
        len(pack),
        pack.source_dir,
        pack.version,
    )

    for runbook in pack.runbooks:
        try:
            outcome = await _ingest_pack_runbook(
                runbook=runbook,
                knowledge_service=knowledge_service,
                db_session_factory=db_session_factory,
                organization_id=organization_id,
            )
            if outcome == "ingested":
                result.ingested.append(runbook.relpath)
            else:  # "skipped"
                result.skipped_unchanged.append(runbook.relpath)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"Bootstrap failed for {runbook.relpath}: {reason}",
                exc_info=True,
            )
            result.failed.append((runbook.relpath, reason))

    # Prune built-in rows whose runbook is no longer in the pack (e.g. removed or
    # its frontmatter ``id`` changed). Keyed off the pack's item_ids.
    result.pruned = await _prune_orphan_builtins(
        pack.item_ids, knowledge_service, db_session_factory
    )

    logger.info(
        f"KB bootstrap complete: {len(result.ingested)} ingested, "
        f"{len(result.skipped_unchanged)} unchanged, "
        f"{len(result.failed)} failed, "
        f"{len(result.pruned)} pruned"
    )
    return result


async def _ingest_pack_runbook(
    runbook: Any,  # kb_pack.PackRunbook
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    organization_id: str,
) -> str:
    """Ingest one pack runbook. Returns ``"ingested"`` or ``"skipped"``.

    Idempotent: skips when an existing row's content hashes equal to the pack's
    ``content_hash``. Writes the SQL row + the pack's pre-embedded chunks; never
    loads the embedding model.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    # Idempotency: skip if the item exists and content is unchanged.
    async with db_session_factory() as session:
        existing = await session.execute(
            select(KnowledgeItemModel).where(
                KnowledgeItemModel.item_id == runbook.item_id
            )
        )
        existing_row = existing.scalar_one_or_none()

    if existing_row is not None:
        existing_hash = hashlib.sha256(existing_row.content.encode("utf-8")).hexdigest()
        if existing_hash == runbook.content_hash:
            logger.debug(f"Skipping {runbook.relpath}: unchanged")
            return "skipped"
        logger.info(
            f"{runbook.relpath}: content changed since last ingest — re-ingesting"
        )
        await _delete_existing(runbook.item_id, knowledge_service, db_session_factory)

    prechunked = [(c.text, c.embedding) for c in runbook.chunks]

    chunks_created = await knowledge_service.ingest_runbook(
        document_id=runbook.item_id,
        title=runbook.title,
        content=runbook.content,
        organization_id=organization_id,
        document_type="runbook",
        tags=runbook.tags,
        source_url=runbook.source_url,
        scope=runbook.scope,
        owner_id=runbook.owner_id,
        team_id=runbook.team_id,
        # Pre-deployed runbooks carry COMMUNITY trust via verification_level, not
        # a fake verified_by (an FK to users.user_id — must be a real user or
        # NULL). See the FK regression history in #378.
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
        prechunked=prechunked,
        # v4 per-Cause graph records → knowledge_items.metadata["causes"], for the
        # runbook-cause matcher to read back by item_id (inert until that lands).
        causes=runbook.causes,
    )

    if chunks_created <= 0:
        await _delete_existing(runbook.item_id, knowledge_service, db_session_factory)
        raise RuntimeError(
            f"Vector indexing produced 0 chunks for {runbook.relpath} "
            f"(pack supplied {len(prechunked)} chunks). SQL row cleaned up."
        )

    logger.info(
        f"Ingested {runbook.relpath} ({chunks_created} chunks, scope={runbook.scope})"
    )
    return "ingested"


# Built-in rows carry a deterministic ``kb_<12 hex>`` item_id (see
# ``_item_id_from_runbook_id``). User/authored items use random UUIDs, so this
# pattern NEVER matches them — the prune below is structurally incapable of
# touching authored content.
_BUILTIN_ITEM_ID_RE = re.compile(r"^kb_[0-9a-f]{12}$")


async def _prune_orphan_builtins(
    keep_ids: set,
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
) -> list[str]:
    """Delete built-in knowledge_items (+ vectors) not present in the pack.

    A built-in row is an orphan when its deterministic ``kb_<hash>`` id is not in
    ``keep_ids`` (the pack's item_ids) — typically because the runbook was
    removed from the pack or its frontmatter ``id`` changed. Authored/uuid items
    are out of scope: the id pattern can't match them.

    Safety: if ``keep_ids`` is empty (a pack-load anomaly, not a legitimate
    "remove everything" signal), prune NOTHING.
    """
    if not keep_ids:
        logger.warning(
            "Orphan prune skipped: pack yielded no item_ids "
            "(possible pack problem). No rows removed."
        )
        return []

    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    async with db_session_factory() as session:
        rows = await session.execute(select(KnowledgeItemModel.item_id))
        all_item_ids = [r[0] for r in rows.all()]

    pruned: list[str] = []
    for item_id in all_item_ids:
        if _BUILTIN_ITEM_ID_RE.match(item_id) and item_id not in keep_ids:
            await _delete_existing(item_id, knowledge_service, db_session_factory)
            pruned.append(item_id)
            logger.info(
                f"Pruned orphaned built-in knowledge_item {item_id} "
                f"(not in current pack)"
            )
    return pruned


async def _delete_existing(
    item_id: str,
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
) -> None:
    """Best-effort cleanup of a knowledge_items row + ChromaDB chunks.

    Errors are logged but don't propagate — this is recovery code; the
    caller is already on a failure path.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    # ChromaDB chunks first (idempotent — no-op if not present).
    try:
        if knowledge_service._vector_store and hasattr(
            knowledge_service._vector_store, "delete_documents_by_parent_id"
        ):
            await knowledge_service._vector_store.delete_documents_by_parent_id(item_id)
    except Exception as e:
        logger.warning(f"Failed to delete ChromaDB chunks for {item_id}: {e}")

    # SQL row second.
    try:
        async with db_session_factory() as session:
            existing = await session.execute(
                select(KnowledgeItemModel).where(KnowledgeItemModel.item_id == item_id)
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()
    except Exception as e:
        logger.warning(f"Failed to delete SQL row for {item_id}: {e}")


def _item_id_from_runbook_id(runbook_id: str) -> str:
    """Stable item_id derived from the runbook's frontmatter ``id``.

    Thin wrapper over :func:`faultmaven.utils.runbook_id.item_id_from_runbook_id`
    so the pack builder and bootstrap derive the same id.
    """
    from faultmaven.utils.runbook_id import item_id_from_runbook_id

    return item_id_from_runbook_id(runbook_id)
