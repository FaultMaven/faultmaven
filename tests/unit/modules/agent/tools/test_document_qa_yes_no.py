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
    async def test_yes_with_markup_or_leading_whitespace(self):
        # With the larger max_tokens a model may wrap the verdict; the first
        # alpha token is what counts.
        for content in ("**YES**", "  yes", "\nYES\n", "Y"):
            tool, _ = _tool(_chunks(), llm_content=content)
            assert await tool.answer_yes_no("q", scope_id="case_1") is True, content

    @pytest.mark.asyncio
    async def test_ambiguous_answer_is_false(self):
        # Conservative: anything whose first token isn't yes/y → False (a "no"
        # after a preamble stays False — never a wrong-way match).
        for content in ("I am not sure", "No", "The condition is yes", "Based on it"):
            tool, _ = _tool(_chunks(), llm_content=content)
            assert await tool.answer_yes_no("q", scope_id="case_1") is False, content

    @pytest.mark.asyncio
    async def test_no_evidence_returns_false_without_llm_call(self):
        tool, router = _tool([], llm_content="YES")
        assert await tool.answer_yes_no("q", scope_id="case_1") is False
        router.route.assert_not_awaited()  # no chunks → no classify call

    @pytest.mark.asyncio
    async def test_llm_error_returns_false(self):
        tool, _ = _tool(_chunks(), llm_raises=True)
        assert await tool.answer_yes_no("q", scope_id="case_1") is False

    @pytest.mark.asyncio
    async def test_no_classifier_model_returns_false_without_llm_call(
        self, monkeypatch
    ):
        # Misconfiguration: no classifier model → return False (logged), don't
        # call the router with an empty model.
        tool, router = _tool(_chunks(), llm_content="YES")
        monkeypatch.setattr(
            type(tool._settings.llm),
            "get_classifier_model",
            lambda self: "",
        )
        assert await tool.answer_yes_no("q", scope_id="case_1") is False
        router.route.assert_not_awaited()


class TestAnswerYesNoRawEvidenceFallback:
    """#543: when the vector collection is empty, judge against caller-supplied
    raw evidence instead of abstaining — so the matcher's T2 tier can fire even
    when case evidence was never vectorized."""

    @pytest.mark.asyncio
    async def test_empty_collection_judges_fallback_context(self):
        # No vectors, but a raw-evidence fallback is supplied → the classifier
        # runs over it. LLM says YES → matched.
        tool, router = _tool([], llm_content="YES")
        result = await tool.answer_yes_no(
            "is SSL required?",
            scope_id="case_1",
            fallback_context="[CAUSAL_EVIDENCE] migration fails: SSL connection required",
        )
        assert result is True
        router.route.assert_awaited_once()
        # The fallback text reached the prompt (not the empty chunk context).
        sent = router.route.await_args.kwargs["messages"][0]["content"]
        assert "SSL connection required" in sent

    @pytest.mark.asyncio
    async def test_empty_collection_fallback_can_still_be_no(self):
        # Fallback present but the evidence doesn't support the condition → NO.
        # (Never a refutation — the matcher treats NO as 'not matched'.)
        tool, _ = _tool([], llm_content="NO")
        assert (
            await tool.answer_yes_no(
                "is X present?", scope_id="case_1", fallback_context="unrelated text"
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_empty_collection_blank_fallback_returns_false_no_llm(self):
        # A blank/whitespace fallback is treated as no fallback → abstain, no call.
        tool, router = _tool([], llm_content="YES")
        assert (
            await tool.answer_yes_no("q", scope_id="case_1", fallback_context="   ")
            is False
        )
        router.route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vectors_present_ignore_fallback(self):
        # When chunks exist, the vector path wins — the fallback is not consulted.
        tool, router = _tool(_chunks(), llm_content="YES")
        await tool.answer_yes_no(
            "q", scope_id="case_1", fallback_context="RAW FALLBACK SHOULD NOT APPEAR"
        )
        sent = router.route.await_args.kwargs["messages"][0]["content"]
        assert "RAW FALLBACK SHOULD NOT APPEAR" not in sent
        assert "chunk 0" in sent  # the vector chunk context was used

    @pytest.mark.asyncio
    async def test_store_error_still_uses_fallback(self):
        # A vector-search failure (ChromaDB down / collection unreadable) must be
        # treated as 'no vectors' so the fallback still fires — otherwise the very
        # case the fallback exists for would skip it.
        tool, router = _tool(_chunks(), llm_content="YES")
        tool._vector_store.hybrid_search = AsyncMock(side_effect=RuntimeError("down"))
        tool._vector_store.search = AsyncMock(side_effect=RuntimeError("down"))
        result = await tool.answer_yes_no(
            "is X present?",
            scope_id="case_1",
            fallback_context="[CAUSAL_EVIDENCE] X is present",
        )
        assert result is True
        sent = router.route.await_args.kwargs["messages"][0]["content"]
        assert "X is present" in sent

    @pytest.mark.asyncio
    async def test_store_error_no_fallback_returns_false(self):
        # Store error + no fallback → still conservative False (never refutes).
        tool, _ = _tool(_chunks(), llm_content="YES")
        tool._vector_store.hybrid_search = AsyncMock(side_effect=RuntimeError("down"))
        tool._vector_store.search = AsyncMock(side_effect=RuntimeError("down"))
        assert await tool.answer_yes_no("q", scope_id="case_1") is False
