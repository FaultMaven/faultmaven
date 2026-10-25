"""#1294: the tool loop's failure is a paid attempt the fallback ladder hears about.

On a turn with tools, the tool loop's first attempt reaches the provider
OUTSIDE any retry ladder: whatever it fails with becomes
``ToolCallingUnsupportedError`` (chained ``from`` the provider's failure), and
``_generate_structured_output_inner`` falls back to the tool-less path, whose
``with_retry`` ladder used to meet a hung provider as fresh. A hang then cost the
tool attempt plus the whole deadline ladder: three full deadlines where a
deadline-shaped failure is owed one retry.

So the invariant is per structured generation, not per ladder: a
deadline-shaped failure is retried at most ``max_deadline_retries`` times across
the tool loop and its fallback. Each test drives the real method with a fake
provider (no live LLM call) and counts what the provider RECEIVED.
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.exceptions import ExternalCallTimeout, LLMException
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext


class _Schema(BaseModel):
    agent_response: str = "ok"


_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_file",
        "description": "Search evidence",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def _hang():
    """What the router raises when a provider never answers."""
    return ExternalCallTimeout(
        "External call to LLM_Providers.route_llm_request timed out after 30.0s",
        service="LLM_Providers",
        operation="route_llm_request",
        timeout=30.0,
    )


def _adapter_timeout():
    """What an adapter raises when its own client timeout fires."""
    return LLMException("provider read timed out", status_code=504)


def _busy():
    return LLMException("upstream 503 over capacity", status_code=503)


def _response(tool_calls):
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=tool_calls,
    )


def _schema_call():
    return _response(
        [
            ToolCall(
                id="call_schema",
                type="function",
                function={
                    "name": "_Schema",
                    "arguments": json.dumps({"agent_response": "fell back"}),
                },
            )
        ]
    )


def _engine(generate):
    """One provider for every call, tool-capable, single shot by function
    calling; the shipped retry counts with the backoffs taken out."""
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
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=None
    )
    engine.deps.llm_error_handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.0))
    return engine


async def _generate(engine):
    return await engine.generator._generate_structured_output_inner(
        "prompt",
        _Schema,
        investigation_tools=[_SEARCH_TOOL],
        tool_context=MagicMock(),
        redaction_ctx=CaseRedactionContext(
            case_id="c-1294", sanitizer=None, enabled=False
        ),
    )


def _counting(tool_loop, single_shot):
    """A provider answering the tool loop (``messages``) and the tool-less
    path (a ``prompt``) each with its own behaviour, counting both."""
    calls = {"tool_loop": 0, "single_shot": 0}

    async def generate(**kwargs):
        path = "tool_loop" if kwargs.get("messages") else "single_shot"
        calls[path] += 1
        behaviour = tool_loop if path == "tool_loop" else single_shot
        outcome = behaviour()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return generate, calls


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheFallbackCarriesThePaidAttempt:
    @pytest.mark.parametrize(
        "deadline", [_hang, _adapter_timeout], ids=["router_timeout", "504"]
    )
    async def test_a_hung_provider_gets_one_retry_across_the_fallback(self, deadline):
        """The tool attempt, then ONE fallback attempt: two in all, not three."""
        generate, calls = _counting(tool_loop=deadline, single_shot=deadline)
        engine = _engine(generate)

        with pytest.raises(MilestoneEngineError) as raised:
            await _generate(engine)

        assert calls == {"tool_loop": 1, "single_shot": 1}
        assert raised.value.error_code == "RETRY_EXHAUSTED"

    async def test_the_carried_retry_is_still_made(self):
        """Never zero (#1287): the fallback's one attempt IS the retry, and a
        provider that answers it is answered."""
        generate, calls = _counting(tool_loop=_hang, single_shot=_schema_call)
        engine = _engine(generate)

        result = await _generate(engine)

        assert result.agent_response == "fell back"
        assert calls == {"tool_loop": 1, "single_shot": 1}

    async def test_a_status_shaped_tool_failure_keeps_the_full_fallback_ladder(
        self,
    ):
        """A 503 in the tool loop spends nothing of the deadline class: the
        fallback ladder makes its four attempts."""
        generate, calls = _counting(tool_loop=_busy, single_shot=_busy)
        engine = _engine(generate)

        with pytest.raises(MilestoneEngineError) as raised:
            await _generate(engine)

        assert calls == {"tool_loop": 1, "single_shot": 4}
        assert raised.value.error_code == "RETRY_EXHAUSTED"

    async def test_a_tool_loop_refusal_that_timed_out_nothing_passes_none_on(self):
        """The loop also falls back when the model will not call the schema
        tool. Nothing spent a deadline, so a hang that follows on the tool-less
        path gets its own retry."""
        generate, calls = _counting(
            tool_loop=lambda: _response(None), single_shot=_hang
        )
        engine = _engine(generate)

        with pytest.raises(MilestoneEngineError) as raised:
            await _generate(engine)

        # Two loop iterations: the reply, then the forced-schema re-ask.
        assert calls == {"tool_loop": 2, "single_shot": 2}
        assert raised.value.error_code == "RETRY_EXHAUSTED"
