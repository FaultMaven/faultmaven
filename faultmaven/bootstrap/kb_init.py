"""Knowledge Base Bootstrap — atomic, idempotent ingestion of the shipped KB pack.

Runs at application startup after the DI container has wired up the
``KnowledgeService``. Loads the **KB pack** (a self-contained bundle of shipped
runbooks + build-time chunk vectors — see ``faultmaven/bootstrap/kb_pack.py``)
and ensures every runbook in it has a row in ``knowledge_items`` and its chunks
in ChromaDB. Because the pack ships pre-chunked and pre-embedded, ingestion is
pure SQL + vector writes — **no chunking and no embedding model at startup**, so
it runs in seconds instead of the ~tens of minutes that on-pod CPU embedding of
~1319 chunks would take (the rollout-timeout bug; see
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
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select

from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    chunk_stamp_identity,
)

logger = logging.getLogger(__name__)


class BootstrapResult:
    """Per-run summary. Logged at INFO; returned for tests."""

    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.skipped_unchanged: list[str] = []
        self.failed: list[tuple[str, str]] = []  # (relpath, reason)
        self.pruned: list[str] = []  # orphaned built-in item_ids removed
        # Reconcile pass (SQL <-> ChromaDB consistency):
        self.orphaned_vectors_cleaned: list[str] = []  # parent_ids w/ no DB row
        self.repaired_rows: list[str] = []  # DB rows re-embedded this boot
        self.orphaned_rows: list[str] = []  # DB rows still w/o vectors after repair

    def __repr__(self) -> str:
        return (
            f"BootstrapResult(ingested={len(self.ingested)}, "
            f"skipped_unchanged={len(self.skipped_unchanged)}, "
            f"failed={len(self.failed)}, "
            f"pruned={len(self.pruned)}, "
            f"orphaned_vectors_cleaned={len(self.orphaned_vectors_cleaned)}, "
            f"repaired_rows={len(self.repaired_rows)}, "
            f"orphaned_rows={len(self.orphaned_rows)})"
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

    # Reconcile the vector index against knowledge_items (the source of truth).
    # Catches drift that the row-keyed prune above is structurally blind to:
    # ChromaDB chunks whose parent has no row (orphaned vectors — deleted) and
    # rows with no chunks (orphaned rows — warned). This is the safety net that
    # keeps retrieval from landing on a ghost vector with no row behind it.
    (
        result.orphaned_vectors_cleaned,
        orphaned_rows,
    ) = await _reconcile_vectors(knowledge_service, db_session_factory)

    # Cross-store repair: an orphaned row (SQL row with no vectors — the
    # half-state a crash between the SQL commit and the Chroma write leaves) is
    # silently non-retrievable. Reconcile can only warn; here we re-chunk +
    # re-embed a BOUNDED number of them so a rare crash self-heals on the next
    # boot. Bounded because repair loads the embedding model the pack path
    # deliberately skips — re-embedding the whole KB reintroduces the on-pod
    # timeout the pack exists to avoid.
    max_rows, max_chunks = _resolve_repair_bounds()
    result.repaired_rows, result.orphaned_rows = await _repair_orphaned_rows(
        orphaned_rows, knowledge_service, max_rows=max_rows, max_chunks=max_chunks
    )

    logger.info(
        f"KB bootstrap complete: {len(result.ingested)} ingested, "
        f"{len(result.skipped_unchanged)} unchanged, "
        f"{len(result.failed)} failed, "
        f"{len(result.pruned)} pruned, "
        f"{len(result.orphaned_vectors_cleaned)} orphaned vectors cleaned, "
        f"{len(result.repaired_rows)} rows repaired, "
        f"{len(result.orphaned_rows)} orphaned rows"
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

    # Chunk-integrity guard — validated FIRST, before any lookup/delete. Rejecting
    # before the re-ingest ``_delete_existing`` below is deliberate: a malformed
    # pack UPDATE must not delete a previously-good row and leave nothing behind
    # (the "no silently-unretrievable runbook" invariant) — the prior copy stays
    # intact, isolated to ``BootstrapResult.failed`` by the caller.
    #
    # (a) A runbook with no chunks can never be retrieved. It would otherwise pass
    #     the ordering check vacuously, then ``_delete_existing`` + an empty
    #     ``prechunked`` would trip the ``chunks_created <= 0`` check below AFTER
    #     the delete — losing the prior row. Reject it here, before the delete.
    if not runbook.chunks:
        raise RuntimeError(
            f"KB pack runbook {runbook.relpath} has no chunks — it could never be "
            f"retrieved (the pack is malformed; rebuild required)."
        )

    # (b) Each pack chunk carries an explicit ``chunk_index``, but the ingest path
    #     below re-derives the chunk_index and chunk id by list position
    #     (``enumerate``). If a pack's chunk list is out of order — or has gaps or
    #     duplicate indices — those ids would silently misalign from the manifest
    #     the pack builder wrote. Require canonical ``0..n-1`` order so a malformed
    #     pack fails loudly instead of misaligning ids.
    declared_indices = [c.chunk_index for c in runbook.chunks]
    if declared_indices != list(range(len(declared_indices))):
        raise RuntimeError(
            f"KB pack chunk ordering invalid for {runbook.relpath}: chunk_index "
            f"sequence {declared_indices} is not contiguous 0..{len(declared_indices) - 1} "
            f"in list order — the pack is malformed (rebuild required)."
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
        content_unchanged = existing_hash == runbook.content_hash
        # The content hash covers only the markdown. ``causes`` is derived from
        # the markdown but travels separately in metadata, so a pack change that
        # edits ONLY causes (markdown byte-identical) would hash-match and be
        # skipped — leaving the live consumer (the KB cause seeder) reading stale
        # structure. Compare the persisted causes too and re-ingest on drift.
        existing_metadata = _decode_metadata(existing_row.knowledge_metadata)
        existing_causes = existing_metadata.get("causes")
        causes_unchanged = _causes_fingerprint(existing_causes) == _causes_fingerprint(
            runbook.causes
        )
        # Third axis (fm#1108): the chunks carry a stamp of the cause letters the
        # seeder joins on, and that stamp is only as good as the schema+grammar
        # that produced it. Neither is in the content hash — the grammar lives in
        # code — so before this a grammar change left every stored stamp meaning
        # something it no longer meant, with nothing to notice. Comparing the
        # identity makes the re-stamp automatic: edit the grammar, and the next
        # boot re-ingests the pack. Cheap, because pack re-ingest is prechunked.
        stamp_unchanged = existing_metadata.get("chunk_stamp") == chunk_stamp_identity()
        if content_unchanged and causes_unchanged and stamp_unchanged:
            logger.debug(f"Skipping {runbook.relpath}: unchanged")
            return "skipped"
        if content_unchanged and causes_unchanged:
            # Re-ingest is delete-then-create, and the created row is published
            # (``KnowledgeItem.is_published`` defaults True). For a runbook an
            # operator UNPUBLISHED that would silently put it back into
            # retrieval — ``delete_document`` unpublishes a built-in by dropping
            # its vectors precisely because retrieval ignores the flag, and its
            # contract is that the skip survives restart until the CONTENT
            # changes. A stamp change is not a content change: nothing about the
            # runbook moved, only our code did.
            #
            # Skipping is also the correct answer on its own terms — an
            # unpublished built-in has no vectors, so there are no stamps on it
            # to refresh. If it is ever republished it is re-vectorised then,
            # by code that stamps.
            if not existing_row.is_published:
                logger.debug(
                    f"Skipping {runbook.relpath}: stamp identity changed but the "
                    "runbook is unpublished (no vectors to re-stamp)"
                )
                return "skipped"
            logger.info(
                f"{runbook.relpath}: chunk stamp identity changed (content and "
                "causes unchanged) — re-ingesting to re-stamp"
            )
        elif content_unchanged:
            logger.info(
                f"{runbook.relpath}: causes changed (markdown unchanged) — "
                "re-ingesting"
            )
        else:
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
        # v4 per-Cause graph records → knowledge_items.metadata["causes"], stored
        # verbatim (the cross-repo pack contract pins the shape).
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


async def _reconcile_vectors(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
) -> tuple[list[str], list[str]]:
    """Reconcile the KB vector index against ``knowledge_items`` (source of truth).

    The prune above is keyed off DB rows, so it can only ever delete a row plus
    that row's vectors — it is structurally blind to vectors whose row is already
    gone. Those accumulate (re-ingest/delete paths that failed to clear vectors,
    older drift) and are invisible to every DB-side check. This pass closes the
    loop by comparing both sides:

      * **orphaned vectors** — a built-in ``parent_document_id`` present in
        ChromaDB with no ``knowledge_items`` row. Deleted: the row is the source
        of truth for a shipped runbook, so a vector with no row can never be
        retrieved-then-resolved — and a single such ghost landing as the top KB
        hit silently kills a runbook-cause match.
      * **orphaned rows** — a row with no vectors. Can't be repaired here (no
        embedding model at startup), so it is WARNED, not touched.

    Returns ``(deleted_parent_ids, orphaned_row_ids)``.

    **Scope — only deletes built-in ``kb_<12 hex>`` vectors.** The bootstrap DB
    session is RLS-scoped (``app.current_org_id`` is set per-transaction; see
    ``database.py``), so ``db_ids`` is a SINGLE org's rows, while the shared
    ``faultmaven_kb`` collection holds every org's vectors. Diffing the full
    collection against a per-org row set would mark every OTHER tenant's runbooks
    as orphans and delete them — cross-tenant KB data loss. Restricting deletion
    to the built-in id class (the same discriminator the orphan-prune uses) bounds
    the blast radius to platform-shipped runbooks, which is exactly the drift this
    pass exists to clean; authored/personal/team vectors (uuid or ``kb_<16 hex>``)
    are never touched. The pattern can't match them.

    Safety: if the DB yields ZERO knowledge_items (a pathological all-rows-missing
    state, e.g. ingest fully failed this boot) we do NOT delete any vectors — that
    would wipe the pack's valid vectors on a transient row-write failure. We leave
    the index intact for the next boot to repair.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    vector_store = getattr(knowledge_service, "_vector_store", None)
    if vector_store is None:
        logger.debug("Reconcile skipped: no vector store wired.")
        return [], []
    if not hasattr(vector_store, "list_parent_document_ids"):
        # A store that can't enumerate (e.g. the generic ChromaDBVectorStore
        # fallback when the KB client failed to init) silently leaves drift in
        # place — surface that loudly, it is a degraded configuration, not a no-op.
        logger.warning(
            "Reconcile skipped: wired vector store %s cannot enumerate parents — "
            "KB SQL<->vector drift will NOT be cleaned this boot.",
            type(vector_store).__name__,
        )
        return [], []

    try:
        chroma_parents = await vector_store.list_parent_document_ids()
    except Exception as exc:
        logger.warning(f"Reconcile skipped: could not list vector parents: {exc}")
        return [], []

    async with db_session_factory() as session:
        rows = await session.execute(select(KnowledgeItemModel.item_id))
        db_ids = {r[0] for r in rows.all()}

    # Empty source of truth = anomaly, not a wipe signal. Guard FIRST, before any
    # delete or per-row warning (with no rows there is nothing meaningful to say).
    if not db_ids:
        if chroma_parents:
            logger.warning(
                "Reconcile: knowledge_items is empty but ChromaDB holds %d parent "
                "document(s) — refusing to delete vectors (treating as a transient "
                "row-write failure, not a 'remove everything' signal).",
                len(chroma_parents),
            )
        return [], []

    orphaned_rows = sorted(db_ids - chroma_parents)
    for item_id in orphaned_rows:
        logger.warning(
            f"KB consistency: knowledge_item {item_id} has no vectors — it cannot "
            f"be retrieved. Re-ingest required (no embedding model at startup)."
        )

    # Delete only BUILT-IN orphans (see scope note) — never authored/other-tenant
    # vectors. One batched delete instead of a round-trip per parent.
    orphaned_vectors = sorted(
        p for p in (chroma_parents - db_ids) if _BUILTIN_ITEM_ID_RE.match(p)
    )
    if not orphaned_vectors:
        return [], orphaned_rows

    try:
        deleted = await vector_store.delete_documents_by_parents(orphaned_vectors)
        logger.info(
            f"Reconcile: deleted {deleted} orphaned vector chunk(s) across "
            f"{len(orphaned_vectors)} built-in parent(s) with no knowledge_items row."
        )
        return orphaned_vectors, orphaned_rows
    except Exception as exc:
        logger.warning(f"Reconcile: batched orphan delete failed: {exc}")
        return [], orphaned_rows


