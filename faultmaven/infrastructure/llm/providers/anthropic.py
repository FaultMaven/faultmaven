"""
Anthropic provider implementation.

This module implements the Anthropic Claude LLM provider for high-quality
reasoning and analysis tasks using the Claude API.
"""

import asyncio
import json
import re
import time
from typing import List, Optional

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    extract_provider_error_code,
    normalize_stop_reason,
)


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude LLM provider implementation"""

    # --- Extended thinking (#1116) ------------------------------------------
    # Anthropic's API rejects `budget_tokens` below this value.
    _THINKING_MIN_BUDGET_TOKENS = 1024
    # Floor reserved for the VISIBLE answer. Anthropic bills thinking inside
    # `max_tokens`, so an unguarded budget can starve the structured JSON
    # output — the exact fm#1094 failure (starved answers of 101–215 chars,
    # roughly 30–60 tokens). The structured tool loop calls with
    # max_tokens=8000
    # (milestone_engine.generation.STRUCTURED_OUTPUT_MAX_TOKENS), so a
    # 1024-token floor is ~15–30x the observed starvation region while
    # leaving the default 4096 budget viable. A call that cannot satisfy the
    # floor is downgraded to no-thinking with a warning, never issued.
    _THINKING_MIN_ANSWER_TOKENS = 1024
    # Fallback budget for "enabled" mode when ProviderConfig carries none.
    _THINKING_DEFAULT_BUDGET_TOKENS = 4096

    # --- Per-model request shape (#1695) ------------------------------------
    # The newest Claude models reject two parts of the one request shape this
    # provider used to send every model. Measured live 2026-09-28, one request
    # per cell and no other parameter varied (the matrix is in
    # docs/reference/llm-model-capabilities.md §"Anthropic request shape"):
    #   - any `temperature` (0.7, 0.3 or 0.0) -> 400 "`temperature` is
    #     deprecated for this model." on opus-4-7, opus-4-8, opus-5, sonnet-5,
    #     fable-5, opus-5-5 and fable-5-1; accepted on opus-4-6, sonnet-4-6
    #     and haiku-4-5 (sonnet-4-5 per the model docs);
    #   - forced tool use (`tool_choice` any/tool) -> 400 "tool_choice: type
    #     "tool" and "any" are not supported for this model." on opus-5-5 and
    #     fable-5-1; accepted on every other model above, fable-5 included.
    # So every ceiling below is measured except the mythos entry, which rests
    # on the model docs: mythos-5 and mythos-5-1 are not served to the key
    # that measured the rest.
    # The Models API reports neither property (its `capabilities` cover batch,
    # citations, effort, structured_outputs and thinking), so the gate is a
    # model-family rule, as it is for Gemini's 3.7+ surface. Each table holds,
    # per family, the LAST version measured to accept the property; a model
    # accepts it only when its family has an entry and its version is at or
    # below that entry.
    #
    # Default: an unparseable id, an unlisted family or a version above its
    # ceiling takes the CURRENT shape (no temperature; `auto` plus an
    # instruction naming the tool). Every measured model accepts that shape,
    # so an unknown model never 400s on these two parameters. The cost is
    # losing sampling and forcing on an unknown OLD model.
    _SAMPLING_ACCEPTED_THROUGH = {"opus": (4, 6), "sonnet": (4, 6), "haiku": (4, 5)}
    _FORCED_TOOL_CHOICE_ACCEPTED_THROUGH = {
        "opus": (5, 0),
        "sonnet": (5, 0),
        "haiku": (4, 5),
        "fable": (5, 0),
        "mythos": (5, 0),
    }
    # Thinking shape (#1756), same "last version measured" form. Measured live
    # 2026-09-29, one request per cell, only the `thinking` shape varied
    # (`enabled` with budget_tokens 1024, or `adaptive`):
    #   haiku-4-5, sonnet-4-5, opus-4-5: enabled 200; adaptive 400 ("adaptive
    #     thinking is not supported on this model").
    #   sonnet-4-6, opus-4-6: both 200.
    #   opus-4-7 through opus-5-5, sonnet-5(-5), fable-5(-1): enabled 400
    #     ("thinking.type.enabled" is not supported; use adaptive); adaptive 200.
    # budget_tokens is accepted through the first table's entry; adaptive is
    # rejected through the second's. A parsed id in an unlisted family or above
    # an entry rejects budget_tokens and accepts adaptive. An unparseable id is
    # the exception to #1695's default: no thinking shape is accepted by every
    # model, so an id the adapter cannot read is sent the configured mode.
    _BUDGET_TOKENS_ACCEPTED_THROUGH = {
        "opus": (4, 6),
        "sonnet": (4, 6),
        "haiku": (4, 5),
    }
    _ADAPTIVE_THINKING_REJECTED_THROUGH = {
        "opus": (4, 5),
        "sonnet": (4, 5),
        "haiku": (4, 5),
    }
    # claude-<family>-<major>[-<minor>][-<yyyymmdd>]. The minor is 1-2 digits
    # and the date 8, so claude-opus-4-20250514 is (4, 0) and
    # claude-haiku-4-5-20251001 is (4, 5).
    _CLAUDE_MODEL_ID = re.compile(
        r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?"
    )
    # Model ids whose request shape has been logged: one line per model id per
    # process, not one per call.
    _REQUEST_SHAPE_LOGGED: set[str] = set()
    # (model id, configured thinking mode) pairs whose shape was substituted:
    # one WARNING per pair per process (#1756).
    _THINKING_SUBSTITUTION_LOGGED: set[tuple[str, str]] = set()

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @classmethod
    def _claude_version(cls, model: str) -> tuple[str, tuple[int, int]] | None:
        """(family, (major, minor)) from a Claude model id, or None if the id
        does not follow ``claude-<family>-<major>[-<minor>][-<yyyymmdd>]``."""
        m = cls._CLAUDE_MODEL_ID.fullmatch((model or "").strip().lower())
        if m is None:
            return None
        return m.group(1), (int(m.group(2)), int(m.group(3) or 0))

    @classmethod
    def _accepted_through(cls, model: str, ceilings: dict) -> bool:
        """True when *model*'s family has a ceiling in *ceilings* and its
        version is at or below it (see the #1695 block above)."""
        parsed = cls._claude_version(model)
        if parsed is None:
            return False
        family, version = parsed
        ceiling = ceilings.get(family)
        return ceiling is not None and version <= ceiling

    @classmethod
    def _accepts_sampling(cls, model: str) -> bool:
        return cls._accepted_through(model, cls._SAMPLING_ACCEPTED_THROUGH)

    @classmethod
    def _accepts_forced_tool_choice(cls, model: str) -> bool:
        return cls._accepted_through(model, cls._FORCED_TOOL_CHOICE_ACCEPTED_THROUGH)

    @classmethod
    def _accepts_budget_tokens(cls, model: str) -> bool:
        return cls._accepted_through(model, cls._BUDGET_TOKENS_ACCEPTED_THROUGH)

    @classmethod
    def _accepts_adaptive_thinking(cls, model: str) -> bool:
        return not cls._accepted_through(model, cls._ADAPTIVE_THINKING_REJECTED_THROUGH)

    def _log_request_shape(self, model: str) -> None:
        """Say once per model id what this provider leaves out of its requests.

        An unparseable id gets a WARNING, because it takes the reduced shape by
        default rather than by measurement. A parsed model above a ceiling gets
        an INFO naming what is dropped. A model that accepts both logs nothing.
        """
        if model in self._REQUEST_SHAPE_LOGGED:
            return
        self._REQUEST_SHAPE_LOGGED.add(model)
        if self._claude_version(model) is None:
            self.logger.warning(
                "Anthropic model id %r does not parse as "
                "claude-<family>-<major>[-<minor>][-<yyyymmdd>]; it takes the "
                "reduced request shape: no temperature, and forced tool_choice "
                "sent as auto plus an instruction (#1695)",
                model,
            )
            return
        dropped = []
        if not self._accepts_sampling(model):
            dropped.append("temperature is not sent")
        if not self._accepts_forced_tool_choice(model):
            dropped.append("forced tool_choice is sent as auto plus an instruction")
        if dropped:
            self.logger.info(
                "Anthropic model %s: %s (#1695)", model, "; ".join(dropped)
            )

    @staticmethod
    def _tool_use_instruction(forced_choice: dict, tools: list) -> str:
        """The sentence that stands in for forcing on a model that rejects it.

        A function of the forcing and the offered tools: it names the tool
        when the forcing names one or exactly one tool is offered, and
        otherwise asks for any of them. So it changes exactly when the tool
        set does, and an unchanged tool set gets a byte-identical sentence.
        """
        name = (
            forced_choice.get("name") if forced_choice.get("type") == "tool" else None
        )
        if not name and len(tools) == 1:
            name = tools[0].get("name")
        if name:
            return f"Respond by calling the `{name}` tool."
        return (
            "Respond by calling one of the provided tools; do not reply in plain text."
        )

    def _log_thinking_substitution(self, model: str, configured: str, msg: str) -> None:
        key = (model, configured)
        if key in self._THINKING_SUBSTITUTION_LOGGED:
            return
        self._THINKING_SUBSTITUTION_LOGGED.add(key)
        self.logger.warning(msg, model)

    def _resolve_thinking(self, max_tokens: int, model: str) -> Optional[dict]:
        """Thinking parameter for this call, or None to send none at all.

        Modes (from ProviderConfig.thinking_mode, default None → off):
        - "off"/None: no `thinking` key ever — the request is byte-identical
          to pre-#1116 behavior. This is the shipped default.
        - "adaptive": ``{"type": "adaptive"}`` — the mechanism on Claude 4.6+
          (``budget_tokens`` is deprecated on 4.6 and a 400 on 4.7+; the
          model decides how much to think). A model that rejects it (4.5 and
          older) is sent "enabled" instead.
        - "enabled": ``{"type": "enabled", "budget_tokens": N}`` — accepted
          through opus/sonnet 4.6 and haiku 4.5. A model that rejects
          ``budget_tokens`` (4.7+, an unlisted family, a version above its
          ceiling) is sent "adaptive" instead (#1756).

        An unparseable model id is sent the configured mode, unsubstituted:
        no thinking shape is accepted by every model, so an id the adapter
        cannot read gets what the operator configured.

        Starvation guard (fm#1094): thinking is billed INSIDE ``max_tokens``.
        A configuration that cannot leave ``_THINKING_MIN_ANSWER_TOKENS`` for
        the visible answer is downgraded to no-thinking with a warning — a
        starvable call is never issued.
        """
        mode = (self.config.thinking_mode or "off").strip().lower()
        if mode in ("", "off"):
            return None

        # Send the shape this model accepts (#1756); the substituted mode's own
        # guards below then apply. An unparseable id keeps the configured mode.
        configured = mode
        parsed = self._claude_version(model) is not None
        if parsed and mode == "enabled" and not self._accepts_budget_tokens(model):
            mode = "adaptive"
            self._log_thinking_substitution(
                model,
                configured,
                "ANTHROPIC_THINKING_MODE=enabled: model %s rejects budget_tokens "
                "(a 400), so its thinking requests use adaptive thinking "
                "instead; ANTHROPIC_THINKING_BUDGET_TOKENS does not apply to it "
                "(#1756)",
            )
        elif (
            parsed and mode == "adaptive" and not self._accepts_adaptive_thinking(model)
        ):
            mode = "enabled"
            self._log_thinking_substitution(
                model,
                configured,
                "ANTHROPIC_THINKING_MODE=adaptive: model %s does not support "
                "adaptive thinking (a 400), so its thinking requests use enabled "
                "thinking with ANTHROPIC_THINKING_BUDGET_TOKENS instead (#1756)",
            )

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
        self._log_request_shape(selected_model)

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
        }
        # Sampling is sent only to a model that accepts it: newer models 400
        # on ANY temperature, including 0.0 (#1695, table above).
        if self._accepts_sampling(selected_model):
            request_body["temperature"] = temperature

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
            anthropic_messages = converted["messages"]
            if cache_prompt:
                _mark_cache_boundary(anthropic_messages)
            request_body["messages"] = anthropic_messages
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

                # Handle tool_choice parameter: "required" is Anthropic's
                # {"type": "any"}; a dict is already in Anthropic format.
                tool_choice = kwargs.get("tool_choice")
                if tool_choice == "required":
                    tool_choice = {"type": "any"}
                elif tool_choice == "auto":
                    tool_choice = {"type": "auto"}
                if isinstance(tool_choice, dict):
                    forces = tool_choice.get("type") in ("any", "tool")
                    if forces and not self._accepts_forced_tool_choice(selected_model):
                        # The model 400s on forced tool use (#1695): send
                        # "auto", and put the forcing into words as a
                        # separate TRAILING system block. System, not the
                        # messages: the tool loop resends the engine's message
                        # list each iteration, so text injected into a message
                        # would be gone on the next call, a history edit that
                        # invalidates the preceding thinking blocks. Trailing,
                        # so the cached prefix above stays byte-identical. The
                        # sentence is a function of the offered tools, so it
                        # changes exactly when the tool set does, and an
                        # unchanged tool set sends a byte-identical block.
                        #
                        # "auto" asks; it does not force. A reply that still
                        # carries no tool call:
                        #   - on the single-shot structured path, fails that
                        #     attempt: the text-JSON parse recovers only a
                        #     reply that is itself JSON, and a prose reply
                        #     does not (engine side: #1755);
                        #   - on a non-final tool-loop iteration, gets the
                        #     loop's nudge and force_schema_next. Only a final
                        #     or force-schema iteration reaches the loop's
                        #     "provider ignored tool_choice=required" path.
                        #
                        # Keys other than type and name (e.g.
                        # disable_parallel_tool_use, valid with auto) are the
                        # caller's, and are carried over. A named
                        # {"type": "tool", "name": X} also narrows the offered
                        # tools to X when X is among them, so "auto" cannot
                        # pick another tool; when it is not, the tools are
                        # left as they are.
                        request_body["tool_choice"] = {
                            **{
                                k: v
                                for k, v in tool_choice.items()
                                if k not in ("type", "name")
                            },
                            "type": "auto",
                        }
                        if tool_choice.get("type") == "tool":
                            named = [
                                t
                                for t in anthropic_tools
                                if t.get("name") == tool_choice.get("name")
                            ]
                            if named:
                                request_body["tools"] = named
                        instruction = {
                            "type": "text",
                            "text": self._tool_use_instruction(
                                tool_choice, request_body["tools"]
                            ),
                        }
                        system = request_body.get("system")
                        if isinstance(system, list):
                            request_body["system"] = [*system, instruction]
                        elif isinstance(system, str) and system:
                            request_body["system"] = [
                                {"type": "text", "text": system},
                                instruction,
                            ]
                        else:
                            request_body["system"] = [instruction]
                    else:
                        request_body["tool_choice"] = tool_choice

        # Extended thinking (#1116) — applied ONLY to tool-calling
        # (structured-output) requests, mirroring Gemini's structured-only
        # thinkingConfig scope, and only when explicitly configured
        # (ANTHROPIC_THINKING_MODE, default "off": no `thinking` key and a
        # request byte-identical to pre-#1116 behavior).
        if request_body.get("tools"):
            thinking_param = self._resolve_thinking(max_tokens, selected_model)
            # Thinking supports only tool_choice auto/none — forced tool use
            # ({"type": "any"} / {"type": "tool"}) is rejected with a 400. On
            # a model that ACCEPTS forcing we FAIL CLOSED here: the caller's
            # forcing is left exactly as it set it and thinking is refused
            # for this call. A model above the forcing ceiling (#1695) never
            # reaches this refusal: it rejects forcing outright, so its
            # forcing was already sent as "auto" plus the instruction above,
            # and it can carry thinking.
            #
            # On a model that accepts forcing, the alternative — silently
            # downgrading the forcing to "auto" — trades a soundness property
            # for an experiment knob, in two ways:
            #   1. On the SINGLE-SHOT structured path
            #      (milestone_engine/generation.py ~:1737 sets
            #      tool_choice="required" for FUNCTION_CALLING providers,
            #      which is every Anthropic call) there is NO prose→schema
            #      recovery: an "auto" answer in prose leaves tool_calls
            #      empty, model_validate_json raises, with_retry exhausts and
            #      the turn fails. The nudge-retry loop is tool-loop-only.
            #   2. On DA turns, force_tool_use exists to enforce "gather
            #      evidence before concluding". Dropping it re-opens the
            #      premature-conclusion failure mode the startup tool-calling
            #      gate is built to prevent.
            # Consequence, stated deliberately: forced-schema turns on a
            # model that accepts forcing cannot carry thinking at all. Making
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
                                provider_error_code=extract_provider_error_code(
                                    error_text
                                ),
                            )
                        response_data = await response.json()
                        break
        except asyncio.TimeoutError:
            raise LLMException(
                f"Anthropic API request timed out after {self.config.timeout}s "
                f"(model: {selected_model})",
                status_code=504,  # gateway timeout — transient/retryable
            )
        except aiohttp.ClientError as e:
            # Transport failure with no HTTP status — typed so retryability is
            # DECLARED rather than inferred from aiohttp's wording (#1287).
            raise LLMException(f"Anthropic connection error: {str(e)}", retryable=True)

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


