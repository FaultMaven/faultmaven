"""Tests for DocumentQATool.answer_yes_no — the single-LLM-call boolean
evidence judgment (the runbook Cause matcher's T2 tier).

Retrieve top-k chunks + one classifier call → strict YES/NO. Conservative:
no evidence / unparsed / error → False (the matcher treats False as 'not
matched', never 'refuted').
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig


def _chunks(n=2):
    return [
        {"content": f"chunk {i}", "metadata": {"title": "t"}, "score": 0.9}
        for i in range(n)
    ]


def _tool(chunks, *, llm_content="YES", llm_raises=False):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    router = MagicMock()
    if llm_raises:
        router.route = AsyncMock(side_effect=RuntimeError("llm down"))
    else:
        router.route = AsyncMock(return_value=MagicMock(content=llm_content))
    return DocumentQATool(vector_store, router, UnifiedKBConfig()), router


class TestAnswerYesNo:
    @pytest.mark.asyncio
    async def test_yes_returns_true(self):
        tool, router = _tool(_chunks(), llm_content="YES")
        assert await tool.answer_yes_no("is X present?", scope_id="case_1") is True
        router.route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_returns_false(self):
        tool, _ = _tool(_chunks(), llm_content="NO")
        assert await tool.answer_yes_no("is X present?", scope_id="case_1") is False

    @pytest.mark.asyncio
    async def test_yes_with_punctuation_and_case(self):
        tool, _ = _tool(_chunks(), llm_content="Yes.")
        assert await tool.answer_yes_no("q", scope_id="case_1") is True

    @pytest.mark.asyncio
    async def test_ambiguous_answer_is_false(self):
        # Conservative: anything not clearly affirmative → False.
        tool, _ = _tool(_chunks(), llm_content="I am not sure")
        assert await tool.answer_yes_no("q", scope_id="case_1") is False

    @pytest.mark.asyncio
    async def test_no_evidence_returns_false_without_llm_call(self):
        tool, router = _tool([], llm_content="YES")
        assert await tool.answer_yes_no("q", scope_id="case_1") is False
        router.route.assert_not_awaited()  # no chunks → no classify call

    @pytest.mark.asyncio
    async def test_llm_error_returns_false(self):
        tool, _ = _tool(_chunks(), llm_raises=True)
        assert await tool.answer_yes_no("q", scope_id="case_1") is False
