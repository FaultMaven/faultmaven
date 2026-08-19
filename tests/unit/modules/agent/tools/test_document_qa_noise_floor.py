"""Tests for the noise-floor refuse path in DocumentQATool.

Issue: kb_qa was synthesizing answers grounded in off-topic chunks when the
KB didn't cover the queried topic (e.g. a ZooKeeper question landing on
Kafka chunks via shared vocabulary). The fix short-circuits synthesis when
the top chunk's score is below the KB's configured relevance threshold.

The scores here are REAL cosine similarities measured against the shipped
91-runbook KB with BGE-M3, not invented ones. That matters: this suite
originally used synthetic scores clustered around 0 for "off-topic", which
encoded the same wrong assumption as the threshold it was guarding — that
BGE-M3 sends unrelated text toward orthogonality. It does not. Off-topic
queries floor around 0.36-0.48 on this corpus, and on-topic ones run
0.59-0.75, so a test that says "0.05 is off-topic, 0.42 is on-topic" passes
against any threshold in a wide band and would not have caught #1072.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
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


def _llm_response(content: str, stop_reason: StopReason = StopReason.STOP):
    """Real ``LLMResponse`` rather than a ``MagicMock``.

    A MagicMock answers every attribute with a truthy Mock, so the moment the
    tool started consulting ``is_truncated`` (#1094) the stand-in silently
    claimed every synthesis had been cut off. A fake that cannot say "no" is
    not a fake, it is a defect generator.
    """
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=stop_reason,
    )


def _make_tool(kb_config, chunks):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    llm_router.route = AsyncMock(return_value=_llm_response("synthesized answer"))
    return DocumentQATool(vector_store, llm_router, kb_config), llm_router


class TestNoiseFloorRefuse:
    """Verify DocumentQATool short-circuits when retrieval scores are noise."""

    @pytest.mark.asyncio
    async def test_refuses_synthesis_when_max_score_below_threshold(self):
        """The adjacent-vocabulary case the guard exists for must still refuse.

        Measured top-5 for "ZooKeeper ensemble leader election failure after
        rolling restart" against the shipped KB, which holds no ZooKeeper
        runbook: it lands on Kafka chunks via shared vocabulary. This is the
        tightest real constraint on the threshold — 0.477 is the highest
        off-topic score observed anywhere.
        """
        chunks = _make_chunks([0.477, 0.470, 0.435, 0.422, 0.416])
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

    @pytest.mark.asyncio
    async def test_refusal_does_not_claim_the_kb_lacks_coverage(self):
        """A similarity score is not evidence about what the KB contains.

        The refusal text used to assert "the KB does not cover this topic".
        Under #1072 that made a mis-scaled threshold worse than unhelpful: the
        investigating model was handed a false statement about its own
        knowledge base — on topics with dedicated runbooks — and told to stop
        asking. Withholding an answer is fine; asserting absence is not.
        """
        chunks = _make_chunks([0.477, 0.470, 0.435])
        tool, _ = _make_tool(UnifiedKBConfig(), chunks)

        answer = (
            await tool.answer_question(
                question="Zookeeper QuorumCnxManager connection broken",
                scope_id=None,
                k=3,
                filters={"$or": [{"scope": "global"}]},
            )
        )["answer"].lower()

        assert "does not cover" not in answer
        assert "no relevant content" not in answer
        assert "searched" in answer

    @pytest.mark.asyncio
    async def test_synthesizes_on_the_weakest_observed_on_topic_query(self):
        """The on-topic floor must clear the threshold, not straddle it.

        Measured top-5 for "Runbook for diagnosing historical HikariCP
        connection-pool exhaustion caused by long-held database connections" —
        the weakest on-topic retrieval observed, and one of the live false
        refusals in #1072. Retrieval is correct (9 of top-10 are
        connection-exhaustion runbooks; the top two contain "HikariCP"
        verbatim), so refusing here is purely a calibration failure.
        """
        chunks = _make_chunks([0.591, 0.588, 0.575, 0.570, 0.567])
        tool, llm_router = _make_tool(UnifiedKBConfig(), chunks)

        result = await tool.answer_question(
            question=(
                "Runbook for diagnosing historical HikariCP connection-pool "
                "exhaustion caused by long-held database connections"
            ),
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        llm_router.route.assert_called_once()
        assert result["chunk_count"] == 5
        assert result["answer"] == "synthesized answer"

    @pytest.mark.asyncio
    async def test_threshold_separates_measured_on_and_off_topic_populations(self):
        """Pin the calibration itself, not just two points either side of it.

        The old threshold was not merely a bad number — it was compared against
        a scale nobody had measured. This asserts the invariant that makes any
        number defensible: the floor sits strictly between the two observed
        populations.
        """
        threshold = UnifiedKBConfig().relevance_threshold

        worst_on_topic = 0.591  # HikariCP, weakest correct retrieval
        best_off_topic = 0.477  # ZooKeeper -> Kafka, adjacent vocabulary

        assert best_off_topic < threshold < worst_on_topic

    @pytest.mark.asyncio
    async def test_threshold_disabled_for_case_evidence(self):
        """CaseEvidenceConfig opts out — always synthesizes from closest chunks."""
        chunks = _make_chunks([0.36, 0.35, 0.34])
        tool, llm_router = _make_tool(CaseEvidenceConfig(), chunks)

        result = await tool.answer_question(
            question="when did the first error occur?",
            scope_id="case_abc",
            k=3,
        )

        llm_router.route.assert_called_once()
        assert result["chunk_count"] == 3
