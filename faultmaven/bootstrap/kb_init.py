"""Knowledge Base Bootstrap — idempotent ingestion of the shipped KB pack.

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

Per runbook
    ``_ingest_pack_runbook`` ingests one runbook:
      1. Take the deterministic ``item_id`` the pack carries.
      2. Check ``knowledge_items`` — if present, ingested from this pack
         version (the pack hash stamped on the row) AND served (a chunk
         carries the row's ``vector_generation``, #2388), skip.
      3. Call ``KnowledgeService.ingest_runbook(prechunked=...,
         replaces_generation=...)`` — the pack vectors first, under a new
         generation, then one transaction that deletes the row it observed
         (guarded on that row's generation) and inserts the new one.
      4. ``ingest_runbook`` raises when indexing returns 0 chunks, before any
         row is written. ``bootstrap_kb`` catches an ``Exception`` from
         ``_ingest_pack_runbook``, logs it at ERROR and records the runbook in
         ``BootstrapResult.failed``. What a failed
         ``KnowledgeService.ingest_runbook`` call can leave is listed in its
         docstring.

Idempotency
    Re-running is safe. Runbooks whose pack version is unchanged (the pack's
    content hash equals the one stamped on the row at ingest, NOT a hash of the
    row's current content, which an operator may have edited) and whose row is
    served are skipped; a new pack version, or a row no chunk of its
    generation serves, is re-ingested. Runbooks removed from the pack have
    their rows pruned; the reconcile collects their vectors. No step here
    deletes a vector but the collectors.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Collection,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
)

from sqlalchemy import select

from faultmaven.bootstrap.kb_generation_sweep import (
    GenerationSweepResult,
    sweep_superseded_generations,
    web_startup_generation_mode,
    young_chunk_ids,
)
from faultmaven.bootstrap.kb_incomplete_sweep import (
    IncompleteSweepResult,
    resolve_max_age,
    sweep_incomplete_ingests,
)
from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    AuthoredSweepResult,
    resolve_sweep_mode,
    sweep_authored_orphans,
)
from faultmaven.infrastructure.knowledge.admission import chunk_generation_from_id
from faultmaven.models.interfaces import IKnowledgeVectorStore

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
        # built-in parent_ids whose row is unpublished (#1966)
        self.unpublished_vectors_cleaned: list[str] = []
        # Authored parents with vectors and no row (#1982): what an armed run
        # deletes (report-only by default), what was deleted, and per
        # enterprise what was left and why.
        self.authored_orphans: AuthoredSweepResult = AuthoredSweepResult()
        # Rows whose ingest never completed, past the age bound (#2394).
        self.incomplete_ingests: IncompleteSweepResult = IncompleteSweepResult()
        # Chunks of a generation no row serves, past the age floor (#2388).
        self.generation_sweep: GenerationSweepResult = GenerationSweepResult()
        self.repaired_rows: list[str] = []  # DB rows re-embedded this boot
        # Complete, published rows no chunk of their generation serves after
        # the pack re-ingest and the repair: what this deployment serves
        # nothing for (#2388).
        self.unserved_rows: list[str] = []

    def __repr__(self) -> str:
        # The authored-orphan counts ride on THIS line because it is the one the
        # CD pipeline keeps (`grep -o "BootstrapResult([^)]*)"`). Its gate reads
        # the LAST `failed=` on the line, so no key after the first may end in
        # `failed` (delete failures are `authored_orphans_unremoved`), and no
        # value may contain a closing parenthesis.
        sweep = self.authored_orphans.totals()
        return (
            f"BootstrapResult(ingested={len(self.ingested)}, "
            f"skipped_unchanged={len(self.skipped_unchanged)}, "
            f"failed={len(self.failed)}, "
            f"pruned={len(self.pruned)}, "
            f"orphaned_vectors_cleaned={len(self.orphaned_vectors_cleaned)}, "
            f"unpublished_vectors_cleaned={len(self.unpublished_vectors_cleaned)}, "
            f"authored_orphans_mode={self.authored_orphans.mode}, "
            f"authored_orphans_would_delete={sweep['would_delete']}, "
            f"authored_orphans_deleted={sweep['deleted']}, "
            f"authored_orphans_capped={sweep['capped']}, "
            f"authored_orphans_disjoint={sweep['disjoint']}, "
            f"authored_orphans_unproven={sweep['unproven']}, "
            f"authored_orphans_misstamped={sweep['misstamped']}, "
            f"authored_orphans_unremoved={sweep['unremoved']}, "
            f"incomplete_ingests_mode={self.incomplete_ingests.mode}, "
            "incomplete_ingests_would_delete="
            f"{len(self.incomplete_ingests.would_delete)}, "
            f"incomplete_ingests_deleted={len(self.incomplete_ingests.deleted)}, "
            f"incomplete_ingests_unremoved={self.incomplete_ingests.unremoved}, "
            f"incomplete_ingests_spared={self.incomplete_ingests.spared}, "
            f"incomplete_ingests_skipped={self.incomplete_ingests.skipped}, "
            f"generation_sweep_mode={self.generation_sweep.mode}, "
            f"generation_sweep_would_delete={self.generation_sweep.would_delete}, "
            f"generation_sweep_deleted={self.generation_sweep.deleted}, "
            f"generation_sweep_unremoved={self.generation_sweep.unremoved}, "
            f"generation_sweep_skipped={self.generation_sweep.skipped}, "
            f"repaired_rows={len(self.repaired_rows)}, "
            f"unserved_rows={len(self.unserved_rows)})"
        )


async def bootstrap_kb(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    enterprise_id: str,
    project_root: Optional[Path] = None,
    pack_dir: Optional[Path] = None,
    sweep_dry_run: Optional[bool] = None,
    allow_disjoint_enterprises: Collection[str] = (),
    incomplete_sweep_mode: str = SWEEP_ARMED,
    generation_sweep_mode: Optional[str] = None,
) -> BootstrapResult:
    """Run the KB ingestion bootstrap from the shipped pack.

    Args:
        knowledge_service: The wired-up KnowledgeService (post-DI).
        db_session_factory: Async session factory (``get_db_session``).
        enterprise_id: The enterprise the pack's rows are isolated to
            (ADR-017 D1). Global scope drops the ORGANIZATION, never the
            enterprise — the column is NOT NULL and RLS keys on it.
        project_root: Override the project root (test injection).
        pack_dir: Explicit pack directory (test injection). When None, resolves
            from ``KB_PACK_DIR`` settings, falling back to the bundled baseline.
        sweep_dry_run: The authored-orphan sweep's dry-run override
            (``kb_seed --dry-run`` / ``--no-dry-run``); None defers to
            ``KB_ORPHAN_SWEEP_DRY_RUN``. Report-only unless
            ``KB_ORPHAN_SWEEP_ENABLED`` is set and this is not a dry run (see
            :mod:`faultmaven.bootstrap.kb_orphan_sweep`).
        allow_disjoint_enterprises: The buckets (enterprise ids, or
            ``unattributed``) the operator acknowledged (``kb_seed
            --allow-disjoint-enterprise``) as genuinely holding only orphans.
            Every other bucket none of whose authored vectors has a row is
            skipped.
        incomplete_sweep_mode: The stray-ingest sweep's mode
            (:mod:`faultmaven.bootstrap.kb_incomplete_sweep`): ``armed`` (the
            default, the web startup's) deletes; ``kb_seed`` passes the
            ``KB_INCOMPLETE_INGEST_SWEEP_*`` gate's answer, report-only until
            an operator arms it.
        generation_sweep_mode: The superseded-chunk sweep's mode
            (:mod:`faultmaven.bootstrap.kb_generation_sweep`). None (the web
            startup's and ``fm-reset-kb``'s) is
            :func:`web_startup_generation_mode`: ``armed`` on a local
            ``PersistentClient``, report-only under an external
            ``CHROMADB_URL``; ``kb_seed`` passes the
            ``KB_GENERATION_SWEEP_*`` gate's answer.

    Returns:
        BootstrapResult — per-runbook outcomes (counts + failure reasons).

    Failure handling:
        Individual runbook failures are logged and recorded but do NOT abort the
        bootstrap — one bad runbook shouldn't block the others.
    """
    from faultmaven.bootstrap.data_init import get_project_root
    from faultmaven.bootstrap.kb_pack import KbPack, resolve_pack_dir

    result = BootstrapResult()
    # ``sweep_incomplete_ingests`` reads no pack, so it is called before
    # ``KbPack.load``: a pack that does not load, and every later step of this
    # pass, leave it run (#2394).
    result.incomplete_ingests = await sweep_incomplete_ingests(
        knowledge_service, db_session_factory, mode=incomplete_sweep_mode
    )
    # Reads no pack either, and never touches a served generation (#2388).
    result.generation_sweep = await sweep_superseded_generations(
        knowledge_service,
        db_session_factory,
        mode=(
            web_startup_generation_mode()
            if generation_sweep_mode is None
            else generation_sweep_mode
        ),
    )
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

    # ONE capability answer for the whole pass (#1966). Under multi, kb_seed
    # runs as the maintenance role: INSERT and DELETE on knowledge_items,
    # deliberately no UPDATE. What this pass issues is shaped by the answer,
    # rather than issuing statements the role is refused: the pack-hash stamp
    # is skipped.
    role_may_update = await _role_may_update(db_session_factory)
    if not role_may_update:
        logger.debug(
            "KB bootstrap: this role may not UPDATE knowledge_items — pack-hash "
            "stamps are skipped this pass."
        )

    # Which generations the store holds, read once before the loop: a pack row
    # none of whose chunks carries its generation is re-ingested from the
    # pack even when its hash matches (#2388). None when the listing failed.
    listing = await _list_chunk_ids(knowledge_service)
    served = None if listing is None else _generations_by_parent(listing)
    for runbook in pack.runbooks:
        try:
            outcome = await _ingest_pack_runbook(
                runbook=runbook,
                knowledge_service=knowledge_service,
                db_session_factory=db_session_factory,
                enterprise_id=enterprise_id,
                role_may_update=role_may_update,
                served=served,
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
    result.pruned = await _prune_orphan_builtins(pack.item_ids, db_session_factory)

    # Reconcile the vector index against knowledge_items (the source of truth).
    # Catches drift that the row-keyed prune above is structurally blind to:
    # ChromaDB chunks whose built-in parent has no row (orphaned vectors, the
    # pruned rows' included) or an unpublished row (#1966) — both deleted once
    # older than the age floor — and published rows no chunk of their
    # generation serves (unserved rows — warned; an unpublished row has none
    # by design). Retrieval no longer depends on it: every KB read admits a
    # chunk only when its row is complete and published and carries its
    # generation (#2397, #2388). It removes what admission already hides.
    (
        result.orphaned_vectors_cleaned,
        result.unpublished_vectors_cleaned,
        unserved_rows,
    ) = await _reconcile_vectors(knowledge_service, db_session_factory)

    # The authored half of the same drift (#1982): an authored chunk whose row
    # is gone (a vector delete that failed, or a best-effort one after #2397)
    # is never served, but stays until this sweep removes it. Report-only
    # unless an operator arms it; deleted only where the row read is proven
    # unfiltered by RLS, per enterprise stamp, behind the reference-set check
    # and the cap — see kb_orphan_sweep.
    max_rows, max_chunks = _resolve_repair_bounds()
    result.authored_orphans = await sweep_authored_orphans(
        knowledge_service,
        db_session_factory,
        max_parents=max_rows,
        mode=resolve_sweep_mode(sweep_dry_run),
        allow_disjoint=allow_disjoint_enterprises,
    )

    # Cross-store repair: an unserved row (a complete row no chunk of its
    # generation serves — its vectors lost, or never written under the
    # generation migration 022 filled in) is silently non-retrievable.
    # Reconcile can only warn; here we re-chunk + re-embed a BOUNDED number of
    # them from the row as it stands. Bounded because repair loads the
    # embedding model the pack path deliberately skips. An unedited built-in
    # was re-ingested from the pack above, so what reaches the repair is an
    # authored row, an edited built-in (#1966), or a built-in whose re-ingest
    # failed. What it leaves is counted.
    result.repaired_rows, result.unserved_rows = await _repair_unserved_rows(
        unserved_rows,
        knowledge_service,
        max_rows=max_rows,
        max_chunks=max_chunks,
    )

    logger.info(
        f"KB bootstrap complete: {len(result.ingested)} ingested, "
        f"{len(result.skipped_unchanged)} unchanged, "
        f"{len(result.failed)} failed, "
        f"{len(result.pruned)} pruned, "
        f"{len(result.orphaned_vectors_cleaned)} orphaned vectors cleaned, "
        f"{len(result.unpublished_vectors_cleaned)} unpublished built-ins' vectors "
        "cleaned, "
        f"{len(result.authored_orphans.deleted)} authored orphans swept "
        f"({result.authored_orphans.mode}: "
        f"{len(result.authored_orphans.would_delete)} would be), "
        f"{len(result.incomplete_ingests.deleted)} incomplete ingests swept "
        f"({result.incomplete_ingests.mode}: "
        f"{len(result.incomplete_ingests.would_delete)} would be), "
        f"{result.generation_sweep.deleted} superseded chunks swept "
        f"({result.generation_sweep.mode}: "
        f"{result.generation_sweep.would_delete} would be), "
        f"{len(result.repaired_rows)} rows repaired, "
        f"{len(result.unserved_rows)} rows serving nothing"
    )
    return result


async def _ingest_pack_runbook(
    runbook: Any,  # kb_pack.PackRunbook
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    enterprise_id: str,
    *,
    role_may_update: bool,
    served: Optional[Mapping[str, Collection[Optional[str]]]] = None,
) -> str:
    """Ingest one pack runbook. Returns ``"ingested"`` or ``"skipped"``.

    Idempotent: skips when the row was last ingested from this pack version —
    the pack's ``content_hash`` equals the hash stamped on the row at ingest
    (``metadata.pack_content_hash``). The row's CURRENT content is not the
    comparison (#1966): an operator may have edited it, and an edit read as a
    pack change would delete and re-ingest the row, republishing a runbook
    that was unpublished. A row ingested before the stamp existed is compared
    by content once and, if unchanged, stamped — unless it is UNPUBLISHED,
    which is a retirement whatever its content says: it is taken as this pack
    version, stamped and skipped, so a runbook an operator edited and retired
    before the stamp existed is not republished by the first boot after the
    upgrade. The stamp is written only when the pass's role may UPDATE the
    table (``role_may_update``); under the ``kb_seed`` maintenance role it is
    skipped, and the content comparison stays exact there because a platform
    row cannot be edited or unpublished by a tenant session (RLS refuses).
    Writes the SQL row + the pack's pre-embedded chunks; never loads the
    embedding model.

    A complete, published row that ``served`` (each parent's generations in
    the store, read once per pass) holds no chunk of its generation for is
    re-ingested from the pack even when its stamped hash matches (#2388), if
    its stored content still hashes to the pack's: that is how a row whose
    chunks were lost, or every unedited row migration 022 gave a generation no
    chunk carries, is served again. An edited one is left to the repair, which
    re-embeds the row as it stands (#1966). ``served`` None (the listing
    failed) re-ingests on a hash change only. A re-ingest passes the observed row's
    generation as ``replaces_generation``, so the transaction that replaces
    it deletes only that row: a peer that replaced it first wins, and this
    ingest fails.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        PACK_CONTENT_HASH_KEY,
        VerificationLevel,
    )
    from faultmaven.utils.serialization import decode_json_blob

    # Chunk-integrity guard — validated FIRST, before any lookup or write: a
    # malformed pack update leaves the prior copy intact, isolated to
    # ``BootstrapResult.failed`` by the caller.
    #
    # (a) A runbook with no chunks can never be retrieved. It would otherwise
    #     pass the ordering check vacuously.
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

    replaces_generation: Optional[str] = None
    if existing_row is not None:
        replaces_generation = existing_row.vector_generation
        unserved = (
            served is not None
            and existing_row.is_published
            and existing_row.ingest_complete
            and replaces_generation not in served.get(runbook.item_id, ())
        )
        metadata = decode_json_blob(existing_row.knowledge_metadata) or {}
        stamped_hash = metadata.get(PACK_CONTENT_HASH_KEY)
        if stamped_hash is not None:
            pack_unchanged = stamped_hash == runbook.content_hash
        else:
            # Ingested before the stamp existed: the content is the only
            # evidence of which pack version it came from — except for an
            # unpublished row, whose retirement is the operator's decision and
            # outranks it (its content may be an edit). Compared once: the
            # stamp makes every later boot compare hashes.
            if not existing_row.is_published:
                pack_unchanged = True
            else:
                pack_unchanged = (
                    _content_hash(existing_row.content) == runbook.content_hash
                )
            if pack_unchanged and role_may_update:
                await _stamp_pack_hash(
                    runbook.item_id,
                    {**metadata, PACK_CONTENT_HASH_KEY: runbook.content_hash},
                    db_session_factory,
                )
        if pack_unchanged and not unserved:
            logger.debug(f"Skipping {runbook.relpath}: unchanged")
            return "skipped"
        if pack_unchanged and _content_hash(existing_row.content) != (
            runbook.content_hash
        ):
            # Unserved, but edited since the pack ingested it: re-ingesting
            # would replace the edit with the pack's text (#1966). The repair
            # re-embeds the row as it stands.
            logger.info(
                f"{runbook.relpath}: no chunk carries its row's generation, and "
                "the row was edited since the pack ingested it — left to the "
                "repair, which keeps the edit"
            )
            return "skipped"
        logger.info(
            f"{runbook.relpath}: "
            + (
                "no chunk carries its row's generation"
                if pack_unchanged
                else "the pack carries a new version since the last ingest"
            )
            + " — re-ingesting"
        )

    prechunked = [(c.text, c.embedding) for c in runbook.chunks]

    chunks_created = await knowledge_service.ingest_runbook(
        document_id=runbook.item_id,
        title=runbook.title,
        content=runbook.content,
        enterprise_id=enterprise_id,
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
        pack_content_hash=runbook.content_hash,
        # Vendored, reviewed pack source, not user or model text: its title is
        # stored as shipped, so a redaction outage that begins after a healthy
        # start cannot stop the platform corpus from seeding (#1981).
        shipped=True,
        replaces_generation=replaces_generation,
    )

    logger.info(
        f"Ingested {runbook.relpath} ({chunks_created} chunks, scope={runbook.scope})"
    )
    return "ingested"


def _content_hash(content: str) -> str:
    """The hash a pack runbook's ``content_hash`` is of."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _role_may_update(db_session_factory: Callable[[], Awaitable[Any]]) -> bool:
    """Whether this pass's role may UPDATE ``knowledge_items`` (asked once).

    A failed check answers False: the pass then issues no UPDATE and no
    locked read, which the role would be refused if it really lacked the
    privilege; the cost of a wrong False is only the plain re-read.
    """
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    try:
        async with db_session_factory() as session:
            return await DatabaseKnowledgeItemRepository(session).may_update()
    except Exception as exc:
        logger.warning(
            "KB bootstrap: could not read this role's privileges on "
            f"knowledge_items ({exc}); treating it as unable to UPDATE this pass."
        )
        return False


async def _stamp_pack_hash(
    item_id: str,
    metadata: dict,
    db_session_factory: Callable[[], Awaitable[Any]],
) -> None:
    """Record which pack version a legacy row came from (best effort).

    A targeted write of the metadata blob alone, leaving ``updated_at`` as it
    is: it must not revert a concurrent unpublish or edit, and it is
    bookkeeping, not an edit. A failure is logged; the next boot compares by
    content again, as it did before the stamp existed.
    """
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    try:
        async with db_session_factory() as session:
            await DatabaseKnowledgeItemRepository(session).update_fields(
                item_id, {"metadata": metadata}, touch=False
            )
    except Exception as exc:
        logger.warning(f"Could not stamp the pack content hash on {item_id}: {exc}")


# Built-in rows carry a deterministic ``kb_<12 hex>`` item_id (see
# ``utils.runbook_id.item_id_from_runbook_id``). User/authored items use random
# ids that never match this pattern, so the prune below is structurally
# incapable of touching authored content.
_BUILTIN_ITEM_ID_RE = re.compile(r"^kb_[0-9a-f]{12}$")


async def _prune_orphan_builtins(
    keep_ids: set,
    db_session_factory: Callable[[], Awaitable[Any]],
) -> list[str]:
    """Delete built-in knowledge_items rows not present in the pack.

    A built-in row is an orphan when its deterministic ``kb_<hash>`` id is not in
    ``keep_ids`` (the pack's item_ids) — typically because the runbook was
    removed from the pack or its frontmatter ``id`` changed. Authored/uuid items
    are out of scope: the id pattern can't match them. The row only: its
    vectors then have no row, and :func:`_reconcile_vectors` collects them
    (#2388). A row whose delete raises is logged and left for the next pass.

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
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    async with db_session_factory() as session:
        rows = await session.execute(select(KnowledgeItemModel.item_id))
        all_item_ids = [r[0] for r in rows.all()]

    pruned: list[str] = []
    for item_id in all_item_ids:
        if not _BUILTIN_ITEM_ID_RE.match(item_id) or item_id in keep_ids:
            continue
        try:
            async with db_session_factory() as session:
                deleted = await DatabaseKnowledgeItemRepository(session).delete(item_id)
        except Exception as exc:
            logger.warning(f"Failed to prune built-in knowledge_item {item_id}: {exc}")
            continue
        if deleted:
            pruned.append(item_id)
            logger.info(
                f"Pruned orphaned built-in knowledge_item {item_id} "
                f"(not in current pack)"
            )
    return pruned


async def _list_chunk_ids(knowledge_service: Any) -> Optional[Dict[str, List[str]]]:
    """The store's id-only listing, or None (logged) when it cannot be read."""
    store = getattr(knowledge_service, "_vector_store", None)
    if store is None:
        return None
    try:
        return await store.list_all_chunk_ids_by_parent()
    except Exception as exc:
        logger.warning(f"KB bootstrap: could not list vector chunks: {exc}")
        return None


def _generations_by_parent(
    chunk_ids_by_parent: Mapping[str, List[str]],
) -> Dict[str, Set[Optional[str]]]:
    """Each parent's generations, named by its chunk ids (None: pre-#2388)."""
    return {
        parent: {chunk_generation_from_id(chunk_id) for chunk_id in chunk_ids}
        for parent, chunk_ids in chunk_ids_by_parent.items()
    }


async def _read_rows(
    db_session_factory: Callable[[], Awaitable[Any]],
    item_ids: Optional[List[str]] = None,
) -> List[Any]:
    """``(item_id, is_published, ingest_complete, vector_generation)`` of every
    row, or of ``item_ids``."""
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    query = select(
        KnowledgeItemModel.item_id,
        KnowledgeItemModel.is_published,
        KnowledgeItemModel.ingest_complete,
        KnowledgeItemModel.vector_generation,
    )
    if item_ids is not None:
        query = query.where(KnowledgeItemModel.item_id.in_(item_ids))
    async with db_session_factory() as session:
        rows = (await session.execute(query)).all()
    wanted = None if item_ids is None else set(item_ids)
    return [row for row in rows if wanted is None or row[0] in wanted]


class ReconcileResult(NamedTuple):
    """What :func:`_reconcile_vectors` deleted and what it found unrepaired."""

    #: Built-in parents whose vectors were deleted because no row exists.
    orphaned_vectors_cleaned: list[str]
    #: Built-in parents whose vectors were deleted because the row is
    #: unpublished (#1966) — kept apart so the field above keeps its meaning.
    unpublished_vectors_cleaned: list[str]
    #: Complete, published rows no chunk of their generation serves (#2388).
    unserved_rows: list[str]


async def _reconcile_vectors(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    *,
    max_age: Optional[timedelta] = None,
    now: Optional[datetime] = None,
) -> ReconcileResult:
    """Reconcile the KB vector index against ``knowledge_items`` (source of truth).

    The prune above is keyed off DB rows, so it can only ever delete a row plus
    that row's vectors — it is structurally blind to vectors whose row is already
    gone. Those accumulate (re-ingest/delete paths that failed to clear vectors,
    older drift) and are invisible to every DB-side check. This pass closes the
    loop by comparing both sides:

      * **orphaned vectors** — a built-in ``parent_document_id`` present in
        ChromaDB with no ``knowledge_items`` row. Deleted, but only its chunks
        older than ``max_age`` (``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS``): a pack
        ingest writes its vectors before its row (#2388). The row is the source
        of truth for a shipped runbook, so a vector with no row can never be
        retrieved-then-resolved — and a single such ghost landing as the top KB
        hit silently kills a runbook-cause match.
      * **unpublished vectors** — a built-in parent whose row exists but is
        UNPUBLISHED (#1966). Deleted: every unpublish leaves them, because
        ``delete_document`` unpublishes a built-in with a SQL-only flip and no
        vector call (#2397). No KB door serves them meanwhile, since every
        door admits a chunk only when its row is complete and published; this
        pass is what removes them, older than the floor too: a re-ingest of a
        new pack version writes its vectors before it replaces the row.
      * **unserved rows** — a PUBLISHED, complete row no chunk of whose
        ``vector_generation`` exists, by the generations the chunk ids name
        (#2388; an incomplete row is an authored ingest in flight or failed,
        #2394, which the incomplete-ingest sweep owns). Can't be repaired
        here (no embedding model at startup), so it is WARNED, not touched, and
        returned for :func:`_repair_unserved_rows`. An unpublished row is never
        listed: having no vectors is its intended state. Listing it would hand
        it to repair, which would re-embed it and silently undo the unpublish
        at the next boot (#1966).

    **Scope — only deletes built-in ``kb_<12 hex>`` vectors.** Whether ``db_ids``
    is every enterprise's rows depends on who runs the pass, and this read does
    not ask: complete on SQLite, under ``kb_seed``'s BYPASSRLS maintenance role
    and as the table owner, but only the bound enterprise's rows (plus the
    global tier) under the RLS-enforced app role on PostgreSQL. The shared
    ``faultmaven_kb`` collection holds every enterprise's vectors, so diffing it
    against a partial row set would mark other tenants' runbooks as orphans and
    delete them. Restricting this pass to the built-in id class (the platform
    tier's deterministic ids, whose global rows every session reads) bounds it
    to platform-shipped runbooks; authored vectors (uuid or ``kb_<16 hex>``) are
    never touched here, unpublished or not. Their orphans are
    :mod:`faultmaven.bootstrap.kb_orphan_sweep`'s, which first proves its row
    read unfiltered and deletes per enterprise stamp (#1982).

    Safety: if the DB yields ZERO knowledge_items (a pathological all-rows-missing
    state, e.g. ingest fully failed this boot) we do NOT delete any vectors — that
    would wipe the pack's valid vectors on a transient row-write failure. We leave
    the index intact for the next boot to repair.

    **Order — the rows are read BEFORE the chunks are listed** (#2388). Each
    predicate stays safe in that order:

      * unserved: the generation a row carried at the read had its chunks in
        the store by then (every writer writes them before the commit that
        puts the generation on the row). They leave only through the writer
        that switches the row away from it and then deletes them, so each
        candidate's generation is read again after the listing, and a row
        whose generation changed is not reported.
      * orphaned and unpublished vectors: a built-in row inserted or replaced
        after the read wrote its vectors before its transaction, so they are
        younger than the age floor and spared; a chunk of a generation an
        unpublished row carried is never served again.
    """
    nothing = ReconcileResult([], [], [])
    vector_store: Optional[IKnowledgeVectorStore] = getattr(
        knowledge_service, "_vector_store", None
    )
    if vector_store is None:
        logger.debug("Reconcile skipped: no vector store wired.")
        return nothing

    rows = await _read_rows(db_session_factory)
    try:
        chunk_ids_by_parent = await vector_store.list_all_chunk_ids_by_parent()
    except Exception as exc:
        logger.warning(f"Reconcile skipped: could not list vector parents: {exc}")
        return nothing

    chroma_parents = set(chunk_ids_by_parent)
    # A row whose ingest never completed (#2394) has a row, so its vectors are
    # no orphans; it is neither repaired nor stripped of its vectors here,
    # which may be an ingest still writing them. The stray sweep owns it.
    db_ids = {row[0] for row in rows}
    generations = _generations_by_parent(chunk_ids_by_parent)
    read_as = {
        item_id: generation
        for item_id, published, complete, generation in rows
        if published and complete and generation not in generations.get(item_id, ())
    }
    if read_as:
        # Read again: a switch that committed since the first read (and may
        # have deleted the generation it replaced) is not an unserved row.
        again = await _read_rows(db_session_factory, list(read_as))
        unserved_rows = sorted(
            item_id
            for item_id, _published, _complete, generation in again
            if read_as.get(item_id) == generation
        )
    else:
        unserved_rows = []
    unpublished_ids = {
        item_id
        for item_id, published, complete, _gen in rows
        if complete and not published
    }

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
        return nothing

    for item_id in unserved_rows:
        logger.warning(
            f"KB consistency: no chunk carries knowledge_item {item_id}'s "
            "generation — it cannot be retrieved until it is re-ingested or "
            "repaired."
        )

    # Delete only BUILT-IN parents (see scope note) — never authored/other-tenant
    # vectors: those with no row, and those whose row is unpublished. One
    # batched delete instead of a round-trip per parent.
    orphaned_vectors = sorted(
        p for p in (chroma_parents - db_ids) if _BUILTIN_ITEM_ID_RE.match(p)
    )
    unpublished_with_vectors = sorted(
        p for p in (chroma_parents & unpublished_ids) if _BUILTIN_ITEM_ID_RE.match(p)
    )
    if not orphaned_vectors and not unpublished_with_vectors:
        return ReconcileResult([], [], unserved_rows)

    # Delete the chunk ids the listing OBSERVED, not "whatever carries these
    # parents' metadata": the listing derives parents from ids, so a chunk with
    # no ``parent_document_id`` key is listed and must be deleted by the same
    # key (#2017). A parent is reported only if the delete returned cleanly.
    # Only chunks older than the floor: a pack ingest's vectors precede its
    # row (#2388). An age that cannot be read deletes nothing.
    try:
        young = await young_chunk_ids(
            vector_store, resolve_max_age() if max_age is None else max_age, now
        )
    except Exception as exc:
        logger.warning(f"Reconcile: could not read chunk ages, deleting none: {exc}")
        return ReconcileResult([], [], unserved_rows)
    targeted = orphaned_vectors + unpublished_with_vectors
    spared = {p for p in targeted if young.intersection(chunk_ids_by_parent[p])}
    observed = [
        chunk_id
        for parent in targeted
        for chunk_id in chunk_ids_by_parent[parent]
        if chunk_id not in young
    ]
    try:
        deleted = (
            await vector_store.delete_observed_chunk_ids(observed) if observed else []
        )
    except Exception as exc:
        logger.warning(f"Reconcile: batched orphan delete failed: {exc}")
        return ReconcileResult([], [], unserved_rows)

    # Report only parents with no chunk left: one younger than the floor was
    # spared, and a chunk written for a parent between the listing and the
    # delete survives an id delete (#2017).
    orphaned_vectors = [p for p in orphaned_vectors if p not in spared]
    unpublished_with_vectors = [p for p in unpublished_with_vectors if p not in spared]
    if deleted:
        try:
            remaining = await vector_store.list_all_chunk_ids_by_parent()
        except Exception as exc:
            logger.warning(f"Reconcile: could not re-list after delete: {exc}")
            return ReconcileResult([], [], unserved_rows)
        survivors = {p for p in targeted if p not in spared and remaining.get(p)}
        for parent in sorted(survivors):
            logger.warning(
                f"KB consistency: built-in {parent} still has chunk(s) after the "
                "reconcile delete (written since the listing) — not reported "
                "cleaned; the next boot sees it again."
            )
        orphaned_vectors = [p for p in orphaned_vectors if p not in survivors]
        unpublished_with_vectors = [
            p for p in unpublished_with_vectors if p not in survivors
        ]
    logger.info(
        f"Reconcile: deleted {len(deleted)} vector chunk(s) across "
        f"{len(orphaned_vectors)} built-in parent(s) with no knowledge_items row "
        f"and {len(unpublished_with_vectors)} unpublished built-in parent(s)."
    )
    for item_id in unpublished_with_vectors:
        logger.warning(
            f"KB consistency: unpublished built-in {item_id} still had vectors — "
            "deleted, so the retired runbook is no longer retrievable."
        )
    return ReconcileResult(orphaned_vectors, unpublished_with_vectors, unserved_rows)


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


async def _repair_unserved_rows(
    unserved_rows: list[str],
    knowledge_service: Any,
    *,
    max_rows: int = KB_REPAIR_MAX_ROWS,
    max_chunks: int = KB_REPAIR_MAX_CHUNKS,
) -> tuple[list[str], list[str]]:
    """Re-embed unserved authored knowledge_items rows (a complete row, no
    chunk of its ``vector_generation``, #2388).

    Returns ``(repaired_ids, still_unserved_ids)``. Each repair re-chunks +
    re-embeds the row's persisted content with BGE-M3 under the row's own
    generation and writes ONLY the vector store
    (``KnowledgeService.reindex_missing_vectors``, which reads the row via
    the service's own RLS-scoped session) — the SQL row, the source of truth, is
    never touched.

    Cross-tenant-safe: repair only ADDS vectors for rows the caller's scoped
    session already sees, writing each row's OWN scope/owner metadata — it can
    never fabricate another tenant's vectors or mislabel scope. This holds on
    both entry paths (the single-tenant web-startup bootstrap and the
    cross-tenant ``kb_seed`` job seeding the org-free global tier). Contrast
    reconcile's DELETE side, restricted to built-in ids precisely because
    deletion CAN cross tenants (the authored sweep deletes only behind a proof
    that its row read is unfiltered); addition cannot, so every unserved PUBLISHED
    row is eligible — a lost authored runbook is repaired the same as a
    shipped one. A row whose authored ingest never completed is not unserved
    (#2394): reconcile does not list it, and ``reindex_missing_vectors``
    refuses it. An unpublished row is not unserved (it has no vectors by
    design, #1966): reconcile does not list it, and
    ``reindex_missing_vectors`` refuses it if handed one anyway.

    Bounded twice (``max_rows`` / ``max_chunks``, deployment-configurable):
    ``max_rows`` skips a bulk-loss anomaly entirely, and ``max_chunks`` caps the
    embedding work per boot (deferring the rest to the next boot) so startup
    readiness is never gated on an unbounded re-embed.
    """
    if not unserved_rows:
        return [], []

    if len(unserved_rows) > max_rows:
        logger.warning(
            "KB repair skipped: %d unserved rows exceed the repair cap (%d) — "
            "treating as a bulk-loss anomaly (e.g. the vector collection was wiped), "
            "not the crash recovery this pass handles. Re-embedding all of them on "
            "the boot path would reintroduce the on-pod embedding timeout. Rows left "
            "unretrievable; recover by re-provisioning the KB (fm-reset-kb, or the "
            "deployment wipe under multi), or raise KB_REPAIR_MAX_ROWS.",
            len(unserved_rows),
            max_rows,
        )
        return [], unserved_rows

    # Surface the EFFECTIVE bounds in the log whenever repair actually acts, so an
    # operator can confirm what a KB_REPAIR_MAX_* override resolved to (the values
    # are otherwise only visible in settings, not at runtime).
    logger.info(
        "KB repair: %d unserved row(s) within bounds (max_rows=%d, max_chunks=%d) "
        "— re-embedding with BGE-M3.",
        len(unserved_rows),
        max_rows,
        max_chunks,
    )

    repaired: list[str] = []
    still_unserved: list[str] = []
    chunks_embedded = 0
    for i, item_id in enumerate(unserved_rows):
        # Work budget: stop before starting a row once this boot's chunk budget is
        # spent. Overshoot is bounded by one runbook's chunk count (the row in
        # flight always finishes); the deferred rows self-heal on a later boot.
        if chunks_embedded >= max_chunks:
            deferred = unserved_rows[i:]
            still_unserved.extend(deferred)
            logger.warning(
                "KB repair: per-boot chunk budget (%d) reached after %d row(s) — "
                "deferring %d unserved row(s) to the next boot (repair is "
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
            still_unserved.append(item_id)
            continue
        if chunks > 0:
            repaired.append(item_id)
            chunks_embedded += chunks
            logger.info(
                f"KB repair: re-embedded {chunks} chunk(s) for unserved row {item_id}"
            )
        else:
            # 0 chunks = embedding model unavailable / empty content. Fail-safe:
            # leave the row for a later boot (mirrors reconcile's warn-only prior),
            # never raise on the startup path.
            still_unserved.append(item_id)
            logger.warning(
                f"KB repair produced 0 chunks for {item_id} (embedding model "
                "unavailable, or the row is gone, unpublished or incomplete — "
                "see the line before) — if the row is still live it stays "
                "unretrievable until a later boot retries it."
            )
    return repaired, still_unserved
