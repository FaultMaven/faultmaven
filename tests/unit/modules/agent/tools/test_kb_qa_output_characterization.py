"""Characterization tests for the KB Q&A tool's current output shape.

These pin the *current* behavior of ``answer_from_kb`` (via ``DocumentQATool``
+ ``UnifiedKBConfig``): a retrieved runbook is synthesized into flat prose with
a trailing ``Sources:`` citation line. They are a regression net for upcoming
KB→engine cohesion work — a change that alters how the KB tool relays runbook
content should fail here and be forced to justify the change.

LLM-agnostic: the synthesis LLM and vector store are mocked, so every assertion
is a mechanical statement about how the tool assembles retrieval results into a
response — no model output decides pass/fail.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.llm.truncation import TRUNCATION_NOTICE
from faultmaven.modules.agent.tools.document_qa_tool import (
    SYNTHESIS_MAX_TOKENS,
    SYNTHESIS_MAX_TOKENS_CEILING,
    DocumentQATool,
)
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    UnifiedKBConfig,
)


def _make_chunks(titles, score=0.6):
    """Retrieval chunks above the noise floor, each carrying a runbook title."""
    return [
        {"content": f"body-{i}", "metadata": {"title": t}, "score": score}
        for i, t in enumerate(titles)
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


def _make_tool(chunks, answer="synthesized runbook guidance"):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    llm_router.route = AsyncMock(return_value=_llm_response(answer))
    tool = DocumentQATool(vector_store, llm_router, UnifiedKBConfig())
    return tool, llm_router


@pytest.mark.unit
class TestUnifiedKBFormatting:
    """``UnifiedKBConfig.format_response`` — the citation-appending contract."""

    def test_prose_then_sources_line(self):
        cfg = UnifiedKBConfig()
        out = cfg.format_response(
            answer="Restart the pod, then verify readiness.",
            sources=["CoreDNS Failures", "Pod CrashLoop"],
            chunk_count=2,
            confidence=0.7,
        )
        # Current shape: answer, blank line, then a single "Sources:" line.
        assert out.startswith("Restart the pod, then verify readiness.\n\n")
        assert "Sources: CoreDNS Failures, Pod CrashLoop" in out

    def test_sources_truncated_to_five_with_overflow_marker(self):
        cfg = UnifiedKBConfig()
        out = cfg.format_response(
            answer="A.",
            sources=[f"RB{i}" for i in range(7)],
            chunk_count=7,
            confidence=0.5,
        )
        assert "Sources: RB0, RB1, RB2, RB3, RB4 (+2 more)" in out
        # Sixth/seventh titles are not spelled out — only the overflow count.
        assert "RB5" not in out
        assert "RB6" not in out

    def test_no_sources_line_when_no_sources(self):
        cfg = UnifiedKBConfig()
        out = cfg.format_response(
            answer="No documentation applies.",
            sources=[],
            chunk_count=0,
            confidence=0.0,
        )
        assert "Sources:" not in out

    def test_source_name_extraction_precedence(self):
        cfg = UnifiedKBConfig()
        assert cfg.extract_source_name({"title": "T"}) == "T"
        assert cfg.extract_source_name({"document_title": "D"}) == "D"
        assert cfg.extract_source_name({"kb_article_id": "kb_1"}) == "kb_1"
        assert cfg.extract_source_name({}) == "Unknown document"


@pytest.mark.unit
class TestAnswerFromKBRelaysProseWithSources:
    """End-to-end (mocked LLM): retrieval → synthesized prose + Sources."""

    @pytest.mark.asyncio
    async def test_answer_question_returns_prose_and_titles_as_sources(self):
        chunks = _make_chunks(["CoreDNS Failures", "Pod CrashLoop"])
        tool, llm_router = _make_tool(chunks, answer="Do the runbook steps.")

        result = await tool.answer_question(
            question="how do I fix CoreDNS resolution failures?",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        # The synthesizer was consulted, and its prose is relayed verbatim.
        llm_router.route.assert_called_once()
        assert result["answer"] == "Do the runbook steps."
        # Sources are the deduplicated runbook titles pulled from chunk
        # metadata. answer_question builds them via list(set(...)), so the
        # set of titles is the contract — iteration order is not guaranteed.
        assert set(result["sources"]) == {"CoreDNS Failures", "Pod CrashLoop"}
        assert result["chunk_count"] == 2

    @pytest.mark.asyncio
    async def test_arun_emits_flat_prose_plus_sources_block(self):
        chunks = _make_chunks(["CoreDNS Failures"])
        tool, _ = _make_tool(chunks, answer="Flush the cache.")

        rendered = await tool._arun(
            question="coredns cache",
            scope_id=None,
            filters={"$or": [{"scope": "global"}]},
        )

        # The tool hands the engine a single prose blob with a citation line —
        # NOT structured cause nodes. This is the shape that structured
        # cause-node seeding will replace.
        assert "Flush the cache." in rendered
        assert "Sources: CoreDNS Failures" in rendered


# --------------------------------------------------------------------------- #
# Truncation on the read path (#1094)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestTruncatedSynthesis:
    """A cut KB answer is returned — but never as if it were whole.

    This answer goes into the case transcript with citations and a confidence
    score, and the agent reasons over it. Refusing it would destroy real value
    on a read path; returning it silently is what let "the runbook says only
    this" become indistinguishable from "the answer stopped here".
    """

    async def test_retries_once_at_double_then_annotates(self):
        tool, llm_router = _make_tool(_make_chunks(["Runbook A"]))
        llm_router.route = AsyncMock(
            side_effect=[
                _llm_response("half an answ", StopReason.MAX_TOKENS),
                _llm_response("half an answ", StopReason.MAX_TOKENS),
            ]
        )

        result = await tool.answer_question("why", scope_id="u1", k=5)

        assert llm_router.route.call_count == 2
        caps = [c.kwargs["max_tokens"] for c in llm_router.route.call_args_list]
        assert caps == [SYNTHESIS_MAX_TOKENS, SYNTHESIS_MAX_TOKENS_CEILING]
        assert result["truncated"] is True
        # The partial text is still there — annotated, not replaced.
        assert "half an answ" in result["answer"]
        assert TRUNCATION_NOTICE.strip() in result["answer"]

    async def test_a_recovered_answer_carries_no_notice(self):
        tool, llm_router = _make_tool(_make_chunks(["Runbook A"]))
        llm_router.route = AsyncMock(
            side_effect=[
                _llm_response("half an answ", StopReason.MAX_TOKENS),
                _llm_response("the whole answer"),
            ]
        )

        result = await tool.answer_question("why", scope_id="u1", k=5)

        assert result["truncated"] is False
        assert result["answer"] == "the whole answer"
        assert TRUNCATION_NOTICE.strip() not in result["answer"]

    async def test_a_complete_answer_is_not_retried(self):
        tool, llm_router = _make_tool(_make_chunks(["Runbook A"]))

        result = await tool.answer_question("why", scope_id="u1", k=5)

        assert llm_router.route.call_count == 1
        assert result["truncated"] is False

    async def test_every_return_path_reports_the_flag(self):
        """Uniform shape, so `result["truncated"]` is never a KeyError.

        The quiet paths — an empty KB, a match below the relevance floor — do
        not reach synthesis, and omitting the key there would make a caller
        blow up precisely when nothing interesting happened.
        """
        tool, llm_router = _make_tool([])

        result = await tool.answer_question("why", scope_id="u1", k=5)

        assert result["truncated"] is False
        llm_router.route.assert_not_called()