# Default bounds on the cross-store repair. Overridable per deployment via
# DatabaseSettings.kb_repair_max_rows / kb_repair_max_chunks (env
# KB_REPAIR_MAX_ROWS / KB_REPAIR_MAX_CHUNKS); these constants are the fallback
# used when settings are unavailable (some test contexts) and the direct-call
# defaults. Two distinct guards:
#
#   max_rows — a bulk-loss DISCRIMINATOR. A crash between the SQL commit and the
#     Chroma write orphans a FEW rows (self-healing them is the whole point). But
#     a mass orphan (e.g. the Chroma collection was wiped while knowledge_items
#     stayed intact — a state the content-hash pack skip does NOT re-vector) is
#     an operational problem, not a crash to paper over: above this many orphans
#     we repair NOTHING and warn (recovery there is a full pack re-ingest /
#     reset_kb, not per-row re-embedding on every boot).
#
#   max_chunks — a per-boot WORK budget. Repair is the one bootstrap step that
#     loads BGE-M3 and embeds, and (on the single-tenant web-startup path) it runs
#     BEFORE the app serves readiness — so the risk is the on-pod CPU-embedding
#     time (chunks x pod CPU), NOT the row count. Bounding rows alone is
#     insufficient: even a sub-cap orphan set of small runbooks can be hundreds
#     of chunks (minutes on a limited pod → startupProbe SIGKILL). Once this
#     boot's chunk budget is spent, remaining rows are DEFERRED to the next boot;
#     per-row vectors already persist, so repair is incremental + eventually
#     consistent and startup readiness is never gated on unbounded embedding.
KB_REPAIR_MAX_ROWS = 25
KB_REPAIR_MAX_CHUNKS = 60


