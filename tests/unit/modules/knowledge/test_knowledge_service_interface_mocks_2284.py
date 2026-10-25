"""KnowledgeService runs on mocks of the interfaces it is typed against (#2284).

The service takes ``knowledge_ingester: IKnowledgeIngester``, ``sanitizer:
ISanitizer``, ``tracer: ITracer`` and ``vector_store: IKnowledgeVectorStore``.
Until #2284 two of those types did not declare what the service calls:
``ISanitizer`` had no ``asanitize``, and the store was typed ``IVectorStore``,
whose ``search`` takes ``filters`` where the service passes ``collection_name``
and ``where``. A test that mocked the interfaces, as the testing standards say
to, got mocks that raised on the first call, so the tests and the testing docs
specced their mocks on ``DataSanitizer`` and ``KnowledgeVectorStore`` instead.

``create_autospec`` builds every mock here from the interface alone: a method
the interface does not declare is an ``AttributeError`` and a call it does not
declare is a ``TypeError``, so each call the service makes is proven to be one
its interface declares.
"""

from unittest.mock import AsyncMock, call, create_autospec

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import KB_COLLECTION
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.models import KnowledgeBaseDocument
from faultmaven.models.interfaces import (
    IKnowledgeIngester,
    IKnowledgeVectorStore,
    ISanitizer,
    ITracer,
)
from faultmaven.modules.knowledge.domain.item_title import sanitize_item_title
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = pytest.mark.unit

_HIT = {
    "id": "kb_pool_chunk_0",
    "content": "Raise the connection pool size",
    "score": 0.92,
    "metadata": {"title": "Database pool exhaustion", "parent_document_id": "kb_pool"},
}


def _sanitizer() -> ISanitizer:
    sanitizer = create_autospec(ISanitizer, instance=True)
    sanitizer.asanitize.side_effect = lambda data: data
    return sanitizer


def _service(sanitizer, tracer, vector_store) -> KnowledgeService:
    return KnowledgeService(
        knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
        sanitizer=sanitizer,
        tracer=tracer,
        vector_store=vector_store,
        # Required; neither path below opens a session.
        db_session_factory=create_autospec(get_db_session),
    )


@pytest.mark.parametrize("use_hybrid", [False, True], ids=["vector", "hybrid"])
async def test_search_knowledge_runs_on_interface_mocks(use_hybrid: bool) -> None:
    sanitizer = _sanitizer()
    tracer = create_autospec(ITracer, instance=True)
    vector_store = create_autospec(IKnowledgeVectorStore, instance=True)
    vector_store.search.return_value = [_HIT]
    vector_store.hybrid_search.return_value = [{**_HIT, "rerank_score": 0.7}]
    service = _service(sanitizer, tracer, vector_store)

    results = await service.search_knowledge(
        "database timeout", limit=3, use_hybrid=use_hybrid, min_score=0.3
    )

    sanitizer.asanitize.assert_awaited_once_with("database timeout")
    assert tracer.trace.call_args_list == [
        call("knowledge_service_search"),
        call("knowledge_vector_search"),
    ]
    expected = dict(
        collection_name=KB_COLLECTION,
        query="database timeout",
        k=3,
        where={"scope": "global"},
    )
    if use_hybrid:
        vector_store.hybrid_search.assert_awaited_once_with(**expected, min_score=0.3)
        vector_store.search.assert_not_awaited()
    else:
        vector_store.search.assert_awaited_once_with(**expected)
        vector_store.hybrid_search.assert_not_awaited()
    assert [(r.title, r.parent_document_id) for r in results] == [
        ("Database pool exhaustion", "kb_pool")
    ]
    assert results[0].rerank_score == (0.7 if use_hybrid else None)


async def test_index_path_writes_through_the_store_interface() -> None:
    """The write path's two store calls bind on the interface mock too."""
    vector_store = create_autospec(IKnowledgeVectorStore, instance=True)
    service = _service(
        _sanitizer(), create_autospec(ITracer, instance=True), vector_store
    )
    document = KnowledgeBaseDocument(
        document_id="kb_pool",
        title="Database pool exhaustion",
        content="# Database pool exhaustion\n\nRaise the connection pool size.",
        document_type="runbook",
        scope="global",
        created_at="2026-10-25T00:00:00+00:00",
        updated_at="2026-10-25T00:00:00+00:00",
    )

    indexed = await service._index_document_in_vector_store(
        document,
        prechunked=[("Raise the connection pool size.", [0.1, 0.2])],
        enterprise_id="ent-1",
    )

    assert indexed == 1
    vector_store.delete_documents_by_parent_id.assert_awaited_once_with("kb_pool")
    (written,), kwargs = vector_store.add_documents.await_args
    assert kwargs == {"embeddings": [[0.1, 0.2]]}
    assert [d["id"] for d in written] == ["kb_pool_chunk_0"]
    assert written[0]["metadata"]["enterprise_id"] == "ent-1"


async def test_sanitizer_interface_mock_serves_every_asanitize_caller() -> None:
    """``asanitize`` is declared, so the interface mock is an awaitable double.

    ``sanitize_item_title`` is the knowledge module's other ``asanitize``
    caller; on a mock without the method it fails closed as "scrub
    unavailable" rather than returning the title.
    """
    sanitizer = _sanitizer()

    assert isinstance(sanitizer.asanitize, AsyncMock)
    assert await sanitize_item_title(sanitizer, "Pool exhaustion") == (
        "Pool exhaustion"
    )
    sanitizer.asanitize.assert_awaited_once_with("Pool exhaustion")
    sanitizer.sanitize.assert_not_called()
