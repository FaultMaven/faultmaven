"""Sweep authored KB vectors whose ``knowledge_items`` row is gone (#1982).

Retrieval never checks that a chunk's row exists, so an AUTHORED chunk left
behind by a vector delete that failed in the past (before #1966 ordered
``delete_document``) is retrievable for good: a global one by everyone, a
personal one by its owner. The reconcile in :mod:`faultmaven.bootstrap.kb_init`
deletes no-row vectors for built-in ``kb_<12 hex>`` parents only. This module is
the authored half.

**Report-only unless an operator arms it** — the ``storage_cleanup`` gate,
exactly: ``KB_ORPHAN_SWEEP_ENABLED`` (default false) and
``KB_ORPHAN_SWEEP_DRY_RUN`` (default true), the latter overridable per
``kb_seed`` run with ``--dry-run`` / ``--no-dry-run``. A dry run makes every
decision below and reports, per enterprise, the documents an armed run would
delete; it deletes nothing. Deletion needs ``ENABLED=true`` and a dry run
switched off. **Hard precondition for arming: one ChromaDB per database.** The
``faultmaven_kb`` collection is not deployment-scoped; a second database's
chunks in the same collection carry stamps this database also lists (every
database has the Standalone enterprise) or none, and its rows are invisible
here, so its live documents read as orphans no guard below can tell apart.

A chunk is deleted only when all of these hold:

(a) the chunk is AUTHORED (its parent is not a built-in id) and stamped with an
    enterprise E listed in ``enterprises``;
(b) its parent has no row in ANY enterprise, read in a transaction proven to be
    unfiltered by row-level security — SQLite, or PostgreSQL where BOTH
    guards hold in the SAME transaction as the read:
    ``NOT row_security_active('knowledge_items')``, and the read itself issued
    under ``SET LOCAL row_security = off``, which makes PostgreSQL REFUSE a
    read any policy would filter ("query would be affected by row-level
    security policy") instead of filtering it — pg_dump's mechanism. Either
    guard alone stops a partial read; an error from either is "unproven";
(c) E passes the reference-set check (``jobs.reference_set``): at least one of
    E's authored parents has a row in E, unless the operator acknowledged THAT
    enterprise by id (``kb_seed --allow-disjoint-enterprise <id>``);
(d) the run's total stays within ``KB_REPAIR_MAX_ROWS`` documents — all or
    nothing, decided before any delete.

A chunk stamped E whose parent has a row in another enterprise is a MIS-STAMPED
chunk of a live document, not an orphan: it is counted (``misstamped``) and
left in place — deleting it would leave a live document short a chunk that
repair, which re-embeds only documents with NO vectors, never restores.

An authored chunk whose stamp names no listed enterprise (unstamped, from
before #1168, or a stamp the table does not hold — an id-only listing cannot
tell them apart on chromadb 1.5.8, which has no ``$exists``) is the
``unattributed`` bucket: deleted under the same proof, check and cap when its
parent has no row in any enterprise, and acknowledged by its own name
(``--allow-disjoint-enterprise unattributed``). Nothing else is deleted. What an
unproven read, a disjoint enterprise, the cap or a failed delete leaves behind
is counted per enterprise and reported, never swept.

A wrongful delete is healable only partly: repair re-embeds a published row
with NO vectors, and only while the pass's orphaned rows stay within
``KB_REPAIR_MAX_ROWS`` and the embedding model is available. The cap keeps one
run's own deletes inside that bound; it does not make every wrongful delete
recoverable.

Why the proof is ``row_security_active`` and not the role's ``BYPASSRLS``
attribute: PostgreSQL also exempts the table OWNER (unless the table FORCEs row
security), and the owner reads every row with ``rolbypassrls = false``. A
session bound to one enterprise by the ``app.current_enterprise_id`` GUC is NOT
proven: a policy may filter more than the GUC suggests.

Ordering: the chunks are listed BEFORE the rows are read. Every create path
commits a document's row before it writes the document's vectors, so a chunk
the listing saw has a row the later read sees, unless the row was deleted —
which is what an orphan is. Reading the rows first would let a document created
between the two steps look orphaned.

No database session is held across a ChromaDB call: each read closes before
the next listing or delete (the maintenance role reaps a transaction left idle
for 60 s).

The enterprise-stamp backfill job (``faultmaven.jobs.kb_vector_enterprise_backfill``,
#1777) reads through the same functions: ``read_enterprise_ids``,
``read_rows_if_complete``, ``enterprise_where`` and ``unattributed_where``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Collection,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from sqlalchemy import select, text

from faultmaven.models.interfaces import IKnowledgeVectorStore
from faultmaven.utils.runbook_id import is_builtin_item_id

logger = logging.getLogger(__name__)

#: The sweep's three modes (the ``storage_cleanup`` gate; see the module).
SWEEP_OFF = "off"
SWEEP_REPORT_ONLY = "report_only"
SWEEP_ARMED = "armed"

#: The name of the bucket of authored chunks no listed enterprise's stamp names:
#: how a result reports it and how an acknowledgment names it.
UNATTRIBUTED = "unattributed"

#: The CLI spelling of the per-bucket acknowledgment (``kb_seed``).
ACKNOWLEDGE_FLAG = "--allow-disjoint-enterprise"

#: Guard 1 (#1982 A1): false exactly when no policy filters this transaction's
#: reads of the table.
_ROW_SECURITY_ACTIVE = "SELECT row_security_active(CAST(:table AS text))"
#: Guard 2 (#2018 review R1): with row security off, a read a policy WOULD
#: filter raises instead; superusers, BYPASSRLS roles and a non-FORCEd owner
#: read every row as before. Transaction-local, so it ends with the read.
_ROW_SECURITY_OFF = "SET LOCAL row_security = off"

#: Why a read was not proven complete.
UNPROVEN_FILTERED = "row_security_active"
UNPROVEN_READ_REFUSED = "read_refused_with_row_security_off"
UNPROVEN_CHECK_FAILED = "check_failed"
UNPROVEN_DIALECT = "unsupported_dialect"


def resolve_sweep_mode(dry_run: Optional[bool] = None) -> str:
    """The ``storage_cleanup`` gate, applied to this sweep.

    ``dry_run`` is a caller's override (``kb_seed --dry-run`` /
    ``--no-dry-run``); ``None`` defers to ``KB_ORPHAN_SWEEP_DRY_RUN``. An
    effective dry run is report-only whatever ``KB_ORPHAN_SWEEP_ENABLED``
    says; a run that is not a dry run deletes only when enabled, and is off
    otherwise — ``--no-dry-run`` asks for deletion but cannot grant it.
    Settings that cannot be read leave the sweep report-only.
    """
    return read_sweep_gate(
        "kb_orphan_sweep_enabled", "kb_orphan_sweep_dry_run", dry_run
    )


def read_sweep_gate(
    enabled_field: str, dry_run_field: str, dry_run: Optional[bool]
) -> str:
    """:func:`gate_sweep_mode` over the two named ``DatabaseSettings`` fields,
    ``dry_run`` the run's override; report-only when settings cannot be read.
    The one settings read of both KB sweeps' gates."""
    try:
        from faultmaven.config.settings import get_settings

        db = get_settings().database
        enabled = bool(getattr(db, enabled_field))
        configured_dry_run = bool(getattr(db, dry_run_field))
    except Exception as exc:  # settings unavailable in some test contexts
        logger.debug(f"KB sweep gate: settings unreadable ({exc})")
        return SWEEP_REPORT_ONLY
    return gate_sweep_mode(enabled, configured_dry_run, dry_run)