def _resolve_repair_bounds() -> tuple[int, int]:
    """Repair bounds from settings, falling back to the module defaults.

    Settings can be unavailable in some test contexts (mirrors how bootstrap_kb
    reads kb_pack_dir), so this degrades to the defaults rather than raising.
    """
    try:
        from faultmaven.config.settings import get_settings

        db = get_settings().database
        return int(db.kb_repair_max_rows), int(db.kb_repair_max_chunks)
    except Exception as exc:  # settings unavailable / misconfigured
        logger.debug(f"Could not read KB repair bounds from settings: {exc}")
        return KB_REPAIR_MAX_ROWS, KB_REPAIR_MAX_CHUNKS


async def _repair_orphaned_rows(
    orphaned_rows: list[str],
    knowledge_service: Any,
    *,
    max_rows: int = KB_REPAIR_MAX_ROWS,
    max_chunks: int = KB_REPAIR_MAX_CHUNKS,
) -> tuple[list[str], list[str]]:
    """Re-embed orphaned knowledge_items rows (SQL row present, vectors missing).

    Returns ``(repaired_ids, still_orphaned_ids)``. Each repair re-chunks +
    re-embeds the row's persisted content with BGE-M3 and writes ONLY the vector
    store (``KnowledgeService.reindex_missing_vectors``, which reads the row via
    the service's own RLS-scoped session) — the SQL row, the source of truth, is
    never touched.

    Cross-tenant-safe: repair only ADDS vectors for rows the caller's scoped
    session already sees, writing each row's OWN scope/owner metadata — it can
    never fabricate another tenant's vectors or mislabel scope. This holds on
    both entry paths (the single-tenant web-startup bootstrap and the
    cross-tenant ``kb_seed`` job seeding the org-free global tier). Contrast
    reconcile's DELETE side, restricted to built-in ids precisely because
    deletion CAN cross tenants; addition cannot, so ALL orphaned rows are
    eligible — a lost authored runbook is repaired the same as a shipped one.

    Bounded twice (``max_rows`` / ``max_chunks``, deployment-configurable):
    ``max_rows`` skips a bulk-loss anomaly entirely, and ``max_chunks`` caps the
    embedding work per boot (deferring the rest to the next boot) so startup
    readiness is never gated on an unbounded re-embed.
    """
    if not orphaned_rows:
        return [], []

    if len(orphaned_rows) > max_rows:
        logger.warning(
            "KB repair skipped: %d orphaned rows exceed the repair cap (%d) — "
            "treating as a bulk-loss anomaly (e.g. the vector collection was wiped), "
            "not the crash recovery this pass handles. Re-embedding all of them on "
            "the boot path would reintroduce the on-pod embedding timeout. Rows left "
            "unretrievable; recover with a full pack re-ingest.",
            len(orphaned_rows),
            max_rows,
        )
        return [], orphaned_rows

    # Surface the EFFECTIVE bounds in the log whenever repair actually acts, so an
    # operator can confirm what a KB_REPAIR_MAX_* override resolved to (the values
    # are otherwise only visible in settings, not at runtime).
    logger.info(
        "KB repair: %d orphaned row(s) within bounds (max_rows=%d, max_chunks=%d) "
        "— re-embedding with BGE-M3.",
        len(orphaned_rows),
        max_rows,
        max_chunks,
    )

    repaired: list[str] = []
    still_orphaned: list[str] = []
    chunks_embedded = 0
    for i, item_id in enumerate(orphaned_rows):
        # Work budget: stop before starting a row once this boot's chunk budget is
        # spent. Overshoot is bounded by one runbook's chunk count (the row in
        # flight always finishes); the deferred rows self-heal on a later boot.
        if chunks_embedded >= max_chunks:
            deferred = orphaned_rows[i:]
            still_orphaned.extend(deferred)
            logger.warning(
                "KB repair: per-boot chunk budget (%d) reached after %d row(s) — "
                "deferring %d orphaned row(s) to the next boot (repair is "
                "incremental to keep startup readiness off the embedding path).",
                max_chunks,
                len(repaired),
                len(deferred),
            )
            break
        try:
            chunks = await knowledge_service.reindex_missing_vectors(item_id)
        except Exception as exc:
            logger.warning(f"KB repair failed for {item_id}: {exc}")
            still_orphaned.append(item_id)
            continue
        if chunks > 0:
            repaired.append(item_id)
            chunks_embedded += chunks
            logger.info(
                f"KB repair: re-embedded {chunks} chunk(s) for orphaned row {item_id}"
            )
        else:
            # 0 chunks = embedding model unavailable / empty content. Fail-safe:
            # leave the row for a later boot (mirrors reconcile's warn-only prior),
            # never raise on the startup path.
            still_orphaned.append(item_id)
            logger.warning(
                f"KB repair produced 0 chunks for {item_id} (embedding model "
                "unavailable?) — row remains unretrievable, will retry next boot."
            )
    return repaired, still_orphaned


