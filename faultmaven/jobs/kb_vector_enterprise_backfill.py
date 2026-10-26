"""KB vector enterprise backfill (#1777): stamp each KB chunk with its row's enterprise.

Chunks written before #1780 carry no ``enterprise_id``, or one that disagrees
with their ``knowledge_items`` row. This job rewrites them and changes no read
path. A dry run unless ``--no-dry-run``:

    python -m faultmaven.jobs.run kb_vector_enterprise_backfill
    python -m faultmaven.jobs.run kb_vector_enterprise_backfill --no-dry-run

It reads through the authored-orphan sweep's functions
(``faultmaven/bootstrap/kb_orphan_sweep.py``), in the sweep's order: chunks
FIRST, ids only, once per enterprise stamp and once for chunks no enterprise's
stamp names; THEN the ``(item_id, enterprise_id)`` rows of every enterprise, in
a read proven unfiltered by row-level security. An unproven read writes
nothing and fails the run. Each listed chunk, by its parent's row enterprise R:

- stamped R: ``already_correct``;
- stamped another enterprise: ``mismatched``, rewritten to R;
- no enterprise's stamp, parent has a row: ``unstamped``, stamped R;
- no enterprise's stamp, parent has no row: ``unattributable``, left alone;
- stamped an enterprise, parent has no row: the orphan sweep's; not counted.

Residuals: a chunk stamped with an id no enterprise holds counts as
``unstamped`` and is rewritten. An id-only listing cannot tell it from a
missing key: chromadb 1.5.8 has no ``$exists``, and ``$nin`` matches both.
Orphans are never written.

Each write is ``restamp_chunk_ids``, one parent document at a time: a metadata
merge (``collection.update``) onto the observed ids that still carry the stamp
they were listed under, as of an id-only ``get`` (``update`` takes no
``where``). A stamp written between the two is overwritten with the row's
enterprise, which is what every writer stamps. Never ``add``, which is
insert-or-ignore on an existing id. ``stamped`` / ``corrected`` count the ids
read back as carrying the row's enterprise after the write. The same behaviour
was measured on chromadb 1.5.8 in-process and on the 1.0.10 server Cloud runs
(a local ``chromadb/chroma:1.0.10`` driven by the 1.5.8 ``HttpClient``,
2026-10-26): ``update`` merges, ``$nin`` matches a missing key,
``get(ids, where)`` is the intersection, ``update`` on a missing id is a no-op.

Tenant scope: **cross_tenant** (every enterprise's rows; a collection no
enterprise partitions). Under multi it runs only on the audited maintenance
path; see ``faultmaven.jobs.run`` and docs/operations/evidence-job-scheduling.md.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)

#: The report's counts. ``items`` is rows; every other count is chunks.
_COUNTS = (
    "items",
    "chunks",
    "already_correct",
    "unstamped",
    "mismatched",
    "stamped",
    "corrected",
    "unattributable",
    "failed",
)


async def run(
    settings: FaultMavenSettings,
    container: Any,
    dry_run: Optional[bool] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Stamp every KB chunk whose parent has a row with that row's enterprise.

    ``dry_run``: only ``False`` (``--no-dry-run``) writes; None is a dry run.

    Returns ``job``, ``status`` ("completed"; "failed" when the row read is not
    proven or a write raised; "skipped" with ``reason`` when there is no
    vector store or no enterprise, and every count None: nothing was
    examined), ``dry_run``, ``proven``, ``reason``, and: ``items`` (rows
    read); ``chunks`` (listed chunks whose parent has a row);
    ``already_correct`` / ``unstamped`` / ``mismatched`` (those chunks, before
    any write; ``unstamped`` includes a stamp no enterprise holds);
    ``stamped`` / ``corrected`` (chunks carrying the row's enterprise after
    this run's writes, 0 on a dry run); ``unattributable`` (no enterprise's
    stamp and no row; never written); ``failed`` (chunks of documents whose
    write raised, some of which may have landed; the next run counts them).
    """
    effective_dry_run = dry_run is not False
    result: Dict[str, Any] = {
        "job": JOB_NAME,
        "status": "completed",
        "dry_run": effective_dry_run,
        "proven": None,
        **{key: 0 for key in _COUNTS},
    }
    store = getattr(container, "knowledge_vector_store", None)
    if store is None:
        _skip(result, "no_vector_store")
        return result

    from faultmaven.infrastructure.persistence.database import get_db_session

    try:
        await _backfill(store, get_db_session, result, dry_run=effective_dry_run)
    except Exception as exc:
        logger.error(f"KB vector enterprise backfill failed: {exc}", exc_info=True)
        result.update(status="failed", error=str(exc))
    return result