def gate_sweep_mode(
    enabled: bool, configured_dry_run: bool, dry_run: Optional[bool]
) -> str:
    """The gate itself: a run's ``dry_run`` override, else
    ``configured_dry_run``, makes it report-only; otherwise it deletes when
    ``enabled`` and is off when not."""
    if configured_dry_run if dry_run is None else dry_run:
        return SWEEP_REPORT_ONLY
    return SWEEP_ARMED if enabled else SWEEP_OFF


@dataclass
class SweepCounts:
    """One bucket's outcome. Every count is in parents (documents)."""

    #: Orphan parents an armed run deletes (this run's plan, after the cap).
    would_delete: List[str] = field(default_factory=list)
    #: Orphan parents whose observed chunks were deleted (armed runs only).
    deleted: List[str] = field(default_factory=list)
    #: Authored parents left unclassified: the row read is not proven complete.
    unproven: int = 0
    #: Authored parents left alone: none has a row (the reference-set check).
    disjoint: int = 0
    #: Orphan parents left alone: the run's total exceeded the cap.
    capped: int = 0
    #: Parents stamped here whose row is another enterprise's: left alone.
    misstamped: int = 0
    #: Orphan parents whose delete raised: left for the next pass.
    unremoved: int = 0

    def as_counts(self) -> Dict[str, int]:
        return {
            "would_delete": len(self.would_delete),
            "deleted": len(self.deleted),
            "unproven": self.unproven,
            "disjoint": self.disjoint,
            "capped": self.capped,
            "misstamped": self.misstamped,
            "unremoved": self.unremoved,
        }


