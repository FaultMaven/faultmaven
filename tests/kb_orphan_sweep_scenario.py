"""One KB index + ``knowledge_items`` corpus for the authored-orphan sweep (#1982).

Shared by the SQLite tests (``tests/unit/bootstrap/test_kb_orphan_sweep.py``)
and the PostgreSQL ones
(``tests/integration/modules/knowledge/test_kb_orphan_sweep_postgres_1982.py``),
so both dialects judge the sweep on the same documents.

Three enterprises — the Standalone one (the platform tier's) and two tenants —
each with live authored documents and one authored orphan (chunks, no row).
Beside them:

- unstamped legacy chunks (written before #1168) of two LIVE documents, one
  whose row is the Standalone enterprise's and one whose row is a tenant's,
  plus one unstamped orphan;
- a live built-in and a built-in orphan, which only the built-in reconcile may
  touch.

The two unstamped live documents are what make a missing proof visible under
the app role bound to the Standalone enterprise: that session reads the
Standalone rows but not the tenant's, so the reference-set check passes (it
overlaps the Standalone one) and the tenant's live legacy chunks look orphaned.

Chunks are written straight into the collection, as the legacy data being
modelled was: ``KnowledgeVectorStore.add_documents`` refuses an unstamped chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID

ST = STANDALONE_ENTERPRISE_ID
E1 = "e1000000-0000-0000-0000-00000000e001"
E2 = "e2000000-0000-0000-0000-00000000e002"
ENTERPRISES = (ST, E1, E2)
COLLECTION = "faultmaven_kb"
DIM = 8


@dataclass(frozen=True)
class Doc:
    item_id: str
    #: The enterprise of its ``knowledge_items`` row; None = no row (an orphan).
    row: Optional[str]
    #: The enterprise stamp its chunks carry; None = unstamped (pre-#1168).
    stamp: Optional[str]
    scope: str = "personal"
    chunks: int = 2

    def chunk_ids(self) -> list[str]:
        return [f"{self.item_id}_chunk_{n}" for n in range(self.chunks)]


ST_LIVE_GLOBAL = Doc("5a000000-0000-4000-8000-0000000000a1", ST, ST, scope="global")
ST_LIVE_PERSONAL = Doc("5a000000-0000-4000-8000-0000000000a2", ST, ST)
E1_LIVE_1 = Doc("e1000000-0000-4000-8000-0000000000b1", E1, E1)
E1_LIVE_2 = Doc("kb_e1e1e1e1e1e1e1e1", E1, E1)
E2_LIVE = Doc("e2000000-0000-4000-8000-0000000000c1", E2, E2)
UNSTAMPED_LIVE_ST = Doc("0a000000-0000-4000-8000-0000000000d1", ST, None)
UNSTAMPED_LIVE_E1 = Doc("0a000000-0000-4000-8000-0000000000d2", E1, None)
BUILTIN_LIVE = Doc("kb_aaaaaaaaaaaa", ST, ST, scope="global")
BUILTIN_UNSTAMPED_LIVE = Doc("kb_bbbbbbbbbbbb", ST, None, scope="global")

ST_ORPHAN = Doc("0f000000-0000-4000-8000-0000000000a9", None, ST)
E1_ORPHAN = Doc("0f000000-0000-4000-8000-0000000000b9", None, E1)
E2_ORPHAN = Doc("kb_0f0f0f0f0f0f0fe2", None, E2)
UNSTAMPED_ORPHAN = Doc("0f000000-0000-4000-8000-0000000000d9", None, None)
BUILTIN_ORPHAN = Doc("kb_cccccccccccc", None, ST, scope="global")

LIVE = (
    ST_LIVE_GLOBAL,
    ST_LIVE_PERSONAL,
    E1_LIVE_1,
    E1_LIVE_2,
    E2_LIVE,
    UNSTAMPED_LIVE_ST,
    UNSTAMPED_LIVE_E1,
    BUILTIN_LIVE,
    BUILTIN_UNSTAMPED_LIVE,
)
AUTHORED_ORPHANS = {
    ST: ST_ORPHAN,
    E1: E1_ORPHAN,
    E2: E2_ORPHAN,
}
DOCS = LIVE + tuple(AUTHORED_ORPHANS.values()) + (UNSTAMPED_ORPHAN, BUILTIN_ORPHAN)


def add_chunks(client: Any, docs: Iterable[Doc]) -> None:
    """Write ``docs``' chunks straight into the KB collection."""
    ids, metadatas = [], []
    for doc in docs:
        for chunk_id in doc.chunk_ids():
            md = {"parent_document_id": doc.item_id, "scope": doc.scope}
            if doc.stamp is not None:
                md["enterprise_id"] = doc.stamp
            ids.append(chunk_id)
            metadatas.append(md)
    if not ids:
        return
    collection = client.get_or_create_collection(COLLECTION)
    collection.add(
        ids=ids,
        metadatas=metadatas,
        embeddings=[[0.1 * (1 + i % 7)] * DIM for i in range(len(ids))],
        documents=[f"chunk {i}" for i in ids],
    )


def chunk_ids_present(client: Any) -> set[str]:
    return set(client.get_or_create_collection(COLLECTION).get(include=[])["ids"])


def live_chunk_ids(docs: Iterable[Doc] = LIVE) -> set[str]:
    return {cid for doc in docs for cid in doc.chunk_ids()}


async def insert_enterprises(conn: Any, enterprise_ids: Iterable[str]) -> None:
    """Idempotent: a migrated database already holds the Standalone one."""
    for enterprise_id in enterprise_ids:
        present = (
            await conn.execute(
                text("SELECT 1 FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise_id},
            )
        ).first()
        if present is None:
            await conn.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:e, :e, :e)"
                ),
                {"e": enterprise_id},
            )


async def insert_rows(conn: Any, docs: Iterable[Doc]) -> None:
    """``knowledge_items`` rows for ``docs`` that have one."""
    for doc in docs:
        if doc.row is None:
            continue
        await conn.execute(
            text(
                "INSERT INTO knowledge_items (item_id, enterprise_id, scope, "
                "title, content, item_type, metadata) "
                "VALUES (:i, :e, :s, 't', 'body', 'runbook', '{}')"
            ),
            {"i": doc.item_id, "e": doc.row, "s": doc.scope},
        )


async def seed_database(conn: Any, docs: Iterable[Doc] = DOCS) -> None:
    await insert_enterprises(conn, ENTERPRISES)
    await insert_rows(conn, docs)
