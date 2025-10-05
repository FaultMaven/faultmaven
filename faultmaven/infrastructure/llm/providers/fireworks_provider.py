"""
Fireworks AI provider implementation.

This module implements the Fireworks AI LLM provider for high-performance
inference with open-source models.
"""

import aiohttp
from typing import Any, Dict, List, Optional

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class FireworksProvider(BaseLLMProvider):
    """Fireworks AI LLM provider implementation"""
    
    @property
    def provider_name(self) -> str:
        return "fireworks"
    
    def is_available(self) -> bool:
        """Check if Fireworks provider is properly configured"""
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
        """Generate response using Fireworks AI with optional function calling"""

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

        # Add function calling support (Fireworks uses OpenAI-compatible API)
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

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
                        f"Fireworks API error {response.status}: {error_text}"
                    )
                
                data = await response.json()

                # Extract response content
                if not data.get("choices") or len(data["choices"]) == 0:
                    raise Exception("Fireworks API returned no choices")

                message = data["choices"][0]["message"]
                content = message.get("content") or ""
                content = self._validate_response_content(content) if content else ""

                # Extract tool calls if present
                tool_calls = None
                if message.get("tool_calls"):
                    from .base import ToolCall
                    tool_calls = [
                        ToolCall(
                            id=tc["id"],
                            type=tc["type"],
                            function=tc["function"]
                        )
                        for tc in message["tool_calls"]
                    ]

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
                    tool_calls=tool_calls
                )