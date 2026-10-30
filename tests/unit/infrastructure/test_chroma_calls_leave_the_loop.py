"""Every ChromaDB call every vector store makes runs on the thread its arm uses (#2056).

On the ``HttpClient`` arm a store's ChromaDB calls run in the ``chroma-http``
executor, off the event loop. On the local ``PersistentClient`` arm they stay
on the loop thread, as they always have: that arm's latency is local work,
outside #2056's scope. The stores choose by setting ``blocking_executor``. ``call_external`` sends a SYNC ``call_func``
through it, so a wrapper left ``async def``, or a ChromaDB call made outside a
wrapper, would run on the loop thread on both arms. That is what this table
catches.

A recording fake client and collection note the thread every chromadb method
runs on. Each public method of each store is called with its
``blocking_executor`` set to ``CHROMA_EXECUTOR``, then to ``None``:

- with the executor, every recorded call must be on a ``chroma-http`` thread;
- with none, every recorded call must be on the loop thread;
- and every method must record at least one call, because a method that
  records none pins nothing.
"""

from __future__ import annotations

import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.infrastructure.chroma_client import CHROMA_EXECUTOR
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from tests.utils import admit_every_parent

pytestmark = [pytest.mark.unit]

_EMBEDDING = [0.1, 0.2, 0.3]

Calls = List[Tuple[str, threading.Thread]]


class _RecordingCollection:
    """A chromadb collection that records the thread of every call."""

    def __init__(self, name: str, calls: Calls) -> None:
        self.name = name
        self._calls = calls

    def _record(self, method: str) -> None:
        self._calls.append((f"collection.{method}", threading.current_thread()))

    def add(self, **kwargs: Any) -> None:
        self._record("add")

    def update(self, **kwargs: Any) -> None:
        self._record("update")

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        self._record("query")
        return {
            "ids": [["parent-1_chunk_0"]],
            "documents": [["ENOSPC: no space left on device, disk full"]],
            "metadatas": [[{"scope": "global", "parent_document_id": "parent-1"}]],
            "distances": [[0.4]],
        }

    def get(self, **kwargs: Any) -> Dict[str, Any]:
        self._record("get")
        return {
            "ids": ["parent-1_chunk_0", "parent-2_chunk_0"],
            "documents": [
                "ENOSPC: no space left on device, disk full",
                "connection refused by the upstream service",
            ],
            "metadatas": [{"scope": "global"}, {"scope": "global"}],
        }

    def count(self) -> int:
        self._record("count")
        return 2

    def delete(self, **kwargs: Any) -> None:
        self._record("delete")


class _RecordingClient:
    """A chromadb client that records the thread of every call."""

    def __init__(self, calls: Calls) -> None:
        self._calls = calls

    def _record(self, method: str) -> None:
        self._calls.append((f"client.{method}", threading.current_thread()))

    def get_or_create_collection(self, name: str, **kwargs: Any) -> Any:
        self._record("get_or_create_collection")
        return _RecordingCollection(name, self._calls)

    def get_collection(self, name: str, **kwargs: Any) -> Any:
        self._record("get_collection")
        return _RecordingCollection(name, self._calls)

    def list_collections(self, **kwargs: Any) -> List[Any]:
        self._record("list_collections")
        return [
            _RecordingCollection("case_orphaned", self._calls),
            _RecordingCollection("faultmaven_kb", self._calls),
        ]

    def delete_collection(self, name: str, **kwargs: Any) -> None:
        self._record("delete_collection")


StoreCall = Callable[[Any], Awaitable[Any]]

_KB_WHERE = {"scope": "global"}
_STAMP = {"enterprise_id": "ent-1"}

