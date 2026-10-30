"""Each Q&A tool retrieves through the store production gives it (#2284).

``AnswerFromKB`` and ``AnswerFromCaseEvidence`` were both typed
``KnowledgeVectorStore``, but the composition gives the case tool a
``CaseVectorStore`` (``create_document_qa_tools``). Each is now typed against
what it is given: the KB tool against ``IKnowledgeVectorStore``, the case tool
against ``CaseVectorStore``. ``create_autospec`` of each type proves the
retrieval call the tool makes binds on it; an empty result returns before any
synthesis, so no model is reached.
"""

from unittest.mock import create_autospec, sentinel

import pytest

from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.models.interfaces import IKnowledgeVectorStore
from faultmaven.modules.agent.tools.case_evidence_qa import AnswerFromCaseEvidence
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB

pytestmark = pytest.mark.unit


async def test_the_kb_tool_runs_hybrid_search_on_the_kb_store_interface() -> None:
    store = create_autospec(IKnowledgeVectorStore, instance=True)
    store.hybrid_search.return_value = []
    router = create_autospec(LLMRouter, instance=True)
    tool = AnswerFromKB(vector_store=store, llm_router=router)

    result = await tool.answer_question(
        "pool exhausted",
        None,
        5,
        {"scope": "global"},
        redaction_ctx=sentinel.redaction_ctx,
        empty_result="nothing",
        admit=sentinel.admit,
    )

    assert result["answer"] == "nothing"
    store.hybrid_search.assert_awaited_once_with(
        collection_name="faultmaven_kb",
        query="pool exhausted",
        k=5,
        where={"scope": "global"},
        admit=sentinel.admit,
    )
    store.search.assert_not_awaited()
    assert router.mock_calls == []


async def test_the_case_tool_runs_search_on_the_case_store() -> None:
    store = create_autospec(CaseVectorStore, instance=True)
    store.search.return_value = []
    router = create_autospec(LLMRouter, instance=True)
    tool = AnswerFromCaseEvidence(vector_store=store, llm_router=router)

    result = await tool.answer_question(
        "first error?",
        "case-1",
        5,
        redaction_ctx=sentinel.redaction_ctx,
        empty_result="nothing",
    )

    assert result["answer"] == "nothing"
    store.search.assert_awaited_once_with(
        collection_name="case_case-1", query="first error?", k=5, where=None
    )
    # The case store has no hybrid pipeline, and the case config never asks
    # for one.
    assert not hasattr(store, "hybrid_search")
    assert router.mock_calls == []
