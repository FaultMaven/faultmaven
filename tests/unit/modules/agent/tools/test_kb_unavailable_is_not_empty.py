"""An unavailable knowledge base is not an empty one (#943, supersedes #868).

The investigating model's KB tool is ``kb_qa``: ``KBToolAdapter`` wrapping
``AnswerFromKB`` (a ``DocumentQATool``) over ``KnowledgeVectorStore``. When the
embedding model cannot be loaded there is nothing to search *with* — but every
layer of that chain used to convert the failure into a substantive answer:

  store returns []  ->  tool renders "nothing found"  ->  adapter stamps
  success=True

so the model was handed "the knowledge base holds nothing", an affirmative
negative it would then reason from. Worse, the rendered text named a *cause*
("no files have been uploaded to this case") belonging to a different
subsystem entirely.

These tests assert on **the string the model actually receives**, not on any
single layer's return value. That is deliberate: #868's first attempt fixed the
store alone and would have merged inert, because ``DocumentQATool`` caught
everything one frame later. A fix here is only real if it survives every
handler between the store and the tool boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter

pytestmark = [pytest.mark.unit]

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"

# Causes no layer of this chain is entitled to assert. The first is the
# case-evidence text that used to be emitted for knowledge-base queries; the
# second is the adapter's old guess about why a query failed.
FABRICATED_CAUSES = [
    "no files have been uploaded",
    "still being indexed",
    "may not be populated",
    "indexing failed",
]


def _store(collection=None) -> KnowledgeVectorStore:
    store = KnowledgeVectorStore(client=MagicMock())
    store._get_or_create_collection = MagicMock(
        return_value=collection if collection is not None else MagicMock()
    )
    return store


def _empty_collection() -> MagicMock:
    """A collection that answers a real query with zero matches."""
    collection = MagicMock()
    collection.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    return collection


def _adapter(store: KnowledgeVectorStore) -> KBToolAdapter:
    tool = AnswerFromKB.__new__(AnswerFromKB)
    tool._vector_store = store
    tool._llm_router = MagicMock()
    tool._kb_config = UnifiedKBConfig()
    tool._settings = MagicMock()
    return KBToolAdapter(tool)


def _context() -> ToolContext:
    return ToolContext(
        case_id="case-1",
        user_id="user-1",
        session_id="session-1",
        organization_id="org-1",
    )


# ---------------------------------------------------------------------------
# The store must fail closed rather than degrade to []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_search_raises_when_the_embedder_is_unavailable():
    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError, match="embedding model"):
            await _store().search(
                collection_name="faultmaven_kb",
                query="how do I drain a node?",
                where={"scope": "global"},
            )


@pytest.mark.asyncio
async def test_keyword_arm_does_not_swallow_the_availability_error():
    """The hybrid sweep must raise, not degrade, when the embedder is gone.

    ``_keyword_constrained_search`` catches per-keyword failures so one bad
    keyword cannot sink a query, and that handler is exactly how #868's first
    attempt was rendered inert. The sweep now embeds ONCE up front, which puts
    the availability failure structurally outside that handler rather than
    three lines above it — so this asserts on ``hybrid_search``, the entry the
    tool actually calls, instead of the keyword helper it delegates to.
    """
    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError):
            await _store().hybrid_search(
                collection_name="faultmaven_kb",
                query="CrashLoopBackOff on payment-svc",
                k=5,
                where={"scope": "global"},
            )


@pytest.mark.asyncio
async def test_keyword_loop_still_reraises_a_typed_availability_error():
    """The per-keyword handler must not absorb ``KnowledgeBaseError``.

    The sweep now embeds ONCE up front, so an unavailable embedder surfaces
    before any keyword is probed — which means the ``except KnowledgeBaseError:
    raise`` guard inside the loop is no longer reached by the embedder path that
    motivated it. The guard still stands between a typed availability error and
    the broad ``except Exception`` three lines below, so it is pinned directly
    here rather than left to be deleted as dead code by a later refactor: a
    probe that raises the typed error must propagate, not be folded into a
    partial result set that reads as a complete keyword sweep.
    """
    store = _store()
    store._single_keyword_search = AsyncMock(
        side_effect=KnowledgeBaseError("vector store unavailable")
    )

    with pytest.raises(KnowledgeBaseError):
        await store._keyword_constrained_search(
            collection_name="faultmaven_kb",
            query_embedding=[0.1] * 8,
            keywords=["CrashLoopBackOff", "payment-svc"],
            k=5,
            where={"scope": "global"},
        )


# ---------------------------------------------------------------------------
# The property that matters: what the model is told
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_is_told_the_query_failed_not_that_the_kb_is_empty():
    adapter = _adapter(_store())

    with patch(_EMBEDDER, new=AsyncMock(return_value=None)):
        result = await adapter.execute_with_context(
            {"question": "how do I drain a node?"}, _context()
        )

    assert result.success is False, (
        "an unavailable knowledge base was reported to the DA loop as a "
        "successful tool call"
    )
    assert result.data is None, "a failed query must not carry an answer payload"

    rendered = f"{result.error} {result.data}".lower()
    for cause in FABRICATED_CAUSES:
        assert cause not in rendered, f"fabricated cause reached the model: {cause!r}"


@pytest.mark.asyncio
async def test_a_genuinely_empty_kb_still_reads_as_a_successful_empty_answer():
    """The gate must be able to PASS.

    Without this, raising unconditionally would satisfy every assertion above
    while destroying the tool: "searched and found nothing" is a legitimate,
    useful result and must stay distinguishable from "could not search".
    """
    adapter = _adapter(_store(collection=_empty_collection()))

    with patch(_EMBEDDER, new=AsyncMock(return_value=[0.1] * 1024)):
        result = await adapter.execute_with_context(
            {"question": "how do I drain a node?"}, _context()
        )

    assert result.success is True
    assert result.data, "an empty-but-working KB should still render an answer"
    assert "no matching runbooks" in result.data.lower()


@pytest.mark.asyncio
async def test_empty_kb_answer_never_carries_case_evidence_causes():
    """The KB tool used to emit the case-evidence store's text verbatim,
    naming an ingestion cause for a subsystem the query never touched."""
    adapter = _adapter(_store(collection=_empty_collection()))

    with patch(_EMBEDDER, new=AsyncMock(return_value=[0.1] * 1024)):
        result = await adapter.execute_with_context(
            {"question": "how do I drain a node?"}, _context()
        )

    for cause in FABRICATED_CAUSES:
        assert cause not in result.data.lower(), f"leaked cause: {cause!r}"


# ---------------------------------------------------------------------------
# Every KB config must state its own truth
# ---------------------------------------------------------------------------


def test_every_kb_config_defines_its_own_empty_result_message():
    """``empty_result_message`` is abstract so a new KB cannot silently inherit
    another store's claims — which is precisely how case-evidence text ended up
    answering knowledge-base queries."""
    from faultmaven.modules.agent.tools.kb_configs.case_evidence_config import (
        CaseEvidenceConfig,
    )

    kb = UnifiedKBConfig().empty_result_message
    evidence = CaseEvidenceConfig().empty_result_message

    assert kb and evidence
    assert kb != evidence, "the two stores must not share one claim"
    # The KB has no visibility into case uploads and must not mention them.
    assert "upload" not in kb.lower()


# ---------------------------------------------------------------------------
# The embedding hoist must not lose its time bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hanging_embedder_fails_closed_instead_of_hanging_the_turn():
    """Embedding runs OUTSIDE ``call_external`` (deliberately — retrying an
    unrecoverable model load would burn ChromaDB's retry budget and trip its
    circuit breaker). That also puts it outside ``call_external``'s timeout, so
    it must carry its own: without one a cold BGE-M3 load hangs the tool call
    all the way to the outer turn budget."""
    import asyncio as _asyncio

    from faultmaven.infrastructure import embedding_guard

    async def _never_returns(_text):
        await _asyncio.sleep(60)

    with patch.object(embedding_guard, "EMBED_TIMEOUT_SECONDS", 0.05):
        with patch(_EMBEDDER, new=_never_returns):
            with pytest.raises(KnowledgeBaseError) as excinfo:
                await _store().search(
                    collection_name="faultmaven_kb",
                    query="how do I drain a node?",
                    where={"scope": "global"},
                )

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
