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

from .base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    ReasoningIntent,
    normalize_stop_reason,
)


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
        "o4",  # OpenAI o4 reasoning models
    )
    # INVARIANT: every reasoning family in ``_COMPLETION_TOKENS_MODEL_FAMILIES``
    # (gpt-5, o1, o3, o4) MUST also appear above as STRICT. Otherwise it routes
    # structured extraction through FUNCTION_CALLING (``tools``) instead of
    # ``response_format``, and the reasoning-effort cap below — scoped to
    # ``response_format`` — would silently miss the call that needs it, starving
    # the schema JSON on that model (the #625 truncation).

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

    # Reasoning effort applied to reasoning-family models on structured/tool
    # calls. These models bill hidden reasoning tokens against the output
    # budget; on a deep structured turn the reasoning can exhaust the reserve
    # and truncate the schema JSON (``MAX_TOKENS`` → 500) — the same starvation
    # the Gemini ``thinkingLevel: "low"`` cap prevents. ``"low"`` is the
    # broadly-valid floor across the gpt-5 and o-series families (gpt-5 also
    # accepts ``"minimal"``, but o-series may not — ``"low"`` is the safe common
    # denominator, mirroring the Gemini choice). The reasoning families coincide
    # with ``_COMPLETION_TOKENS_MODEL_FAMILIES``; non-reasoning models
    # (gpt-4.1/gpt-4o) reject the param and must never receive it.
    _STRUCTURED_REASONING_EFFORT = "low"

    # Effort applied to PLAIN chat calls -- no ``tools``, no ``response_format``
    # -- on families that reason by DEFAULT. Those models bill hidden reasoning
    # against ``max_completion_tokens``, the same budget the answer is drawn
    # from, so an uncapped plain call can spend nearly all of it reasoning and
    # return a truncated stub. Observed on the KB synthesis path: a 2000-token
    # budget produced a 215-character answer, ~1950 tokens having gone to hidden
    # reasoning, while sibling calls on the same prompt returned 5-8KB.
    #
    # The two branches below cover tools (forced ``"none"``) and structured JSON
    # (capped ``"low"``); a plain chat call matched NEITHER and so went out with
    # no effort param at all. ``"none"`` rather than ``"low"`` because the plain
    # calls that reach here are grounded generation -- answer strictly from
    # context supplied in the prompt -- where hidden reasoning adds little and
    # competes with the answer for one budget. Verified accepted WITHOUT tools
    # on gpt-5.6 (the tools branch only proves it valid alongside them).
    _DEFAULT_REASONING_PLAIN_EFFORT = "none"

    # Effort for a caller-declared INFERENCE intent (#1118). ``"medium"`` is
    # the API's own default level for reasoning models — the intent knob
    # re-requests the model's default rather than inventing an allocation.
    # No production call site declares INFERENCE yet (#1116 owns that
    # decision); the mapping exists so the experiment is expressible.
    _INFERENCE_REASONING_EFFORT = "medium"

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

    # o1 variants that DON'T accept ``reasoning_effort`` (o1-preview / o1-mini
    # shipped before the param existed and 400 on it), even though they DO
    # require ``max_completion_tokens`` — so the two axes diverge here and the
    # reasoning-effort detection can't simply reuse the completion-tokens regex.
    _REASONING_EFFORT_UNSUPPORTED = ("o1-mini", "o1-preview")

    @classmethod
    def _caps_reasoning_effort(cls, model_name: str) -> bool:
        """Whether ``reasoning_effort`` should be capped for this model.

        True for the reasoning families that accept the param (gpt-5.x, o1 GA,
        o3/o4) — they starve the output budget without a cap. False for
        non-reasoning models (gpt-4.1/gpt-4o), which reject the param, and for
        the ``o1-preview``/``o1-mini`` variants, which predate ``reasoning_effort``
        and 400 on it. Overridable so a gateway subclass can opt out.
        """
        name = model_name.lower()
        if any(u in name for u in cls._REASONING_EFFORT_UNSUPPORTED):
            return False
        return bool(cls._COMPLETION_TOKENS_MODEL_RE.search(name))

    # Model families with server-side DEFAULT reasoning on /v1/chat/completions:
    # they reject non-default ``temperature`` (only 1 accepted) and reject
    # function tools unless ``reasoning_effort`` is explicitly ``"none"``
    # ("use /v1/responses" otherwise). Earlier reasoning families don't share
    # either constraint — gpt-5.4-mini accepts ``temperature: 0.2`` and tool
    # calls without an effort param — so this family needs its own axis rather
    # than reusing the completion-tokens regex. The tuple lists EXACTLY the
    # verified families (probed 2026-08-03): if a later release (gpt-5.7, …)
    # ships the same constraints it must be added here deliberately — until
    # then it fails loudly with the API's own 400, never a silent behavior
    # change.
    _DEFAULT_REASONING_MODEL_FAMILIES = ("gpt-5.6",)
    _DEFAULT_REASONING_MODEL_RE = re.compile(
        r"(?:^|/)(?:"
        + "|".join(re.escape(f) for f in _DEFAULT_REASONING_MODEL_FAMILIES)
        + r")(?:[-.]|$)"
    )

    @classmethod
    def _defaults_reasoning(cls, model_name: str) -> bool:
        """Whether the model reasons by default on /v1/chat/completions.

        These models take only default ``temperature`` (omit the param) and
        need ``reasoning_effort: "none"`` whenever function tools are sent.
        Overridable so a gateway subclass can opt out.
        """
        return bool(cls._DEFAULT_REASONING_MODEL_RE.search(model_name.lower()))

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

    @classmethod
    def _shape_default_effort(
        cls, defaults_reasoning: bool, has_response_format: bool
    ) -> Optional[str]:
        """The effort this call SHAPE gets by default, or None for no default.

        Single source for a policy that was previously written twice — once as
        two guarded assignments in ``generate()`` and once as a ternary in
        ``_apply_reasoning_intent`` — in different vocabulary. They agreed, but
        the next family verified for ``"none"`` on structured calls would be
        added to one and not the other, and the stale copy would still look
        correct because the intent tests pin the old mapping independently.

        Shapes (tools excluded — that decision is a hard constraint made by the
        caller of this helper, not a default):

        - structured (``response_format``): ``"low"`` — the broadly-valid floor
          that stops hidden reasoning starving the schema body (#625);
        - plain chat on a default-reasoning family: ``"none"`` — grounded
          generation, verified accepted on gpt-5.6;
        - plain chat elsewhere: no default (the model does not reason unasked).
        """
        if has_response_format:
            return cls._STRUCTURED_REASONING_EFFORT
        if defaults_reasoning:
            return cls._DEFAULT_REASONING_PLAIN_EFFORT
        return None

    def _apply_reasoning_intent(
        self,
        payload: Dict[str, Any],
        intent: ReasoningIntent,
        model: str,
        *,
        has_tools: bool,
        has_response_format: bool,
        defaults_reasoning: bool,
    ) -> None:
        """Translate a caller's :class:`ReasoningIntent` into ``reasoning_effort``.

        Caller intent is a request; the model's hard constraints override it:

        - Function tools on /chat/completions: the gpt-5.6 family REQUIRES
          ``reasoning_effort: "none"`` (already forced above — that branch is
          correctness, not policy), and the other reasoning families 400 on the
          param alongside tools. Either way no intent can move the effort here.
        - Models that reject the param entirely (gpt-4o/gpt-4.1,
          o1-mini/o1-preview, gateway subclasses that opt out): nothing to set.

        Where the param is available:

        - INFERENCE → ``"medium"`` (the API's default reasoning level).
        - EXTRACTION → the verified minimum: ``"none"`` on the gpt-5.6 family
          for plain calls (the only shape it is verified on), ``"low"``
          otherwise — ``"low"`` is the broadly-valid floor across the gpt-5 and
          o-series families, and the safe choice wherever ``"none"`` is
          unverified.

        An intent that cannot be honoured is logged, never silently dropped
        (#1118) — including EXTRACTION on a model that rejects the parameter,
        where the model still reasons at its own default. "Rejects the
        parameter" and "will not reason" are different things, and conflating
        them is how an operator diagnosing starvation rules out the true cause.
        The one case deliberately NOT logged is an intent that was in fact
        delivered: on a default-reasoning family with tools, the forced
        ``"none"`` IS what EXTRACTION asked for.
        """
        if has_tools:
            # Nothing here can move the effort. WHY differs by model, and the
            # log has to say which — an operator sent after "/v1/responses"
            # for a model that has no reasoning at all is sent nowhere.
            if defaults_reasoning:
                # ``"none"`` was already forced by generate(): the API rejects
                # tools alongside reasoning on this family.
                if intent is ReasoningIntent.INFERENCE:
                    self.logger.warning(
                        f"reasoning_intent='inference' cannot be honoured on "
                        f"{model} with function tools: this family rejects "
                        f"tools alongside reasoning on /chat/completions, so "
                        f"reasoning_effort is pinned to 'none' — reasoning "
                        f"would need /v1/responses support"
                    )
                # EXTRACTION is silent here on purpose: the pinned "none" is
                # exactly what it requested, at the minimum the family
                # accepts. Logging a failure that did not happen spends the
                # signal these logs exist to carry.
            elif self._caps_reasoning_effort(model):
                # A reasoning family that accepts the param, but not next to
                # function tools — no effort parameter is sent at all and the
                # model reasons at its server-side default.
                level = (
                    self.logger.warning
                    if intent is ReasoningIntent.INFERENCE
                    else self.logger.info
                )
                level(
                    f"reasoning_intent='{intent.value}' not applied on {model} "
                    f"with function tools: this model 400s on reasoning_effort "
                    f"alongside tools, so no effort parameter is sent and the "
                    f"model reasons at its own default — hidden reasoning "
                    f"still bills against the output budget"
                )
            elif intent is ReasoningIntent.INFERENCE:
                # Non-reasoning model (gpt-4o/gpt-4.1): there is no reasoning
                # to enable, with or without tools.
                self.logger.warning(
                    f"reasoning_intent='inference' cannot be honoured on "
                    f"{model}: this model has no reasoning mode to enable"
                )
            return

        if not self._caps_reasoning_effort(model):
            # Two very different populations reach here: models with no
            # reasoning mode (gpt-4o), and models that reason natively but
            # reject THIS parameter (o1-mini/o1-preview; OpenRouter, which
            # opts out because it drives reasoning through its own gateway
            # object). Only the first is "no reasoning happens".
            level = (
                self.logger.warning
                if intent is ReasoningIntent.INFERENCE
                else self.logger.info
            )
            level(
                f"reasoning_intent='{intent.value}' not applied on {model}: "
                f"this provider/model does not accept reasoning_effort, so the "
                f"model's own default reasoning behavior stands — if it "
                f"reasons natively, hidden reasoning still bills against the "
                f"output budget and this intent does not change that"
            )
            return

        if intent is ReasoningIntent.INFERENCE:
            payload["reasoning_effort"] = self._INFERENCE_REASONING_EFFORT
        else:  # EXTRACTION
            # Same policy the shape-based defaults use — one source, so a
            # newly-verified family cannot be updated in one place and stay
            # stale in the other. ``or`` covers the shapes with no default
            # (a non-default-reasoning plain call), where the verified
            # minimum is "low".
            payload["reasoning_effort"] = (
                self._shape_default_effort(defaults_reasoning, has_response_format)
                or self._STRUCTURED_REASONING_EFFORT
            )

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
        # Caller-declared reasoning intent (#1118), translated into
        # ``reasoning_effort`` after the shape-based defaults below. Popped
        # here so neither key can leak into the request body. None preserves
        # the shape-based defaults exactly. ``min_output_tokens`` (#1117) is
        # enforced at the router (budget bump + floor check) — there is no
        # OpenAI parameter to translate it into on /chat/completions.
        # The router normalizes, but this provider is also called directly
        # (milestone_engine binds a concrete provider; connection tests) — so
        # coerce here too, or a raw string spelling would fail every ``is``
        # comparison below and be silently ignored.
        reasoning_intent = ReasoningIntent.coerce(kwargs.pop("reasoning_intent", None))
        kwargs.pop("min_output_tokens", None)
        token_limit_param = (
            "max_completion_tokens"
            if self._uses_completion_tokens_param(effective_model)
            else "max_tokens"
        )
        # One evaluation serves both the temperature and reasoning_effort
        # decisions below — split call sites with opposite polarity invite the
        # two axes drifting apart when the predicate's scope changes.
        defaults_reasoning = self._defaults_reasoning(effective_model)
        payload = {
            "model": effective_model,
            "messages": messages if messages else [{"role": "user", "content": prompt}],
            token_limit_param: max_tokens,
        }
        # The gpt-5.6 family accepts only the default temperature and 400s on
        # any other value; omission means default.
        if not defaults_reasoning:
            payload["temperature"] = temperature

        # Add function calling support
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
            # The gpt-5.6 family rejects function tools on /v1/chat/completions
            # unless reasoning is explicitly disabled — there is no combination
            # on this endpoint where tools run WITH reasoning (that would need
            # /v1/responses support), so "none" is mandatory, not a preference:
            # pop any stray kwargs copy so the merge below can't restore the
            # 400. Trade-off: tool-loop turns on these models run without
            # hidden reasoning.
            if defaults_reasoning:
                kwargs.pop("reasoning_effort", None)
                payload["reasoning_effort"] = "none"

        # Add response format if specified in kwargs
        response_format = kwargs.pop("response_format", None)
        if response_format:
            payload["response_format"] = response_format

        # Shape-based reasoning effort, from the single policy helper.
        #
        # Structured JSON (``response_format``) on a reasoning family is capped
        # so hidden reasoning can't starve the output reserve and truncate the
        # schema (#625); a plain chat call on a default-reasoning family is
        # pinned to "none" so uncapped server-side reasoning can't bill against
        # the answer's own budget. Set BEFORE the kwargs merge, so a caller
        # passing ``reasoning_effort`` deliberately still overrides it.
        #
        # Scoped to calls WITHOUT ``tools``: newer GPT-5.x (e.g. gpt-5.4-mini)
        # 400 on ``reasoning_effort`` + FUNCTION TOOLS on /v1/chat/completions
        # ("use /v1/responses instead"), and the gpt-5.6 tools branch above has
        # already pinned its mandatory "none". ``_caps_reasoning_effort`` is
        # the "does this model accept the parameter at all" predicate and the
        # documented opt-out hook for gateway subclasses.
        if not tools and self._caps_reasoning_effort(effective_model):
            shape_effort = self._shape_default_effort(
                defaults_reasoning, bool(response_format)
            )
            if shape_effort is not None:
                payload["reasoning_effort"] = shape_effort

        # Caller-declared intent (#1118) refines the shape-based defaults
        # above; the hard constraints (tools, models that reject the param)
        # stay in charge. Runs BEFORE the kwargs merge so an explicit
        # ``reasoning_effort`` kwarg still has the last word.
        if reasoning_intent is not None:
            self._apply_reasoning_intent(
                payload,
                reasoning_intent,
                effective_model,
                has_tools=bool(tools),
                has_response_format=bool(response_format),
                defaults_reasoning=defaults_reasoning,
            )

        # Discards the router-level knobs (this provider has already consumed
        # the intent above; the discard is what stops a FUTURE knob leaking),
        # drops the Anthropic-only caching hint, then merges the rest.
        self._merge_extra_kwargs(payload, kwargs, model=effective_model)

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

                    choice = data["choices"][0]
                    message = choice["message"]

                    # Why generation stopped. "length" means the body was cut at
                    # max_completion_tokens and is INCOMPLETE — the caller has to
                    # be able to see that, so it travels on the response instead
                    # of being dropped here (#1094).
                    stop_reason = normalize_stop_reason(choice.get("finish_reason"))

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

                    # Extract token usage. prompt_tokens is INCLUSIVE of cached
                    # prompt tokens on OpenAI-family APIs, so subtract to keep
                    # input_tokens/cache_read_tokens disjoint for cost summing.
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
                f"OpenAI API request timed out after {self.config.timeout}s "
                f"(model: {effective_model})",
                status_code=504,  # gateway timeout — transient/retryable
            )
