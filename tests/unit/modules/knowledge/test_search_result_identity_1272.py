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
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)


class _Store:
    def __init__(self, hits, supports_hybrid=True):
        self._hits = hits
        self.search = AsyncMock(return_value=hits)
        if supports_hybrid:
            self.hybrid_search = AsyncMock(return_value=hits)


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
    "term_coverage": 0.93,
    "identity_terms_in_query": ["linux"],
}


class TestHitIdentity:
    @pytest.mark.asyncio
    async def test_title_type_and_tags_come_from_chunk_metadata(self):
        service = _service(_Store([_HIT]))
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
        service = _service(_Store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.title == "Explicit Title"

    @pytest.mark.asyncio
    async def test_missing_identity_still_degrades_gracefully(self):
        hit = {"id": "x", "content": "c", "score": 0.5, "metadata": {"scope": "global"}}
        service = _service(_Store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.title == "Untitled"
        assert result.document_type == "general"
        assert result.tags == []

    @pytest.mark.asyncio
    async def test_grounding_evidence_is_propagated(self):
        service = _service(_Store([_HIT]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.rerank_score == pytest.approx(0.71)
        assert result.term_coverage == pytest.approx(0.93)
        assert result.identity_terms_in_query == ["linux"]

    @pytest.mark.asyncio
    async def test_pure_vector_hits_carry_no_grounding_evidence(self):
        """Absent, not defaulted — a consumer must be able to tell 'unknown'."""
        hit = {
            k: v
            for k, v in _HIT.items()
            if k not in ("rerank_score", "term_coverage", "identity_terms_in_query")
        }
        service = _service(_Store([hit]))
        [result] = await service.search_knowledge("q", filters={"scope": "global"})
        assert result.rerank_score is None
        assert result.term_coverage is None
        assert result.identity_terms_in_query == []


class TestHybridDispatch:
    @pytest.mark.asyncio
    async def test_use_hybrid_routes_to_hybrid_search_with_the_floor(self):
        store = _Store([_HIT])
        service = _service(store)
        await service.search_knowledge(
            "q", limit=10, filters={"scope": "global"}, use_hybrid=True, min_score=0.5
        )
        store.search.assert_not_called()
        assert store.hybrid_search.call_args.kwargs["min_score"] == 0.5

    @pytest.mark.asyncio
    async def test_default_is_unchanged_pure_vector(self):
        store = _Store([_HIT])
        service = _service(store)
        await service.search_knowledge("q", filters={"scope": "global"})
        store.hybrid_search.assert_not_called()
        store.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_store_without_hybrid_falls_back(self):
        store = _Store([_HIT], supports_hybrid=False)
        service = _service(store)
        await service.search_knowledge(
            "q", filters={"scope": "global"}, use_hybrid=True
        )
        store.search.assert_called_once()
