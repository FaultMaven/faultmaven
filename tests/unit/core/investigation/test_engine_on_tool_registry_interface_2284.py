"""The milestone engine runs on a mock of the tool-registry interface (#2284).

``MilestoneEngine`` took ``investigation_tools: Any`` and called
``get_all_tools`` and ``execute_tool`` on it, and no interface declared
either, so a test could only spec its registry on the concrete
``AgentToolRegistry``. The engine is now typed against
``IAgentToolRegistry``; these drive the two engine paths that reach the
registry without a model call, on ``create_autospec`` of that interface, so a
registry call the interface does not declare fails here.
"""

from unittest.mock import MagicMock, create_autospec, sentinel

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.models.interfaces import IAgentToolRegistry, ILLMProvider, ToolResult
from faultmaven.modules.agent.tools.base import AgentTool

pytestmark = pytest.mark.unit


def _tool(name: str) -> AgentTool:
    tool = create_autospec(AgentTool, instance=True)
    tool.get_schema.return_value = {
        "name": name,
        "description": f"the {name} tool",
        "parameters": {"type": "object", "properties": {}},
    }
    return tool


def _engine(registry: IAgentToolRegistry) -> MilestoneEngine:
    return MilestoneEngine(
        llm_provider=create_autospec(ILLMProvider, instance=True),
        repository=MagicMock(),
        investigation_tools=registry,
    )


def test_tool_schemas_are_built_from_the_registry_interface() -> None:
    registry = create_autospec(IAgentToolRegistry, instance=True)
    registry.get_all_tools.return_value = [_tool("search_file"), _tool("kb_qa")]
    generator = _engine(registry).generator

    every = generator.build_da_tool_schemas()
    narrowed = generator.build_da_tool_schemas(allowed=frozenset({"kb_qa"}))

    assert [s["function"]["name"] for s in every] == ["search_file", "kb_qa"]
    assert all(s["type"] == "function" for s in every)
    assert [s["function"]["name"] for s in narrowed] == ["kb_qa"]


async def test_vectorization_executes_through_the_registry_interface() -> None:
    registry = create_autospec(IAgentToolRegistry, instance=True)
    # A completed run that indexed nothing: the vectorizer must not report
    # the file searchable, and so never reaches the repository (#941).
    registry.execute_tool.return_value = ToolResult(
        success=True, data={"indexed": False, "message": "no chunkable content"}
    )
    engine = _engine(registry)

    indexed = await engine.vectorizer._vectorize_evidence("ev-1", sentinel.tool_context)

    assert indexed is False
    registry.execute_tool.assert_awaited_once_with(
        "vectorize_file", {"evidence_id": "ev-1"}, sentinel.tool_context
    )
