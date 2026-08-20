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

from .base import BaseLLMProvider, LLMResponse, ProviderConfig, normalize_stop_reason


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude LLM provider implementation"""

    # --- Extended thinking (#1116) ------------------------------------------
    # Anthropic's API rejects `budget_tokens` below this value.
    _THINKING_MIN_BUDGET_TOKENS = 1024
    # Floor reserved for the VISIBLE answer. Anthropic bills thinking inside
    # `max_tokens`, so an unguarded budget can starve the structured JSON
    # output — the exact fm#1094 failure (starved answers of 101–215 chars,
    # roughly 30–60 tokens). The structured tool loop calls with
    # max_tokens=8000 (milestone_engine.STRUCTURED_OUTPUT_MAX_TOKENS), so a
    # 1024-token floor is ~15–30x the observed starvation region while
    # leaving the default 4096 budget viable. A call that cannot satisfy the
    # floor is downgraded to no-thinking with a warning, never issued.
    _THINKING_MIN_ANSWER_TOKENS = 1024
    # Fallback budget for "enabled" mode when ProviderConfig carries none.
    _THINKING_DEFAULT_BUDGET_TOKENS = 4096

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def _resolve_thinking(self, max_tokens: int) -> Optional[dict]:
        """Thinking parameter for this call, or None to send none at all.

        Modes (from ProviderConfig.thinking_mode, default None → off):
        - "off"/None: no `thinking` key ever — the request is byte-identical
          to pre-#1116 behavior. This is the shipped default.
        - "adaptive": ``{"type": "adaptive"}`` — the mechanism on Claude 4.6+
          (``budget_tokens`` is deprecated on 4.6 and a 400 on 4.7+; the
          model decides how much to think).
        - "enabled": ``{"type": "enabled", "budget_tokens": N}`` — pre-4.6
          models only.

        Starvation guard (fm#1094): thinking is billed INSIDE ``max_tokens``.
        A configuration that cannot leave ``_THINKING_MIN_ANSWER_TOKENS`` for
        the visible answer is downgraded to no-thinking with a warning — a
        starvable call is never issued.
        """
        mode = (self.config.thinking_mode or "off").strip().lower()
        if mode in ("", "off"):
            return None

        if mode == "adaptive":
            # No caller-controlled partition exists in adaptive mode, but the
            # pool is still shared: require room for at least the minimum
            # thinking grain Anthropic would bill plus the answer floor.
            floor = self._THINKING_MIN_BUDGET_TOKENS + self._THINKING_MIN_ANSWER_TOKENS
            if max_tokens < floor:
                self.logger.warning(
                    "Anthropic adaptive thinking disabled for this call: "
                    "max_tokens=%d < %d (thinking shares the max_tokens pool "
                    "and would risk starving the visible answer — fm#1094)",
                    max_tokens,
                    floor,
                )
                return None
            return {"type": "adaptive"}

        if mode == "enabled":
            # `is None` (not `or`): an explicit budget of 0 must reach the
            # below-minimum refuse path, not silently take the default.
            budget = self.config.thinking_budget_tokens
            if budget is None:
                budget = self._THINKING_DEFAULT_BUDGET_TOKENS
            if budget < self._THINKING_MIN_BUDGET_TOKENS:
                self.logger.warning(
                    "Anthropic thinking disabled for this call: "
                    "budget_tokens=%d is below the API minimum of %d",
                    budget,
                    self._THINKING_MIN_BUDGET_TOKENS,
                )
                return None
            # budget_tokens must be strictly less than max_tokens AND leave
            # the answer floor; the second condition subsumes the first.
            if max_tokens - budget < self._THINKING_MIN_ANSWER_TOKENS:
                self.logger.warning(
                    "Anthropic thinking disabled for this call: "
                    "budget_tokens=%d + answer floor %d exceeds max_tokens=%d "
                    "(thinking bills inside max_tokens; issuing this call "
                    "would starve the visible answer — fm#1094)",
                    budget,
                    self._THINKING_MIN_ANSWER_TOKENS,
                    max_tokens,
                )
                return None
            return {"type": "enabled", "budget_tokens": budget}

        self.logger.warning(
            "Unknown ANTHROPIC_THINKING_MODE %r — thinking stays off", mode
        )
        return None

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
            selected_model = "claude-sonnet-4-6"

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
        # Ephemeral prompt caching (5-min TTL). Applied as a post-processing step
        # below so it works regardless of where `system` came from. Transparent
        # to the model output — only affects billing of the stable prefix.
        cache_prompt = bool(kwargs.pop("cache_prompt", False))
        # Router-level reasoning knobs (#1117/#1118). This provider does not
        # translate them yet — extended-thinking support replaces this call
        # with a real mapping (intent → `thinking` config, floor → the
        # budget_tokens/max_tokens partition).
        self._discard_reasoning_kwargs(kwargs, model=selected_model)
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

        # Apply the cache breakpoint once, after `system` is finalized. Caching
        # the system block also caches the tool definitions (the large, stable
        # prefix). When there is no system prompt, cache the sole user turn.
        if cache_prompt:
            system_text = request_body.get("system")
            if isinstance(system_text, str) and system_text:
                request_body["system"] = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif not messages and isinstance(prompt, str) and prompt:
                request_body["messages"] = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ]

        if "stop_sequences" in kwargs:
            request_body["stop_sequences"] = kwargs["stop_sequences"]

        # Handle tool/function calling (for structured output). Guard on a truthy
        # value, not mere presence: the router always forwards ``tools`` as a
        # kwarg (``tools=None`` for non-tool calls), and a bare ``"tools" in
        # kwargs`` check would then iterate ``None`` and raise TypeError.
        if kwargs.get("tools"):
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

        # Extended thinking (#1116) — applied ONLY to tool-calling
        # (structured-output) requests, mirroring Gemini's structured-only
        # thinkingConfig scope, and only when explicitly configured
        # (ANTHROPIC_THINKING_MODE, default "off": no `thinking` key and a
        # request byte-identical to pre-#1116 behavior).
        if request_body.get("tools"):
            thinking_param = self._resolve_thinking(max_tokens)
            # Thinking supports only tool_choice auto/none — forced tool use
            # ({"type": "any"} / {"type": "tool"}) is rejected with a 400. We
            # FAIL CLOSED here: the caller's forcing is left exactly as it set
            # it and thinking is refused for this call.
            #
            # The alternative — silently downgrading the forcing to "auto" —
            # trades a soundness property for an experiment knob, in two ways:
            #   1. On the SINGLE-SHOT structured path (milestone_engine
            #      ~:8163 sets tool_choice="required" for FUNCTION_CALLING
            #      providers, which is every Anthropic call) there is NO
            #      prose→schema recovery: an "auto" answer in prose leaves
            #      tool_calls empty, model_validate_json raises, with_retry
            #      exhausts and the turn fails. The nudge-retry loop is
            #      tool-loop-only.
            #   2. On DA turns, force_tool_use exists to enforce "gather
            #      evidence before concluding". Dropping it re-opens the
            #      premature-conclusion failure mode the startup tool-calling
            #      gate is built to prevent.
            # Consequence, stated deliberately: forced-schema turns on
            # Anthropic cannot carry thinking at all under this PR. Making
            # them able to would require prose→schema recovery on the
            # single-shot path — an engine-wide change affecting every
            # provider, and an owner decision (#1116).
            forced_choice = request_body.get("tool_choice")
            forced = isinstance(forced_choice, dict) and forced_choice.get("type") in (
                "any",
                "tool",
            )
            if thinking_param is not None and forced:
                self.logger.warning(
                    "Anthropic thinking refused for this call: the caller "
                    "forced tool use (tool_choice=%s) and Anthropic rejects "
                    "forced tool use with thinking enabled. Keeping the "
                    "forcing — dropping it would disarm schema forcing on the "
                    "single-shot structured path (no prose recovery there) "
                    "and the evidence-before-conclusion guarantee on DA turns.",
                    forced_choice,
                )
                thinking_param = None
            if thinking_param is not None:
                request_body["thinking"] = thinking_param
                # Thinking is incompatible with temperature modification —
                # only the default (1) is accepted when thinking is on.
                if "temperature" in request_body:
                    self.logger.debug(
                        "Dropping temperature=%s: Anthropic rejects a "
                        "modified temperature when thinking is enabled",
                        request_body["temperature"],
                    )
                    del request_body["temperature"]

        # Make API request
        url = f"{self.config.base_url.rstrip('/')}/messages"

        _MAX_RATE_LIMIT_RETRIES = 2
        try:
            async with aiohttp.ClientSession() as session:
                for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
                    async with session.post(
                        url,
                        headers=headers,
                        json=request_body,
                        timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                    ) as response:
                        if response.status == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                            retry_after = float(
                                response.headers.get("retry-after", "60")
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        if response.status != 200:
                            error_text = await response.text()
                            # Pass status_code only; LLMException derives
                            # retryable (429 + 5xx). An explicit
                            # retryable=status==429 would wrongly force 5xx
                            # to non-retryable.
                            raise LLMException(
                                f"Anthropic API request failed: {response.status} - {error_text}",
                                status_code=response.status,
                            )
                        response_data = await response.json()
                        break
        except asyncio.TimeoutError:
            raise LLMException(
                f"Anthropic API request timed out after {self.config.timeout}s "
                f"(model: {selected_model})",
                status_code=504,  # gateway timeout — transient/retryable
            )

        # Extract content from Anthropic response format
        content = ""
        tool_calls = None

        # Thinking blocks (including redacted_thinking) must be echoed back
        # VERBATIM on the next assistant turn or the model loses its chain —
        # Anthropic validates block signatures and rejects tampered or
        # missing blocks. Same discipline as Gemini's thoughtSignature
        # round-trip (gemini.py assistant_parts): when the response carries
        # thinking, preserve the ENTIRE raw content array as the source of
        # truth for the next turn, rather than rebuilding it from
        # content + tool_calls (which would drop the thinking blocks).
        raw_content_blocks = response_data.get("content") or []
        has_thinking_blocks = any(
            block.get("type") in ("thinking", "redacted_thinking")
            for block in raw_content_blocks
        )
        provider_metadata = (
            {"assistant_content": raw_content_blocks} if has_thinking_blocks else None
        )

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

        # Why generation stopped: end_turn / max_tokens / stop_sequence /
        # tool_use. "max_tokens" means the body is INCOMPLETE (#1094).
        stop_reason = normalize_stop_reason(response_data.get("stop_reason"))

        # Calculate metrics. Anthropic reports disjoint token buckets:
        # input_tokens is the UNCACHED prompt; cache_read/creation are separate.
        response_time_ms = int((time.time() - start_time) * 1000)
        usage_data = response_data.get("usage") or {}
        input_tokens = usage_data.get("input_tokens") or 0
        output_tokens = usage_data.get("output_tokens") or 0
        cache_write_tokens = usage_data.get("cache_creation_input_tokens") or 0
        cache_read_tokens = usage_data.get("cache_read_input_tokens") or 0
        tokens_used = (
            input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        )

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
            provider_metadata=provider_metadata,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            prompt_cache_hit=bool(cache_read_tokens > 0),
            stop_reason=stop_reason,
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
                # When the original response carried thinking blocks, the
                # raw content array was captured verbatim (see
                # provider_metadata.assistant_content in generate()). Echo it
                # as-is: Anthropic validates thinking/redacted_thinking block
                # signatures and rejects the request if any block is missing
                # or altered. Rebuilding from `content` + `tool_calls` would
                # drop the thinking blocks and break the model's chain —
                # same discipline as Gemini's assistant_parts round-trip.
                msg_pmeta = msg.get("provider_metadata") or {}
                saved_blocks = msg_pmeta.get("assistant_content")
                if saved_blocks:
                    anthropic_messages.append(
                        {"role": "assistant", "content": saved_blocks}
                    )
                    continue

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
