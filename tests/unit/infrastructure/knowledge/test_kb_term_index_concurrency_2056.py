"""The KB term index under concurrency (#2056).

On the ``HttpClient`` arm the index build runs in a worker thread, so it can
overlap writes and other builds; on the loop thread it never could. Three
properties keep that safe:

- a build that spans a write does not keep its index (the generation check),
  so a same-size replace, the edited-runbook re-ingest, cannot leave a stale
  index behind;
- concurrent cache misses share one build (single-flight);
- the build runs after the vector search, so a search that raises (the
  circuit breaker open included) spends no worker on it.

Driven by ``threading.Event`` barriers, never by a clock (#1579): the fake
collection holds the build's document read until the test lets it go.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.base_client import CircuitBreakerError
from faultmaven.infrastructure.chroma_client import CHROMA_EXECUTOR
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)

pytestmark = [pytest.mark.unit]

_DOCUMENTS = [
    "ENOSPC: no space left on device, disk full",
    "connection refused by the upstream service",
]


class _Collection:
    """A KB collection whose FIRST index build is held at its document read."""

    def __init__(self) -> None:
        self.counts = 0
        self.builds = 0
        self.build_reading = threading.Event()
        self.let_build_finish = threading.Event()

    def count(self) -> int:
        self.counts += 1
        # Constant: every write in these tests is a same-size replace, the
        # case the size signature cannot see.
        return len(_DOCUMENTS)

    def get(self, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("include") == ["documents"]:
            self.builds += 1
            if self.builds == 1:
                self.build_reading.set()
                self.let_build_finish.wait(10)
            return {"ids": ["p1_chunk_0", "p2_chunk_0"], "documents": _DOCUMENTS}
        return {"ids": ["p1_chunk_0"]}

    def add(self, **kwargs: Any) -> None:
        pass

    def delete(self, **kwargs: Any) -> None:
        pass


class _Client:
    def __init__(self, collection: _Collection) -> None:
        self._collection = collection

    def get_or_create_collection(self, name: str, **kwargs: Any) -> _Collection:
        return self._collection

    def get_collection(self, name: str, **kwargs: Any) -> _Collection:
        return self._collection


def _http_arm_store(collection: _Collection) -> KnowledgeVectorStore:
    store = KnowledgeVectorStore(_Client(collection))
    store.blocking_executor = CHROMA_EXECUTOR
    return store


async def _wait_for(event: threading.Event) -> bool:
    return await asyncio.get_running_loop().run_in_executor(None, event.wait, 10)


async def test_a_build_that_spans_a_same_size_replace_is_not_kept() -> None:
    collection = _Collection()
    store = _http_arm_store(collection)

    build = asyncio.create_task(store._term_stats_for(KB_COLLECTION))
    assert await _wait_for(collection.build_reading)

    # The replace lands while the build is reading the pre-replace documents.
    await store.delete_documents_by_parent_id("p1")
    documents: List[Dict[str, Any]] = [
        {
            "id": "p1_chunk_0",
            "content": "edited",
            "metadata": {"enterprise_id": "ent-1"},
        }
    ]
    await store.add_documents(documents, embeddings=[[0.1]])
    collection.let_build_finish.set()

    stats = await build

    assert stats is not None, "the waiting search is still served"
    assert KB_COLLECTION not in store._term_stats, "a stale index was kept"

    rebuilt = await store._term_stats_for(KB_COLLECTION)

    assert collection.builds == 2, "the next lookup did not rebuild"
    assert store._term_stats[KB_COLLECTION] is rebuilt


async def test_concurrent_misses_share_one_build() -> None:
    collection = _Collection()
    store = _http_arm_store(collection)

    first = asyncio.create_task(store._term_stats_for(KB_COLLECTION))
    assert await _wait_for(collection.build_reading)
    second = asyncio.create_task(store._term_stats_for(KB_COLLECTION))
    # One step for ``second``: far enough to join the lookup in flight, or,
    # without single-flight, to start its own.
    await asyncio.sleep(0)
    collection.let_build_finish.set()

    a, b = await asyncio.gather(first, second)

    assert a is b
    assert collection.counts == 1, "the second miss ran a lookup of its own"
    assert collection.builds == 1


async def test_a_search_that_raises_spends_nothing_on_the_index() -> None:
    store = KnowledgeVectorStore(MagicMock())
    store._embed_query_or_raise = AsyncMock(return_value=[0.1])
    store.search = AsyncMock(
        side_effect=CircuitBreakerError(
            "Circuit breaker is open for KnowledgeVectorStore",
            service="KnowledgeVectorStore",
        )
    )
    store._corpus_term_stats = MagicMock()

    with pytest.raises(CircuitBreakerError):
        await store.hybrid_search(
            KB_COLLECTION, "ENOSPC disk full", where={"scope": "global"}
        )

    store._corpus_term_stats.assert_not_called()
