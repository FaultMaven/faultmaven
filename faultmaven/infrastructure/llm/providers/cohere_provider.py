"""
Cohere provider implementation.

This module implements the Cohere LLM provider using Cohere's native v2 Chat API.
Cohere provides Command-R models optimized for RAG, tool use, and enterprise applications.

API Reference: https://docs.cohere.com/reference/chat
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class CohereProvider(BaseLLMProvider):
    """Cohere LLM provider implementation using v2 Chat API

    Supports:
    - Command-R and Command-R+ models
    - Tool use (function calling) with strict mode
    - Streaming responses
    - Multi-turn conversations
    """

    @property
    def provider_name(self) -> str:
        return "cohere"

    def is_available(self) -> bool:
        """Check if Cohere provider is properly configured"""
        return bool(self.config.api_key and self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for Cohere models.

        All Cohere Command-R models use BEST_EFFORT mode (prompt-based JSON generation).
        Cohere doesn't currently support strict json_schema enforcement.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: Always BEST_EFFORT for all Cohere models
        """
        # All Cohere models use BEST_EFFORT mode
        return StructuredOutputCapability.BEST_EFFORT

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs,
    ) -> LLMResponse:
        """Generate response using Cohere v2 Chat API

        Args:
            prompt: Input prompt
            model: Specific model to use (default: command-r-plus)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature (0.0-1.0)
            tools: List of tool definitions for function calling
            tool_choice: Control tool usage (Cohere supports "REQUIRED" or "AUTO")
            **kwargs: Additional Cohere-specific parameters:
                - strict_tools: bool (eliminate tool hallucinations)
                - stream: bool (enable streaming)
                - preamble: str (system message)

        Returns:
            LLMResponse with generated content and metadata

        Raises:
            LLMException: If API request fails
        """
        self._start_timing()

        # Get effective model
        effective_model = self.get_effective_model(model)

        # Prepare request headers
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-Client-Name": "faultmaven",  # Identify client
        }

        # Handle messages for multi-turn conversations
        messages = kwargs.pop("messages", None)

        # Prepare request payload in Cohere v2 format
        payload = {
            "model": effective_model,
            "messages": messages if messages else [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add tool support (Cohere format)
        if tools:
            # Convert OpenAI-style tools to Cohere format if needed
            # Cohere v2 supports OpenAI format natively
            payload["tools"] = tools

            # Map tool_choice to Cohere format
            if tool_choice:
                # OpenAI uses "auto", "none", "required"
                # Cohere uses "AUTO", "NONE", "REQUIRED"
                payload["tool_choice"] = tool_choice.upper()

            # Enable strict tools by default for better reliability
            payload["strict_tools"] = kwargs.pop("strict_tools", True)

        # Add preamble (system message) if provided
        if "preamble" in kwargs:
            payload["preamble"] = kwargs.pop("preamble")

        # Add streaming if requested
        stream = kwargs.pop("stream", False)
        if stream:
            payload["stream"] = True

        # Handle response format (v2 supports type: "json_object")
        if "response_format" in kwargs:
            payload["response_format"] = kwargs.pop("response_format")

        # Add any additional kwargs, filtering out None values
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        # Make request to Cohere API
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.base_url}/chat",  # v2 endpoint
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        raise LLMException(
                            f"Cohere API error {response.status}: {error_text}",
                            status_code=response.status,
                        )

                    data = await response.json()

                    # Extract response from Cohere v2 format
                    if "message" not in data:
                        raise LLMException("Cohere API returned no message")

                    message = data["message"]

                    # Extract content
                    content = message.get("content", "")

                    # Extract tool calls if present
                    tool_calls = self._extract_tool_calls_from_message(message)
                    # If tool_calls present but no content, use tool arguments
                    # as fallback content.
                    if tool_calls and not content:
                        try:
                            content = tool_calls[0].function.get("arguments", "{}")
                        except Exception:
                            content = "{}"

                    # Only validate content if we don't have tool_calls (tool_calls can have empty content)
                    if (
                        content
                        and hasattr(self, "_validate_response_content")
                        and not tool_calls
                    ):
                        content = self._validate_response_content(content)

                    # Extract token usage (Cohere v2 format)
                    usage = data.get("usage", {}).get("tokens", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    total_tokens = input_tokens + output_tokens

                    response_time = self._get_response_time_ms()

                    return LLMResponse(
                        content=content,
                        confidence=self.config.confidence_score,
                        provider=self.provider_name,
                        model=effective_model,
                        tokens_used=total_tokens,
                        response_time_ms=response_time,
                        tool_calls=tool_calls,
                    )
        except asyncio.TimeoutError:
            raise LLMException(
                f"Cohere API request timed out after {self.config.timeout}s "
                f"(model: {effective_model})"
            )
        except aiohttp.ClientError as e:
            raise LLMException(f"Cohere connection error: {str(e)}", retryable=True)
