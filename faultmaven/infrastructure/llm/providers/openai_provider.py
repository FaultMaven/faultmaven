"""
OpenAI provider implementation.

This module implements the OpenAI LLM provider for GPT models.
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class OpenAIProvider(BaseLLMProvider):
    """OpenAI LLM provider implementation"""

    @property
    def provider_name(self) -> str:
        return "openai"

    def is_available(self) -> bool:
        """Check if OpenAI provider is properly configured"""
        return bool(self.config.api_key and self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    # Modern OpenAI models with strict json_schema support
    # (response_format.type = "json_schema", strict=True).
    # Order: most-specific first to avoid false-positive prefix matches.
    _STRICT_MODEL_INDICATORS = (
        "gpt-3.5-turbo-0125",  # GPT-3.5 Turbo with structured output (legacy carve-out)
        "gpt-4o",  # All GPT-4 Omni variants (gpt-4o, gpt-4o-mini, ...)
        "gpt-4-turbo",  # GPT-4 Turbo
        "gpt-4-2024",  # GPT-4 with 2024 date suffix
        "gpt-4.1",  # GPT-4.1 family
        "gpt-4.5",  # GPT-4.5 family
        "gpt-5",  # All GPT-5.x — matches "gpt-5", "gpt-5.4", "gpt-5-mini", ...
        "chatgpt-4o",  # chatgpt-4o-latest
        "o1",  # OpenAI o1 reasoning models
        "o3",  # OpenAI o3 reasoning models
    )

    # OpenAI model families that REQUIRE ``max_completion_tokens`` and reject the
    # legacy ``max_tokens`` with a 400 ``unsupported_parameter`` error (the
    # o-series reasoning models and the GPT-5 family). Older Chat Completions
    # models (gpt-4o, gpt-4, gpt-3.5) still take ``max_tokens``. Kept separate
    # from the STRICT indicators because the two axes don't coincide — gpt-4o is
    # STRICT but still uses ``max_tokens``.
    _COMPLETION_TOKENS_MODEL_FAMILIES = ("gpt-5", "o1", "o3", "o4")
    # Anchored at the start of the (optionally ``vendor/``-prefixed) id so a
    # family token embedded MID-name (e.g. ``my-gpt-4-o1-test``) does not
    # false-match; the real families always LEAD the id (``o3-mini``,
    # ``gpt-5.4-mini``, ``openai/o4-mini``). A trailing ``-``/``.``/end keeps
    # ``o1`` from matching a hypothetical ``o10``.
    _COMPLETION_TOKENS_MODEL_RE = re.compile(
        r"(?:^|/)(?:" + "|".join(_COMPLETION_TOKENS_MODEL_FAMILIES) + r")(?:[-.]|$)"
    )

    @classmethod
    def _uses_completion_tokens_param(cls, model_name: str) -> bool:
        """Whether the model takes ``max_completion_tokens`` over ``max_tokens``.

        The o-series and GPT-5 families reject ``max_tokens`` with a 400
        ``unsupported_parameter`` error and require ``max_completion_tokens``
        instead. Older models keep ``max_tokens``. Overridable so a subclass
        that targets a different gateway (e.g. OpenRouter, whose unified API
        normalizes the legacy parameter itself) can opt out.
        """
        return bool(cls._COMPLETION_TOKENS_MODEL_RE.search(model_name.lower()))

    @classmethod
    def _capability_for_model_name(cls, model_name: str) -> StructuredOutputCapability:
        """Classify an OpenAI model *name* (no config lookup).

        Pure string classifier so subclasses that route to OpenAI-named
        models under a different namespace (e.g. OpenRouter's
        ``openai/gpt-5``) can reuse the indicator list without going through
        ``get_effective_model()`` (whose config.models won't contain the
        stripped suffix).
        """
        model_lower = model_name.lower()
        if any(ind in model_lower for ind in cls._STRICT_MODEL_INDICATORS):
            return StructuredOutputCapability.STRICT
        # Older OpenAI models support function calling as fallback
        return StructuredOutputCapability.FUNCTION_CALLING

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for OpenAI models.

        OpenAI's "structured outputs" feature (``response_format.type=json_schema``
        with ``strict: true``) is supported on gpt-4o and gpt-4o-mini from
        2024-08-06 onward, and on all subsequent gpt-4.x / gpt-5.x / o1 / o3
        releases. Older models (gpt-3.5-turbo, gpt-4 legacy) fall back to
        FUNCTION_CALLING.

        The previous allow-list missed several modern model families
        (gpt-4.1, gpt-5.x, chatgpt-4o-latest, o1/o3 reasoning models),
        meaning recent operator configs (e.g. ``OPENAI_MODEL=GPT-5.4``)
        silently downgraded to FUNCTION_CALLING instead of STRICT.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: STRICT or FUNCTION_CALLING
        """
        return self._capability_for_model_name(self.get_effective_model(model))

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

        # Handle messages for multi-turn conversations
        messages = kwargs.pop("messages", None)

        # GPT-5 / o-series models reject the legacy ``max_tokens`` parameter and
        # require ``max_completion_tokens``; older models keep ``max_tokens``.
        # The token limit is owned by the explicit ``max_tokens`` arg + this
        # selection; drop any stray copy from kwargs so the later
        # ``payload.update(kwargs)`` can't inject a conflicting/duplicate key
        # (sending both 400s on the models that reject the legacy name).
        kwargs.pop("max_tokens", None)
        kwargs.pop("max_completion_tokens", None)
        token_limit_param = (
            "max_completion_tokens"
            if self._uses_completion_tokens_param(effective_model)
            else "max_tokens"
        )
        payload = {
            "model": effective_model,
            "messages": messages if messages else [{"role": "user", "content": prompt}],
            token_limit_param: max_tokens,
            "temperature": temperature,
        }

        # Add function calling support
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # Add response format if specified in kwargs
        response_format = kwargs.pop("response_format", None)
        if response_format:
            payload["response_format"] = response_format

        # Add any additional kwargs, filtering out None values
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        # Make request
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        raise LLMException(
                            f"OpenAI API error {response.status}: {error_text}",
                            status_code=response.status,
                        )

                    data = await response.json()

                    # Extract response content
                    if not data.get("choices") or len(data["choices"]) == 0:
                        raise LLMException("OpenAI API returned no choices")

                    message = data["choices"][0]["message"]

                    # Extract content (may be None if tool_calls present)
                    content = message.get("content", "")
                    if content:
                        content = self._validate_response_content(content)

                    # Extract tool calls if present
                    tool_calls = self._extract_tool_calls_from_message(message)
                    # If tool_calls present but no content, parse function
                    # arguments as content.
                    if tool_calls and not content:
                        try:
                            content = tool_calls[0].function.get("arguments", "{}")
                        except Exception:
                            content = "{}"

                    # Extract token usage
                    usage = data.get("usage") or {}
                    input_tokens = usage.get("prompt_tokens") or 0
                    output_tokens = usage.get("completion_tokens") or 0
                    tokens_used = usage.get("total_tokens") or (
                        input_tokens + output_tokens
                    )

                    prompt_details = usage.get("prompt_tokens_details") or {}
                    cache_read_tokens = prompt_details.get("cached_tokens") or 0

                    response_time = self._get_response_time_ms()

                    return LLMResponse(
                        content=content,
                        confidence=self.config.confidence_score,
                        provider=self.provider_name,
                        model=effective_model,
                        tokens_used=tokens_used,
                        response_time_ms=response_time,
                        cached=bool(cache_read_tokens > 0),
                        tool_calls=tool_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                    )
        except asyncio.TimeoutError:
            raise LLMException(
                f"OpenAI API request timed out after {self.config.timeout}s "
                f"(model: {effective_model})"
            )
