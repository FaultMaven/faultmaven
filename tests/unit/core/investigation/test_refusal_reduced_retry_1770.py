"""A refused structured call is re-issued once without ``evidence_trail`` (#1770).

A refusal is a response normalized to ``StopReason.CONTENT_FILTER``. Before
this, every layer read one as something else: the tool loop nudged it, then
fell back to the no-tools single shot, whose ladder doubled the cap on the
empty body, and #662's minimal-prompt degrade ran it twice more (six billed
calls). Now, on a schema that declares ``evidence_trail``, the refused call is
re-issued once, where it was refused, on the same provider, without the field
and never from cache; a second refusal fails with ``PROVIDER_REFUSED``.

Entered at ``generate_structured_output``. The DA provider is distinct from the
router double, so a call reaching the router is the no-tools fallback.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine import generation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    StructuredOutputGenerator,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
)
from faultmaven.exceptions import PROVIDER_REFUSED
from faultmaven.infrastructure.llm.providers import ReasoningIntent, StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from faultmaven.models.interfaces import ToolResult
from tests.model_content import disabled_redaction, vetted

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SCHEMAS = [
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    InvestigationResponse_General,
]
SEARCH = {"type": "function", "function": {"name": "search_file", "parameters": {}}}
ARGS = {"agent_response": "disk", "state_updates": {}}
LAST = StructuredOutputGenerator.MAX_TOOL_ITERATIONS


def _reply(content="", tool=None, stop=StopReason.STOP) -> LLMResponse:
    calls = None
    if tool is not None:
        args = {"query": "disk"} if tool == "search_file" else ARGS
        calls = [
            ToolCall(
                id=f"call_{tool}",
                type="function",
                function={"name": tool, "arguments": json.dumps(args)},
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


REFUSED = _reply("", stop=StopReason.CONTENT_FILTER)
SEARCHED = _reply(tool="search_file", stop=StopReason.TOOL_CALLS)


def _answer(schema=InvestigationResponse_Diagnosis):
    return _reply(tool=schema.__name__, stop=StopReason.TOOL_CALLS)


def _queue(replies):
    """A send recorder answering *replies* in order; it snapshots ``messages``
    (the loop keeps appending to the list it sent)."""
    queue, sent = list(replies), []

    async def _send(**kwargs):
        if kwargs.get("messages") is not None:
            kwargs["messages"] = list(kwargs["messages"])
        sent.append(kwargs)
        return queue.pop(0)

    return AsyncMock(side_effect=_send), sent


def _engine(capability=StructuredOutputCapability.FUNCTION_CALLING):
    registry = MagicMock()
    registry.execute_tool = AsyncMock(return_value=ToolResult(success=True, data="ok"))
    repo = MagicMock()
    repo.save = AsyncMock()
    da = AsyncMock()
    da.provider_name = "anthropic"
    da.supports_tool_calling = MagicMock(return_value=True)
    da.get_structured_output_capability = MagicMock(return_value=capability)
    engine = MilestoneEngine(
        llm_provider=AsyncMock(),
        repository=repo,
        investigation_tools=registry,
        da_provider=da,
    )
    engine.deps.llm_error_handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    engine.deps.llm_provider.get_structured_output_strategy = MagicMock(
        side_effect=lambda schema, model=None: create_strategy_for_capability(
            capability, schema
        )
    )
    return engine


def _case():
    case = MagicMock()
    case.case_id = "case_1770"
    return case


async def _turn(engine, schema=InvestigationResponse_Diagnosis, tools=True):
    """One structured call, with the #662 degrade armed (case + user message)."""
    kwargs = (
        dict(investigation_tools=[SEARCH], tool_context=MagicMock())
        if tools
        else dict(
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
        )
    )
    with patch(
        "faultmaven.core.investigation.prompts.templates.fallback."
        "get_fallback_prompt_for_case",
        return_value="MINIMAL FALLBACK PROMPT",
    ):
        return await engine.generator.generate_structured_output(
            "why is node-3 NotReady?",
            schema,
            case=_case(),
            user_message="node-3 went NotReady",
            redaction_ctx=disabled_redaction(),
            **kwargs,
        )


def _carries_the_field(request) -> bool:
    text = json.dumps(
        {k: request.get(k) for k in ("prompt", "messages", "tools", "response_format")},
        default=str,
    )
    return "evidence_trail" in text or "EvidenceTrail" in text


def _names(call):
    return [tool["function"]["name"] for tool in call["tools"]]


# --- The tool loop -------------------------------------------------------------


@pytest.mark.parametrize("schema", SCHEMAS)
async def test_a_refused_iteration_is_reissued_once_on_the_da_provider(schema):
    engine = _engine(StructuredOutputCapability.STRICT)  # the strict schema tool
    engine.deps.da_provider._generate, sent = _queue([REFUSED, _answer(schema)])
    engine.deps.llm_provider.route = AsyncMock()

    result = await _turn(engine, schema)

    assert result.agent_response == "disk" and result.evidence_trail is None
    first, second = sent
    engine.deps.llm_provider.route.assert_not_called()  # no no-tools fallback
    assert second["messages"] == first["messages"]  # no nudge appended
    assert _names(second) == _names(first) == ["search_file", schema.__name__]
    assert (second["tool_choice"], second["max_tokens"]) == (
        first["tool_choice"],
        first["max_tokens"],
    )
    assert _carries_the_field(first) and not _carries_the_field(second)