async def _backfill(
    store: Any, db_session_factory: Any, result: Dict[str, Any], *, dry_run: bool
) -> None:
    from faultmaven.bootstrap.kb_orphan_sweep import (
        enterprise_where,
        read_enterprise_ids,
        read_rows_if_complete,
        unattributed_where,
    )

    enterprises = await read_enterprise_ids(db_session_factory)
    if not enterprises:
        # ``$nin`` refuses an empty list, and no row exists without one.
        _skip(result, "no_enterprises")
        return

    # 1. Chunks FIRST (ids only): per enterprise stamp, then no known stamp.
    wheres = [(e, enterprise_where(e)) for e in enterprises]
    wheres.append((None, unattributed_where(enterprises)))
    listings = [(e, w, await store.list_chunk_ids_by_parent(w)) for e, w in wheres]

    # 2. THEN the rows, proven unfiltered.
    proven, rows, why_not = await read_rows_if_complete(db_session_factory)
    result["proven"] = proven
    if not proven:
        result.update(status="failed", reason=why_not)
        logger.error(
            f"KB vector enterprise backfill: the row read is not proven "
            f"unfiltered ({why_not}); nothing was written. Run it under the "
            "maintenance role (multi) or as the table owner."
        )
        return
    row_of = {item: e for e, items in rows.items() for item in items}
    result["items"] = len(row_of)

    # 3. Classify.
    writes: List[Tuple[str, str, List[str], Dict[str, Any], str]] = []
    for stamp, where, by_parent in listings:
        for parent, ids in sorted(by_parent.items()):
            row = row_of.get(parent)
            if row is None:
                if stamp is None:
                    result["unattributable"] += len(ids)
                continue  # stamped, no row: the orphan sweep's
            result["chunks"] += len(ids)
            if stamp == row:
                result["already_correct"] += len(ids)
                continue
            result["unstamped" if stamp is None else "mismatched"] += len(ids)
            outcome = "stamped" if stamp is None else "corrected"
            writes.append((parent, row, ids, where, outcome))

    # 4. Write, unless a dry run: one parent document at a time.
    for parent, row, ids, where, outcome in [] if dry_run else writes:
        try:
            result[outcome] += len(await store.restamp_chunk_ids(ids, row, where))
        except Exception as exc:
            result["failed"] += len(ids)
            logger.warning(
                f"KB vector enterprise backfill: restamping {len(ids)} chunk(s) "
                f"of {parent} with {row} raised ({exc}); left for the next run."
            )
    if result["failed"]:
        result["status"] = "failed"
    counts = " ".join(f"{key}={result[key]}" for key in _COUNTS)
    logger.info(f"KB vector enterprise backfill (dry_run={dry_run}): {counts}")


def _skip(result: Dict[str, Any], reason: str) -> None:
    """Nothing was examined: no count may read as a pass."""
    result.update({key: None for key in _COUNTS}, status="skipped", reason=reason)


# Job metadata for CLI discovery
JOB_NAME = "kb_vector_enterprise_backfill"
JOB_DESCRIPTION = (
    "Stamp each KB vector chunk with its knowledge_items row's enterprise_id "
    "(dry run unless --no-dry-run)"
)
# Reads every enterprise's knowledge_items rows: under the multi-tenant
# provider the runner allows it only on the audited maintenance path.
JOB_TENANT_SCOPE = "cross_tenant"
