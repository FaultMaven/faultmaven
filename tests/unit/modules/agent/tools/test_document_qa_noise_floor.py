"""Tests for the noise-floor refuse path in DocumentQATool.

Issue: kb_qa was synthesizing answers grounded in off-topic chunks when the
KB didn't cover the queried topic (e.g. a ZooKeeper question landing on
Kafka chunks via shared vocabulary). The fix short-circuits synthesis when
the top chunk's score is below the KB's configured relevance threshold.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.case_evidence_config import (
    CaseEvidenceConfig,
)
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    UnifiedKBConfig,
)


def _make_chunks(scores):
    return [
        {"content": f"chunk-{i}", "metadata": {"title": f"Doc {i}"}, "score": s}
        for i, s in enumerate(scores)
    ]


def _make_tool(kb_config, chunks):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    llm_router.route = AsyncMock(return_value=MagicMock(content="synthesized answer"))
    return DocumentQATool(vector_store, llm_router, kb_config), llm_router


class TestNoiseFloorRefuse:
    """Verify DocumentQATool short-circuits when retrieval scores are noise."""

    @pytest.mark.asyncio
    async def test_refuses_synthesis_when_max_score_below_threshold(self):
        """Noise-floor scores (~0) must short-circuit before the synthesis call."""
        # Mirrors the ISS-013 ZooKeeper run: avg ≈ -0.01, max well below 0.3.
        chunks = _make_chunks([-0.01, -0.02, 0.05, 0.0, -0.03])
        tool, llm_router = _make_tool(UnifiedKBConfig(), chunks)

        result = await tool.answer_question(
            question="Zookeeper QuorumCnxManager connection broken",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        llm_router.route.assert_not_called()
        assert result["sources"] == []
        assert result["chunk_count"] == 0
        assert result["confidence"] == 0.0
        assert "no relevant" in result["answer"].lower()

    @pytest.mark.asyncio
    async def test_synthesizes_when_max_score_clears_threshold(self):
        """Even one strong chunk should let synthesis proceed."""
        chunks = _make_chunks([0.05, 0.42, 0.1, 0.0, -0.01])
        tool, llm_router = _make_tool(UnifiedKBConfig(), chunks)

        result = await tool.answer_question(
            question="standard heap-dump procedure",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        llm_router.route.assert_called_once()
        assert result["chunk_count"] == 5
        assert result["answer"] == "synthesized answer"

    @pytest.mark.asyncio
    async def test_threshold_disabled_for_case_evidence(self):
        """CaseEvidenceConfig opts out — always synthesizes from closest chunks."""
        chunks = _make_chunks([-0.05, -0.1, 0.02])
        tool, llm_router = _make_tool(CaseEvidenceConfig(), chunks)

        result = await tool.answer_question(
            question="when did the first error occur?",
            scope_id="case_abc",
            k=3,
        )

        llm_router.route.assert_called_once()
        assert result["chunk_count"] == 3