_COUNT_KEYS = (
    "would_delete",
    "deleted",
    "unproven",
    "disjoint",
    "capped",
    "misstamped",
    "unremoved",
)


@dataclass
class AuthoredSweepResult:
    """What the authored-orphan sweep decided, deleted, and left and why."""

    #: ``off`` / ``report_only`` / ``armed``.
    mode: str = SWEEP_REPORT_ONLY
    #: True: the row read was proven unfiltered. False: it was not (nothing is
    #: swept). None: the sweep stopped before it reached the proof.
    proven: Optional[bool] = None
    #: Why the read was not proven, when it was not.
    unproven_reason: Optional[str] = None
    #: Enterprises from ``enterprises`` holding at least one authored chunk.
    enterprises: Dict[str, SweepCounts] = field(default_factory=dict)
    #: Authored chunks whose stamp names no listed enterprise.
    unattributed: SweepCounts = field(default_factory=SweepCounts)
    #: Every chunk (built-in included) whose stamp names no listed enterprise:
    #: the id-only ``$nin`` count — how much of the index no stamp attributes.
    unattributed_chunks: int = 0
    #: The total-deletion cap this run applied (``KB_REPAIR_MAX_ROWS``).
    cap: int = 0
    #: Why the sweep stopped early, when it did.
    skipped: Optional[str] = None

    def _buckets(self) -> Dict[str, SweepCounts]:
        return {**self.enterprises, UNATTRIBUTED: self.unattributed}

    @property
    def deleted(self) -> List[str]:
        """Every parent whose orphan chunks were deleted this run."""
        return sorted(p for c in self._buckets().values() for p in c.deleted)

    @property
    def would_delete(self) -> List[str]:
        """Every parent an armed run deletes, given this run's decisions."""
        return sorted(p for c in self._buckets().values() for p in c.would_delete)

    def totals(self) -> Dict[str, int]:
        buckets = self._buckets().values()
        return {key: sum(b.as_counts()[key] for b in buckets) for key in _COUNT_KEYS}

    def summary(self) -> Dict[str, Any]:
        """The job-result shape: totals, per-bucket counts where any is
        non-zero, and the documents an armed run deletes (bounded by the cap)."""
        return {
            "mode": self.mode,
            "proven": self.proven,
            "unproven_reason": self.unproven_reason,
            "skipped": self.skipped,
            "cap": self.cap,
            **self.totals(),
            "enterprises": {
                e: c.as_counts()
                for e, c in sorted(self.enterprises.items())
                if any(c.as_counts().values())
            },
            "unattributed": self.unattributed.as_counts(),
            "unattributed_chunks": self.unattributed_chunks,
            "would_delete_documents": {
                b: list(c.would_delete)
                for b, c in sorted(self._buckets().items())
                if c.would_delete
            },
        }


