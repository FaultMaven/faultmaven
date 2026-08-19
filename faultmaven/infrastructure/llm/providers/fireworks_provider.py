"""
Fireworks AI provider implementation.

This module implements the Fireworks AI LLM provider for high-performance
inference with open-source models.
"""

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import BaseLLMProvider, LLMResponse, ProviderConfig, normalize_stop_reason


class FireworksProvider(BaseLLMProvider):
    """Fireworks AI LLM provider implementation"""

    @property
    def provider_name(self) -> str:
        return "fireworks"

    def is_available(self) -> bool:
        """Check if Fireworks provider is properly configured"""
        return bool(self.config.api_key and self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    # Models hosted on Fireworks that cannot reliably satisfy
    # tool_choice=required under FaultMaven's schema sizes.
    # MiniMax M2P7: forced tool use times out at the 180s Fireworks timeout
    # (see 2026-05-20 Run 7 post-mortem in handoff docs). Adding here makes
    # Layer 1 (pre-check) skip the tool-augmented path and go straight to
    # the non-tool structured-output route for these models.
    #
    # When to add a model: only after observing REPEATED Layer 2 timeouts
    # or tool-calling failures for that model in production (or in
    # reproducible eval runs). Trust Layer 2 (ToolCallingUnsupportedError
    # runtime fallback) for one-off or transient incompatibilities — the
    # denylist is for models with a known, reproducible incompatibility
    # where paying for the first failure on every turn is wasted work.
    _TOOL_CALLING_DENYLIST = frozenset(
        {
            "accounts/fireworks/models/minimax-m2p7",
        }
    )

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Check if the model supports OpenAI-compatible tool calling on Fireworks.

        Returns True for most models. Earlier versions blocked DeepSeek
        models due to proprietary tool-calling tokens in older versions
        (V2, R1), but DeepSeek V3+ supports OpenAI-compatible tool calling
        on Fireworks.

        Specific models on the denylist (see ``_TOOL_CALLING_DENYLIST``)
        return False — they accept the tool-calling API but cannot
        satisfy ``tool_choice=required`` under FaultMaven's schema sizes
        within the provider timeout.

        Layer 2 runtime fallback (ToolCallingUnsupportedError) catches
        any models that genuinely can't handle tools at runtime.
        """
        effective_model = model or self.config.default_model
        if effective_model in self._TOOL_CALLING_DENYLIST:
            return False
        return True

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for Fireworks AI models.

        All Fireworks AI models use BEST_EFFORT mode (prompt-based JSON generation).
        Fireworks doesn't currently support strict json_schema enforcement.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: Always BEST_EFFORT for all Fireworks models
        """
        # All Fireworks models use BEST_EFFORT mode
        return StructuredOutputCapability.BEST_EFFORT

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
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

        # Fireworks API requires stream=true for max_tokens > 4096.
        # Cap at 4096 since streaming is not implemented.
        effective_max_tokens = min(max_tokens, 4096)

        # Use explicit messages if provided, otherwise construct from prompt
        effective_messages = (
            messages if messages is not None else [{"role": "user", "content": prompt}]
        )

        payload = {
            "model": effective_model,
            "messages": effective_messages,
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
        }

        # Add function calling support (Fireworks uses OpenAI-compatible API)
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # Extract routing-level timeout override before payload update so it
        # is not forwarded to the Fireworks API as an unknown request field.
        effective_timeout = kwargs.pop("timeout", None) or self.config.timeout
        # Anthropic-only caching hint; drop before payload.update(kwargs).
        kwargs.pop("cache_prompt", None)

        # Add any additional kwargs, filtering out None values to avoid
        # overwriting constructed payload fields
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        # Make request
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=effective_timeout),
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        raise LLMException(
                            f"Fireworks API error {response.status}: {error_text}",
                            status_code=response.status,
                        )

                    data = await response.json()

                    # Extract response content
                    if not data.get("choices") or len(data["choices"]) == 0:
                        raise LLMException("Fireworks API returned no choices")

                    choice = data["choices"][0]
                    message = choice["message"]
                    # OpenAI-compatible "length" ⇒ cut at the cap (#1094).
                    stop_reason = normalize_stop_reason(choice.get("finish_reason"))
                    content = message.get("content") or ""
                    content = (
                        self._validate_response_content(content) if content else ""
                    )

                    # Extract tool calls if present
                    tool_calls = self._extract_tool_calls_from_message(message)

                    # Extract token usage (OpenAI-compatible; prompt_tokens is
                    # inclusive of cached tokens, so subtract for disjoint buckets).
                    usage = data.get("usage") or {}
                    prompt_tokens = usage.get("prompt_tokens") or 0
                    output_tokens = usage.get("completion_tokens") or 0
                    tokens_used = usage.get("total_tokens") or (
                        prompt_tokens + output_tokens
                    )
                    prompt_details = usage.get("prompt_tokens_details") or {}
                    cache_read_tokens = prompt_details.get("cached_tokens") or 0
                    input_tokens = max(prompt_tokens - cache_read_tokens, 0)

                    response_time = self._get_response_time_ms()

                    return LLMResponse(
                        content=content,
                        confidence=self.config.confidence_score,
                        provider=self.provider_name,
                        model=effective_model,
                        tokens_used=tokens_used,
                        response_time_ms=response_time,
                        tool_calls=tool_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        prompt_cache_hit=bool(cache_read_tokens > 0),
                        stop_reason=stop_reason,
                    )
        except asyncio.TimeoutError:
            raise LLMException(
                f"Fireworks API request timed out after {effective_timeout}s "
                f"(model: {effective_model})",
                status_code=504,  # gateway timeout — transient/retryable
            )
