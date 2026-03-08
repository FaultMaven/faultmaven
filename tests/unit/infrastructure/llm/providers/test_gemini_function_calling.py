"""
Tests for Gemini provider function calling and message conversion.

Validates:
- OpenAI tools -> Gemini functionDeclarations conversion
- tool_choice mapping (required -> ANY, auto -> AUTO)
- functionCall response parsing into ToolCall objects
- _convert_messages_to_gemini() message format conversion
- _calculate_confidence() with tool calls
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    ProviderConfig,
    ToolCall,
)
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider


@pytest.fixture
def gemini_config():
    """Minimal Gemini provider config for unit tests."""
    return ProviderConfig(
        name="gemini",
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        models=["gemini-1.5-pro"],
        default_model="gemini-1.5-pro",
        timeout=30,
        confidence_score=0.9,
    )


@pytest.fixture
def provider(gemini_config):
    """Create a GeminiProvider instance for testing."""
    return GeminiProvider(gemini_config)


def _mock_aiohttp_session(response_data: dict):
    """Create a properly nested mock for aiohttp.ClientSession().post() pattern."""
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


# =========================================================================
# _convert_messages_to_gemini tests
# =========================================================================


@pytest.mark.unit
class TestConvertMessagesToGemini:
    """Tests for _convert_messages_to_gemini()."""

    def test_system_messages_extracted_to_system_instruction(self, provider):
        """System messages should be extracted to 'system_instruction' field."""
        messages = [
            {"role": "system", "content": "You are a troubleshooting assistant."},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_gemini(messages)

        assert result["system_instruction"] == "You are a troubleshooting assistant."
        assert len(result["contents"]) == 1
        assert result["contents"][0]["role"] == "user"

    def test_multiple_system_messages_joined(self, provider):
        """Multiple system messages should be joined with double newlines."""
        messages = [
            {"role": "system", "content": "Rule 1"},
            {"role": "system", "content": "Rule 2"},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_gemini(messages)

        assert result["system_instruction"] == "Rule 1\n\nRule 2"

    def test_user_messages_converted_to_user_role_with_text_parts(self, provider):
        """User messages should have role 'user' with text parts."""
        messages = [
            {"role": "user", "content": "What is the issue?"},
        ]

        result = provider._convert_messages_to_gemini(messages)

        assert len(result["contents"]) == 1
        content = result["contents"][0]
        assert content["role"] == "user"
        assert content["parts"] == [{"text": "What is the issue?"}]
        assert "system_instruction" not in result

    def test_assistant_messages_converted_to_model_role(self, provider):
        """Assistant messages should have role 'model' with text parts."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        result = provider._convert_messages_to_gemini(messages)

        model_msg = result["contents"][1]
        assert model_msg["role"] == "model"
        assert model_msg["parts"] == [{"text": "Hi there!"}]

    def test_assistant_messages_with_tool_calls_to_function_call_parts(self, provider):
        """Assistant messages with tool_calls should produce functionCall parts."""
        messages = [
            {"role": "user", "content": "Search logs"},
            {
                "role": "assistant",
                "content": "Searching...",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "SSH error"}',
                        },
                    },
                ],
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        model_msg = result["contents"][1]
        assert model_msg["role"] == "model"
        assert len(model_msg["parts"]) == 2

        # First part: text
        assert model_msg["parts"][0] == {"text": "Searching..."}

        # Second part: functionCall
        fc_part = model_msg["parts"][1]
        assert "functionCall" in fc_part
        assert fc_part["functionCall"]["name"] == "search_file"
        assert fc_part["functionCall"]["args"] == {"query": "SSH error"}

    def test_assistant_tool_call_arguments_parsed_from_json_string(self, provider):
        """JSON string arguments should be parsed to dict."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "deep_analysis",
                            "arguments": '{"file_id": "ev_123"}',
                        },
                    },
                ],
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        # No text content, so only functionCall part
        model_msg = result["contents"][1]
        assert len(model_msg["parts"]) == 1
        assert model_msg["parts"][0]["functionCall"]["args"] == {"file_id": "ev_123"}

    def test_assistant_tool_call_invalid_json_fallback(self, provider):
        """Invalid JSON in arguments should fall back to empty dict."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": "not json",
                        },
                    },
                ],
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        fc = result["contents"][1]["parts"][0]["functionCall"]
        assert fc["args"] == {}

    def test_tool_messages_converted_to_function_role(self, provider):
        """Tool messages should become role 'function' with functionResponse parts."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool",
                "name": "search_file",
                "content": '{"results": ["error in line 42"]}',
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        fn_msg = result["contents"][1]
        assert fn_msg["role"] == "function"
        assert len(fn_msg["parts"]) == 1

        fn_resp = fn_msg["parts"][0]["functionResponse"]
        assert fn_resp["name"] == "search_file"
        assert fn_resp["response"] == {"results": ["error in line 42"]}

    def test_tool_messages_non_json_content_wrapped(self, provider):
        """Non-JSON tool content should be wrapped in {"result": content}."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool",
                "name": "search_file",
                "content": "Found 3 errors in syslog",
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        fn_resp = result["contents"][1]["parts"][0]["functionResponse"]
        assert fn_resp["response"] == {"result": "Found 3 errors in syslog"}

    def test_consecutive_function_responses_grouped(self, provider):
        """Consecutive tool messages should be grouped into a single function turn."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool",
                "name": "search_file",
                "content": "Result 1",
            },
            {
                "role": "tool",
                "name": "deep_analysis",
                "content": "Result 2",
            },
        ]

        result = provider._convert_messages_to_gemini(messages)

        # Should have: user, function (grouped)
        assert len(result["contents"]) == 2

        fn_msg = result["contents"][1]
        assert fn_msg["role"] == "function"
        assert len(fn_msg["parts"]) == 2
        assert fn_msg["parts"][0]["functionResponse"]["name"] == "search_file"
        assert fn_msg["parts"][1]["functionResponse"]["name"] == "deep_analysis"

    def test_empty_assistant_content_no_tool_calls_omitted(self, provider):
        """Assistant with empty content and no tool_calls should produce empty parts list (skipped)."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": ""},
        ]

        result = provider._convert_messages_to_gemini(messages)

        # Empty content + no tool_calls = no parts, message should not be appended
        assert len(result["contents"]) == 1
        assert result["contents"][0]["role"] == "user"