def _authored(by_parent: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Drop built-in parents: the built-in reconcile owns those."""
    return {p: ids for p, ids in by_parent.items() if not is_builtin_item_id(p)}


def enterprise_where(enterprise_id: str) -> Dict[str, Any]:
    return {"enterprise_id": enterprise_id}


def unattributed_where(enterprise_ids: List[str]) -> Dict[str, Any]:
    # ``$nin`` matches a chunk with no ``enterprise_id`` key AND one whose
    # value is outside the list (measured on chromadb 1.5.8 in-process and the
    # 1.0.10 server); there is no ``$exists``, so an id-only listing cannot
    # tell the two apart.
    return {"enterprise_id": {"$nin": enterprise_ids}}


async def read_enterprise_ids(
    db_session_factory: Callable[[], Awaitable[Any]],
) -> List[str]:
    """Every enterprise, live or retired (``enterprises`` carries no RLS)."""
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    async with db_session_factory() as session:
        rows = await session.execute(select(EnterpriseModel.enterprise_id))
        return sorted({r[0] for r in rows.all()})


async def read_rows_if_complete(
    db_session_factory: Callable[[], Awaitable[Any]],
) -> Tuple[bool, Dict[str, Set[str]], Optional[str]]:
    """``(proven, item ids by enterprise, why not proven)``.

    Both guards and the read share one transaction, so the answer is about the
    rows actually read. SQLite has no row-level security: always complete. An
    unproven read returns no rows: a partial view has no use here.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    query = select(KnowledgeItemModel.item_id, KnowledgeItemModel.enterprise_id)
    async with db_session_factory() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            fetched = (await session.execute(query)).all()
        elif dialect == "postgresql":
            try:
                active = (
                    await session.execute(
                        text(_ROW_SECURITY_ACTIVE),
                        {"table": KnowledgeItemModel.__table__.fullname},
                    )
                ).scalar()
            except Exception as exc:
                logger.warning(
                    "KB authored-orphan sweep: could not ask whether row-level "
                    f"security filters knowledge_items ({exc}); treating the "
                    "row read as unproven."
                )
                await session.rollback()
                return False, {}, UNPROVEN_CHECK_FAILED
            if active is not False:
                return False, {}, UNPROVEN_FILTERED
            try:
                await session.execute(text(_ROW_SECURITY_OFF))
                fetched = (await session.execute(query)).all()
            except Exception as exc:
                logger.warning(
                    "KB authored-orphan sweep: with row_security off the row "
                    f"read was refused ({exc}) — a policy would have filtered "
                    "it; treating it as unproven."
                )
                await session.rollback()
                return False, {}, UNPROVEN_READ_REFUSED
        else:
            return False, {}, UNPROVEN_DIALECT
        by_enterprise: Dict[str, Set[str]] = {}
        for item_id, enterprise_id in fetched:
            by_enterprise.setdefault(enterprise_id, set()).add(item_id)
        return True, by_enterprise, None


def _decide(
    *,
    bucket: str,
    candidates: Dict[str, List[str]],
    referenced: Set[str],
    all_ids: Set[str],
    counts: SweepCounts,
    dry_run: bool,
    acknowledged: Collection[str],
    authority: str,
) -> List[str]:
    """The orphan parents this bucket may delete, or [] with ``counts`` set."""
    from faultmaven.jobs.reference_set import assess_reference_set

    verdict = assess_reference_set(
        candidates=list(candidates),
        referenced=referenced,
        dry_run=dry_run,
        acknowledged=bucket in acknowledged,
        authority=authority,
        candidate_noun="authored KB document",
        acknowledge_flag=f"{ACKNOWLEDGE_FLAG} {bucket}",
    )
    if verdict.disjoint and bucket not in acknowledged:
        # Decided the same way in a dry run as in an armed one, so a dry run's
        # would-delete set is what the armed run deletes.
        counts.disjoint = len(candidates)
        logger.warning(f"KB authored-orphan sweep skipped {bucket}: {verdict.message}")
        return []
    if verdict.disjoint:
        logger.warning(f"KB authored-orphan sweep: {verdict.message}")
    orphans = sorted(set(candidates) - referenced)
    # A parent with a row in ANY enterprise is not an orphan, whatever its
    # chunks' stamp says (#2018 review R2): a mis-stamped chunk of a live
    # document, left in place.
    misstamped = [p for p in orphans if p in all_ids]
    orphans = [p for p in orphans if p not in all_ids]
    if misstamped:
        counts.misstamped = len(misstamped)
        logger.warning(
            f"KB authored-orphan sweep: {len(misstamped)} document(s) with a row "
            f"in another enterprise carry chunks stamped {bucket}; left in "
            f"place: {misstamped}"
        )
    return orphans


async def sweep_authored_orphans(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    *,
    max_parents: int,
    mode: str = SWEEP_REPORT_ONLY,
    allow_disjoint: Collection[str] = (),
) -> AuthoredSweepResult:
    """Decide which authored chunks provably have no row, and — when ``mode``
    is ``armed`` — delete them (see the module).

    ``allow_disjoint`` names the buckets (enterprise ids, or ``unattributed``)
    the operator acknowledged as disjoint for this run; every other disjoint
    bucket is skipped.

    Never raises: a failure anywhere before the deletes leaves everything in
    place and is logged; a failed delete is counted for its bucket.
    """
    result = AuthoredSweepResult(mode=mode, cap=max_parents)
    if mode == SWEEP_OFF:
        result.skipped = "disabled"
        logger.info(
            "KB authored-orphan sweep off: KB_ORPHAN_SWEEP_ENABLED is false and "
            "this run is not a dry run."
        )
        return result
    store: Optional[IKnowledgeVectorStore] = getattr(
        knowledge_service, "_vector_store", None
    )
    if store is None:
        result.skipped = "no_vector_store"
        return result
    try:
        await _sweep(
            store,
            db_session_factory,
            result,
            dry_run=mode != SWEEP_ARMED,
            acknowledged=frozenset(allow_disjoint),
        )
    except Exception as exc:
        logger.warning(f"KB authored-orphan sweep skipped: {exc}")
        result.skipped = "error"
    return result


async def _sweep(
    store: IKnowledgeVectorStore,
    db_session_factory: Callable[[], Awaitable[Any]],
    result: AuthoredSweepResult,
    *,
    dry_run: bool,
    acknowledged: frozenset,
) -> None:
    enterprise_ids = await read_enterprise_ids(db_session_factory)
    if not enterprise_ids:
        result.skipped = "no_enterprises"
        return
    unknown = sorted(acknowledged - set(enterprise_ids) - {UNATTRIBUTED})
    if unknown:
        logger.warning(
            f"KB authored-orphan sweep: {ACKNOWLEDGE_FLAG} names {unknown}, which "
            f"is no enterprise in this database (nor {UNATTRIBUTED}); ignored."
        )

    # 1. Chunks FIRST (ids only), per enterprise stamp and for no listed stamp.
    stamped: Dict[str, Dict[str, List[str]]] = {}
    for enterprise_id in enterprise_ids:
        found = _authored(
            await store.list_chunk_ids_by_parent(enterprise_where(enterprise_id))
        )
        if found:
            stamped[enterprise_id] = found
    unattributed_all = await store.list_chunk_ids_by_parent(
        unattributed_where(enterprise_ids)
    )
    result.unattributed_chunks = sum(len(ids) for ids in unattributed_all.values())
    unattributed = _authored(unattributed_all)
    if not stamped and not unattributed:
        return

    # 2. THEN the rows, with both guards in the same transaction.
    proven, rows, why_not = await read_rows_if_complete(db_session_factory)
    result.proven = proven
    result.unproven_reason = why_not
    for enterprise_id in stamped:
        result.enterprises[enterprise_id] = SweepCounts()
    if not proven:
        for enterprise_id, found in stamped.items():
            result.enterprises[enterprise_id].unproven = len(found)
        result.unattributed.unproven = len(unattributed)
        logger.warning(
            f"KB authored-orphan sweep: the row read is not proven complete "
            f"({why_not}) — row-level security filters this role's reads of "
            "knowledge_items, so no authored vector can be proven orphaned: "
            f"{sum(len(f) for f in stamped.values())} authored document(s) "
            f"across {len(stamped)} enterprise(s) and {len(unattributed)} "
            "unattributed left unclassified. A pass whose reads are unfiltered "
            "classifies them: kb_seed under the maintenance role (multi), or "
            "as the table owner."
        )
        return
    all_ids = set().union(*rows.values()) if rows else set()
    if not all_ids:
        logger.warning(
            "KB authored-orphan sweep skipped: knowledge_items is empty while "
            "the index holds authored chunks — an anomaly, not a signal to "
            "delete them."
        )
        result.skipped = "no_rows"
        return

    # 3. Decide per enterprise, then for the unattributed bucket.
    plan: Dict[str, List[str]] = {}
    for enterprise_id, found in stamped.items():
        orphans = _decide(
            bucket=enterprise_id,
            candidates=found,
            referenced=rows.get(enterprise_id, set()),
            all_ids=all_ids,
            counts=result.enterprises[enterprise_id],
            dry_run=dry_run,
            acknowledged=acknowledged,
            authority=f"enterprise {enterprise_id}'s knowledge_items rows",
        )
        if orphans:
            plan[enterprise_id] = orphans
    if unattributed:
        orphans = _decide(
            bucket=UNATTRIBUTED,
            candidates=unattributed,
            referenced=all_ids,
            all_ids=all_ids,
            counts=result.unattributed,
            dry_run=dry_run,
            acknowledged=acknowledged,
            authority="the knowledge_items rows of every enterprise",
        )
        if orphans:
            plan[UNATTRIBUTED] = orphans

    def counts_of(bucket: str) -> SweepCounts:
        return (
            result.unattributed
            if bucket == UNATTRIBUTED
            else result.enterprises[bucket]
        )

    def chunks_of(bucket: str, parent: str) -> List[str]:
        return (unattributed if bucket == UNATTRIBUTED else stamped[bucket])[parent]

    # 4. The cap: all or nothing, before any delete.
    total = sum(len(o) for o in plan.values())
    if total > result.cap:
        for bucket, orphans in plan.items():
            counts_of(bucket).capped = len(orphans)
        logger.error(
            f"KB authored-orphan sweep: {total} authored document(s) have vectors "
            f"and no row, above the cap of {result.cap} (KB_REPAIR_MAX_ROWS) — "
            f"{'an armed run would delete' if dry_run else 'deleting'} NONE. A "
            "mass of no-row vectors is an anomaly to investigate (a database "
            "or index restored to a different point in time, or a ChromaDB "
            "shared with another database), not drift to sweep; raise "
            "KB_REPAIR_MAX_ROWS for one run once confirmed."
        )
        return

    for bucket, orphans in plan.items():
        counts_of(bucket).would_delete = list(orphans)
    if dry_run:
        if plan:
            logger.warning(
                "KB authored-orphan sweep [report-only]: an armed run would "
                f"delete {total} authored document(s) with no knowledge_items "
                f"row: {plan}. Nothing was deleted."
            )
        return

    # 5. Delete exactly what was observed, scoped to the stamp it was observed
    #    under. No database session is open here.
    for bucket, orphans in plan.items():
        where = (
            unattributed_where(enterprise_ids)
            if bucket == UNATTRIBUTED
            else enterprise_where(bucket)
        )
        await _delete(
            store,
            [cid for p in orphans for cid in chunks_of(bucket, p)],
            where,
            counts_of(bucket),
            len(orphans),
            label=bucket,
        )


async def _delete(
    store: IKnowledgeVectorStore,
    chunk_ids: List[str],
    where: Dict[str, Any],
    counts: SweepCounts,
    n_parents: int,
    *,
    label: str,
) -> None:
    try:
        deleted = await store.delete_chunk_ids(chunk_ids, where)
    except Exception as exc:
        counts.unremoved = n_parents
        logger.warning(
            f"KB authored-orphan sweep: deleting {n_parents} orphan document(s) "
            f"stamped {label} raised ({exc}); they stay until the next pass."
        )
        return
    counts.deleted = sorted({cid.rsplit("_chunk_", 1)[0] for cid in deleted})
    if counts.deleted:
        logger.info(
            f"KB authored-orphan sweep: deleted {len(deleted)} chunk(s) of "
            f"{len(counts.deleted)} authored document(s) stamped {label} with no "
            f"knowledge_items row: {counts.deleted}"
        )
