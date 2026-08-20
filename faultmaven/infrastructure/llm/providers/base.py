"""
Base provider interface for LLM providers.

This module defines the abstract base class that all LLM providers must implement,
ensuring consistent behavior and configuration across all provider implementations.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

# Import structured output capability system
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputStrategy,
    create_strategy_for_capability,
)


class StopReason(str, Enum):
    """Why the model stopped generating, normalised across nine provider APIs.

    Every provider reports this and every provider words it differently —
    OpenAI ``finish_reason: "length"``, Anthropic ``stop_reason: "max_tokens"``,
    Gemini ``finishReason: "MAX_TOKENS"``, Ollama ``done_reason: "length"``,
    llama.cpp a ``stopped_limit`` boolean. Consumers must never match on those
    strings: a normalised enum is the single vocabulary, and the raw value is
    only ever read by the provider that produced it.

    ``UNKNOWN`` is a real state, not a synonym for "finished normally". Some
    providers supply no signal at all on the way we call them (HuggingFace),
    and any response we did not parse a reason out of lands here too. Collapsing
    it into "not truncated" would make every ``if response.is_truncated`` check
    fail open — silently reporting a cut body as complete, which is exactly the
    defect this field exists to close.

    ``CONTENT_FILTER`` is deliberately distinct from ``MAX_TOKENS``. Both cut
    the body short, but the recovery is opposite: retrying a length-stop with a
    bigger budget is correct, retrying a safety block with a bigger budget just
    spends money to be refused again.
    """

    STOP = "stop"  # Model finished on its own (or hit a stop sequence)
    MAX_TOKENS = "max_tokens"  # Output cut at the generation cap — INCOMPLETE
    CONTENT_FILTER = "content_filter"  # Blocked by the provider's safety layer
    TOOL_CALLS = "tool_calls"  # Stopped to hand control back for a tool call
    UNKNOWN = "unknown"  # No signal parsed — NOT a claim that it completed


# Raw provider spellings → normalised reason. Keys are lowercased before lookup.
#
# One shared table rather than a map per provider because the vocabularies are
# disjoint: no provider spells "length" to mean anything but the output cap, and
# no two providers use the same token for different reasons. A single table is
# therefore unambiguous, and it means a new provider usually needs no new
# mapping code at all.
_STOP_REASON_ALIASES: Dict[str, "StopReason"] = {
    # Natural completion
    "stop": StopReason.STOP,
    "stop_sequence": StopReason.STOP,
    "end_turn": StopReason.STOP,  # Anthropic
    "complete": StopReason.STOP,  # Cohere v2
    "eos_token": StopReason.STOP,  # HuggingFace TGI
    "eos": StopReason.STOP,
    # Output cap reached — the body is INCOMPLETE
    "length": StopReason.MAX_TOKENS,  # OpenAI-family, Ollama done_reason
    "max_tokens": StopReason.MAX_TOKENS,  # Anthropic, Cohere v2, Gemini
    "max_length": StopReason.MAX_TOKENS,  # HuggingFace TGI
    "stopped_limit": StopReason.MAX_TOKENS,  # llama.cpp
    "model_length": StopReason.MAX_TOKENS,  # vLLM
    # Safety / policy block — NOT truncation, do not retry bigger
    "content_filter": StopReason.CONTENT_FILTER,  # OpenAI-family
    "safety": StopReason.CONTENT_FILTER,  # Gemini
    "recitation": StopReason.CONTENT_FILTER,  # Gemini
    "prohibited_content": StopReason.CONTENT_FILTER,  # Gemini
    "blocklist": StopReason.CONTENT_FILTER,  # Gemini
    # Gemini's "blocked for a reason it did not name" — still a block, and the
    # Gemini provider treated it as one before this table existed.
    "blocked_reason_unspecified": StopReason.CONTENT_FILTER,
    "spii": StopReason.CONTENT_FILTER,  # Gemini
    "image_safety": StopReason.CONTENT_FILTER,  # Gemini
    "refusal": StopReason.CONTENT_FILTER,
    # Handing control back for a tool call
    "tool_calls": StopReason.TOOL_CALLS,  # OpenAI-family
    "tool_call": StopReason.TOOL_CALLS,  # Cohere v2
    "tool_use": StopReason.TOOL_CALLS,  # Anthropic
    "function_call": StopReason.TOOL_CALLS,
}


def normalize_stop_reason(raw: Any) -> StopReason:
    """Map a provider's raw stop/finish value onto :class:`StopReason`.

    Anything unrecognised — including ``None``, an empty string, and a value a
    provider added after this table was written — becomes ``UNKNOWN`` rather
    than ``STOP``. Guessing "it finished" from silence is the failure mode the
    enum exists to prevent.
    """
    if raw is None:
        return StopReason.UNKNOWN
    if isinstance(raw, StopReason):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return StopReason.UNKNOWN
    return _STOP_REASON_ALIASES.get(text, StopReason.UNKNOWN)


class ReasoningIntent(str, Enum):
    """What a call needs from hidden reasoning, declared by the CALLER (#1118).

    Before this existed, providers inferred the reasoning decision from the
    call's *shape* (tools present / ``response_format`` / plain chat). Shape is
    a proxy for purpose, and a coarse one: KB synthesis and hypothesis
    generation are both structured calls, but one is grounded extraction and
    the other is inference over candidate causes — the provider cannot tell
    them apart from shape alone.

    The vocabulary is deliberately semantic, not a raw effort level.
    ``reasoning_effort`` is OpenAI's word, ``thinkingLevel`` is Gemini's,
    Anthropic partitions a ``thinking`` budget — a caller in a tool module has
    no business knowing which provider it landed on. Each provider translates
    the intent into its own mechanism, and the mapping can change per provider
    without touching a single call site.

    Layering: caller intent is a REQUEST; provider/model hard constraints are
    the override (e.g. gpt-5.6 rejects function tools alongside reasoning on
    /chat/completions — that call runs with reasoning off no matter what the
    caller asked). An intent that cannot be honoured is logged, never silently
    dropped, so "I asked for reasoning and did not get it" is diagnosable.

    ``None`` (the parameter's default, not an enum member) preserves the
    shape-based per-provider defaults exactly — existing callers see no change.
    """

    # Grounded transformation of context supplied in the prompt ("answer
    # strictly from the provided documents"). Hidden reasoning adds little and
    # competes with the answer for one shared token budget — translate to the
    # provider's minimum reasoning.
    EXTRACTION = "extraction"

    # Reasoning over candidates (hypothesis generation, causal analysis).
    # Reasoning is welcome — translate to the provider's default/moderate
    # reasoning where the model allows it. NOTE: no production call site
    # declares this yet; whether any call SHOULD reason is #1116's experiment
    # to answer. This member exists so that experiment is expressible.
    INFERENCE = "inference"

    @classmethod
    def coerce(cls, value: Any) -> Optional["ReasoningIntent"]:
        """Normalise ``None`` / a member / the string spelling to a member.

        Every consumer compares with ``is`` against a member, so a raw string
        that reaches a comparison evaluates ``False`` and the intent is
        silently ignored — no exception, no log, the declaration simply lost.
        The router normalises, but providers are reached directly too
        (``milestone_engine`` binds a concrete provider; ``admin_config``
        tests connections), so each entry point coerces through here rather
        than re-implementing the check. ``cls(member) is member`` for a
        str-enum, so this is idempotent, and an unknown spelling raises
        ``ValueError`` at the boundary that introduced it.
        """
        if value is None:
            return None
        return cls(value)


@dataclass
class ToolCall:
    """Tool/function call from LLM.

    `provider_metadata` carries opaque provider-specific artifacts that must
    round-trip with the tool call across turns — e.g. Gemini 3.x's
    `thought_signature`, Anthropic's thinking-block signatures, or OpenAI
    Responses-API reasoning IDs. Stays `None` for providers that don't emit
    such artifacts (Gemini 2.5, OpenAI Chat Completions, all non-reasoning
    models). Provider serializers ignore the field when absent — zero
    behavior change for existing flows.
    """

    id: str
    type: str  # "function"
    function: Dict[str, Any]  # {"name": "...", "arguments": "..."}
    provider_metadata: Dict[str, Any] | None = None


@dataclass
class NormalizedResponse:
    """
    Normalized LLM response with validity checking.

    This replaces the old confidence-based system with explicit validity checks.
    Provider layer determines if response is actionable; orchestration layer
    assesses quality on normalized output.

    Design principles:
    - Binary validity check: is_valid property
    - Separates network/API failures from content refusals
    - Distinguishes hard refusals from helpful hedged responses
    - Provider-agnostic structure
    """

    provider: str
    model: str
    content: str = ""
    tool_calls: List[ToolCall] = None
    usage: Dict[str, int] = None
    response_time_ms: int = 0
    cached: bool = False
    raw_response: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    prompt_cache_hit: bool = False

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}

    @property
    def is_valid(self) -> bool:
        """
        Determines if the response is actionable.

        A response is VALID if:
        - Has non-empty content OR has tool_calls
        - Does not contain a hard refusal

        A response is INVALID if:
        - Both content and tool_calls are empty
        - Contains hard refusal ("I cannot help", "I'm unable to assist")

        IMPORTANT: Hedged responses are VALID:
        - "I cannot verify X without Y, but here's what I can tell you..."
        - "I'm not able to see Z, but based on the evidence..."

        Returns:
            bool: True if response is actionable, False otherwise
        """
        # Check for empty response
        has_content = bool(self.content.strip())
        has_tool_calls = bool(self.tool_calls)

        if not has_content and not has_tool_calls:
            return False

        # Check for hard refusals (complete inability to help)
        if has_content:
            content_lower = self.content.lower()

            # Hard refusal patterns - these invalidate the entire response
            hard_refusal_patterns = [
                "i cannot help",
                "i'm unable to assist",
                "i can't assist",
                "i'm not able to help",
                "i don't have access to",
                "i'm sorry, but i cannot",
                "i apologize, but i cannot",
            ]

            # Check if response starts with hard refusal (first 200 chars)
            response_start = content_lower[:200]
            for pattern in hard_refusal_patterns:
                if pattern in response_start:
                    # Check if there's substantial content after the hedge
                    # If response is short (<150 chars), it's a pure refusal
                    if len(self.content.strip()) < 150:
                        return False

                    # If response is longer, check if it's actually providing value
                    # Look for value indicators after the hedge
                    value_indicators = [
                        "however",
                        "but",
                        "although",
                        "based on",
                        "here's what",
                        "i can tell you",
                        "i notice",
                        "the evidence shows",
                    ]

                    has_value_after_hedge = any(
                        indicator in content_lower for indicator in value_indicators
                    )

                    if not has_value_after_hedge:
                        return False

        # Response is valid if we got here
        return True

    @property
    def has_structured_output(self) -> bool:
        """Check if response contains structured output (tool calls)."""
        return bool(self.tool_calls)

    @property
    def is_text_only(self) -> bool:
        """Check if response is text-only (no tool calls)."""
        return bool(self.content.strip()) and not self.tool_calls


@dataclass
class LLMResponse:
    """One provider response, normalised across the nine provider APIs.

    This carried a "DEPRECATED, will be replaced by NormalizedResponse" note
    for years. ``NormalizedResponse`` was in fact written — it is directly
    above — but nothing ever constructed it: every provider returns this type,
    and every consumer reads it. The note was therefore misdirection either
    way, inviting new cross-provider fields to be deferred to a successor that
    never arrived, which is part of how the stop reason went nine providers
    without a home. Treat THIS as the normalised response and add such fields
    here; migrating to the other class is a separate decision that has not been
    taken in either direction.

    Fields are additive with defaults. Downstream repos pin core ``@main`` and
    construct responses of their own, so a new field must never be required.
    """

    content: str
    confidence: float
    provider: str
    model: str
    tokens_used: int
    response_time_ms: int
    cached: bool = False
    tool_calls: Optional[List[ToolCall]] = None  # Function calling support
    sanitized_prompt: Optional[str] = None  # PII-sanitized prompt for telemetry
    raw_prompt: Optional[str] = None  # Raw prompt (local debugging only)
    # Provider-specific message-level artifacts that must round-trip across
    # turns. Used by Gemini 3.x to preserve the full `parts[]` array (text +
    # functionCall + thought) verbatim — each part may carry its own
    # thoughtSignature, and skipping any one of them produces an HTTP 400 on
    # the next turn. Stays `None` for providers that don't need this.
    provider_metadata: Optional[Dict[str, Any]] = None
    # Fine-grained token accounting. Buckets are DISJOINT so they can be summed
    # for cost: input_tokens is the *uncached* prompt tokens only; cached prompt
    # tokens are reported separately in cache_read_tokens. Providers whose API
    # reports an inclusive prompt count (OpenAI-family: prompt_tokens already
    # contains cached_tokens) must subtract before populating input_tokens.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # True when the provider served part of the prompt from ITS OWN prompt cache
    # (a real, reduced-rate billed read). Distinct from `cached`, which means the
    # response was served from FaultMaven's local LLMResponseCache (zero provider
    # spend). Never conflate the two — doing so mislabels billed calls as free.
    prompt_cache_hit: bool = False
    # Why generation stopped, normalised (see StopReason). Defaults to UNKNOWN
    # so a provider that has not been taught to report — or a response we could
    # not parse a reason out of — never claims to have finished cleanly.
    stop_reason: StopReason = StopReason.UNKNOWN

    @property
    def is_truncated(self) -> bool:
        """True only when the provider SAID it cut the body at the output cap.

        Derived, never stored: ``UNKNOWN`` reads as False here because there is
        nothing to act on, but it stays distinguishable on ``stop_reason`` for
        anything that needs to tell "completed" from "no signal" — logging and
        metrics especially, where the UNKNOWN share is what tells you which
        providers still have a blind spot.
        """
        return self.stop_reason is StopReason.MAX_TOKENS


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider"""

    name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    models: List[str] = None
    max_retries: int = 3
    timeout: int = 30
    default_model: Optional[str] = None
    confidence_score: float = 0.8

    def __post_init__(self):
        if self.models is None:
            self.models = []
        if self.default_model is None and self.models:
            self.default_model = self.models[0]


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers"""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.start_time = None
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the unique name of this provider"""
        pass

    @abstractmethod
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
        """
        Generate a response using this provider

        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional provider-specific parameters

        Returns:
            LLMResponse with generated content
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is properly configured and available"""
        pass

    @abstractmethod
    def get_supported_models(self) -> List[str]:
        """Get list of models supported by this provider"""
        pass

    def _start_timing(self):
        """Start timing for response measurement"""
        self.start_time = time.time()

    def _get_response_time_ms(self) -> int:
        """Get response time in milliseconds"""
        if self.start_time is None:
            return 0
        return int((time.time() - self.start_time) * 1000)

    def _validate_response_content(self, content: str) -> str:
        """Validate and clean response content"""
        if content is None:
            raise ValueError(f"{self.provider_name} returned None content")

        content = content.strip()
        if not content:
            raise ValueError(f"{self.provider_name} returned empty content")

        return content

    def get_effective_model(self, requested_model: Optional[str] = None) -> str:
        """Get the model to use, with fallback logic"""
        if requested_model and requested_model in self.config.models:
            return requested_model

        if self.config.default_model:
            return self.config.default_model

        if self.config.models:
            return self.config.models[0]

        raise ValueError(f"No valid model available for provider {self.provider_name}")

    @staticmethod
    def _extract_tool_calls_from_message(
        message: Dict[str, Any],
    ) -> Optional[List[ToolCall]]:
        """Build a list of ToolCall from an OpenAI-style message dict, or None.

        Shared by every provider that speaks the OpenAI chat-completions wire
        format (OpenAI, OpenRouter, Groq, Fireworks, Cohere v2, local
        OpenAI-compatible servers). Uses defensive ``.get()`` access so a
        malformed tool_call (missing ``id``/``type``/``function`` — possible
        from less-strict local/self-hosted servers) degrades to a best-effort
        ToolCall instead of raising ``KeyError`` and failing the turn.

        Returns ``None`` when the message carries no tool calls, so callers can
        keep ``response.tool_calls = None`` as the "no structured call" signal.
        """
        raw = message.get("tool_calls")
        if not raw:
            return None
        return [
            ToolCall(
                id=tc.get("id", ""),
                type=tc.get("type", "function"),
                function=tc.get("function", {}),
            )
            for tc in raw
        ]

    def _discard_reasoning_kwargs(
        self, kwargs: Dict[str, Any], model: Optional[str] = None
    ) -> None:
        """Pop the router-level reasoning knobs this provider cannot act on.

        ``reasoning_intent`` (#1118) and ``min_output_tokens`` (#1117) travel
        to every provider as ``generate()`` kwargs. Providers with a reasoning
        control (OpenAI ``reasoning_effort``, Gemini ``thinkingLevel``)
        translate the intent themselves; every other provider calls this at
        the top of ``generate()`` — mirroring the ``cache_prompt`` pattern —
        so the keys can never leak into a request body, and so an intent that
        cannot be applied is LOGGED rather than silently dropped.

        ``min_output_tokens`` needs no log: it is enforced at the router
        (pre-call budget bump + post-call floor check), so a provider with no
        reasoning partition to size loses nothing by dropping it.
        """
        intent = kwargs.pop("reasoning_intent", None)
        kwargs.pop("min_output_tokens", None)
        if intent is None:
            return
        # TOLERANT on purpose — deliberately not ``ReasoningIntent.coerce``.
        # This is a pure logging path in providers that have no reasoning
        # mechanism at all; raising here would turn an unrecognised intent
        # into a hard failure from a provider that was never going to act on
        # it, and behind the fallback chain every provider would raise the
        # same thing and surface it as "All providers failed".
        #
        # This is a deliberate SPLIT, not an inconsistency with the providers
        # that do raise. ``route()`` and the three translating providers
        # (OpenAI, OpenRouter via inheritance, Gemini) coerce and therefore
        # raise, because they are about to ACT on the value and a typo there
        # must fail at the call site that made it. This path only reports, so
        # tolerance costs nothing and refusing costs a turn. Validate where you
        # act; tolerate where you narrate.
        intent_value = getattr(intent, "value", intent)
        # Worded as "does not act on", not "has no reasoning control": a
        # provider may well have a reasoning mechanism (or grow one) that
        # simply is not driven by the intent yet — the log records that the
        # DECLARED intent was not translated, nothing more.
        message = (
            f"reasoning_intent='{intent_value}' declared, but "
            f"{self.provider_name} does not act on reasoning intent on this "
            f"call path (model: {model or self.config.default_model}) — "
            f"proceeding with the provider's configured behavior"
        )
        # INFERENCE warns, EXTRACTION informs. An unhonoured INFERENCE is the
        # caller asking for reasoning and not getting it — the case an
        # operator needs to see, and a fallback chain can land it on any of
        # these providers. Logging it at INFO made diagnosability a function
        # of which provider answered, because the runbook prescribes
        # ``LOG_LEVEL=WARNING`` as the log-volume remedy
        # (docs/operations/monitoring/operations-runbook.md) and OpenAI warns
        # for the identical condition. EXTRACTION stays INFO: the intent asks
        # for LESS reasoning, so not applying it risks spend, not silence.
        # Compares on the VALUE, case-insensitively, so an uncoerced string
        # spelling is classified the same as the member — the tolerance above
        # must not silently demote an INFERENCE warning to INFO, and a caller
        # that wrote ``"INFERENCE"`` meant the same thing as ``"inference"``.
        if str(intent_value).strip().lower() == ReasoningIntent.INFERENCE.value:
            self.logger.warning(message)
        else:
            self.logger.info(message)

    def _merge_extra_kwargs(
        self,
        payload: Dict[str, Any],
        kwargs: Dict[str, Any],
        model: Optional[str] = None,
    ) -> None:
        """Discard the router-level knobs, then merge what remains into *payload*.

        Six providers repeat ``payload.update({k: v for k, v in kwargs.items()
        if v is not None})`` verbatim, which sends every unconsumed kwarg to
        the provider API as a body field. That makes "remember to pop the
        router's knobs first" a convention rather than a mechanism — and the
        convention already has holes for the older ``cache_prompt`` knob. A
        provider written from the checklist in ``CLAUDE.md`` with the copied
        merge idiom would send ``reasoning_intent`` and ``min_output_tokens``
        on every routed call and be dead on arrival against any
        OpenAI-compatible endpoint.

        Routing the merge through here makes the discard automatic: a provider
        that consumes a knob pops it before calling this, and one that does not
        cannot leak it. ``None`` values are still filtered, so a caller passing
        ``tools=None`` cannot overwrite a constructed payload field.

        NOTE: ``registry.route_request`` is deliberately NOT the chokepoint —
        ``milestone_engine`` and ``admin_config`` call ``provider.generate()``
        directly, so a registry-level pop would miss them.
        """
        self._discard_reasoning_kwargs(kwargs, model=model)
        kwargs.pop("cache_prompt", None)
        payload.update({k: v for k, v in kwargs.items() if v is not None})

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Whether this provider/model supports function calling (tools API).

        Default returns True since most providers support OpenAI-compatible
        tool calling. Providers should override for models that don't support it.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            True if tool calling is supported
        """
        return True

    def supports_engine_response_schemas(self, model: Optional[str] = None) -> bool:
        """Whether this provider/model can SERVE the investigation engine's schemas.

        Separate axis from ``get_structured_output_capability``, which answers
        *how* a schema is enforced (STRICT / FUNCTION_CALLING / BEST_EFFORT). This
        answers whether the model can accept the engine's schemas **at all**: a
        constrained-decoding backend compiles the schema into a state machine and
        can reject an otherwise-valid schema for exceeding its own budget. That is
        a hard, deterministic rejection — the same model will refuse the same
        schema every time — so it is a *capability*, not a transient failure, and
        belongs beside ``supports_tool_calling`` where the startup gate can see it.

        The engine's stage schemas are large by design (DIAGNOSIS resolves to
        ~31 KB with ~100 leaf fields across 18 enums), and a model that cannot
        serve them fails only once the case reaches that stage — several turns
        into a live investigation, not at boot.

        Default True: only providers with a measured, reproducible limit override.

        Args:
            model: Model name to check (uses the provider default if None)

        Returns:
            True unless this provider/model is known to reject the engine's schemas
        """
        return True

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Get the structured output capability for this provider/model.

        **IMPORTANT: All provider subclasses MUST override this method.**

        This method determines what level of structured output support is available
        for the given model. Each provider implements provider-specific logic to
        detect capabilities based on model names and features.

        **Provider Override Pattern:**
        Subclasses MUST override this method to provide accurate capability detection:

        ```python
        def get_structured_output_capability(
            self, model: Optional[str] = None
        ) -> StructuredOutputCapability:
            effective_model = self.get_effective_model(model)

            # Provider-specific logic
            if "model-with-strict-support" in effective_model:
                return StructuredOutputCapability.STRICT
            elif "model-with-function-calling" in effective_model:
                return StructuredOutputCapability.FUNCTION_CALLING
            else:
                return StructuredOutputCapability.BEST_EFFORT
        ```

        **Default Behavior:**
        If not overridden, returns BEST_EFFORT as a conservative fallback and logs
        a warning that the provider should implement this method.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: One of STRICT, FUNCTION_CALLING, BEST_EFFORT, or NONE

        Design Reference:
            Template Method pattern - base provides default, subclasses specialize
        """
        # Conservative fallback for providers that haven't implemented this yet
        self.logger.warning(
            f"{self.provider_name} provider has not overridden get_structured_output_capability(). "
            f"Returning BEST_EFFORT as conservative default. Provider should implement this method."
        )
        return StructuredOutputCapability.BEST_EFFORT

    def get_structured_output_strategy(
        self, schema: Dict[str, Any], model: Optional[str] = None
    ) -> StructuredOutputStrategy:
        """
        Get the appropriate structured output strategy for this provider/model.

        This method determines the best strategy for requesting structured output
        based on the model's capabilities.

        Args:
            schema: Pydantic JSON schema
            model: Model name (uses default if None)

        Returns:
            StructuredOutputStrategy with configuration
        """
        capability = self.get_structured_output_capability(model)
        strategy = create_strategy_for_capability(capability, schema)

        # Log strategy for debugging
        self.logger.info(
            f"Using structured output strategy for {self.provider_name}: "
            f"mode={strategy.mode.value}, include_schema_in_prompt={strategy.include_schema_in_prompt}"
        )

        return strategy
