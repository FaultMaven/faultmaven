"""
Anthropic provider implementation.

This module implements the Anthropic Claude LLM provider for high-quality
reasoning and analysis tasks using the Claude API.
"""

import asyncio
import json
import time
from typing import List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude LLM provider implementation"""

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def is_available(self) -> bool:
        """Check if Anthropic provider is properly configured"""
        return bool(self.config.api_key and self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported Claude models"""
        return self.config.models.copy()

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for Anthropic Claude models.

        All Claude models support function calling (tools API) but do not support
        strict json_schema enforcement like OpenAI's STRICT mode.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: Always FUNCTION_CALLING for all Claude models
        """
        # All Anthropic models support function calling via the tools API
        # No model-specific logic needed - all Claude models have the same capability
        return StructuredOutputCapability.FUNCTION_CALLING

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """
        Generate text using Anthropic Claude API

        Args:
            prompt: Input prompt for text generation
            model: Specific Claude model to use
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature (0.0-1.0)
            **kwargs: Additional parameters

        Returns:
            LLMResponse with generated text
        """
        start_time = time.time()

        # Use specified model or default
        selected_model = model or self.config.default_model
        if not selected_model:
            selected_model = "claude-3-sonnet-20240229"

        # Prepare headers for Anthropic API
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }

        # Prepare request body for Anthropic API format
        request_body = {
            "model": selected_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Handle messages for multi-turn conversations
        messages = kwargs.pop("messages", None)
        if messages:
            converted = self._convert_messages_to_anthropic(messages)
            request_body["messages"] = converted["messages"]
            if converted.get("system"):
                request_body["system"] = converted["system"]
        else:
            request_body["messages"] = [{"role": "user", "content": prompt}]

        # Add any additional parameters (system kwarg overrides messages-extracted system)
        if "system" in kwargs:
            request_body["system"] = kwargs["system"]

        if "stop_sequences" in kwargs:
            request_body["stop_sequences"] = kwargs["stop_sequences"]

        # Handle tool/function calling (for structured output)
        if "tools" in kwargs:
            # Convert OpenAI-style tools to Anthropic format
            openai_tools = kwargs["tools"]
            anthropic_tools = []

            for tool in openai_tools:
                if tool.get("type") == "function":
                    func = tool.get("function", {})
                    anthropic_tools.append(
                        {
                            "name": func.get("name"),
                            "description": func.get("description", ""),
                            "input_schema": func.get("parameters", {}),
                        }
                    )

            if anthropic_tools:
                request_body["tools"] = anthropic_tools

                # Handle tool_choice parameter
                tool_choice = kwargs.get("tool_choice")
                if tool_choice == "required":
                    # Force tool use - use "any" type for Anthropic
                    request_body["tool_choice"] = {"type": "any"}
                elif tool_choice == "auto":
                    request_body["tool_choice"] = {"type": "auto"}
                elif isinstance(tool_choice, dict):
                    # Already in Anthropic format
                    request_body["tool_choice"] = tool_choice

        # Make API request
        url = f"{self.config.base_url.rstrip('/')}/messages"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        raise LLMException(
                            f"Anthropic API request failed: {response.status} - {error_text}",
                            status_code=response.status,
                        )

                    response_data = await response.json()
        except asyncio.TimeoutError:
            raise LLMException(
                f"Anthropic API request timed out after {self.config.timeout}s "
                f"(model: {selected_model})"
            )

        # Extract content from Anthropic response format
        content = ""
        tool_calls = None

        if "content" in response_data and response_data["content"]:
            # Anthropic returns content as a list of blocks
            for block in response_data["content"]:
                if block.get("type") == "text":
                    content += block.get("text", "")
                elif block.get("type") == "tool_use":
                    # Convert Anthropic tool_use to OpenAI-style tool_calls
                    if tool_calls is None:
                        tool_calls = []

                    # Import ToolCall here to avoid circular import
                    from .base import ToolCall

                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            type="function",
                            function={
                                "name": block.get("name", ""),
                                # Anthropic returns input as dict, we need JSON string
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        )
                    )

        # Calculate metrics
        response_time_ms = int((time.time() - start_time) * 1000)
        tokens_used = response_data.get("usage", {}).get("output_tokens", 0)

        # Calculate confidence based on model and response quality
        # For structured output (tool calls), content may be empty - that's expected
        has_valid_tool_calls = tool_calls is not None and len(tool_calls) > 0
        confidence = self._calculate_confidence(
            selected_model, content, response_data, has_valid_tool_calls
        )

        return LLMResponse(
            content=content,
            confidence=confidence,
            provider=self.provider_name,
            model=selected_model,
            tokens_used=tokens_used,
            response_time_ms=response_time_ms,
            cached=False,
            tool_calls=tool_calls,  # Add tool_calls for function calling support
        )

    def _calculate_confidence(
        self,
        model: str,
        content: str,
        response_data: dict,
        has_valid_tool_calls: bool = False,
    ) -> float:
        """
        Calculate confidence score for Anthropic response

        Args:
            model: Model used for generation
            content: Generated content
            response_data: Full API response
            has_valid_tool_calls: Whether the response has valid tool calls (structured output)

        Returns:
            Confidence score (0.0-1.0)
        """
        base_confidence = self.config.confidence_score

        # Anthropic models have different confidence characteristics
        model_confidence_map = {
            "claude-3-opus": 0.95,
            "claude-3-sonnet": 0.90,
            "claude-3-haiku": 0.85,
            "claude-2.1": 0.85,
            "claude-2.0": 0.80,
            "claude-instant": 0.75,
        }

        # Find matching model confidence
        model_confidence = base_confidence
        for model_name, confidence in model_confidence_map.items():
            if model_name in model.lower():
                model_confidence = confidence
                break

        # Adjust based on content quality
        content_length = len(content.strip())

        # For structured output (function calling), empty content is expected and valid
        if content_length == 0 and not has_valid_tool_calls:
            return 0.0
        elif content_length == 0 and has_valid_tool_calls:
            # Valid structured output with no text content - use base model confidence
            return model_confidence
        elif content_length < 50:
            # Very short responses might be less reliable
            model_confidence *= 0.8
        elif content_length > 500:
            # Longer, more detailed responses are often higher quality
            model_confidence *= 1.05

        # Check for refusal or inability to answer
        refusal_indicators = [
            "i cannot",
            "i can't",
            "i'm not able",
            "i don't have",
            "i'm sorry",
            "i apologize",
            "i cannot provide",
        ]

        content_lower = content.lower()
        for indicator in refusal_indicators:
            if indicator in content_lower:
                model_confidence *= 0.6
                break

        # Ensure confidence is within valid range
        return min(1.0, max(0.0, model_confidence))

    def _convert_messages_to_anthropic(self, messages: list) -> dict:
        """Convert OpenAI-format messages to Anthropic API format.

        Handles:
        - system messages → extracted to top-level 'system' field
        - user messages → passed through
        - assistant messages with tool_calls → content blocks with tool_use
        - tool messages → user messages with tool_result content blocks
        - Consecutive tool results grouped into single user message

        Returns:
            Dict with 'messages' list and optional 'system' string.
        """
        system_parts = []
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_parts.append(content)

            elif role == "user":
                anthropic_messages.append({"role": "user", "content": content})

            elif role == "assistant":
                content_blocks = []
                if content:
                    content_blocks.append({"type": "text", "text": content})

                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": args,
                        }
                    )

                if content_blocks:
                    anthropic_messages.append(
                        {"role": "assistant", "content": content_blocks}
                    )
                else:
                    anthropic_messages.append(
                        {"role": "assistant", "content": content or ""}
                    )

            elif role == "tool":
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                }

                # Group consecutive tool results into one user message
                if (
                    anthropic_messages
                    and anthropic_messages[-1]["role"] == "user"
                    and isinstance(anthropic_messages[-1]["content"], list)
                    and anthropic_messages[-1]["content"]
                    and anthropic_messages[-1]["content"][0].get("type")
                    == "tool_result"
                ):
                    anthropic_messages[-1]["content"].append(tool_result)
                else:
                    anthropic_messages.append(
                        {
                            "role": "user",
                            "content": [tool_result],
                        }
                    )

        result = {"messages": anthropic_messages}
        if system_parts:
            result["system"] = "\n\n".join(system_parts)

        return result