def _causes_fingerprint(causes: Optional[list]) -> str:
    """Order-stable, key-order-insensitive fingerprint of a causes record.

    Cause order is authoring-significant (kept), but dict key order is not, so
    ``sort_keys`` lets a semantically-identical re-serialization compare equal.
    """
    return json.dumps(causes or [], sort_keys=True, ensure_ascii=False)


def _decode_metadata(value: Any) -> dict:
    """Decode a ``knowledge_items.metadata`` value to a dict.

    ``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so the column
    type differs by backend — but rows written through
    ``KnowledgeItemRepository`` come back as a **``str`` on both**, because that
    writer binds an already-serialized ``json.dumps(...)`` value: SQLite stores it
    verbatim in TEXT, and JSONB stores it as a JSON *string scalar* which
    deserializes back to the same ``str`` (verified against
    ``JSONB.bind_processor`` — binding a ``str`` yields ``"{\\"causes\\": …}"``,
    not an object). Handling only the dict shape therefore made the causes
    comparison read ``None`` on **every** deployment, so ``causes_unchanged``
    could never be true and every runbook re-ingested on every boot.

    The dict branch is kept because it is the shape any writer binding a real
    object would produce, and because it is the documented JSONB contract — the
    two branches together make this independent of who wrote the row.
    Returns ``{}`` for absent/undecodable values so the caller's ``.get`` is
    always safe.

    Same intent as ``KnowledgeItemRepository._parse_json_dict``, duplicated (not
    imported) to keep bootstrap off a module's infrastructure layer — but
    deliberately NOT its copy semantics: that one ``deepcopy``s the dict branch
    because its callers hand the result out. **The result here is read-only**; the
    dict branch aliases the session-bound ORM attribute, so a caller that mutates
    it would dirty the row. Copy before mutating.
    """
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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

    # ChromaDB chunks first (idempotent — no-op if not present). The vector store
    # implements ``delete_documents_by_parent_id`` (IVectorStore contract); a
    # missing method surfaces as a logged warning here rather than a silent skip,
    # so vector-side cleanup can never quietly become a no-op (the drift bug).
    try:
        if knowledge_service._vector_store:
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
