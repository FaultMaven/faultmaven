"""Forced tool use that the provider only REQUESTS (#1755).

Above Anthropic's forcing ceiling the adapter sends ``tool_choice="required"``
as ``auto`` plus an instruction, and ``forced_tool_choice_enforced`` says so.
The engine must then handle what a request can return: a reply without the
tool call is re-asked once before it fails the turn (single shot), and a DA
turn keeps its evidence tools through one prose reply (tool loop). A body is
truncated only when the provider said it was cut. With enforcement, nothing
changes. Every double here is a provider reporting the predicate both ways.
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    STRUCTURED_OUTPUT_MAX_TOKENS,
)
from faultmaven.infrastructure.llm.providers import StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from tests.model_content import disabled_redaction, vetted

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Answer(BaseModel):
    agent_response: str


PROSE = "The kubelet on node-3 is out of disk."


def _reply(content="", tool=None, args=None, stop=StopReason.STOP) -> LLMResponse:
    calls = None
    if tool is not None:
        calls = [
            ToolCall(
                id="call_1",
                type="function",
                function={"name": tool, "arguments": json.dumps(args or {})},
            )
        ]
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="anthropic",
        model="claude-opus-5-5",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=calls,
        stop_reason=stop,
    )


ANSWER = _reply(
    tool="_Answer", args={"agent_response": "disk"}, stop=StopReason.TOOL_CALLS
)


def _engine(enforced: bool, replies: list) -> tuple[MilestoneEngine, list]:
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=AsyncMock(), repository=repo, investigation_tools=MagicMock()
    )
    engine.deps.llm_error_handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    provider = engine.deps.llm_provider
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,
            response_format=None,
        )
    )
    provider.forced_tool_choice_enforced = Mock(return_value=enforced)
    queue, sent = list(replies), []

    async def _route(**kwargs):
        # The loop keeps appending to the list it sent: snapshot it.
        if "messages" in kwargs:
            kwargs["messages"] = list(kwargs["messages"])
        sent.append(kwargs)
        return queue.pop(0)

    provider.route = AsyncMock(side_effect=_route)
    return engine, sent


async def _single_shot(engine):
    return await engine.generator._generate_structured_output_inner(
        prompt="why is node-3 NotReady?",
        schema_model=_Answer,
        redaction_ctx=disabled_redaction(),
    )


# --- Single shot -------------------------------------------------------------


async def test_a_prose_reply_is_reasked_once_with_the_instruction():
    engine, sent = _engine(enforced=False, replies=[_reply(PROSE), ANSWER])

    result = await _single_shot(engine)

    assert result.agent_response == "disk"
    first, second = sent
    assert first["bypass_cache"] is False
    assert second["bypass_cache"] is True
    assert "prompt" not in second
    user, echo, nudge = second["messages"]
    assert user["content"] == first["prompt"]
    assert echo == {"role": "assistant", "content": PROSE}
    assert nudge["role"] == "user"
    assert "calling the `_Answer` tool" in nudge["content"]
    assert second["tools"] == first["tools"]
    assert second["tool_choice"] == "required"


async def test_two_misses_fail_after_two_calls_and_enforcement_never_reasks():
    engine, sent = _engine(enforced=False, replies=[_reply(PROSE), _reply(PROSE)])
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 2

    engine, sent = _engine(enforced=True, replies=[_reply(PROSE), ANSWER])
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 1


async def test_an_empty_body_is_truncation_only_when_the_provider_said_so():
    # END_TURN: not a cut, so no cap bump; the not-enforced double re-asks.
    engine, sent = _engine(enforced=False, replies=[_reply(""), ANSWER])
    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert "messages" in sent[1]

    # ...and the enforced double fails it at once rather than climbing.
    engine, sent = _engine(enforced=True, replies=[_reply(""), ANSWER])
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 1

    # MAX_TOKENS: the provider said it was cut, so the ladder still runs.
    cut = _reply("", stop=StopReason.MAX_TOKENS)
    engine, sent = _engine(enforced=False, replies=[cut, ANSWER])
    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert "messages" not in sent[1]


# --- DA tool loop ------------------------------------------------------------

SEARCH = {"type": "function", "function": {"name": "search_file", "parameters": {}}}


def _offered(call) -> list[str]:
    return [tool["function"]["name"] for tool in call["tools"]]


async def _da_turn(engine):
    return await engine.generator._tool_augmented_generate(
        prompt=vetted("Search the uploaded log"),
        schema_model=_Answer,
        investigation_tools=[SEARCH],
        tool_context=MagicMock(),
        force_tool_use=True,
        redaction_ctx=disabled_redaction(),
    )


async def test_a_da_turn_keeps_its_evidence_tools_through_one_prose_reply():
    engine, sent = _engine(
        enforced=False, replies=[_reply(PROSE), _reply(PROSE), ANSWER]
    )

    assert (await _da_turn(engine)).agent_response == "disk"
    first, second, third = sent
    assert _offered(second) == _offered(first) == ["search_file", "_Answer"]
    assert second["tool_choice"] == "required"
    nudge = second["messages"][-1]["content"]
    assert "`search_file`" in nudge and "`_Answer`" in nudge
    # A second consecutive prose reply takes today's schema-only path.
    assert _offered(third) == ["_Answer"]
    assert "calling the `_Answer` tool" in third["messages"][-1]["content"]

    # With enforcement, unchanged: one prose reply gets the schema-only call.
    engine, sent = _engine(enforced=True, replies=[_reply(PROSE), ANSWER])
    assert (await _da_turn(engine)).agent_response == "disk"
    assert _offered(sent[1]) == ["_Answer"]


# --- Router delegation -------------------------------------------------------


async def test_the_router_asks_the_provider_the_call_lands_on():
    primary = Mock(forced_tool_choice_enforced=Mock(return_value=True))
    override = Mock(forced_tool_choice_enforced=Mock(return_value=False))
    registry = MagicMock(_providers={"gemini": primary, "anthropic": override})
    registry.get_fallback_chain.return_value = ["gemini"]
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        router = LLMRouter()
        assert router.forced_tool_choice_enforced("m") is True
        assert router.forced_tool_choice_enforced("m", "anthropic") is False
        assert router.forced_tool_choice_enforced("m", "unregistered") is True
    override.forced_tool_choice_enforced.assert_called_once_with("m")