# (store factory, method, how to call it). Every public method of every store.
_TABLE: List[Tuple[str, Callable[[Any], Any], StoreCall]] = [
    # -- CaseVectorStore ------------------------------------------------------
    (
        "case.add_documents",
        CaseVectorStore,
        lambda s: s.add_documents(
            "c1",
            [{"id": "d1", "content": "text", "metadata": {"k": "v"}}],
            embeddings=[_EMBEDDING],
        ),
    ),
    ("case.search", CaseVectorStore, lambda s: s.search(case_id="c1", query="disk")),
    (
        "case.delete_case_collection",
        CaseVectorStore,
        lambda s: s.delete_case_collection("c1"),
    ),
    ("case.erase_case", CaseVectorStore, lambda s: s.erase_case("c1", "ent-1")),
    (
        "case.list_case_collection_ids",
        CaseVectorStore,
        lambda s: s.list_case_collection_ids(),
    ),
    (
        "case.cleanup_orphaned_collections",
        CaseVectorStore,
        lambda s: s.cleanup_orphaned_collections(
            active_case_ids=[], case_exists=AsyncMock(return_value=False)
        ),
    ),
    (
        "case.get_case_document_count",
        CaseVectorStore,
        lambda s: s.get_case_document_count("c1"),
    ),
    # -- KnowledgeVectorStore -------------------------------------------------
    (
        "kb.search",
        KnowledgeVectorStore,
        lambda s: s.search(
            KB_COLLECTION,
            "disk full",
            where=_KB_WHERE,
            query_embedding=_EMBEDDING,
            admit=admit_every_parent,
        ),
    ),
    (
        "kb.hybrid_search",
        KnowledgeVectorStore,
        lambda s: s.hybrid_search(
            KB_COLLECTION, "ENOSPC disk full", where=_KB_WHERE, admit=admit_every_parent
        ),
    ),
    (
        "kb.add_documents",
        KnowledgeVectorStore,
        lambda s: s.add_documents(
            [{"id": "parent-1_chunk_0", "content": "text", "metadata": dict(_STAMP)}],
            embeddings=[_EMBEDDING],
        ),
    ),
    (
        "kb.get_document_count",
        KnowledgeVectorStore,
        lambda s: s.get_document_count(KB_COLLECTION),
    ),
    (
        "kb.delete_documents_by_parent_id",
        KnowledgeVectorStore,
        lambda s: s.delete_documents_by_parent_id("parent-1"),
    ),
    (
        "kb.delete_generation",
        KnowledgeVectorStore,
        lambda s: s.delete_generation("parent-1", "generation-1"),
    ),
    (
        "kb.list_all_chunk_ids_by_parent",
        KnowledgeVectorStore,
        lambda s: s.list_all_chunk_ids_by_parent(),
    ),
    (
        "kb.list_chunk_ids_by_parent",
        KnowledgeVectorStore,
        lambda s: s.list_chunk_ids_by_parent(where=dict(_STAMP)),
    ),
    (
        "kb.delete_chunk_ids",
        KnowledgeVectorStore,
        lambda s: s.delete_chunk_ids(["parent-1_chunk_0"], where=dict(_STAMP)),
    ),
    (
        "kb.delete_observed_chunk_ids",
        KnowledgeVectorStore,
        lambda s: s.delete_observed_chunk_ids(["parent-1_chunk_0"]),
    ),
    (
        "kb.restamp_chunk_ids",
        KnowledgeVectorStore,
        lambda s: s.restamp_chunk_ids(
            ["parent-1_chunk_0"], "ent-1", where={"enterprise_id": {"$nin": ["e"]}}
        ),
    ),
    # -- ChromaDBVectorStore --------------------------------------------------
    ("chromadb.search", ChromaDBVectorStore, lambda s: s.search("disk full")),
    (
        "chromadb.query_by_embedding",
        ChromaDBVectorStore,
        lambda s: s.query_by_embedding(_EMBEDDING, where=_KB_WHERE),
    ),
    ("chromadb.count", ChromaDBVectorStore, lambda s: s.count()),
]

