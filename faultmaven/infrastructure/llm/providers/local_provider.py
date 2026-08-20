"""
Local LLM provider implementation.

This module implements the local LLM provider for self-hosted models
including Phi-3, Ollama, and other local inference servers.
"""

import asyncio
import logging
from typing import List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    StopReason,
    normalize_stop_reason,
)


class LocalProvider(BaseLLMProvider):
    """Local LLM provider implementation"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def provider_name(self) -> str:
        return "local"

    def is_available(self) -> bool:
        """Check if local provider is properly configured"""
        return bool(self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    def _uses_openai_compatible_transport(self, effective_model: str) -> bool:
        """Whether this model will be served over the OpenAI-compatible
        ``/v1/chat/completions`` path (the only local transport that returns
        OpenAI-style ``tool_calls``).

        ``generate()`` routes to the Ollama ``/api/generate`` transport when
        ``base_url`` or the model name says "ollama" — that protocol does NOT
        return ``tool_calls``, so function calling cannot work there. Mirrors
        the dispatch in ``generate()``.
        """
        base = (self.config.base_url or "").lower()
        return "ollama" not in base and "ollama" not in effective_model.lower()

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Check if the local model supports tool calling.

        Only functionary and hermes models have native function calling support,
        AND only over the OpenAI-compatible transport — the Ollama
        ``/api/generate`` path cannot return ``tool_calls`` regardless of model.
        Other local models (plain llama.cpp, etc.) do not support the tools API.
        """
        effective_model = self.get_effective_model(model)
        model_lower = effective_model.lower()

        if ("functionary" in model_lower or "hermes" in model_lower) and (
            self._uses_openai_compatible_transport(effective_model)
        ):
            return True

        return False

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for local models.

        Local models have varying structured output support:
        - FUNCTION_CALLING: functionary/hermes models served over the
          OpenAI-compatible transport (native function calling)
        - BEST_EFFORT: all other local models (prompt-based JSON generation),
          INCLUDING functionary/hermes on the Ollama transport — that path
          can't return ``tool_calls``, so claiming FUNCTION_CALLING there would
          make the engine request a forced tool call the transport silently
          can't satisfy.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: FUNCTION_CALLING or BEST_EFFORT
        """
        effective_model = self.get_effective_model(model)
        model_lower = effective_model.lower()

        # Native function calling — only on the OpenAI-compatible transport.
        if ("functionary" in model_lower or "hermes" in model_lower) and (
            self._uses_openai_compatible_transport(effective_model)
        ):
            return StructuredOutputCapability.FUNCTION_CALLING

        # All other local models / transports use BEST_EFFORT (prompt-based)
        return StructuredOutputCapability.BEST_EFFORT

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Generate response using local LLM server"""

        self._start_timing()

        # Get effective model
        effective_model = self.get_effective_model(model)

        # Router-level reasoning knobs (#1117/#1118) this provider has no
        # mechanism for. Popped HERE, before transport dispatch, because the
        # Ollama path merges raw kwargs into payload["options"] — the keys
        # must never reach a request body. Logs any intent it cannot act on.
        self._discard_reasoning_kwargs(kwargs, model=effective_model)

        # Intelligently detect API format for optimal compatibility
        # Priority order: Ollama -> OpenAI-compatible -> Raw llama.cpp

        if (
            "ollama" in self.config.base_url.lower()
            or "ollama" in effective_model.lower()
        ):
            # Ollama-specific API
            return await self._call_ollama_api(
                prompt, effective_model, max_tokens, temperature, **kwargs
            )

        # First try OpenAI-compatible API (most common for modern local LLM servers)
        try:
            return await self._call_openai_compatible_api(
                prompt, effective_model, max_tokens, temperature, **kwargs
            )
        except Exception as openai_error:
            # If OpenAI-compatible fails, try raw llama.cpp completion endpoint as fallback
            if "404" in str(openai_error) or "not found" in str(openai_error).lower():
                try:
                    return await self._call_llamacpp_api(
                        prompt, effective_model, max_tokens, temperature, **kwargs
                    )
                except Exception as llamacpp_error:
                    # If both fail, raise the more informative error
                    raise LLMException(
                        f"Local LLM server failed with both API formats. OpenAI-compatible: {openai_error}. Raw llama.cpp: {llamacpp_error}"
                    )
            else:
                # For non-404 errors, re-raise the OpenAI error
                raise openai_error

    async def _call_ollama_api(
        self, prompt: str, model: str, max_tokens: int, temperature: float, **kwargs
    ) -> LLMResponse:
        """Call Ollama-style API"""

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }

        # Add any additional options
        if kwargs:
            # Handle structured output (Ollama uses "format": "json")
            if "response_format" in kwargs:
                payload["format"] = "json"
                # Remove it so it doesn't clutter options
                kwargs.pop("response_format")

            payload["options"].update(kwargs)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:

                if response.status != 200:
                    error_text = await response.text()
                    raise LLMException(
                        f"Ollama API error {response.status}: {error_text}",
                        status_code=response.status,
                    )

                data = await response.json()

                # Extract response content
                content = data.get("response")
                if not content:
                    raise LLMException("Ollama API returned no response content")

                content = self._validate_response_content(content)

                # Extract token usage (Ollama specific)
                tokens_used = data.get("eval_count", 0)

                # Ollama reports why it stopped in `done_reason`: "stop" for a
                # natural end, "length" when num_predict was reached (#1094).
                stop_reason = normalize_stop_reason(data.get("done_reason"))

                response_time = self._get_response_time_ms()

                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                    stop_reason=stop_reason,
                )

    async def _call_openai_compatible_api(
        self, prompt: str, model: str, max_tokens: int, temperature: float, **kwargs
    ) -> LLMResponse:
        """Call OpenAI-compatible API (for llama.cpp with OpenAI API, Phi-3 ONNX and similar)"""

        self.logger.debug(
            f"Starting OpenAI-compatible API call to {self.config.base_url}"
        )
        self.logger.debug(
            f"Model: {model}, Max tokens: {max_tokens}, Temperature: {temperature}"
        )

        headers = {
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Handle messages for multi-turn conversations
        messages = kwargs.pop("messages", None)
        if messages:
            payload["messages"] = messages

        # Add any additional kwargs
        if "response_format" in kwargs:
            payload["response_format"] = kwargs.pop("response_format")

        # Discards the router-level knobs, then merges the rest (see base).
        self._merge_extra_kwargs(payload, kwargs, model=model)

        self.logger.debug(f"Request payload: {payload}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.config.base_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    self.logger.debug(f"Response status: {response.status}")

                    if response.status != 200:
                        error_text = await response.text()
                        error_msg = f"Local OpenAI-compatible API error {response.status}: {error_text}"
                        self.logger.error(f"HTTP Error: {error_msg}")
                        raise LLMException(error_msg, status_code=response.status)

                    data = await response.json()
                    self.logger.debug(f"Response data: {data}")

                    # Extract response content
                    if not data.get("choices") or len(data["choices"]) == 0:
                        error_msg = "Local OpenAI-compatible API returned no choices"
                        self.logger.error(f"No choices: {error_msg}")
                        raise LLMException(error_msg)

                    choice = data["choices"][0]
                    message = choice["message"]
                    # OpenAI-compatible "length" ⇒ cut at the cap (#1094).
                    stop_reason = normalize_stop_reason(choice.get("finish_reason"))
                    content = message.get("content") or ""
                    self.logger.debug(f"Raw content: {repr(content)}")

                    # Extract tool calls if present. FUNCTION_CALLING-capable
                    # local models (functionary, hermes served via vLLM/llama.cpp
                    # OpenAI-compatible endpoints) return structured output as
                    # tool_calls with empty content. Without this, the engine's
                    # FUNCTION_CALLING strategy gets no tool_calls back and the
                    # validation below would raise on the empty content.
                    tool_calls = self._extract_tool_calls_from_message(message)
                    # If tool_calls present but no content, use the first tool
                    # call's arguments as JSON content (mirrors the OpenAI/Cohere
                    # providers).
                    if tool_calls and not content:
                        try:
                            content = tool_calls[0].function.get("arguments", "{}")
                        except Exception:
                            content = "{}"

                    # Only validate when there are no tool_calls — a valid
                    # function-calling response legitimately has empty content.
                    if not tool_calls:
                        try:
                            content = self._validate_response_content(content)
                            self.logger.debug(f"Validated content: {repr(content)}")
                        except Exception as e:
                            self.logger.error(f"Content validation failed: {e}")
                            raise

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

                    self.logger.info(
                        f"Successful response with {tokens_used} tokens, {response_time}ms"
                    )

                    return LLMResponse(
                        content=content,
                        confidence=self.config.confidence_score,
                        provider=self.provider_name,
                        model=model,
                        tokens_used=tokens_used,
                        response_time_ms=response_time,
                        tool_calls=tool_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        prompt_cache_hit=bool(cache_read_tokens > 0),
                        stop_reason=stop_reason,
                    )

            except asyncio.TimeoutError as e:
                response_time = self._get_response_time_ms()
                self.logger.warning(
                    f"Timeout after {response_time}ms (limit: {self.config.timeout * 1000}ms)"
                )
                self.logger.warning(
                    f"Model: {model}, Max tokens: {max_tokens}, Temperature: {temperature}"
                )
                self.logger.debug(f"Timeout error: {e}")
                raise LLMException(
                    f"Local LLM request timed out after {self.config.timeout} seconds",
                    status_code=504,  # gateway timeout — transient/retryable
                )

            except Exception as e:
                response_time = self._get_response_time_ms()
                self.logger.error(f"Request failed after {response_time}ms")
                self.logger.error(f"Error type: {type(e).__name__}")
                self.logger.error(f"Error details: {e}")
                raise

    async def _call_llamacpp_api(
        self, prompt: str, model: str, max_tokens: int, temperature: float, **kwargs
    ) -> LLMResponse:
        """Call raw llama.cpp server API (completions endpoint)"""

        # llama.cpp server uses completions endpoint, not chat/completions
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "stop": ["\\n\\n"],  # Basic stop tokens
            "stream": False,
        }

        # Add any additional options (knobs discarded, None values filtered).
        if kwargs:
            self._merge_extra_kwargs(payload, kwargs, model=model)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/completion",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:

                if response.status != 200:
                    error_text = await response.text()
                    raise LLMException(
                        f"Raw llama.cpp server API error {response.status}: {error_text}",
                        status_code=response.status,
                    )

                data = await response.json()

                # Extract response content
                content = data.get("content")
                if not content:
                    raise LLMException("Raw llama.cpp server API returned no content")

                content = self._validate_response_content(content)

                # Extract token usage (llama.cpp specific)
                tokens_used = data.get("tokens_predicted", 0)

                # llama.cpp reports stop conditions as booleans. `stopped_limit`
                # is the one that means "hit n_predict" — the output cap.
                #
                # NOT `truncated`: on this server that flag means the PROMPT
                # exceeded the context window and was cut, which is an input
                # problem. Reading it as output truncation would send the
                # retry-with-a-bigger-cap ladder after a failure a bigger cap
                # makes strictly worse (#1094).
                if data.get("stopped_limit"):
                    stop_reason = StopReason.MAX_TOKENS
                elif data.get("stopped_eos") or data.get("stopped_word"):
                    stop_reason = StopReason.STOP
                else:
                    stop_reason = StopReason.UNKNOWN

                response_time = self._get_response_time_ms()

                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                    stop_reason=stop_reason,
                )
