"""#1272 — what a KB hit arrives carrying, on the path that feeds the prompt.

``search_knowledge`` read ``title``, ``document_type`` and ``tags`` off the TOP
LEVEL of the vector store's formatted hit. The store puts them in
``metadata``, so every hit came back titled "Untitled", typed "general" and
untagged — and ``case.kb_context`` stores ``r.title``, which the prompt renders
as ``MATCH 1: Untitled``. The model was shown runbook prose with no way to tell
which runbook it was reading or to cite it back.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from faultmaven.models.interfaces import IKnowledgeVectorStore
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)


def _store(hits) -> IKnowledgeVectorStore:
    """The store interface ``KnowledgeService`` is typed against (#2284)."""
    store = create_autospec(IKnowledgeVectorStore, instance=True)
    store.search.return_value = hits
    store.hybrid_search.return_value = hits
    return store


def _service(store):
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = store
    service._tracer = SimpleNamespace(trace=lambda *a, **k: contextlib.nullcontext())
    service._sanitizer = SimpleNamespace(asanitize=AsyncMock(side_effect=lambda q: q))
    return service


_HIT = {
    "id": "kb_dc601a7a98ff_chunk_0",
    "content": "Services fail to start or crash with write errors referencing ENOSPC",
    "score": 0.6674,
    "metadata": {
        "title": "Linux Disk Full",
        "document_type": "runbook",
        "tags": "disk-space,inode,enospc",
        "parent_document_id": "kb_dc601a7a98ff",
        "total_chunks": 12,
        "service": "linux",
        "scope": "global",
    },
    "rerank_score": 0.71,
}


class TestHitIdentity:
    @pytest.mark.asyncio
    async def test_title_type_and_tags_come_from_chunk_metadata(self):
        service = _service(_store([_HIT]))
        [result] = await service.search_knowledge(
            "disk full", filters={"scope": "global"}
        )
        assert result.title == "Linux Disk Full", (
            "the prompt renders this as 'MATCH 1: <title>' — 'Untitled' told "
            "the model nothing about what it was reading"
        )
        assert result.document_type == "runbook"
        assert result.tags == ["disk-space", "inode", "enospc"]

    @pytest.mark.asyncio
    async def test_a_top_level_value_still_wins(self):
        hit = dict(_HIT, title="Explicit Title")
        service = _service(_store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.title == "Explicit Title"

    @pytest.mark.asyncio
    async def test_missing_identity_still_degrades_gracefully(self):
        hit = {"id": "x", "content": "c", "score": 0.5, "metadata": {"scope": "global"}}
        service = _service(_store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.title == "Untitled"
        assert result.document_type == "general"
        assert result.tags == []

    @pytest.mark.asyncio
    async def test_rerank_score_is_propagated(self):
        service = _service(_store([_HIT]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.rerank_score == pytest.approx(0.71)

    @pytest.mark.asyncio
    async def test_a_hit_with_no_rerank_score_carries_none(self):
        """Absent, not defaulted — a consumer must be able to tell 'unknown'."""
        hit = {k: v for k, v in _HIT.items() if k != "rerank_score"}
        service = _service(_store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.rerank_score is None


class TestHybridDispatch:
    @pytest.mark.asyncio
    async def test_the_push_routes_to_hybrid_search_with_the_floor_and_the_check(
        self,
    ):
        store = _store([_HIT])
        service = _service(store)
        await service.search_knowledge(
            "q", limit=10, filters={"scope": "global"}, min_score=0.5
        )
        store.search.assert_not_called()
        kwargs = store.hybrid_search.call_args.kwargs
        assert kwargs["min_score"] == 0.5
        # Row-checked (#2397): the service's own admission check.
        assert kwargs["admit"] == service.admitted_document_ids

    @pytest.mark.asyncio
    async def test_there_is_no_pure_vector_path(self):
        """The pure-vector branch had no production caller and, under the
        admission filter, would under-fill by asking for exactly ``limit``."""
        store = _store([_HIT])
        service = _service(store)
        await service.search_knowledge("q", filters={"scope": "global"})
        store.search.assert_not_called()
        store.hybrid_search.assert_called_once()