async def test_a_refusal_after_a_tool_call_keeps_the_tool_result():
    engine = _engine()
    replies = [SEARCHED, SEARCHED, REFUSED, _answer()]
    engine.deps.da_provider._generate, sent = _queue(replies)

    assert (await _turn(engine)).agent_response == "disk"
    assert len(sent) == 4
    assert sent[3]["messages"] == sent[2]["messages"]
    assert [m["role"] for m in sent[3]["messages"]].count("tool") == 2
    assert not _carries_the_field(sent[3])


async def test_a_refusal_on_the_final_iteration_is_still_reissued():
    engine = _engine()
    replies = [SEARCHED] * LAST + [REFUSED, _answer()]
    engine.deps.da_provider._generate, sent = _queue(replies)
    engine.deps.llm_provider.route = AsyncMock()

    assert (await _turn(engine)).agent_response == "disk"
    assert len(sent) == LAST + 2
    refused, reissued = sent[-2:]
    assert _names(reissued) == _names(refused) == ["InvestigationResponse_Diagnosis"]
    assert reissued["tool_choice"] == "required"
    assert reissued["messages"] == refused["messages"]
    engine.deps.llm_provider.route.assert_not_called()


async def test_a_refusal_on_a_forced_schema_iteration_is_still_reissued():
    prose = _reply("The kubelet is out of disk.")
    engine = _engine()
    engine.deps.da_provider._generate, sent = _queue([prose, REFUSED, _answer()])
    engine.deps.llm_provider.route = AsyncMock()

    assert (await _turn(engine)).agent_response == "disk"
    _, refused, reissued = sent
    assert _names(reissued) == _names(refused) == ["InvestigationResponse_Diagnosis"]
    assert reissued["messages"] == refused["messages"]
    engine.deps.llm_provider.route.assert_not_called()


@pytest.mark.parametrize("tools", [True, False], ids=["tool_loop", "single_shot"])
async def test_refused_twice_fails_after_two_calls(tools):
    engine = _engine()
    send, sent = _queue([REFUSED] * 6)
    if tools:
        engine.deps.da_provider._generate = send
        engine.deps.llm_provider.route = AsyncMock()
    else:
        engine.deps.llm_provider.route = send
    degrade = MagicMock()

    with (
        patch.object(generation, "prompt_context_recovery_total", degrade),
        pytest.raises(MilestoneEngineError) as raised,
    ):
        await _turn(engine, tools=tools)

    assert raised.value.error_code == PROVIDER_REFUSED
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == sent[1]["max_tokens"]  # no cap raise
    degrade.labels.assert_not_called()  # no #662 degrade
    if tools:
        engine.deps.llm_provider.route.assert_not_called()


# --- The single shot -----------------------------------------------------------


@pytest.mark.parametrize("content", ["", "I can't help with that."])
async def test_a_single_shot_refusal_is_reissued_once_past_the_cache(content):
    engine = _engine()
    refused = _reply(content, stop=StopReason.CONTENT_FILTER)
    engine.deps.llm_provider.route, sent = _queue([refused, _answer()])

    result = await _turn(engine, tools=False)

    assert result.agent_response == "disk" and result.evidence_trail is None
    first, second = sent
    assert (first["bypass_cache"], second["bypass_cache"]) == (False, True)
    assert second["max_tokens"] == first["max_tokens"]
    assert second["prompt"] == first["prompt"] and "messages" not in second
    assert second["reasoning_intent"] is ReasoningIntent.INFERENCE


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize(
    "capability",
    [
        StructuredOutputCapability.STRICT,
        StructuredOutputCapability.BEST_EFFORT,
        StructuredOutputCapability.FUNCTION_CALLING,
    ],
)
async def test_the_reduced_request_carries_no_trace_of_the_field(capability, schema):
    """Through the real strategy: prompt, ``response_format`` and tools."""
    engine = _engine(capability)
    engine.deps.llm_provider.route, sent = _queue([REFUSED, REFUSED])

    with pytest.raises(MilestoneEngineError):
        await _turn(engine, schema, tools=False)

    full, reduced = sent
    assert _carries_the_field(full), "positive control: the full request has it"
    assert not _carries_the_field(reduced)


@pytest.mark.parametrize("tools", [True, False], ids=["tool_loop", "single_shot"])
async def test_a_refusal_with_the_budget_spent_is_final(tools, monkeypatch):
    # ``raising=False``: on a tree without the name the test fails on the count.
    monkeypatch.setattr(
        generation, "spendable_turn_budget", lambda: -1.0, raising=False
    )
    engine = _engine()
    send, sent = _queue([REFUSED, _answer()])
    if tools:
        engine.deps.da_provider._generate = send
    else:
        engine.deps.llm_provider.route = send

    with pytest.raises(MilestoneEngineError) as raised:
        await _turn(engine, tools=tools)

    assert raised.value.error_code == PROVIDER_REFUSED
    assert len(sent) == 1


# --- The cache -----------------------------------------------------------------


async def test_a_refusal_is_not_stored_so_its_reissue_reaches_the_provider():
    """Through a real ``LLMRouter`` and ``LLMResponseCache``: a refusal carrying
    text at a storable confidence must not answer the identical call again."""
    from faultmaven.infrastructure.llm.cache import LLMResponseCache

    registry = MagicMock()
    registry.get_available_providers.return_value = ["anthropic"]
    registry.get_fallback_chain.return_value = ["anthropic"]
    registry.route_request = AsyncMock(
        side_effect=[_reply("I can't help.", stop=StopReason.CONTENT_FILTER), _answer()]
    )
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        router = LLMRouter()
        router.cache = LLMResponseCache()
        for _ in range(2):
            await router.route(prompt=vetted("p"), model="claude-opus-5-5")

    assert registry.route_request.await_count == 2
