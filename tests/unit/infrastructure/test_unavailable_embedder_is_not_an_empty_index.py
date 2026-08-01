"""An unavailable embedder is not an empty index (#941, extends #943).

#943 fixed this shape at ``KnowledgeVectorStore``. The remaining retrieval
paths kept their own, differently-wrong answers to "BGE-M3 would not load":

  * ``ChromaDBVectorStore.search``  -> logged an error and returned ``[]``
  * ``CaseVectorStore.search``      -> silently re-issued the query with
                                       ChromaDB's default embedding

Both reach the investigating model as a *finding*. ``[]`` is rendered as "the
knowledge base holds nothing"; the second is worse than a wrong answer, because
the query lands in a different vector space than the stored evidence — so the
collection either rejects it on dimension or answers from a space nothing was
indexed in, and either way the model is told something about the case that no
search established.

These tests assert two things the store alone cannot demonstrate:

1. **the property, over every store** — not one instance of it. A new vector
   store that quietly degrades to ``[]`` fails the sweep below.
2. **what the model is actually told** — the case-evidence chain is driven end
   to end (store -> ``AnswerFromCaseEvidence`` -> ``CaseEvidenceQAAdapter``),
   because a store-level raise that a handler four frames up converts back into
   an answer string has fixed nothing. That is exactly how #868's first attempt
   merged inert.

And, because a guard that can only fail is not a guard: the gate must still
PASS for a case that was genuinely searched and is genuinely empty.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.case_evidence_qa import AnswerFromCaseEvidence
from faultmaven.modules.agent.tools.kb_tool_adapter import CaseEvidenceQAAdapter

pytestmark = [pytest.mark.unit]

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"

# Claims no retrieval layer is entitled to make about a search that never ran.
FABRICATED_CAUSES = [
    "no vectorized evidence",
    "nothing has been uploaded",
    "still being indexed",
    "was searched successfully and is empty",
]


def _collection(matches: bool = False) -> MagicMock:
    """A ChromaDB collection that answers a real query, with or without hits."""
    collection = MagicMock()
    collection.query.return_value = (
        {
            "ids": [["ev1_chunk_0"]],
            "documents": [["OutOfMemoryError in worker-3"]],
            "metadatas": [[{"filename": "app.log"}]],
            "distances": [[0.1]],
        }
        if matches
        else {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    )
    return collection


def _client(collection: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get_or_create_collection.return_value = collection
    return client


# ---------------------------------------------------------------------------
# The property: NO retrieval path degrades an unavailable embedder into a result
# ---------------------------------------------------------------------------
#
# Parameterised over every vector store that embeds a query, so this is a
# statement about the layer rather than about three call sites that happen to
# be correct today.

_STORES = [
    pytest.param(
        lambda client: KnowledgeVectorStore(client=client),
        lambda store: store.search(
            collection_name="faultmaven_kb",
            query="how do I drain a node?",
            where={"scope": "global"},
        ),
        id="knowledge_vector_store",
    ),
    pytest.param(
        lambda client: ChromaDBVectorStore(client=client),
        lambda store: store.search(query="how do I drain a node?"),
        id="chromadb_vector_store",
    ),
    pytest.param(
        lambda client: CaseVectorStore(client=client),
        lambda store: store.search(
            case_id="case-1", query="what errors are in app.log?"
        ),
        id="case_vector_store",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store,search", _STORES)
async def test_no_store_reports_an_unavailable_embedder_as_a_result(make_store, search):
    collection = _collection()
    store = make_store(_client(collection))

    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await search(store)

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store,search", _STORES)
async def test_no_store_queries_in_a_second_embedding_space(make_store, search):
    """The failure mode that is not ``[]``.

    ``CaseVectorStore`` used to fall back to ``query_texts=[...]``, which hands
    the text to ChromaDB's own embedding model. Stored vectors are BGE-M3
    (1024-dim), so that query is not a degraded version of the right question —
    it is a different question, asked of a space the evidence was never indexed
    in. No store may issue a query at all once the embedder is gone.
    """
    collection = _collection(matches=True)
    store = make_store(_client(collection))

    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError):
            await search(store)

    collection.query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store,search", _STORES)
async def test_an_embedder_fault_is_not_charged_to_chromadbs_circuit_breaker(
    make_store, search
):
    """Embedding is hoisted OUT of ``call_external``.

    Retrying a model that cannot load in seconds spends ChromaDB's whole retry
    budget and trips its circuit breaker for a fault ChromaDB did not cause —
    which then denies healthy KB queries for the breaker timeout, turning one
    unavailable embedder into a broader outage.
    """
    store = make_store(_client(_collection()))

    with patch.object(BaseExternalClient, "call_external") as call_external:
        with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
            with pytest.raises(KnowledgeBaseError):
                await search(store)

    call_external.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store,search", _STORES)
async def test_a_hanging_embedder_fails_closed_instead_of_hanging_the_turn(
    make_store, search
):
    """Outside ``call_external`` also means outside its timeout, so the hoist
    has to carry its own bound — otherwise a cold BGE-M3 load hangs the tool
    call all the way to the outer turn budget."""
    import asyncio

    from faultmaven.infrastructure import embedding_guard

    async def _never_returns(_text):
        await asyncio.sleep(60)

    store = make_store(_client(_collection()))

    with patch.object(embedding_guard, "EMBED_TIMEOUT_SECONDS", 0.05):
        with patch(_EMBEDDER, new=_never_returns):
            with pytest.raises(KnowledgeBaseError) as excinfo:
                await search(store)

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store,search", _STORES)
async def test_every_store_states_its_own_subject(make_store, search):
    """One shared guard, but not one shared claim.

    The message names the search that failed. A store that borrowed another's
    wording is how case-evidence text ended up answering knowledge-base
    queries (#943) — the shared implementation must not re-introduce that by
    hard-coding a single subject.
    """
    store = make_store(_client(_collection()))

    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await search(store)

    message = str(excinfo.value).lower()
    subject = (
        "case evidence" if isinstance(store, CaseVectorStore) else "knowledge base"
    )
    assert subject in message, f"{type(store).__name__} did not name what failed"


# ---------------------------------------------------------------------------
# What the model is told, over the live case-evidence chain
# ---------------------------------------------------------------------------


def _evidence_adapter(store: CaseVectorStore) -> CaseEvidenceQAAdapter:
    tool = AnswerFromCaseEvidence.__new__(AnswerFromCaseEvidence)
    from faultmaven.modules.agent.tools.kb_configs.case_evidence_config import (
        CaseEvidenceConfig,
    )

    tool._vector_store = store
    tool._llm_router = MagicMock()
    tool._kb_config = CaseEvidenceConfig()
    tool._settings = MagicMock()
    return CaseEvidenceQAAdapter(tool)


def _context() -> ToolContext:
    return ToolContext(
        case_id="case-1",
        user_id="user-1",
        session_id="session-1",
        organization_id="org-1",
    )


@pytest.mark.asyncio
async def test_model_is_told_the_search_failed_not_that_the_case_has_no_evidence():
    adapter = _evidence_adapter(CaseVectorStore(client=_client(_collection())))

    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        result = await adapter.execute_with_context(
            {"question": "what errors are in app.log?"}, _context()
        )

    assert result.success is False, (
        "an unsearchable case evidence store was reported to the DA loop as a "
        "successful tool call"
    )
    assert result.data is None, "a failed search must not carry an answer payload"

    rendered = f"{result.error} {result.data}".lower()
    for cause in FABRICATED_CAUSES:
        assert cause not in rendered, f"fabricated cause reached the model: {cause!r}"


@pytest.mark.asyncio
async def test_a_genuinely_empty_case_still_reads_as_a_successful_empty_answer():
    """The gate must be able to PASS.

    Raising unconditionally would satisfy every assertion above while destroying
    the tool: "searched this case and found nothing" is a legitimate, useful
    result — and the one state ``CaseEvidenceConfig.empty_result_message`` is
    written to describe — so it must stay distinguishable from "could not
    search".
    """
    adapter = _evidence_adapter(CaseVectorStore(client=_client(_collection())))

    with patch(_EMBEDDER, new=AsyncMock(return_value=[0.1] * 1024)):
        result = await adapter.execute_with_context(
            {"question": "what errors are in app.log?"}, _context()
        )

    assert result.success is True
    assert result.data, "an empty-but-working case store should still render an answer"
    assert "no vectorized evidence" in result.data.lower()


# ---------------------------------------------------------------------------
# The write half: one embedding space, or the collection is unsearchable
# ---------------------------------------------------------------------------


async def _index(case_vector_store, embeddings) -> None:
    from faultmaven.core.preprocessing.models import UnifiedDataType
    from faultmaven.core.preprocessing.vector_storage import (
        store_in_vector_db_background,
    )

    with patch(
        "faultmaven.core.preprocessing.vector_storage.model_cache.aembed_texts",
        new=AsyncMock(return_value=embeddings),
    ):
        await store_in_vector_db_background(
            case_id="case-1",
            evidence_id="ev-1",
            structural_index="## Errors\nOutOfMemoryError in worker-3\n",
            data_type=UnifiedDataType.LOGS,
            metadata={},
            case_vector_store=case_vector_store,
            max_chunk_tokens=500,
            overlap_tokens=50,
        )


@pytest.mark.asyncio
async def test_indexing_skips_rather_than_writing_into_a_second_embedding_space():
    """ChromaDB pins a collection's dimension on first write.

    Indexing with the default embedding while the search path uses BGE-M3
    leaves those chunks unsearchable forever AND makes every later BGE-M3 write
    to that collection fail on dimension — one unavailable-model window
    poisoning the case's evidence index for good. Skipping keeps the collection
    writable once the model returns.
    """
    store = MagicMock()
    store.add_documents = AsyncMock()

    await _index(store, embeddings=None)

    store.add_documents.assert_not_called()


@pytest.mark.asyncio
async def test_indexing_still_writes_when_the_embedder_is_available():
    """The skip above must be conditional, not a disabled indexing path."""
    store = MagicMock()
    store.add_documents = AsyncMock()

    await _index(store, embeddings=[[0.1] * 1024])

    store.add_documents.assert_called_once()
    assert store.add_documents.call_args.kwargs["embeddings"] == [[0.1] * 1024]