def _mark_cache_boundary(messages: list) -> None:
    """Put a second cache breakpoint at the end of the prompt's durable prefix (#613).

    The investigation prompt reaches this provider as ONE user text block whose
    first part — the standing instructions, up to ``CACHE_BOUNDARY`` — renders
    the same bytes on every turn, and whose rest is this turn's case. The
    system breakpoint in ``generate()`` caches the tools and the system
    instruction only; this one extends the cached prefix through the boundary
    line, so the next turn reads the instructions from the cache too.

    Splits at the FIRST occurrence, when the FIRST message is a user message
    whose content is a string holding ``CACHE_BOUNDARY`` with non-blank text
    after it. The first occurrence is always the template's: everything above
    it is static instruction text, which the prefix structure guard keeps free
    of case data (``test_investigation_prefix_613.py``). So case content that
    quotes the boundary lands after the split and cannot move it, or turn
    caching off. Anything else — no boundary, content that is already a block
    list, a blank tail — is left untouched: the request goes out exactly as
    before, uncached past the system block. The two text blocks concatenate to
    the original string, so the model reads the same prompt either way.
    """
    if not messages:
        return
    first = messages[0]
    if first.get("role") != "user":
        return
    content = first.get("content")
    if not isinstance(content, str) or CACHE_BOUNDARY not in content:
        return
    end = content.index(CACHE_BOUNDARY) + len(CACHE_BOUNDARY)
    if content.startswith("\n", end):
        end += 1  # the breakpoint closes the boundary LINE, newline included
    head, tail = content[:end], content[end:]
    if not tail.strip():
        return  # Anthropic rejects a blank text block
    messages[0] = {
        **first,
        "content": [
            {
                "type": "text",
                "text": head,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": tail},
        ],
    }
