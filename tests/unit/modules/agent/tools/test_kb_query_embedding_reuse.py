"""One query, one embedding.

``hybrid_search`` embedded the query for vector recall and then
``_keyword_constrained_search`` re-embedded the SAME text once per keyword. The
vector is loop-invariant -- only ``where_document.$contains`` varies -- so every
repeat was an identical local model call: four BGE-M3 embeddings per lookup
where one suffices, measured at 1.2-2.3s each on CPU.

Asserted as the number of embed calls at the surface that would regress, not
against any constant.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)

pytestmark = [pytest.mark.unit]

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"


def _collection_with_hits(n: int = 5) -> MagicMock:
    collection = MagicMock()
    collection.query = MagicMock(
        return_value={
            "ids": [[f"id-{i}" for i in range(n)]],
            "documents": [[f"content-{i}" for i in range(n)]],
            "metadatas": [[{"title": f"Runbook {i}"} for i in range(n)]],
            "distances": [[0.30 + i * 0.01 for i in range(n)]],
        }
    )
    return collection


def _store() -> KnowledgeVectorStore:
    store = KnowledgeVectorStore(client=MagicMock())
    store._get_or_create_collection = MagicMock(return_value=_collection_with_hits())
    return store


@pytest.mark.asyncio
async def test_hybrid_search_embeds_the_query_once_regardless_of_keyword_count():
    """One query, one embedding -- however many keywords the sweep probes.

    The keyword arm probes up to three keywords, each a separate ChromaDB query
    but all constraining the SAME query vector. Embedding inside that loop made
    the dominant cost of a KB lookup scale with keyword count for no difference
    in result.
    """
    query = "CrashLoopBackOff on payment-svc after release OOMKilled exit 137"
    store = _store()

    # Guard against a vacuous pass: if keyword extraction yielded 0 or 1
    # keywords there would be nothing to repeat, and a single embed would prove
    # nothing about the loop.
    keywords = store._extract_search_keywords(query)
    assert len(keywords) >= 2, f"test needs a multi-keyword query, got {keywords}"

    embedder = AsyncMock(return_value=[0.1] * 8)
    with patch(_EMBEDDER, new=embedder):
        results = await store.hybrid_search(
            collection_name="faultmaven_kb",
            query=query,
            k=5,
            where={"scope": "global"},
        )

    assert results, "sanity: the sweep returned candidates"
    assert embedder.await_count == 1, (
        f"expected the query to be embedded exactly once, "
        f"got {embedder.await_count} embeddings for {len(keywords[:3])} keywords"
    )


@pytest.mark.asyncio
async def test_keyword_probes_all_reuse_the_single_query_vector():
    """Every keyword probe must carry the same vector the recall arm used.

    Guards the other half of the change: reusing one embedding is only correct
    if each probe still searches the query it was given, rather than silently
    querying with a stale or differing vector.
    """
    vector = [0.42] * 8
    store = _store()

    with patch(_EMBEDDER, new=AsyncMock(return_value=vector)):
        await store.hybrid_search(
            collection_name="faultmaven_kb",
            query="CrashLoopBackOff on payment-svc after release OOMKilled exit 137",
            k=5,
            where={"scope": "global"},
        )

    calls = store._get_or_create_collection.return_value.query.call_args_list
    assert len(calls) >= 2, "expected a recall query plus at least one keyword probe"
    for call in calls:
        assert call.kwargs["query_embeddings"] == [vector]
