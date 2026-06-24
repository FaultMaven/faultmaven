"""Unit tests for the shared BaseLLMProvider._extract_tool_calls_from_message
helper.

Consolidates the OpenAI-style tool_call extraction that previously lived
(duplicated) in the OpenAI/OpenRouter/Groq/Fireworks/Cohere/Local providers.
Defensive .get() access keeps a malformed tool_call (missing id/type/function,
possible from less-strict local servers) from raising KeyError and failing the
turn.
"""

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    ToolCall,
)

extract = BaseLLMProvider._extract_tool_calls_from_message


class TestExtractToolCalls:
    def test_no_tool_calls_returns_none(self):
        assert extract({"content": "hi"}) is None
        assert extract({"tool_calls": []}) is None
        assert extract({"tool_calls": None}) is None

    def test_well_formed_tool_call(self):
        msg = {
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ]
        }
        result = extract(msg)
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], ToolCall)
        assert result[0].id == "call_1"
        assert result[0].function["name"] == "f"

    def test_malformed_tool_call_does_not_raise(self):
        """Missing id/type/function must degrade, not KeyError."""
        msg = {"tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}
        result = extract(msg)
        assert result is not None
        assert result[0].id == ""  # defaulted
        assert result[0].type == "function"  # defaulted
        assert result[0].function["name"] == "f"

    def test_completely_empty_tool_call_degrades(self):
        result = extract({"tool_calls": [{}]})
        assert result is not None
        assert result[0].id == ""
        assert result[0].type == "function"
        assert result[0].function == {}

    def test_multiple_tool_calls(self):
        msg = {
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "x"}},
                {"id": "b", "type": "function", "function": {"name": "y"}},
            ]
        }
        result = extract(msg)
        assert [tc.id for tc in result] == ["a", "b"]
