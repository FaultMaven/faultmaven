"""Sweep authored KB vectors whose ``knowledge_items`` row is gone (#1982).

Retrieval never checks that a chunk's row exists, so an AUTHORED chunk left
behind by a vector delete that failed in the past (before #1966 ordered
``delete_document``) is retrievable for good: a global one by everyone, a
personal one by its owner. The reconcile in :mod:`faultmaven.bootstrap.kb_init`
deletes no-row vectors for built-in ``kb_<12 hex>`` parents only. This module is
the authored half, and it deletes a chunk only when all of these hold:

(a) the chunk is AUTHORED (its parent is not a built-in id) and stamped with an
    enterprise E listed in ``enterprises``;
(b) its parent has no row among E's ``knowledge_items`` rows, read in a
    transaction proven to be unfiltered by row-level security — SQLite, or
    PostgreSQL where ``NOT row_security_active('knowledge_items')`` holds in
    the SAME transaction as the read;
(c) E passes the reference-set check (``jobs.reference_set``): at least one of
    E's authored parents has a row, unless the operator acknowledged a
    disjoint set (``--allow-disjoint-reference-set`` on ``kb_seed``);
(d) the run's total stays within ``kb_repair_max_rows`` parents — all or
    nothing, decided before any delete, so a wrongful delete stays within what
    repair re-embeds at the next pass.

An authored chunk whose stamp names no listed enterprise (unstamped, from
before #1168, or a stamp the table does not hold) is deleted only under the
same proof — which covers every enterprise at once — when its parent has no row
in ANY enterprise, under the same check and cap. Nothing else is deleted. What
an unproven read, a disjoint enterprise or the cap leaves behind is counted per
enterprise and reported, never swept.

Why the proof is ``row_security_active`` and not the role's ``BYPASSRLS``
attribute: PostgreSQL also exempts the table OWNER (unless the table FORCEs row
security), and the owner reads every row with ``rolbypassrls = false``. The
function answers the question the sweep asks — is this read filtered — for the
superuser, the owner, a ``BYPASSRLS`` role and a ``FORCE``d table alike. A
session bound to one enterprise by the ``app.current_enterprise_id`` GUC is NOT
proven: a policy may filter more than the GUC suggests, so that posture is
reported, not swept.

Ordering: the chunks are listed BEFORE the rows are read. Every create path
commits a document's row before it writes the document's vectors, so a chunk
the listing saw has a row the later read sees, unless the row was deleted —
which is what an orphan is. Reading the rows first would let a document created
between the two steps look orphaned.

No database session is held across a ChromaDB call: each read closes before
the next listing or delete (the maintenance role reaps a transaction left idle
for 60 s).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import select, text

from faultmaven.utils.runbook_id import is_builtin_item_id

logger = logging.getLogger(__name__)

#: The completeness proof (#1982 A1). False exactly when no policy filters this
#: transaction's reads of the table.
_ROW_SECURITY_ACTIVE = "SELECT row_security_active(CAST(:table AS text))"


@dataclass
class SweepCounts:
    """One enterprise's outcome. Every count is in parents (documents)."""

    #: Orphan parents whose observed chunks were deleted.
    deleted: List[str] = field(default_factory=list)
    #: Authored parents left unclassified: the row read is RLS-filtered.
    unproven: int = 0
    #: Authored parents left alone: none has a row (the reference-set check).
    disjoint: int = 0
    #: Orphan parents left alone: the run's total exceeded the cap.
    capped: int = 0
    #: Orphan parents whose delete raised.
    failed: int = 0

    def as_counts(self) -> Dict[str, int]:
        return {
            "deleted": len(self.deleted),
            "unproven": self.unproven,
            "disjoint": self.disjoint,
            "capped": self.capped,
            "failed": self.failed,
        }


@dataclass
class AuthoredSweepResult:
    """What the authored-orphan sweep deleted, and what it left and why."""

    #: True: the row read was proven unfiltered. False: it was not (nothing is
    #: swept). None: the sweep stopped before it reached the proof.
    proven: Optional[bool] = None
    #: Enterprises from ``enterprises`` holding at least one authored chunk.
    enterprises: Dict[str, SweepCounts] = field(default_factory=dict)
    #: Authored chunks whose stamp names no listed enterprise.
    unattributed: SweepCounts = field(default_factory=SweepCounts)
    #: Every chunk (built-in included) whose stamp names no listed enterprise:
    #: the id-only ``$nin`` count — how much of the index no stamp attributes.
    unattributed_chunks: int = 0
    #: The total-deletion cap this run applied (``kb_repair_max_rows``).
    cap: int = 0
    #: Why the sweep stopped early, when it did.
    skipped: Optional[str] = None

    @property
    def deleted(self) -> List[str]:
        """Every parent whose orphan chunks were deleted this run."""
        found = [p for c in self.enterprises.values() for p in c.deleted]
        return sorted(found + list(self.unattributed.deleted))

    def summary(self) -> Dict[str, Any]:
        """The job-result shape: totals, plus per-enterprise counts where any
        is non-zero (an enterprise with nothing to report is left out)."""
        buckets = list(self.enterprises.values()) + [self.unattributed]
        totals = {
            key: sum(b.as_counts()[key] for b in buckets)
            for key in ("deleted", "unproven", "disjoint", "capped", "failed")
        }
        return {
            "proven": self.proven,
            "skipped": self.skipped,
            "cap": self.cap,
            **totals,
            "enterprises": {
                e: c.as_counts()
                for e, c in sorted(self.enterprises.items())
                if any(c.as_counts().values())
            },
            "unattributed": self.unattributed.as_counts(),
            "unattributed_chunks": self.unattributed_chunks,
        }


