"""
OpenAI provider implementation.

This module implements the OpenAI LLM provider for GPT models.
"""

import aiohttp
import json
from typing import List, Optional, Dict, Any

from .base import BaseLLMProvider, LLMResponse, ProviderConfig, ToolCall


class OpenAIProvider(BaseLLMProvider):
    """OpenAI LLM provider implementation"""
    
    @property
    def provider_name(self) -> str:
        return "openai"
    
    def is_available(self) -> bool:
        """Check if OpenAI provider is properly configured"""
        return bool(
            self.config.api_key and
            self.config.base_url and
            self.config.models
        )
    
    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()
    
    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> LLMResponse:
        """Generate response using OpenAI API

        Args:
            prompt: Input prompt
            model: Specific model to use
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            tools: List of function/tool definitions for function calling
            tool_choice: Control tool usage ("auto", "none", or specific tool)
            **kwargs: Additional OpenAI-specific parameters
        """

        self._start_timing()

        # Get effective model
        effective_model = self.get_effective_model(model)

        # Prepare request
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add function calling support
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # Add response format if specified in kwargs
        if "response_format" in kwargs:
            payload["response_format"] = kwargs.pop("response_format")

        # Add any additional kwargs
        payload.update(kwargs)
        
        # Make request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"OpenAI API error {response.status}: {error_text}"
                    )
                
                data = await response.json()

                # Extract response content
                if not data.get("choices") or len(data["choices"]) == 0:
                    raise Exception("OpenAI API returned no choices")

                message = data["choices"][0]["message"]

                # Extract content (may be None if tool_calls present)
                content = message.get("content", "")
                if content:
                    content = self._validate_response_content(content)

                # Extract tool calls if present
                tool_calls = None
                if "tool_calls" in message and message["tool_calls"]:
                    tool_calls = [
                        ToolCall(
                            id=tc["id"],
                            type=tc["type"],
                            function=tc["function"]
                        )
                        for tc in message["tool_calls"]
                    ]

                    # If tool_calls present but no content, parse function arguments as content
                    if not content and tool_calls:
                        # Use the first tool call's arguments as JSON content
                        try:
                            content = tool_calls[0].function.get("arguments", "{}")
                        except Exception:
                            content = "{}"

                # Extract token usage
                usage = data.get("usage", {})
                tokens_used = usage.get("total_tokens", 0)

                response_time = self._get_response_time_ms()

                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=effective_model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                    tool_calls=tool_calls,
                )