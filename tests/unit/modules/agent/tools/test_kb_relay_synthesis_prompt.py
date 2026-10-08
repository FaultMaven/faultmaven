"""KB relay-synthesis fidelity.

The KB synthesis prompt relays procedural detail (steps/commands) rather than
compressing it away before the answer reaches the engine — asserted
mechanically (LLM-agnostic).

This file used to pin a second contract too: that a case's affected service
reached ``hybrid_search`` as a soft rerank boost (#709). That input was removed
with the field it read (#1880); ``test_no_case_kb_context_metadata_1880.py``
pins its absence.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

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