def _authored(by_parent: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Drop built-in parents: the built-in reconcile owns those."""
    return {p: ids for p, ids in by_parent.items() if not is_builtin_item_id(p)}


def _enterprise_where(enterprise_id: str) -> Dict[str, Any]:
    return {"enterprise_id": enterprise_id}


def _unattributed_where(enterprise_ids: List[str]) -> Dict[str, Any]:
    # ``$nin`` matches a chunk with no ``enterprise_id`` key AND one whose
    # value is outside the list (measured on chromadb 1.5.8); the store has no
    # ``$exists``, so an id-only listing cannot tell the two apart.
    return {"enterprise_id": {"$nin": enterprise_ids}}


async def _read_enterprise_ids(
    db_session_factory: Callable[[], Awaitable[Any]],
) -> List[str]:
    """Every enterprise, live or retired (``enterprises`` carries no RLS)."""
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    async with db_session_factory() as session:
        rows = await session.execute(select(EnterpriseModel.enterprise_id))
        return sorted({r[0] for r in rows.all()})


async def _read_rows_if_complete(
    db_session_factory: Callable[[], Awaitable[Any]],
) -> Tuple[bool, Dict[str, Set[str]]]:
    """``(proven, item ids by enterprise)`` — the proof and the read share a
    transaction, so the answer is about the rows actually read.

    SQLite has no row-level security: always complete. On PostgreSQL the read
    is complete when ``row_security_active`` is false for this transaction. A
    check that fails is unproven, and an unproven read returns no rows: there
    is nothing a partial view may be used for here.
    """
    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    async with db_session_factory() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            proven = True
        elif dialect == "postgresql":
            try:
                active = (
                    await session.execute(
                        text(_ROW_SECURITY_ACTIVE),
                        {"table": KnowledgeItemModel.__table__.fullname},
                    )
                ).scalar()
                proven = active is False
            except Exception as exc:
                logger.warning(
                    "KB authored-orphan sweep: could not ask whether row-level "
                    f"security filters knowledge_items ({exc}); treating the "
                    "row read as unproven."
                )
                await session.rollback()
                proven = False
        else:
            proven = False
        if not proven:
            return False, {}
        rows = await session.execute(
            select(KnowledgeItemModel.item_id, KnowledgeItemModel.enterprise_id)
        )
        by_enterprise: Dict[str, Set[str]] = {}
        for item_id, enterprise_id in rows.all():
            by_enterprise.setdefault(enterprise_id, set()).add(item_id)
        return True, by_enterprise


def _decide(
    *,
    candidates: Dict[str, List[str]],
    referenced: Set[str],
    counts: SweepCounts,
    allow_disjoint_reference_set: bool,
    authority: str,
) -> List[str]:
    """The orphan parents this bucket may delete, or [] with ``counts`` set."""
    from faultmaven.jobs.reference_set import assess_reference_set

    verdict = assess_reference_set(
        candidates=list(candidates),
        referenced=referenced,
        dry_run=False,
        acknowledged=allow_disjoint_reference_set,
        authority=authority,
        candidate_noun="authored KB document",
    )
    if verdict.refuse:
        counts.disjoint = len(candidates)
        logger.warning(f"KB authored-orphan sweep skipped: {verdict.message}")
        return []
    if verdict.disjoint:
        logger.warning(f"KB authored-orphan sweep: {verdict.message}")
    return sorted(set(candidates) - referenced)


async def sweep_authored_orphans(
    knowledge_service: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    *,
    max_parents: int,
    allow_disjoint_reference_set: bool = False,
) -> AuthoredSweepResult:
    """Delete authored chunks whose parent provably has no row (see module).

    Never raises: a failure anywhere before the deletes leaves everything in
    place and is logged; a failed delete is counted for its bucket.
    """
    result = AuthoredSweepResult(cap=max_parents)
    store = getattr(knowledge_service, "_vector_store", None)
    if store is None:
        result.skipped = "no_vector_store"
        return result
    if not (
        hasattr(store, "list_chunk_ids_by_parent")
        and hasattr(store, "delete_chunk_ids")
    ):
        logger.warning(
            "KB authored-orphan sweep skipped: wired vector store %s cannot list "
            "chunk ids by stamp — authored vectors with no row are NOT swept.",
            type(store).__name__,
        )
        result.skipped = "store_cannot_enumerate"
        return result
    try:
        await _sweep(
            store,
            db_session_factory,
            result,
            allow_disjoint_reference_set=allow_disjoint_reference_set,
        )
    except Exception as exc:
        logger.warning(f"KB authored-orphan sweep skipped: {exc}")
        result.skipped = "error"
    return result


async def _sweep(
    store: Any,
    db_session_factory: Callable[[], Awaitable[Any]],
    result: AuthoredSweepResult,
    *,
    allow_disjoint_reference_set: bool,
) -> None:
    enterprise_ids = await _read_enterprise_ids(db_session_factory)
    if not enterprise_ids:
        result.skipped = "no_enterprises"
        return

    # 1. Chunks FIRST (ids only), per enterprise stamp and for no listed stamp.
    stamped: Dict[str, Dict[str, List[str]]] = {}
    for enterprise_id in enterprise_ids:
        found = _authored(
            await store.list_chunk_ids_by_parent(_enterprise_where(enterprise_id))
        )
        if found:
            stamped[enterprise_id] = found
    unattributed_all = await store.list_chunk_ids_by_parent(
        _unattributed_where(enterprise_ids)
    )
    result.unattributed_chunks = sum(len(ids) for ids in unattributed_all.values())
    unattributed = _authored(unattributed_all)
    if not stamped and not unattributed:
        return

    # 2. THEN the rows, with the proof in the same transaction.
    proven, rows = await _read_rows_if_complete(db_session_factory)
    result.proven = proven
    for enterprise_id in stamped:
        result.enterprises[enterprise_id] = SweepCounts()
    if not proven:
        for enterprise_id, found in stamped.items():
            result.enterprises[enterprise_id].unproven = len(found)
        result.unattributed.unproven = len(unattributed)
        logger.warning(
            "KB authored-orphan sweep: this role's reads of knowledge_items are "
            "filtered by row-level security, so no authored vector can be "
            f"proven orphaned — {sum(len(f) for f in stamped.values())} authored "
            f"document(s) across {len(stamped)} enterprise(s) and "
            f"{len(unattributed)} unattributed left unswept. A pass whose reads "
            "are unfiltered sweeps them: kb_seed under the maintenance role "
            "(multi), or as the table owner."
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
            candidates=found,
            referenced=rows.get(enterprise_id, set()),
            counts=result.enterprises[enterprise_id],
            allow_disjoint_reference_set=allow_disjoint_reference_set,
            authority=f"enterprise {enterprise_id}'s knowledge_items rows",
        )
        if orphans:
            plan[enterprise_id] = orphans
    unattributed_orphans: List[str] = []
    if unattributed:
        unattributed_orphans = _decide(
            candidates=unattributed,
            referenced=all_ids,
            counts=result.unattributed,
            allow_disjoint_reference_set=allow_disjoint_reference_set,
            authority="the knowledge_items rows of every enterprise",
        )

    # 4. The cap: all or nothing, before any delete.
    total = sum(len(o) for o in plan.values()) + len(unattributed_orphans)
    if total > result.cap:
        for enterprise_id, orphans in plan.items():
            result.enterprises[enterprise_id].capped = len(orphans)
        result.unattributed.capped = len(unattributed_orphans)
        logger.error(
            f"KB authored-orphan sweep: {total} authored document(s) have vectors "
            f"and no row, above the cap of {result.cap} (KB_REPAIR_MAX_ROWS) — "
            "deleting NONE. A mass of no-row vectors is an anomaly to investigate "
            "(a restored or re-provisioned database beside an older index), not "
            "drift to sweep; raise KB_REPAIR_MAX_ROWS for one run once confirmed."
        )
        return

    # 5. Delete exactly what was observed, scoped to the stamp it was observed
    #    under. No database session is open here.
    for enterprise_id, orphans in plan.items():
        await _delete(
            store,
            [cid for p in orphans for cid in stamped[enterprise_id][p]],
            _enterprise_where(enterprise_id),
            result.enterprises[enterprise_id],
            len(orphans),
            label=f"enterprise {enterprise_id}",
        )
    if unattributed_orphans:
        await _delete(
            store,
            [cid for p in unattributed_orphans for cid in unattributed[p]],
            _unattributed_where(enterprise_ids),
            result.unattributed,
            len(unattributed_orphans),
            label="no listed enterprise",
        )


async def _delete(
    store: Any,
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
        counts.failed = n_parents
        logger.warning(
            f"KB authored-orphan sweep: deleting {n_parents} orphan document(s) "
            f"stamped {label} failed ({exc}); they stay until the next pass."
        )
        return
    counts.deleted = sorted({cid.rsplit("_chunk_", 1)[0] for cid in deleted})
    if counts.deleted:
        logger.info(
            f"KB authored-orphan sweep: deleted {len(deleted)} chunk(s) of "
            f"{len(counts.deleted)} authored document(s) stamped {label} with no "
            f"knowledge_items row: {counts.deleted}"
        )
