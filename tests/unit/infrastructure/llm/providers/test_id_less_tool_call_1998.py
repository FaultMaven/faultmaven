"""An id-less tool call completes the real tool loop through the dispatch (#1998 P3).

An OpenAI-compatible server that issues no tool-call id (some local and
self-hosted stacks) is read by ``_extract_tool_calls_from_message`` with
``id=""``. The tool loop echoes the call back as the assistant message's
``tool_calls[].id`` and the tool result's ``tool_call_id``, so the loop's next
request carries ``""`` in both identifier channels. Under refusal, an
identifier check that required one character refused that request: the tool
loop failed on every such server. The empty identifier carries no content and
is sent as the server returned it, so it is admitted (``IDENTIFIER_RE``).

Driven here end to end: the real generator and tool loop, a dedicated DA
provider that is the real ``LocalProvider`` on its OpenAI-compatible transport
(so every request passes ``dispatch.send_to_model``), and only aiohttp a
double. No connection is made.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.models.interfaces import ToolResult
from tests.model_content import disabled_redaction
from tests.utils import sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

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


def _completion_without_an_id(name: str, arguments: str) -> dict:
    """A chat completion whose one tool call carries no ``id``."""
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


def _answer(payload: dict):
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value=payload)
    response.text = AsyncMock(return_value="")
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


@pytest.fixture(autouse=True)
def _no_network():
    with sockets_refused():
        yield


async def test_an_id_less_tool_call_completes_the_tool_loop():
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(
        side_effect=[
            _answer(_completion_without_an_id("search_file", '{"query": "q"}')),
            _answer(
                _completion_without_an_id(
                    "_Schema", json.dumps({"agent_response": "done"})
                )
            ),
        ]
    )
    local = LocalProvider(
        ProviderConfig(
            name="local",
            api_key="",
            base_url="http://127.0.0.1:9/v1",
            models=["local-model"],
            default_model="local-model",
        )
    )
    tools = MagicMock()
    tools.get_all_tools.return_value = []
    tools.execute_tool = AsyncMock(
        return_value=ToolResult(success=True, data="found it")
    )
    repository = MagicMock()
    repository.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=MagicMock(),
        repository=repository,
        investigation_tools=tools,
        da_provider=local,
        da_model="local-model",
    )

    with patch("aiohttp.ClientSession", return_value=session):
        result = await engine.generator.generate_structured_output(
            "PROMPT",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=disabled_redaction(),
        )

    assert result.agent_response == "done"
    tools.execute_tool.assert_awaited_once()
    assert session.post.call_count == 2, "the loop's second request was not sent"
    second = session.post.call_args_list[1].kwargs["json"]
    [echo] = [m for m in second["messages"] if m["role"] == "assistant"]
    [result_row] = [m for m in second["messages"] if m["role"] == "tool"]
    # The wire is the server's own: the empty id, unchanged, in both channels.
    assert echo["tool_calls"][0]["id"] == ""
    assert result_row["tool_call_id"] == ""
