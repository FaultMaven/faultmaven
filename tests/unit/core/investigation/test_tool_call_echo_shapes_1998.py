"""The tool loop echoes a tool call in the OpenAI chat shape, whatever shape the
server answered in (#1998 P2a, #2033 review F2).

``model_output`` rebuilds the echoed assistant message: ``content`` as text,
and each call's ``function`` as exactly ``name`` then ``arguments``, a JSON
string. A canonical server's echo is what it sent. A server that deviates —
arguments as an object, the keys in another order, an extra ``description:
null`` (older TGI) — is echoed in that normalised shape, which the
specification takes on input. And an adapter that copies an arguments OBJECT
into ``content`` (a tool call with no text) has it JSON-encoded: before that,
``model_output`` refused the dict and the turn failed after its first request.

The five shapes are the #2033 review's table. Each runs the real tool loop
over a router double (no live LLM call) and reads the echo the next request
carried.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.infrastructure.llm.dispatch import model_call_violations
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.infrastructure.security.model_content import (
    SOURCE_MODEL_OUTPUT,
    provenance,
)
from faultmaven.models.interfaces import ToolResult
from tests.model_content import disabled_redaction

pytestmark = [pytest.mark.unit, pytest.mark.security]

ARGUMENTS = '{"query": "q"}'

#: shape -> (the ``function`` the server answered with, the ``content`` the
#: adapter set, the ``content`` the echo carries).
SHAPES = {
    "canonical": (
        {"name": "search_file", "arguments": ARGUMENTS},
        ARGUMENTS,
        ARGUMENTS,
    ),
    "args_object_with_text": (
        {"name": "search_file", "arguments": {"query": "q"}},
        "I will search",
        "I will search",
    ),
    "args_object_no_text": (
        {"name": "search_file", "arguments": {"query": "q"}},
        {"query": "q"},
        ARGUMENTS,
    ),
    "arguments_first": (
        {"arguments": ARGUMENTS, "name": "search_file"},
        ARGUMENTS,
        ARGUMENTS,
    ),
    "extra_function_key": (
        {"name": "search_file", "arguments": ARGUMENTS, "description": None},
        ARGUMENTS,
        ARGUMENTS,
    ),
}

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_file",
        "description": "Search evidence",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


class _Schema(BaseModel):
    agent_response: str = "ok"


def _response(content, tool_calls) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=tool_calls,
    )


def _engine(generate) -> MilestoneEngine:
    provider = AsyncMock()
    provider.supports_tool_calling = Mock(return_value=True)
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,
            response_format=None,
            extra_config={},
        )
    )
    provider.route = AsyncMock(side_effect=generate)
    registry = MagicMock()
    registry.get_all_tools.return_value = []
    registry.execute_tool = AsyncMock(
        return_value=ToolResult(success=True, data="found it")
    )
    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=registry
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_the_echo_is_the_normalised_shape(shape):
    function, content, echoed_content = SHAPES[shape]
    sent: list[dict] = []

    async def generate(**kwargs):
        sent.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(sent) == 1:
            return _response(
                content,
                [ToolCall(id="call_abc123", type="function", function=dict(function))],
            )
        return _response(
            "",
            [
                ToolCall(
                    id="call_schema",
                    type="function",
                    function={
                        "name": "_Schema",
                        "arguments": json.dumps({"agent_response": "done"}),
                    },
                )
            ],
        )

    result = await _engine(generate).generator.generate_structured_output(
        "PROMPT",
        _Schema,
        investigation_tools=[_SEARCH_TOOL],
        tool_context=MagicMock(),
        redaction_ctx=disabled_redaction(),
    )

    assert result.agent_response == "done"
    assert len(sent) == 2
    [echo] = [m for m in sent[1]["messages"] if m["role"] == "assistant"]
    assert echo == {
        "role": "assistant",
        "content": echoed_content,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "search_file", "arguments": ARGUMENTS},
            }
        ],
    }
    # Exactly name then arguments, in that order: the wire order.
    assert list(echo["tool_calls"][0]["function"]) == ["name", "arguments"]
    assert type(echo["content"]) is not dict
    for minted in (echo["content"], echo["tool_calls"][0]["function"]["arguments"]):
        assert provenance(minted) == (SOURCE_MODEL_OUTPUT, False)
    assert model_call_violations(None, sent[1]["messages"], {}) == []