_PUBLIC_METHODS = {
    CaseVectorStore: {
        "add_documents",
        "search",
        "delete_case_collection",
        "erase_case",
        "list_case_collection_ids",
        "cleanup_orphaned_collections",
        "get_case_document_count",
    },
    KnowledgeVectorStore: {
        "search",
        "hybrid_search",
        "add_documents",
        "get_document_count",
        "delete_documents_by_parent_id",
        "delete_generation",
        "list_all_chunk_ids_by_parent",
        "list_chunk_ids_by_parent",
        "delete_chunk_ids",
        "delete_observed_chunk_ids",
        "restamp_chunk_ids",
    },
    # No document writes: the KB collection's one writer in this codebase is
    # KnowledgeVectorStore (#1782).
    ChromaDBVectorStore: {
        "search",
        "query_by_embedding",
        "count",
    },
}


def test_the_table_covers_every_public_coroutine_method() -> None:
    """A public method added to a store without a row here would go unpinned."""
    import inspect

    for store_type, listed in _PUBLIC_METHODS.items():
        own = {
            name
            for name, member in vars(store_type).items()
            if not name.startswith("_") and inspect.iscoroutinefunction(member)
        }
        assert own == listed, store_type.__name__
    rows = {(factory, row_id.split(".", 1)[1]) for row_id, factory, _ in _TABLE}
    expected = {(t, m) for t, methods in _PUBLIC_METHODS.items() for m in methods}
    assert rows == expected


def _make_store(factory: Callable[[Any], Any], calls: Calls) -> Any:
    client = _RecordingClient(calls)
    if factory is ChromaDBVectorStore:
        store = ChromaDBVectorStore(client=client, collection_name=KB_COLLECTION)
    else:
        store = factory(client)
    # The fake is not chromadb's HttpClient, so the store chose the loop.
    assert store.blocking_executor is None
    return store


async def _call(store: Any, invoke: StoreCall) -> None:
    embed = AsyncMock(return_value=_EMBEDDING)
    with (
        patch(
            "faultmaven.infrastructure.persistence.case_vector_store."
            "embed_query_or_raise",
            new=embed,
        ),
        patch(
            "faultmaven.infrastructure.persistence.chromadb_store."
            "embed_query_or_raise",
            new=embed,
        ),
        patch(
            "faultmaven.infrastructure.knowledge.knowledge_vector_store."
            "embed_query_or_raise",
            new=embed,
        ),
    ):
        await invoke(store)


@pytest.mark.parametrize(
    "factory,invoke", [row[1:] for row in _TABLE], ids=[row[0] for row in _TABLE]
)
async def test_on_the_http_arm_every_chroma_call_runs_in_chroma_http(
    factory: Callable[[Any], Any], invoke: StoreCall
) -> None:
    calls: Calls = []
    store = _make_store(factory, calls)
    store.blocking_executor = CHROMA_EXECUTOR
    calls.clear()  # construction (boot) is out of scope

    await _call(store, invoke)

    assert calls, "the method made no ChromaDB call, so this row pins nothing"
    on_loop = [m for m, t in calls if not t.name.startswith("chroma-http")]
    assert not on_loop, f"ran on {threading.current_thread().name}: {on_loop}"


@pytest.mark.parametrize(
    "factory,invoke", [row[1:] for row in _TABLE], ids=[row[0] for row in _TABLE]
)
async def test_on_the_local_arm_every_chroma_call_stays_on_the_loop(
    factory: Callable[[Any], Any], invoke: StoreCall
) -> None:
    calls: Calls = []
    store = _make_store(factory, calls)
    calls.clear()
    loop_thread: Optional[threading.Thread] = threading.current_thread()

    await _call(store, invoke)

    assert calls, "the method made no ChromaDB call, so this row pins nothing"
    elsewhere = [(m, t.name) for m, t in calls if t is not loop_thread]
    assert not elsewhere, f"left the loop thread: {elsewhere}"
