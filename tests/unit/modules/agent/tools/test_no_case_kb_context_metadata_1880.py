"""#1880 — the agent's KB path carries no case-derived rerank metadata.

The reranker's metadata signal matches a runbook's ``service``, which is the
technology ("postgresql"). The case-derived input that fed it (#709) read
``ProblemVerification.affected_services`` — the user's own services
("checkout"), a field nothing wrote and a value no runbook carries. The field
is gone, so is the derivation, and so is the ``ToolContext`` field it filled:
a field with no writer is not kept on the agent path (the #1883 invariant).

What stays is the knowledge layer's mechanism — ``hybrid_search(
context_metadata=…, filter_mode=…)`` and ``_compute_metadata_score`` — with its
own tests; the copilot page context the retrieval design names wires the agent
path when it is built.
"""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import faultmaven.modules.agent.tools.base as tools_base
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    UnifiedKBConfig,
)

pytestmark = [pytest.mark.unit]


def _generator():
    from faultmaven.core.investigation.milestone_engine.generation import (
        StructuredOutputGenerator,
    )

    deps = EngineDeps()
    deps.repository = MagicMock()
    deps.team_service = None
    deps.share_repository = None
    return StructuredOutputGenerator(deps=deps, vectorizer=None)


def _a_case_that_names_its_service():
    """A case whose statement names the user's own service."""
    from faultmaven.modules.case.domain.models.problem import ProblemVerification

    return SimpleNamespace(
        case_id="case_1880",
        enterprise_id="ent_1",
        progress=None,
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 500s"
        ),
    )


def test_tool_context_has_no_kb_context_metadata_field():
    names = {f.name for f in dataclasses.fields(ToolContext)}
    assert not {n for n in names if "context_metadata" in n}, names


def test_the_derivation_is_gone():
    assert not hasattr(tools_base, "derive_kb_context_metadata")


async def test_build_tool_context_carries_no_kb_context_metadata():
    context = await _generator().build_tool_context(
        _a_case_that_names_its_service(), user_id="u_1"
    )

    assert isinstance(context, ToolContext)
    assert not hasattr(context, "kb_context_metadata")
    assert not any("context_metadata" in key for key in vars(context))
    # ``with_execution_id`` copies every field; nothing re-appears there.
    assert not hasattr(context.with_execution_id("exec_1"), "kb_context_metadata")


async def test_the_kb_tool_sends_no_context_metadata_to_hybrid_search():
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(
        return_value=[{"content": "c", "metadata": {"title": "Doc"}, "score": 0.5}]
    )
    llm_router = MagicMock()
    llm_router.route = AsyncMock(return_value=MagicMock(content="answer"))
    tool = DocumentQATool(vector_store, llm_router, UnifiedKBConfig())

    await tool.answer_question(
        question="rollback procedure",
        scope_id=None,
        k=5,
        filters={"$or": [{"scope": "global"}]},
    )

    kwargs = vector_store.hybrid_search.await_args.kwargs
    assert "context_metadata" not in kwargs
    assert "filter_mode" not in kwargs