# =========================================================================
# Function calling integration in generate()
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestGeminiFunctionCalling:
    """Tests for function calling in generate()."""

    async def test_openai_tools_converted_to_function_declarations(self, provider):
        """OpenAI-format tools should be converted to Gemini functionDeclarations."""
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_file",
                    "description": "Search a file for patterns",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                        },
                        "required": ["query"],
                    },
                },
            },
        ]

        mock_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Searching..."}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 10},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("Test", tools=openai_tools, tool_choice="auto")

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # Check functionDeclarations format
            assert "tools" in request_body
            fn_decls = request_body["tools"][0]["functionDeclarations"]
            assert len(fn_decls) == 1
            assert fn_decls[0]["name"] == "search_file"
            assert fn_decls[0]["description"] == "Search a file for patterns"

    async def test_tool_choice_required_maps_to_any(self, provider):
        """tool_choice='required' should map to mode: 'ANY' in toolConfig."""
        tools = [
            {"type": "function", "function": {"name": "test_tool", "parameters": {}}},
        ]

        mock_resp = {
            "candidates": [
                {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"candidatesTokenCount": 5},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("Test", tools=tools, tool_choice="required")

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["toolConfig"] == {
                "functionCallingConfig": {"mode": "ANY"}
            }

    async def test_tool_choice_auto_maps_to_auto(self, provider):
        """tool_choice='auto' should map to mode: 'AUTO' in toolConfig."""
        tools = [
            {"type": "function", "function": {"name": "test_tool", "parameters": {}}},
        ]

        mock_resp = {
            "candidates": [
                {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"candidatesTokenCount": 5},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("Test", tools=tools, tool_choice="auto")

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["toolConfig"] == {
                "functionCallingConfig": {"mode": "AUTO"}
            }

    async def test_function_call_response_parsed_to_tool_call_objects(self, provider):
        """functionCall parts in response should be parsed into ToolCall objects."""
        mock_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "search_file",
                                    "args": {"query": "ssh error"},
                                }
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 20},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("Test")

            assert isinstance(result, LLMResponse)
            assert result.tool_calls is not None
            assert len(result.tool_calls) == 1

            tc = result.tool_calls[0]
            assert isinstance(tc, ToolCall)
            assert tc.type == "function"
            assert tc.function["name"] == "search_file"
            assert json.loads(tc.function["arguments"]) == {"query": "ssh error"}
            assert tc.id.startswith("call_")

    async def test_tool_calls_included_in_llm_response(self, provider):
        """tool_calls should be included in the LLMResponse dataclass."""
        mock_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Let me search."},
                            {
                                "functionCall": {
                                    "name": "search_file",
                                    "args": {"query": "error"},
                                }
                            },
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 15},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("Test")

            assert result.content == "Let me search."
            assert result.tool_calls is not None
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0].function["name"] == "search_file"

    async def test_messages_kwarg_converted_for_gemini(self, provider):
        """Messages kwarg should be converted to Gemini format."""
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]

        mock_resp = {
            "candidates": [
                {"content": {"parts": [{"text": "Hi!"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"candidatesTokenCount": 5},
        }
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("ignored", messages=messages)

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # System instruction should be extracted
            assert request_body["systemInstruction"] == {
                "parts": [{"text": "Be helpful."}]
            }
            # Contents should have converted messages
            assert len(request_body["contents"]) == 1
            assert request_body["contents"][0]["role"] == "user"


# =========================================================================
# _calculate_confidence with tool calls
# =========================================================================


@pytest.mark.unit
class TestGeminiConfidenceWithToolCalls:
    """Tests for _calculate_confidence() with has_valid_tool_calls parameter."""

    def test_empty_content_no_tool_calls_returns_zero(self, provider):
        """Empty content without tool calls should return 0.0."""
        response_data = {
            "candidates": [{"finishReason": "STOP"}],
        }
        confidence = provider._calculate_confidence(
            "gemini-1.5-pro", "", response_data, has_valid_tool_calls=False
        )
        assert confidence == 0.0

    def test_empty_content_with_valid_tool_calls_returns_model_confidence(
        self, provider
    ):
        """Empty content with valid tool calls should return model confidence (not zero)."""
        response_data = {
            "candidates": [{"finishReason": "STOP"}],
        }
        confidence = provider._calculate_confidence(
            "gemini-1.5-pro", "", response_data, has_valid_tool_calls=True
        )
        # gemini-1.5-pro maps to 0.92, with STOP finish reason it gets 1.05x
        # 0.92 * 1.05 = 0.966 -> capped at 1.0 would be 0.966
        assert confidence > 0.0
        assert confidence > 0.5

    def test_content_with_tool_calls_normal_confidence(self, provider):
        """Content with tool calls should calculate normally."""
        response_data = {
            "candidates": [{"finishReason": "STOP"}],
        }
        confidence = provider._calculate_confidence(
            "gemini-1.5-pro",
            "Here is some analysis content " * 20,  # >500 chars
            response_data,
            has_valid_tool_calls=True,
        )
        assert confidence > 0.0
        assert confidence <= 1.0


# =========================================================================
# _resolve_refs_for_gemini tests
# =========================================================================


@pytest.mark.unit
class TestResolveRefsForGemini:
    """Tests for _resolve_refs_for_gemini() schema resolution."""

    def test_strips_additional_properties_from_top_level(self):
        """additionalProperties should be removed at top level."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert "additionalProperties" not in result

    def test_strips_additional_properties_from_nested_objects(self):
        """additionalProperties should be removed from nested object schemas."""
        schema = {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert "additionalProperties" not in result
        assert "additionalProperties" not in result["properties"]["inner"]

    def test_strips_additional_properties_from_resolved_refs(self):
        """additionalProperties in $defs should be stripped after inlining."""
        schema = {
            "type": "object",
            "properties": {
                "inner": {"$ref": "#/$defs/Inner"},
            },
            "$defs": {
                "Inner": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                    "title": "Inner",
                },
            },
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        inner = result["properties"]["inner"]
        assert "additionalProperties" not in inner
        assert "$defs" not in result
        assert inner["type"] == "object"
        assert inner["properties"]["x"] == {"type": "integer"}

    def test_strips_title_and_default(self):
        """title and default should be stripped from all levels."""
        schema = {
            "type": "object",
            "title": "MyModel",
            "properties": {
                "field": {
                    "type": "string",
                    "title": "Field",
                    "default": "hello",
                },
            },
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert "title" not in result
        assert "title" not in result["properties"]["field"]
        assert "default" not in result["properties"]["field"]

    def test_strips_additional_properties_dict_form(self):
        """additionalProperties as a dict (schema) should also be stripped."""
        schema = {
            "type": "object",
            "properties": {
                "metadata": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert "additionalProperties" not in result["properties"]["metadata"]

    def test_preserves_supported_fields(self):
        """Gemini-supported fields (type, description, enum, nullable, etc.) should remain."""
        schema = {
            "type": "object",
            "description": "A model",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "inactive"],
                    "description": "Current status",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["status"],
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert result["type"] == "object"
        assert result["description"] == "A model"
        assert result["required"] == ["status"]
        assert result["properties"]["status"]["enum"] == ["active", "inactive"]
        assert result["properties"]["items"]["items"] == {"type": "string"}

    def test_deeply_nested_additional_properties_stripped(self):
        """additionalProperties deep in nested schemas should be stripped."""
        schema = {
            "type": "object",
            "properties": {
                "level1": {
                    "type": "object",
                    "properties": {
                        "level2": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        assert "additionalProperties" not in result
        assert "additionalProperties" not in result["properties"]["level1"]
        assert (
            "additionalProperties"
            not in result["properties"]["level1"]["properties"]["level2"]
        )

    def test_strips_unsupported_from_array_items(self):
        """Unsupported fields within array items should be stripped."""
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "additionalProperties": False,
                        "title": "Tag",
                    },
                },
            },
        }
        result = GeminiProvider._resolve_refs_for_gemini(schema)
        items = result["properties"]["tags"]["items"]
        assert "additionalProperties" not in items
        assert "title" not in items
