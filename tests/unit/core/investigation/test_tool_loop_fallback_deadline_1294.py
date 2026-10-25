"""#1294: a turn with tools pays the tool loop's attempt, then a fresh ladder.

On a turn with tools, the tool loop's first attempt reaches the provider
OUTSIDE any retry ladder: whatever it fails with becomes
``ToolCallingUnsupportedError``, and ``_generate_structured_output_inner`` falls
back to the tool-less path, whose ``with_retry`` ladder starts fresh. Against a
hung provider that is the tool attempt plus the deadline-shaped ladder's own two
attempts. This pins that behaviour as shipped; telling the ladder about the
paid attempt is #2319, which changes this count on purpose.

Driven through the real method with a fake provider (no live LLM call),
counting what the provider RECEIVED.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.exceptions import ExternalCallTimeout
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_hung_provider_costs_the_tool_attempt_then_a_fresh_ladder():
    calls = {"tool_loop": 0, "single_shot": 0}

    async def generate(**kwargs):
        calls["tool_loop" if kwargs.get("messages") else "single_shot"] += 1
        # What the router raises when a provider never answers.
        raise ExternalCallTimeout(
            "External call to LLM_Providers.route_llm_request timed out after 30.0s",
            service="LLM_Providers",
            operation="route_llm_request",
            timeout=30.0,
        )

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
    engine = MilestoneEngine(
        llm_provider=provider, repository=MagicMock(), investigation_tools=None
    )
    engine.deps.llm_error_handler = LLMErrorHandler(RetryConfig(base_delay_seconds=0.0))

    with pytest.raises(MilestoneEngineError) as raised:
        await engine.generator._generate_structured_output_inner(
            "prompt",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=CaseRedactionContext(
                case_id="c-1294", sanitizer=None, enabled=False
            ),
        )

    # The tool attempt, then the ladder's own one retry: 1 + 2, not 1 + 4.
    assert calls == {"tool_loop": 1, "single_shot": 2}
    assert raised.value.error_code == "RETRY_EXHAUSTED"
