"""Knowledge Base Bootstrap — atomic, idempotent ingestion of shipped runbooks.

Runs at application startup after the DI container has wired up the
``KnowledgeService``. Walks ``data/knowledge/{scope}/**/*.md`` and ensures
every runbook on disk has a corresponding row in ``knowledge_items`` and
embeddings in ChromaDB. Pre-deployed runbooks (built-ins, toolkit-deployed,
community contributions) ingest **directly** without passing through the
``conversion_drafts`` table — that table is reserved for case-generated and
document-converted drafts that legitimately need human review.

Design Notes
============

Why a separate flow from ``conversion_drafts``?
    Pre-deployed `.md` files are already authored, validated, and scored
    by the time they hit ``data/knowledge/{scope}/``. Requiring the user
    to click "Activate" in the dashboard for every shipped runbook is
    ceremony for content the platform vendor (or admin) has already
    approved. Conflating the two flows is what produced the bugs traced
    in ``docs/architecture/knowledge-and-ai/kb-ingestion-architecture.md``.

Atomicity
    Each file's ingestion is all-or-nothing:
      1. Verify BGE-M3 model is loaded (eager pre-load at bootstrap entry).
      2. Compute deterministic ``item_id`` from runbook frontmatter ``id``.
      3. Check ``knowledge_items`` — if present AND content matches, skip.
      4. Call ``KnowledgeService.ingest_runbook()`` — SQL row first,
         ChromaDB embeddings second.
      5. Validate ``chunks_created > 0``; on failure, surface loudly
         (don't silently leave a half-state — that bug history is what
         this bootstrap exists to prevent).

Idempotency
    Re-running the bootstrap is safe. Files that haven't changed get
    skipped; new/changed files get (re-)ingested. Deleted files are
    NOT removed from the KB by the bootstrap — that's a separate
    concern owned by the dashboard's explicit delete path.

BGE-M3 race avoidance
    The most common silent-failure mode in the previous design was
    ``_index_document_in_vector_store`` returning 0 chunks because the
    BGE-M3 model wasn't yet loaded. We eagerly load it here, **before**
    any ingestion attempt, so the lazy-load race can't happen on the
    bootstrap path.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)


# Scopes whose `data/knowledge/{scope}/` directories the bootstrap will walk.
# ``global`` is the only one currently shipped from the repo; ``team`` and
# ``personal`` are listed defensively so an admin who manually drops a file
# into those directories gets the same auto-ingest behaviour.
_BOOTSTRAP_SCOPES = ("global", "team", "personal")


class BootstrapResult:
    """Per-run summary. Logged at INFO; returned for tests."""

    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.skipped_unchanged: list[str] = []
        self.failed: list[tuple[str, str]] = []  # (filename, reason)

    def __repr__(self) -> str:
        return (
            f"BootstrapResult(ingested={len(self.ingested)}, "
            f"skipped_unchanged={len(self.skipped_unchanged)}, "
            f"failed={len(self.failed)})"
        )


async def bootstrap_kb(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    organization_id: str,
    project_root: Optional[Path] = None,
) -> BootstrapResult:
    """Run the KB ingestion bootstrap.

    Args:
        knowledge_service: The wired-up KnowledgeService (post-DI).
        db_session_factory: Async session factory (``get_db_session``).
        organization_id: Default org for the global-scope runbooks. In
            single-tenant mode this is the platform-default org id.
        project_root: Override the project root path (test injection).

    Returns:
        BootstrapResult — per-file outcomes (counts + failure reasons).

    Failure handling:
        Individual file failures are logged and recorded in the result
        but do NOT abort the bootstrap — a single bad runbook shouldn't
        block the other 58 from ingesting. Caller can inspect
        ``result.failed`` to surface failures (e.g., at API healthcheck
        time).
    """
    from faultmaven.bootstrap.data_init import get_project_root

    result = BootstrapResult()
    root = project_root or get_project_root()
    knowledge_dir = root / "data" / "knowledge"

    if not knowledge_dir.exists():
        logger.info("No data/knowledge/ directory found — nothing to bootstrap")
        return result

    # Eager-load BGE-M3 so the lazy-load race that produced silent
    # ``chunks_created=0`` failures in the previous design can't happen.
    # ``startup`` is the canonical triggered_by string for this path.
    from faultmaven.infrastructure.model_cache import model_cache

    embed_model = model_cache.get_bge_m3_model(triggered_by="startup")
    if embed_model is None:
        logger.error(
            "BGE-M3 model failed to load — KB bootstrap aborted. "
            "Runbooks on disk will not be queryable until the model loads "
            "and the bootstrap re-runs (restart API)."
        )
        # Don't iterate files when we know ingestion will fail. The result
        # carries an empty ingested list which the caller can surface.
        return result

    files_to_ingest = _enumerate_runbook_files(knowledge_dir)
    if not files_to_ingest:
        logger.info("No runbook files found under data/knowledge/{scope}/")
        return result

    logger.info(
        f"Bootstrap: found {len(files_to_ingest)} runbook file(s) "
        f"across {_BOOTSTRAP_SCOPES} scopes"
    )

    for scope, md_path in files_to_ingest:
        relative = md_path.relative_to(knowledge_dir)
        try:
            outcome = await _ingest_one(
                md_path=md_path,
                scope=scope,
                knowledge_service=knowledge_service,
                db_session_factory=db_session_factory,
                organization_id=organization_id,
            )
            if outcome == "ingested":
                result.ingested.append(str(relative))
            else:  # "skipped"
                result.skipped_unchanged.append(str(relative))
        except Exception as exc:
            # Per-file failures don't abort the bootstrap.
            reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"Bootstrap failed for {relative}: {reason}",
                exc_info=True,
            )
            result.failed.append((str(relative), reason))

    logger.info(
        f"KB bootstrap complete: {len(result.ingested)} ingested, "
        f"{len(result.skipped_unchanged)} unchanged, "
        f"{len(result.failed)} failed"
    )
    return result


def _enumerate_runbook_files(knowledge_dir: Path) -> list[tuple[str, Path]]:
    """Return all (scope, .md path) pairs under data/knowledge/{scope}/.

    Skips:
      - data/knowledge/sources/ (toolkit-staged sources, not runbooks)
      - non-scope subdirectories (anything not in _BOOTSTRAP_SCOPES)
    """
    pairs: list[tuple[str, Path]] = []
    for scope in _BOOTSTRAP_SCOPES:
        scope_dir = knowledge_dir / scope
        if not scope_dir.exists():
            continue
        for md in sorted(scope_dir.rglob("*.md")):
            pairs.append((scope, md))
    return pairs


async def _ingest_one(
    md_path: Path,
    scope: str,
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    organization_id: str,
) -> str:
    """Ingest a single runbook file. Returns ``"ingested"`` or ``"skipped"``.

    Raises if any step fails; the caller records the failure and continues
    to the next file.

    Idempotency: a stable ``item_id`` is derived from frontmatter ``id``
    so re-runs find existing rows. Content comparison (SHA-256) skips
    re-ingestion when the file is unchanged.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    # Parse frontmatter to get the canonical runbook id and metadata.
    try:
        import frontmatter
    except ImportError:  # pragma: no cover - dep is in pyproject
        raise RuntimeError("python-frontmatter is required for KB bootstrap")

    post = frontmatter.load(str(md_path))
    metadata = post.metadata
    content = md_path.read_text(encoding="utf-8")  # full file incl. frontmatter

    runbook_id = metadata.get("id")
    title = metadata.get("title")
    if not runbook_id or not title:
        raise ValueError(
            f"Missing required frontmatter (id or title) in {md_path.name}"
        )

    # Deterministic item_id keyed off the runbook's stable identity.
    # Same runbook id → same item_id across runs.
    item_id = _item_id_from_runbook_id(runbook_id)

    # Idempotency check: skip if the item exists and content is unchanged.
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    async with db_session_factory() as session:
        existing = await session.execute(
            select(KnowledgeItemModel).where(KnowledgeItemModel.item_id == item_id)
        )
        existing_row = existing.scalar_one_or_none()

    if existing_row is not None:
        existing_hash = hashlib.sha256(existing_row.content.encode("utf-8")).hexdigest()
        if existing_hash == content_hash:
            logger.debug(f"Skipping {md_path.name}: unchanged")
            return "skipped"
        # Changed content — remove the existing row (and its ChromaDB
        # chunks) so the fresh ingestion writes clean state.
        logger.info(f"{md_path.name}: content changed since last ingest — re-ingesting")
        await _delete_existing(item_id, knowledge_service, db_session_factory)

    # Atomic ingest: writes knowledge_items row + ChromaDB embeddings.
    # `ingest_runbook` raises on any failure (including 0-chunk silent
    # failure) and cleans up its own SQL row before raising. The
    # `chunks_created <= 0` branch below is defence-in-depth in case that
    # contract regresses — the bootstrap should never tolerate half-state.
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]

    chunks_created = await knowledge_service.ingest_runbook(
        document_id=item_id,
        title=title,
        content=content,
        organization_id=organization_id,
        document_type="runbook",
        tags=tags,
        source_url=metadata.get("source_url"),
        scope=scope,
        owner_id=metadata.get("owner_id"),
        team_id=metadata.get("team_id"),
        # Pre-deployed runbooks bypass per-user verification — they ship
        # in the repo or are admin-deployed; the platform is the verifier.
        verified_by="system",
    )

    if chunks_created <= 0:
        # Should be unreachable — `ingest_runbook` raises on this case.
        # If we land here, the contract has regressed; clean up defensively
        # and surface a loud error so the regression is visible.
        logger.error(
            f"ingest_runbook returned 0 chunks for {md_path.name} without raising — "
            f"contract violation. Cleaning up orphaned SQL row defensively."
        )
        await _delete_existing(item_id, knowledge_service, db_session_factory)
        raise RuntimeError(
            f"Vector indexing produced 0 chunks for {md_path.name} (contract "
            f"violation: ingest_runbook should have raised). ChromaDB or "
            f"chunker may be misbehaving."
        )

    logger.info(f"Ingested {md_path.name} ({chunks_created} chunks, scope={scope})")
    return "ingested"


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

    Format: ``kb_{sha256(runbook_id)[:12]}``. Deterministic across runs;
    avoids collision with the random-uuid item_ids generated by the
    user-facing ``verify_draft`` path.
    """
    digest = hashlib.sha256(runbook_id.encode("utf-8")).hexdigest()[:12]
    return f"kb_{digest}"
