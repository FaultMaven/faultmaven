"""Regression tests: Local provider parses tool_calls from the
OpenAI-compatible endpoint.

Audit 2026-06: LocalProvider claimed FUNCTION_CALLING for functionary/hermes
models, but ``_call_openai_compatible_api`` only read ``message["content"]`` and
ran ``_validate_response_content``, which raises on the empty content that a
function-calling response legitimately returns. The engine's FUNCTION_CALLING
strategy therefore got no tool_calls back and the turn failed. The fix mirrors
the OpenAI/Cohere providers: extract tool_calls and skip empty-content
validation when tool_calls are present.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider


@pytest.fixture
def local_config():
    return ProviderConfig(
        name="local",
        api_key=None,
        base_url="http://localhost:5000",
        models=["functionary-7b-v2"],
        default_model="functionary-7b-v2",
        timeout=30,
    )


def _mock_session(response_data: dict):
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.unit
@pytest.mark.asyncio
class TestLocalToolCallParsing:
    async def test_tool_call_with_empty_content_is_parsed(self, local_config):
        """A function-calling response (tool_calls + empty content) must be
        returned as tool_calls, not raise on empty content."""
        provider = LocalProvider(local_config)
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "emit_state",
                    "arguments": '{"state_updates": {"symptom_verified": true}}',
                },
            }
        ]
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    }
                }
            ],
            "usage": {"total_tokens": 42},
        }

        with patch("aiohttp.ClientSession", return_value=_mock_session(resp)):
            result = await provider.generate(
                "investigate", tools=[{"type": "function"}], tool_choice="required"
            )

        assert isinstance(result, LLMResponse)
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function["name"] == "emit_state"
        # Empty content falls back to the tool-call arguments (mirrors OpenAI).
        assert "state_updates" in result.content

    async def test_plain_text_response_still_validated(self, local_config):
        """Without tool_calls, normal content is returned as-is."""
        provider = LocalProvider(local_config)
        resp = {
            "choices": [{"message": {"role": "assistant", "content": "Hello there"}}],
            "usage": {"total_tokens": 5},
        }
        with patch("aiohttp.ClientSession", return_value=_mock_session(resp)):
            result = await provider.generate("hi")
        assert result.content == "Hello there"
        assert result.tool_calls is None
