"""KB context-metadata wiring + relay-synthesis fidelity.

Two retrieval-fidelity contracts, both asserted mechanically (LLM-agnostic):

1. Case context (affected service) is derived from the case and threaded all
   the way into ``KnowledgeVectorStore.hybrid_search`` as a **soft** rerank
   boost — so the reranker's metadata signal fires on domain/service, not just
   frontmatter status. No pre-filtering.
2. The KB synthesis prompt relays procedural detail (steps/commands) rather
   than compressing it away before the answer reaches the engine.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import derive_kb_context_metadata
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    UnifiedKBConfig,
)


def _make_tool(chunks):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    llm_router.route = AsyncMock(return_value=MagicMock(content="synthesized answer"))
    return (
        DocumentQATool(vector_store, llm_router, UnifiedKBConfig()),
        vector_store,
        llm_router,
    )


def _chunk(score=0.5):
    return {"content": "chunk", "metadata": {"title": "Doc"}, "score": score}


class TestDeriveKbContextMetadata:
    """The case → context_metadata extraction (soft-boost source)."""

    def test_extracts_service_from_first_affected_service(self):
        case = SimpleNamespace(
            problem_verification=SimpleNamespace(
                affected_services=["payment-api", "checkout"]
            )
        )
        assert derive_kb_context_metadata(case) == {"service": "payment-api"}

    def test_omits_domain_no_fabrication(self):
        """The case has no domain field; we must not synthesize one (a default
        domain would produce false exact-matches in the reranker)."""
        case = SimpleNamespace(
            problem_verification=SimpleNamespace(affected_services=["db"])
        )
        assert "domain" not in derive_kb_context_metadata(case)

    def test_empty_when_no_problem_verification(self):
        assert derive_kb_context_metadata(SimpleNamespace()) == {}
        assert (
            derive_kb_context_metadata(SimpleNamespace(problem_verification=None)) == {}
        )

    def test_empty_when_no_affected_services(self):
        case = SimpleNamespace(
            problem_verification=SimpleNamespace(affected_services=[])
        )
        assert derive_kb_context_metadata(case) == {}

    def test_ignores_blank_service(self):
        case = SimpleNamespace(
            problem_verification=SimpleNamespace(affected_services=["   "])
        )
        assert derive_kb_context_metadata(case) == {}


class TestContextMetadataReachesHybridSearch:
    """context_metadata is threaded into hybrid_search as a soft boost."""

    @pytest.mark.asyncio
    async def test_soft_boost_passed_to_hybrid_search(self):
        tool, vector_store, _ = _make_tool([_chunk()])

        await tool.answer_question(
            question="rollback procedure",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
            context_metadata={"service": "payment-api"},
        )

        kwargs = vector_store.hybrid_search.call_args.kwargs
        assert kwargs["context_metadata"] == {"service": "payment-api"}
        # Soft, never hard — the hard pre-filter awaits copilot high-confidence
        # page context; retrieval must not silently drop chunks meanwhile.
        assert kwargs["filter_mode"] == "soft"

    @pytest.mark.asyncio
    async def test_none_context_still_soft(self):
        tool, vector_store, _ = _make_tool([_chunk()])

        await tool.answer_question(
            question="rollback procedure",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        kwargs = vector_store.hybrid_search.call_args.kwargs
        assert kwargs["context_metadata"] is None
        assert kwargs["filter_mode"] == "soft"


class TestRelaySynthesisPrompt:
    """The synthesis prompt must relay procedural detail, not compress it."""

    @pytest.mark.asyncio
    async def test_prompt_instructs_preserving_procedural_detail(self):
        tool, _, llm_router = _make_tool([_chunk()])

        await tool.answer_question(
            question="rollback procedure",
            scope_id=None,
            k=5,
            filters={"$or": [{"scope": "global"}]},
        )

        # The user-role synthesis prompt is the last message sent to the LLM.
        messages = llm_router.route.call_args.kwargs["messages"]
        synthesis_prompt = messages[-1]["content"].lower()
        assert "preserve procedural detail" in synthesis_prompt
        assert "steps" in synthesis_prompt or "commands" in synthesis_prompt
        # Background may compress; actionable steps may not.
        assert "never" in synthesis_prompt and "actionable" in synthesis_prompt

    def test_system_prompt_favors_step_by_step_over_terse(self):
        """The unified KB system prompt asks for step-by-step procedures — it
        must not instruct terse/concise summarization that strips steps."""
        system_prompt = UnifiedKBConfig().system_prompt.lower()
        assert "step-by-step" in system_prompt
        assert "concise" not in system_prompt
