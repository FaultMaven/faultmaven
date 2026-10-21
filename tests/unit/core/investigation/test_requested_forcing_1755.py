"""A forced call whose reply carries no tool call (#1755).

The engine knows it sent ``tool_choice="required"``. A reply that carries no
tool call and was neither cut (``MAX_TOKENS``) nor refused (``CONTENT_FILTER``)
is forcing that did not hold, whatever provider answered it: Anthropic above
its forcing ceiling sends ``auto`` plus an instruction, and an
OpenAI-compatible server may ignore ``tool_choice``. The single shot re-asks it
once; a DA iteration that is not the last nudges it with every tool. Every
other strategy, and every reply that carries the tool call, behaves as on
``main``. The provider predicate is for the startup advisory only, so the
doubles here set it to the answer the PR's first head would have acted on.
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    STRUCTURED_OUTPUT_MAX_TOKENS,
    StructuredOutputGenerator,
)
from faultmaven.infrastructure.llm.providers import StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ToolResult
from tests.model_content import disabled_redaction, vetted

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Answer(BaseModel):
    agent_response: str


PROSE = "The kubelet on node-3 is out of disk."
ECHO_NUDGE = "Use the text above"
TOOLS_NUDGE = "Call one of the offered tools (`search_file`)"


def _reply(content="", tool=None, args=None, stop=StopReason.STOP) -> LLMResponse:
    calls = None
    if tool is not None:
        calls = [
            ToolCall(
                id=f"call_{tool}",
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
SEARCH_CALL = _reply(
    tool="search_file", args={"query": "disk"}, stop=StopReason.TOOL_CALLS
)


def _engine(replies, *, reports_enforced, mode=StructuredOutputMode.FUNCTION_CALLING):
    registry = MagicMock()
    registry.execute_tool = AsyncMock(return_value=ToolResult(success=True, data="ok"))
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=AsyncMock(), repository=repo, investigation_tools=registry
    )
    engine.deps.llm_error_handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    provider = engine.deps.llm_provider
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=mode,
            include_schema_in_prompt=False,
            response_format=None,
        )
    )
    provider.forced_tool_choice_enforced = Mock(return_value=reports_enforced)
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


async def test_a_prose_reply_is_reasked_once_whatever_provider_answered():
    engine, sent = _engine([_reply(PROSE), ANSWER], reports_enforced=True)

    assert (await _single_shot(engine)).agent_response == "disk"
    first, second = sent
    user, echo, nudge = second["messages"]
    assert user["content"] == first["prompt"] and "prompt" not in second
    assert echo == {"role": "assistant", "content": PROSE}
    assert "calling the `_Answer` tool" in nudge["content"]
    assert ECHO_NUDGE in nudge["content"]
    assert (second["tools"], second["tool_choice"]) == (first["tools"], "required")
    engine.deps.llm_provider.forced_tool_choice_enforced.assert_not_called()


async def test_two_misses_fail_after_two_calls():
    engine, sent = _engine([_reply(PROSE), _reply(PROSE)], reports_enforced=True)
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 2


async def test_an_empty_reply_is_reasked_with_no_echo_and_no_cap_bump():
    engine, sent = _engine([_reply(""), ANSWER], reports_enforced=False)

    assert (await _single_shot(engine)).agent_response == "disk"
    first, second = sent
    assert second["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    [only] = second["messages"]
    assert only["role"] == "user"
    assert only["content"].startswith(first["prompt"])
    assert "calling the `_Answer` tool" in only["content"]
    assert ECHO_NUDGE not in only["content"]


async def test_an_empty_stop_reply_under_strict_still_climbs_the_ladder():
    """Every non-FUNCTION_CALLING strategy behaves as on main."""
    engine, sent = _engine(
        [_reply(""), _reply('{"agent_response": "disk"}')],
        reports_enforced=True,
        mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
    )

    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert sent[1]["bypass_cache"] is True and "messages" not in sent[1]


async def test_a_refused_or_cut_reply_is_not_reasked():
    refusal = _reply("I can't help with that.", stop=StopReason.CONTENT_FILTER)
    engine, sent = _engine([refusal, ANSWER], reports_enforced=False)
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 1

    cut = _reply("", stop=StopReason.MAX_TOKENS)
    engine, sent = _engine([cut, ANSWER], reports_enforced=False)
    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert "messages" not in sent[1]


async def test_the_reasked_echo_is_bounded_like_a_tool_result():
    cap = StructuredOutputGenerator.TOOL_RESULT_MAX_CHARS
    engine, sent = _engine([_reply("x" * (cap * 3)), ANSWER], reports_enforced=True)

    assert (await _single_shot(engine)).agent_response == "disk"
    echo = sent[1]["messages"][1]["content"]
    assert echo == "x" * cap + "\n[truncated]"


async def test_a_whitespace_reply_is_reasked_as_empty():
    engine, sent = _engine([_reply("\n\n"), ANSWER], reports_enforced=True)

    assert (await _single_shot(engine)).agent_response == "disk"
    [only] = sent[1]["messages"]
    assert only["role"] == "user" and ECHO_NUDGE not in only["content"]


async def test_a_tool_call_whose_arguments_fail_to_parse_is_not_reasked():
    bad = _reply(tool="_Answer", stop=StopReason.TOOL_CALLS)
    bad.tool_calls[0].function["arguments"] = '{agent_response: "disk"}'
    engine, sent = _engine([bad, ANSWER], reports_enforced=False)
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)
    assert len(sent) == 1


async def test_an_empty_reply_is_reasked_once_per_call_across_the_ladder():
    engine, sent = _engine([_reply("")] * 6, reports_enforced=True)
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine)

    shapes = [(c["max_tokens"], "messages" in c) for c in sent]
    cap = STRUCTURED_OUTPUT_MAX_TOKENS
    # main's ladder (cap, 2 x cap) plus exactly one re-ask, at the first cap.
    assert shapes == [(cap, False), (cap, True), (cap * 2, False)]


async def test_with_no_stop_reason_a_cut_looking_body_ladders_and_prose_is_reasked():
    cut = _reply('{"agent_response": "the kubelet', stop=StopReason.UNKNOWN)
    engine, sent = _engine([cut, ANSWER], reports_enforced=True)
    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert sent[1]["bypass_cache"] is True and "messages" not in sent[1]

    prose = _reply(PROSE, stop=StopReason.UNKNOWN)
    engine, sent = _engine([prose, ANSWER], reports_enforced=True)
    assert (await _single_shot(engine)).agent_response == "disk"
    assert sent[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert sent[1]["messages"][1]["content"] == PROSE


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


async def test_a_da_turn_keeps_every_tool_through_one_reply_without_a_tool_call():
    replies = [_reply(PROSE), _reply(PROSE), ANSWER]
    engine, sent = _engine(replies, reports_enforced=True)

    assert (await _da_turn(engine)).agent_response == "disk"
    first, second, third = sent
    assert _offered(second) == _offered(first) == ["search_file", "_Answer"]
    assert second["tool_choice"] == "required"
    assert TOOLS_NUDGE in second["messages"][-1]["content"]
    # A second consecutive one takes the schema-only path.
    assert _offered(third) == ["_Answer"]
    assert "calling the `_Answer` tool" in third["messages"][-1]["content"]


@pytest.mark.parametrize(
    "stop,calls",  # the loop retries a cut once at a bigger cap
    [(StopReason.CONTENT_FILTER, 1), (StopReason.MAX_TOKENS, 2)],
)
async def test_a_refused_or_cut_da_reply_takes_the_schema_only_path(stop, calls):
    replies = [_reply(PROSE, stop=stop)] * calls + [ANSWER]
    engine, sent = _engine(replies, reports_enforced=True)

    assert (await _da_turn(engine)).agent_response == "disk"
    assert _offered(sent[-1]) == ["_Answer"]
    assert TOOLS_NUDGE not in sent[-1]["messages"][-1]["content"]


@pytest.mark.parametrize("blank", ["", "\n\n"])
async def test_a_blank_da_reply_is_not_echoed_and_its_nudge_is_its_own_turn(blank):
    engine, sent = _engine([_reply(blank), ANSWER], reports_enforced=True)
    assert (await _da_turn(engine)).agent_response == "disk"
    first, second = sent
    assert [m["role"] for m in second["messages"]] == ["system", "user", "user"]
    nudge = second["messages"][-1]["content"]
    assert TOOLS_NUDGE in nudge and ECHO_NUDGE not in nudge
    # Append-only: no earlier message dict was replaced.
    assert all(a is b for a, b in zip(first["messages"], second["messages"]))

    engine, sent = _engine([SEARCH_CALL, _reply(blank), ANSWER], reports_enforced=True)
    assert (await _da_turn(engine)).agent_response == "disk"
    *_, tool_turn, nudge = sent[2]["messages"]
    assert (tool_turn["role"], nudge["role"]) == ("tool", "user")
    assert TOOLS_NUDGE in nudge["content"] and ECHO_NUDGE not in nudge["content"]


async def test_with_no_stop_reason_a_cut_looking_da_body_takes_todays_path():
    cut = _reply('{"agent_response": "the kubelet', stop=StopReason.UNKNOWN)
    engine, sent = _engine([cut, ANSWER], reports_enforced=True)
    assert (await _da_turn(engine)).agent_response == "disk"
    assert [_offered(call) for call in sent] == [
        ["search_file", "_Answer"],
        ["_Answer"],
    ]

    # A fenced cut body is judged on its payload, not on the fence.
    fenced = _reply('```json\n{"agent_response": "the kubelet', stop=StopReason.UNKNOWN)
    engine, sent = _engine([fenced, ANSWER], reports_enforced=True)
    assert (await _da_turn(engine)).agent_response == "disk"
    assert _offered(sent[1]) == ["_Answer"]

    # Prose, and a complete object, did not run out: every tool stays.
    for body in (PROSE, '{"agent_response": "the kubelet"}'):
        engine, sent = _engine(
            [_reply(body, stop=StopReason.UNKNOWN), ANSWER], reports_enforced=True
        )
        assert (await _da_turn(engine)).agent_response == "disk"
        assert _offered(sent[1]) == ["search_file", "_Answer"]


async def test_the_reply_before_the_final_iteration_gets_the_schema_nudge():
    last = StructuredOutputGenerator.MAX_TOOL_ITERATIONS
    replies = [SEARCH_CALL] * (last - 1) + [_reply(PROSE), ANSWER]
    engine, sent = _engine(replies, reports_enforced=False)

    assert (await _da_turn(engine)).agent_response == "disk"
    final = sent[last]
    assert _offered(final) == ["_Answer"]
    assert "calling the `_Answer` tool" in final["messages"][-1]["content"]
    assert TOOLS_NUDGE not in final["messages"][-1]["content"]
