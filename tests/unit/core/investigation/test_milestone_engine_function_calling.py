"""
Test milestone_engine with FUNCTION_CALLING capability mode

Verifies that the engine correctly handles providers that use function/tool calling
for structured output (like Anthropic Claude).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
)


class ResponseSchema(BaseModel):
    """Response schema for function calling tests"""

    message: str
    value: int


class MockFunctionCallingProvider(ILLMProvider):
    """Mock provider that uses FUNCTION_CALLING mode"""

    async def generate(self, prompt, **kwargs):
        # Simulate Anthropic-style response with tool_calls
        tool_calls = kwargs.get("tools")

        if tool_calls:
            # Return mock LLMResponse with tool_calls
            return LLMResponse(
                content="",  # Content may be empty when tool_calls present
                confidence=0.95,
                provider="mock_anthropic",
                model="claude-3-opus",
                tokens_used=100,
                response_time_ms=500,
                tool_calls=[
                    ToolCall(
                        id="call_123",
                        type="function",
                        function={
                            "name": "ResponseSchema",
                            "arguments": json.dumps(
                                {"message": "Function calling works", "value": 42}
                            ),
                        },
                    )
                ],
            )
        else:
            # Fallback to regular JSON response
            return json.dumps({"message": "Regular response", "value": 0})

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return "{}"

    def get_structured_output_strategy(self, schema):
        """Return FUNCTION_CALLING strategy"""
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,  # Schema goes in tools
            response_format=None,  # Don't use response_format
            extra_config={},
        )


@pytest.fixture
def function_calling_provider():
    """Provider that uses function calling mode"""
    provider = MockFunctionCallingProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    return provider


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    return repo


@pytest.fixture
def base_case():
    return Case(
        case_id="case_fc1234567890",  # Exactly 17 chars
        title="Test Function Calling",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        enterprise_id="org_123",
        description="Test function calling mode",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


class TestMilestoneEngineFunctionCalling:
    @pytest.mark.asyncio
    async def test_function_calling_mode_uses_tools(
        self, function_calling_provider, mock_repo, base_case
    ):
        """Test that FUNCTION_CALLING mode passes tools parameter"""
        engine = MilestoneEngine(
            function_calling_provider,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # This will fail validation but we just want to check the parameters passed
        try:
            result = await engine.process_turn(base_case, "Analyze the issue")
        except Exception:
            pass  # Expected - mock returns wrong schema

        # Verify that generate was called with tools parameter
        function_calling_provider.generate.assert_called()
        call_kwargs = function_calling_provider.generate.call_args[1]

        # Should have tools parameter
        assert "tools" in call_kwargs
        assert isinstance(call_kwargs["tools"], list)
        assert len(call_kwargs["tools"]) > 0

        # Should have tool_choice parameter
        assert "tool_choice" in call_kwargs
        assert call_kwargs["tool_choice"] == "required"

        # Should NOT have response_format parameter
        assert "response_format" not in call_kwargs

    @pytest.mark.asyncio
    async def test_function_calling_no_schema_in_prompt(
        self, function_calling_provider, mock_repo, base_case
    ):
        """Test that FUNCTION_CALLING mode doesn't include schema in prompt"""
        engine = MilestoneEngine(
            function_calling_provider,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # This will fail validation but we just want to check the prompt
        try:
            await engine.process_turn(base_case, "Analyze the issue")
        except Exception:
            pass  # Expected - mock returns wrong schema

        # Get the prompt that was sent
        call_kwargs = function_calling_provider.generate.call_args[1]
        prompt = call_kwargs["prompt"]

        # Should NOT include "## RESPONSE FORMAT" section
        assert "## RESPONSE FORMAT" not in prompt
        assert "You MUST respond with valid JSON" not in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
