"""
New LLM Router using Centralized Provider Registry.

This router replaces the old scattered configuration approach with a clean,
centralized provider registry system that handles provider management,
fallback strategies, and configuration in a unified way.

Inherits from BaseExternalClient for unified logging, retry logic, and
circuit breaker patterns for external LLM provider calls.
"""

import functools
import logging
import os
import time
from typing import Any, Dict, List, Optional

from faultmaven.config.settings import get_settings
from faultmaven.exceptions import LLMOutputFloorError
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.health.sla_tracker import sla_tracker
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.infrastructure.shims import (
    llm_latency,
    llm_requests,
    llm_stop_reasons,
    llm_tokens,
)
from faultmaven.models import DataType
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.utils.token_estimation import estimate_tokens

from .cache import LLMResponseCache
from .providers import LLMResponse, ReasoningIntent, StopReason, get_registry

# Opik native tracing for LLM calls
try:
    import opik
    from opik import opik_context

    OPIK_AVAILABLE = True
except ImportError:
    OPIK_AVAILABLE = False

TELEMETRY_PAYLOAD_MAX_CHARS = 8000

# Providers ``utils.token_estimation.estimate_tokens`` actually tokenizes.
# Everything else falls through to its ``len // 4`` character heuristic, which
# is NOT a safe measure for the output floor (see below). Mirrors the dispatch
# in that module; kept here rather than changing the shared helper, because its
# fallback is load-bearing for other callers (prompt budgeting) that want a
# middle-of-the-road estimate rather than this one's deliberate bias.
_TOKENIZER_BACKED_PROVIDERS = frozenset(
    {"openai", "openrouter", "anthropic", "fireworks"}
)

# Longest body handed to a real tokenizer. Above this we do not sample it —
# we stop measuring and fall back to the conservative rule below.
#
# tiktoken's BPE merge loop is super-linear on a long unbroken run of ONE
# character — precisely the degenerate "model looped until it hit MAX_TOKENS"
# body this branch selects for. Measured on this venv: 8k chars 91 ms, 16k
# 385 ms, 32k 1316 ms of BLOCKING CPU, against 1-4 ms for prose or dense JSON
# of the same length. The floor check runs on the event loop, so an unbounded
# call would hand a starved-and-looping response a second failure mode. At 4k
# the same worst case measures ~24 ms.
#
# WHY NOT SAMPLE-AND-SCALE. The obvious bound — tokenize a 4k prefix and scale
# by ``len(text)/4000`` — is sound only if density is uniform, which the block
# below argues at length it is not. Measured on a 16,000-char body of 4k prose
# followed by 12k id-dense JSON: 7,180 real tokens, scored 2,448 (0.34x). That
# is the SAME false positive as B1 — a body that met its floor being killed —
# reintroduced on the four providers we promise measurement for. Sampling more
# slices and taking the densest improves the average (1.23x on that body) but
# still cannot GUARANTEE the direction: any dense region the slices miss brings
# the estimate back under.
#
# The invariant is what matters, not the accuracy: the estimate must never
# UNDER-state, because under-stating kills a turn that met its floor while
# over-stating only wastes a guard. Only a bound that stops guessing satisfies
# that, so above this length we use the conservative rule — the same one the
# off-tokenizer providers already use, which makes it one policy rather than
# two. What it costs is guard REACH on long bodies, and that is affordable
# precisely because starvation produces SHORT bodies: fm#1094's was 215 chars,
# and every body at or under this bound is still tokenized EXACTLY.
_TOKENIZER_EXACT_MAX_CHARS = 4000

# Conservative ceiling, in BYTES per token: used for providers with no
# tokenizer, and for any body too long to tokenize exactly (see above). One
# rule for both — they are the same question ("we cannot measure this; what
# can we still prove?") and must not drift into two answers.
#
# WHY A DIVISOR OF 4 IS WRONG, and why the fix is not a better divisor. Real
# cl100k density by shape: CJK 0.92, base64 1.41, id-dense JSON 1.98, log
# lines 2.07, escaped JSON 2.55, English-valued JSON 4.58, English prose 6.53
# chars/token. A divisor of 4 sits in the MIDDLE of that spread, so it
# understates the dense shapes by 2-4x — and understating makes the guard fire
# on a body that MET its floor, killing an otherwise-usable turn. That false
# positive is the unsafe direction; a missed starvation merely leaves the
# pre-#1117 behaviour. But no divisor drawn from that table is safe either: it
# would be an average masquerading as a bound.
#
# THE BOUND IS PROVED, NOT MEASURED. A byte-level BPE token maps to a
# non-empty byte string, so a body of N UTF-8 bytes cannot tokenize to more
# than N tokens, whatever its script or content. That is why the unit here is
# bytes and not characters: ``tokens <= chars`` is only the ASCII COROLLARY of
# this bound, and it fails outside ASCII — measured, ``'🔥🚀💡' * 100`` is 300
# characters but 800 tokens (2.67 tokens per character), and a 4,000-string
# mixed-script fuzz put 3,912 strings over the character rule (worst 3.00) and
# ZERO over the byte count.
#
# Both ends of the contract are unchanged by the switch, because both boundary
# bodies are pure ASCII, where bytes and characters are identical:
#   fm#1094 starved body — 215 bytes vs a 500-token floor -> 215 < 500, RAISES;
#   dense-JSON false positive — 3143 bytes vs a 1000-token floor -> 3143 >= 1000,
#   PASSES (it scored 785 under ``len // 4`` and wrongly raised).
_CONSERVATIVE_BYTES_PER_TOKEN = 1


def _estimate_visible_output_tokens(
    text: str, provider: Optional[str], model: Optional[str]
) -> int:
    """Visible output tokens: exact where cheap, bounded from above elsewhere.

    INVARIANT — this must never UNDER-state the token count. Under-stating
    makes the floor fire on a body that met it, killing an otherwise-usable
    turn; over-stating only wastes a guard. Every branch below is chosen for
    that direction rather than for accuracy:

    - exact tokenization for a body short enough to measure cheaply, which is
      every body in the regime the guard exists for (starvation is short);
    - otherwise its UTF-8 byte count — an unknown or untokenizable provider,
      or a body too long to tokenize inside the CPU bound.

    Nothing is extrapolated from a sample: a scaled sample is an assumption
    about the part it did not read, and there is no sampling scheme that
    cannot be defeated by a body whose sparse part comes first.

    The byte ceiling holds for any tokenizer whose vocabulary is made of
    NON-EMPTY byte strings — byte-level BPE (the OpenAI/Anthropic families
    here) and SentencePiece with byte fallback both qualify, since every token
    consumes at least one byte of input. Two things it does not promise: a
    tokenizer that prepends BOS or a word-prefix marker can add a small O(1)
    number of tokens beyond the input's own bytes, and a vocabulary admitting
    zero-width tokens would break it outright. Neither is material against
    floors in the hundreds of tokens, but the claim is stated at exactly its
    real strength rather than as an absolute.
    """
    if not text:
        return 0

    # ``LLMResponse.provider`` is an unvalidated field, and the registry itself
    # rewrites it ("openai (low-confidence)") — so normalise, and never let a
    # ``None`` reach ``provider.lower()``. An AttributeError here would be
    # swallowed by the generic handler and reported as "All providers failed".
    provider_name = (provider or "unknown").strip().lower()
    if (
        provider_name in _TOKENIZER_BACKED_PROVIDERS
        and len(text) <= _TOKENIZER_EXACT_MAX_CHARS
    ):
        return estimate_tokens(text, provider=provider_name, model=model)

    # ONE rule serves both untokenizable cases — no tokenizer for this
    # provider, and no CPU budget for a body this long.
    return len(text.encode("utf-8")) // _CONSERVATIVE_BYTES_PER_TOKEN


def _opik_tracing_enabled() -> bool:
    """FaultMaven-level gate for the @opik.track decorators (OPIK_ENABLED).

    Fail closed: any error reading settings means no tracing. Without this
    gate the decorators are live even when OPIK_ENABLED=false and the SDK
    lazily builds a client pointed at its default (Comet Cloud), producing
    continuous outbound requests from a self-hosted deployment (#1121).

    Read LAZILY, at call time. Reading it at import time would bootstrap the
    settings singleton merely by importing this module — get_settings() runs
    load_dotenv(), applies presets that mutate os.environ, and writes
    data/.jwt_secret via ensure_local_jwt_secret_env() — so any script or
    test that imports the router would write a secret into its cwd and freeze
    settings ahead of the test fixtures that exist to clear ambient env.
    """
    try:
        return get_settings().observability.opik_enabled
    except Exception:
        return False


def _opik_track_llm(name: str):
    """Decorator: routes through @opik.track(type='llm') only when Opik is
    installed AND tracing is enabled at CALL time (OPIK_ENABLED=true).

    The tracked wrapper is built at import time (decoration alone constructs
    no client — the SDK builds one lazily on first traced call), but every
    call re-checks the gate and dispatches to the RAW function when tracing
    is off. So a disabled deployment never reaches the SDK at all: no client,
    no batch senders, no is-alive ping.
    """

    def decorator(func):
        if not OPIK_AVAILABLE:
            # Fail-closed passthrough: Opik not installed.
            return func

        tracked = opik.track(
            name=name, type="llm", capture_input=False, capture_output=False
        )(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not _opik_tracing_enabled():
                return await func(*args, **kwargs)
            return await tracked(*args, **kwargs)

        return wrapper

    return decorator


class LLMRouter(BaseExternalClient, ILLMProvider):
    """Simplified LLM router using centralized provider registry"""

    def __init__(self, confidence_threshold: float = 0.8):
        # Initialize BaseExternalClient
        super().__init__(
            client_name="llm_router",
            service_name="LLM_Providers",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for LLM failures
            circuit_breaker_timeout=30,  # Shorter timeout for LLM recovery
        )

        self.sanitizer = DataSanitizer()
        self.cache = LLMResponseCache()
        self.confidence_threshold = confidence_threshold
        self._router_initialized = False  # Track router initialization separately

        # Get timeout from settings with environment variable override.
        # Per-provider overrides (LLMSettings.provider_timeout_overrides /
        # LLM_PROVIDER_TIMEOUT_OVERRIDES) are resolved at call time in
        # ``_resolve_timeout`` below, since the chosen provider may differ
        # from the configured default when fallback chains fire.
        self.settings = get_settings()
        self.request_timeout = float(
            os.getenv("LLM_REQUEST_TIMEOUT", str(self.settings.llm.request_timeout))
        )

        # Don't initialize registry immediately - wait for first use
        self.logger.info(
            f"🔍 LLMRouter created, base request timeout: {self.request_timeout}s; "
            f"per-provider overrides: {self.settings.llm.provider_timeout_overrides or 'none'}"
        )
        self.logger.info("🔍 LLMRouter registry will be initialized on first use")

    @property
    def registry(self):
        """Always fetch the current global registry so hot-reloads take effect."""
        return get_registry()

    def _resolve_timeout(self) -> float:
        """Return the request timeout for the currently-active primary provider.

        Looks up the configured CHAT_PROVIDER name and applies any per-
        provider override from ``LLMSettings.provider_timeout_overrides``.
        Falls back to ``self.request_timeout`` (the env-overridden default)
        for unknown providers, so behaviour is unchanged when overrides
        are empty (the common case).

        Provider-aware timeouts let slow models (Fireworks DeepSeek V4 Pro,
        local Ollama on CPU) exceed the global 30-90s ceiling without
        widening it for everyone.
        """
        # ``provider`` is the field; CHAT_PROVIDER is only its env alias, and
        # there has never been a ``chat_provider`` attribute — the getattr
        # always missed, leaving the env var as the sole source. That was
        # survivable while an unset CHAT_PROVIDER could not boot; now that the
        # field carries a usable default, a deployment that never exports the
        # var resolved to None here and silently lost its per-provider timeout
        # override. Read the resolved field, keeping the env var as the
        # fallback for settings doubles that lack it.
        provider_name = getattr(self.settings.llm, "provider", None) or os.getenv(
            "CHAT_PROVIDER"
        )
        # str enums need .value; raw strings pass through unchanged.
        if provider_name is not None and not isinstance(provider_name, str):
            provider_name = getattr(provider_name, "value", str(provider_name))

        override = self.settings.llm.timeout_for_provider(provider_name)
        # If the user has explicitly raised the env var above the override,
        # respect that ceiling (env is the operator's last word).
        return float(max(override, self.request_timeout))

    @_opik_track_llm("llm_router_route")
    async def route(
        self,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        case_id: Optional[str] = None,
        provider_override: Optional[str] = None,
        cache_prompt: bool = False,
        bypass_cache: bool = False,
        reasoning_intent: Optional[ReasoningIntent] = None,
        min_output_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """
        Route request through the centralized provider registry

        cache_prompt: when True, ask the provider to cache the stable prompt
            prefix (system + tools). Only the Anthropic provider acts on it;
            every other provider pops and ignores it, so it can never leak into
            a request body. Transparent to model output.

        bypass_cache: when True, do not answer this call from the response
            cache — but still store the result. This is for a caller retrying a
            request whose cached answer it has already found unusable (a
            truncated structured-output body, #513): it needs the provider to be
            reached, and it needs the good response to replace the bad entry,
            because nothing else ever will.

        reasoning_intent: what this call needs from hidden reasoning (#1118),
            declared per call site — ``ReasoningIntent.EXTRACTION`` for
            grounded transformation of supplied context, ``INFERENCE`` for
            reasoning over candidates. Semantic on purpose: each provider
            translates it into its own mechanism (OpenAI ``reasoning_effort``,
            Gemini ``thinkingLevel``), hard model constraints override it, and
            an intent that cannot be honoured is logged by the provider, never
            silently dropped. ``None`` (default) preserves the shape-based
            per-provider defaults exactly. INVARIANT: ``INFERENCE`` must be
            paired with ``min_output_tokens`` — it lifts provider starvation
            guards, and the floor is what makes that safe; the pairing is
            enforced here with a ``ValueError``, not left as a convention.

        min_output_tokens: the minimum VISIBLE output this call needs (#1117).
            Reasoning models bill hidden reasoning against the same token
            budget the answer is drawn from, so a nominally-large ``max_tokens``
            can still yield a starved stub. The floor is enforced twice: before
            the call, ``max_tokens`` is raised to at least the floor (a total
            budget below it can never satisfy it); after the call, a response
            cut at the cap with less visible output than the floor raises
            :class:`~faultmaven.exceptions.LLMOutputFloorError` instead of
            returning a body the caller pre-declared unusable. ``None``
            (default) keeps the existing behavior — truncated responses are
            returned for the caller to inspect. A floor bounds STARVATION, not
            verbosity: a response the model finished cleanly below the floor is
            returned as-is.

            Two limits worth knowing. The floor is UNENFORCEABLE on a provider
            that reports no stop reason (HuggingFace as called, or any finish
            value newer than ``_STOP_REASON_ALIASES``): "cut" and "short" are
            indistinguishable there, so a starved-looking body is warned about
            and returned rather than raised on. And the pre-call bump raises
            ``max_tokens`` only TO the floor, which leaves no room for hidden
            reasoning to be billed from the same budget — on a reasoning path
            the caller must size ``max_tokens`` above the floor itself; a call
            declaring INFERENCE with ``max_tokens <= min_output_tokens`` is
            warned about here rather than silently given headroom this layer
            cannot correctly guess.

        Args:
            prompt: Input prompt (optional if messages is provided)
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            data_type: Type of data being processed

        Returns:
            LLMResponse with generated content
        """
        # Validate prompt or messages
        if prompt is None and not messages:
            raise TypeError("Either prompt or messages must be provided")

        # Normalize the reasoning knobs up front, loudly — a typo'd intent or a
        # nonsensical floor is a caller bug and must fail at the call site, not
        # mutate into silent provider behavior.
        reasoning_intent = ReasoningIntent.coerce(reasoning_intent)
        if reasoning_intent is ReasoningIntent.INFERENCE and min_output_tokens is None:
            # Enforced as a mechanism, not a convention: INFERENCE asks the
            # provider to lift its starvation guards (Gemini's structured
            # thinkingLevel cap among them), and reasoning bills against the
            # same budget the answer is drawn from — an inference call with no
            # declared floor re-arms exactly the silent starvation fm#1094
            # closed. The floor is what makes lifting the guard safe.
            raise ValueError(
                "reasoning_intent=INFERENCE requires min_output_tokens: "
                "reasoning shares one token budget with the visible answer, so "
                "an inference call without an output floor can starve silently "
                "(fm#1094). Declare the minimum visible output the call needs "
                "alongside the intent."
            )
        if min_output_tokens is not None:
            # ``bool`` is an ``int`` subclass and must be rejected explicitly.
            # Without the type test a float floor (config arithmetic like
            # ``max_tokens * 0.25``, or a JSON value parsed as a float) passes
            # the comparison, is assigned into ``max_tokens`` below, and
            # reaches the provider as ``{"max_completion_tokens": 1500.5}`` —
            # every provider in the chain 400s and it surfaces as the
            # misleading "All providers failed" instead of this ValueError.
            if isinstance(min_output_tokens, bool) or not isinstance(
                min_output_tokens, int
            ):
                raise ValueError(
                    f"min_output_tokens must be a positive integer, "
                    f"got {type(min_output_tokens).__name__} "
                    f"{min_output_tokens!r}"
                )
            if min_output_tokens < 1:
                raise ValueError(
                    f"min_output_tokens must be a positive integer, "
                    f"got {min_output_tokens!r}"
                )
            if max_tokens < min_output_tokens:
                # A total budget below the floor can never satisfy it — the
                # "larger total" translation from #1117. Logged so budget
                # arithmetic stays visible in traces.
                self.logger.info(
                    f"⬆️ Raising max_tokens {max_tokens} → {min_output_tokens} "
                    f"to honour the declared output floor"
                )
                max_tokens = min_output_tokens
            if (
                reasoning_intent is ReasoningIntent.INFERENCE
                and max_tokens <= min_output_tokens
            ):
                # No headroom above the floor, on the one intent that invites
                # hidden reasoning into the same budget. Every MAX_TOKENS stop
                # at this cap then has visible <= floor - reasoning < floor and
                # raises by construction; the only success is a clean STOP
                # under the cap.
                #
                # Deliberately a warning rather than a silent multiplier: what
                # headroom a model needs is a property of that model's
                # reasoning, which this layer cannot know, and inventing a
                # factor here would be an undocumented budget change applied to
                # every caller. The caller sizes max_tokens; this says so.
                self.logger.warning(
                    f"⚠️ reasoning_intent=INFERENCE with max_tokens "
                    f"({max_tokens}) <= min_output_tokens ({min_output_tokens}): "
                    f"hidden reasoning bills against the SAME budget as the "
                    f"answer, so this call can only satisfy its floor by "
                    f"finishing early — size max_tokens above the floor to "
                    f"leave room for reasoning"
                )

        # Sanitize before sending to external providers (conditional).
        # Off the event loop via the sanitizer's async boundary (#654).
        sanitized_prompt = await self._sanitize_if_needed(prompt) if prompt else None
        sanitized_messages = (
            await self._sanitize_if_needed(messages) if messages else None
        )

        # Check cache first. The cache is exact-key (#940): it can only answer
        # when the caller named a model, since the model is part of the key.
        # Multi-turn `messages` calls are skipped outright — an identical
        # message list is not a thing that recurs, so there is nothing to hit.
        cache_model = model  # Use the requested model for cache lookup
        if cache_model and not messages and sanitized_prompt and not bypass_cache:
            cached_response = self.cache.check(
                sanitized_prompt,
                cache_model,
                case_id=case_id,
                reasoning_intent=reasoning_intent,
            )
            if cached_response:
                self.logger.info("✅ Using cached response")
                if cached_response.is_truncated:
                    # UNREACHABLE while the store guard below holds — that
                    # guard declines to write a response the provider reported
                    # as cut, this cache is a per-process dict so nothing
                    # survives a restart, and the store site 100 lines down is
                    # the only writer. Kept anyway, as a backstop rather than a
                    # live path, and this comment says which it is because the
                    # earlier version claimed the opposite and contradicted the
                    # store site.
                    #
                    # It earns its place on cost: relaxing that guard, or adding
                    # a second writer (``store`` is public), reintroduces a
                    # failure that is both silent and permanent — a cut body
                    # served as an answer for the life of the process, with no
                    # TTL and no eviction API to clear it. One branch is a cheap
                    # price for that not being silent (#1094). Exercised by
                    # storing a truncated entry directly, which is exactly the
                    # shape a future writer would take.
                    self.logger.warning(
                        f"⚠️ Serving a TRUNCATED response from cache "
                        f"(provider={cached_response.provider} "
                        f"model={cached_response.model})"
                    )
                cached_response.sanitized_prompt = sanitized_prompt
                if self.settings.observability.opik_log_raw_prompts:
                    cached_response.raw_prompt = prompt
                self._update_opik_span(
                    cached_response, sanitized_prompt=sanitized_prompt, cached=True
                )
                llm_requests.labels(
                    provider=cached_response.provider,
                    model=cached_response.model,
                    status="cached",
                ).inc()
                return cached_response

        # Route through registry with BaseExternalClient wrapping
        request_started = time.monotonic()
        try:
            # Initialize registry and log provider info only once
            if not self._router_initialized:
                available = self.registry.get_available_providers()
                fallback_chain = self.registry.get_fallback_chain()
                self.logger.info(f"🔍 LLM Router: Available providers: {available}")
                self.logger.info(
                    f"🔍 LLM Router: Fallback chain: {' -> '.join(fallback_chain)}"
                )
                self._router_initialized = True

            response = await self.call_external(
                operation_name="route_llm_request",
                call_func=self.registry.route_request,
                prompt=sanitized_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
                messages=sanitized_messages,
                confidence_threshold=self.confidence_threshold,
                provider_override=provider_override,
                cache_prompt=cache_prompt,
                reasoning_intent=reasoning_intent,
                min_output_tokens=min_output_tokens,
                timeout=self._resolve_timeout(),  # Provider-aware ceiling
                retries=0,  # Retries handled inside each provider (rate-limit backoff) and via fallback chain
                retry_delay=1.0,
            )

            # Store successful response in cache. Pure dict write plus a sha256
            # — no embedding, so this is safe to do inline on the event loop.
            #
            # The gate is the *same predicate as the check above*: store only on
            # the calls the cache can later answer. Storing under the model the
            # provider happened to pick (`model or response.model`) filled the
            # cache with entries no lookup could reach, since `check` keys on the
            # model the caller named — dead writes for every no-model caller
            # (title generation, suggestions).
            #
            # Residual, deliberately not fixed here (fm#513 cluster): when the
            # fallback chain answered with a different model than the one
            # requested, the entry is still keyed under the *requested* model, so
            # a later identical call is served a response another model produced.
            #
            # ``bypass_cache`` suppresses the *lookup* only — this write still
            # runs, and it must. A caller sets the flag precisely because the
            # entry already under this key is unusable: a truncated body an
            # earlier attempt stored before the caller discovered it would not
            # parse. max_tokens is not part of the cache key, so the retry lands
            # on that same key, and the cache has no eviction API — overwriting
            # it with the complete response is the only thing that stops the
            # poisoned entry being served to every later identical call (#513).
            #
            # A response the provider SAYS it cut is never stored at all
            # (#1094). The cache exists to serve a good answer a second time,
            # and an incomplete one is never worth replaying: storing it poisons
            # the key until something happens to overwrite it, and — because
            # max_tokens is not part of the key — it is what a retry at a bigger
            # cap would be served instead of reaching the provider, silently
            # turning "retry with more room" into "return the same cut body".
            # Declining the write closes that structurally, for every caller
            # present and future, with no flag to remember to pass.
            #
            # This does NOT make ``bypass_cache`` redundant, and the two cover
            # disjoint halves. A provider that reports no stop reason
            # (HuggingFace, or any parse gap) can hand back a cut body with
            # ``is_truncated`` False: invisible here, so it IS stored, and the
            # engine's parse-time positional test is what catches it — with
            # ``bypass_cache`` the only thing stopping the retry being answered
            # from that entry. Conversely the truncation helper never retries on
            # UNKNOWN, so it can never reach that half.
            if (
                model
                and not messages
                and sanitized_prompt
                and response.confidence >= self.confidence_threshold
                and not response.is_truncated
            ):
                self.cache.store(
                    sanitized_prompt,
                    model,
                    response,
                    case_id=case_id,
                    reasoning_intent=reasoning_intent,
                )

            # Attach prompt data for telemetry
            response.sanitized_prompt = sanitized_prompt
            if self.settings.observability.opik_log_raw_prompts:
                response.raw_prompt = prompt
                response.raw_messages = messages

            response.sanitized_messages = sanitized_messages

            # Update the current Opik span with LLM-specific data
            self._update_opik_span(
                response,
                sanitized_prompt=sanitized_prompt,
                sanitized_messages=sanitized_messages,
            )

            # Truncation is a normal-looking success at every other layer: HTTP
            # 200, a body, a token count. This is the one chokepoint every
            # routed call passes through, so it is where the cut becomes
            # observable — one log line and one metric, from which the real
            # truncation rate can be read rather than inferred from token
            # counts landing suspiciously exactly on a cap (#1094).
            if response.is_truncated:
                self.logger.warning(
                    f"⚠️ LLM response truncated at the output cap "
                    f"(provider={response.provider} model={response.model} "
                    f"max_tokens={max_tokens} "
                    f"output_tokens={response.output_tokens or response.tokens_used})"
                    f" — the body is INCOMPLETE"
                )
            llm_stop_reasons.labels(
                provider=response.provider,
                model=response.model,
                stop_reason=response.stop_reason.value,
            ).inc()

            # Output floor (#1117): a response cut at the cap with less visible
            # output than the caller's declared floor is a starved answer —
            # hidden reasoning consumed the budget the answer needed — and the
            # caller pre-declared it unusable, so fail loudly instead of
            # returning it.
            #
            # The gate is ``is_truncated``, which is THREE-valued underneath,
            # and the distinction matters:
            #   MAX_TOKENS → the provider says it cut the body: enforce.
            #   STOP       → the model finished; a short answer, not a starved
            #                one. Correctly exempt.
            #   UNKNOWN    → NO SIGNAL. ``is_truncated`` reads False (its
            #                documented contract, base.py), so the floor does
            #                NOT run. That is a real gap, not just a
            #                HuggingFace footnote: ``normalize_stop_reason``
            #                maps every unrecognised value to UNKNOWN, so any
            #                finish reason a provider API adds after that table
            #                was written disables the floor on that provider.
            #                Below, a starved-looking no-signal body is warned
            #                about rather than raised on — raising would mean
            #                treating "we don't know" as "it was cut", which
            #                buys false positives on short answers.
            if min_output_tokens is not None and (
                response.is_truncated or response.stop_reason is StopReason.UNKNOWN
            ):
                # Tool-call responses can carry their whole payload in
                # ``tool_calls`` with EMPTY content — counting content alone
                # would read every floored tools call as fully starved. Count
                # both. (The OpenAI provider copies the first call's arguments
                # into content, so there the arguments count twice; that
                # overstates visible output, which errs toward NOT raising —
                # the safe direction for a guard whose false positive would
                # kill an otherwise-usable turn.)
                visible_text = response.content or ""
                for tool_call in response.tool_calls or []:
                    visible_text += str(tool_call.function or "")
                # Measured with the provider's real tokenizer where one exists,
                # and deliberately OVER-estimated where none does — never the
                # middle-of-the-range ``len//4``, which is not a conservative
                # approximation in either direction but shape-dependent:
                # English prose runs 6.5 chars/token (it OVERSTATES, guard
                # under-fires) while id-dense JSON, base64 and CJK run 0.9-2.1
                # (it UNDERSTATES by 2-4x, and the guard fires on a body that
                # MET its floor). See ``_estimate_visible_output_tokens``.
                #
                # NOT read from ``output_tokens``: OpenAI's
                # ``completion_tokens`` and Anthropic's ``output_tokens``
                # INCLUDE hidden reasoning, so on exactly the starved call this
                # exists to catch (fm#1094: ~1,946 reasoning tokens inside a
                # 2,000 count, 215 chars of answer) the reported number reads
                # as ample and the check would fail open.
                visible_output_tokens = _estimate_visible_output_tokens(
                    visible_text,
                    provider=response.provider,
                    model=response.model,
                )
                below_floor = visible_output_tokens < min_output_tokens
                if below_floor and not response.is_truncated:
                    # No stop signal: the floor cannot be enforced honestly,
                    # but silently returning a body that looks starved is how
                    # the gap stays invisible. Say so.
                    self.logger.warning(
                        f"⚠️ Output floor NOT ENFORCEABLE: ~"
                        f"{visible_output_tokens} visible output tokens < "
                        f"min_output_tokens={min_output_tokens}, but "
                        f"{response.provider}/{response.model} reported NO "
                        f"stop reason — cannot tell a cut body from a short "
                        f"answer, so the response is being returned unchecked"
                    )
                elif below_floor:
                    # Failure accounting BEFORE the raise. This path leaves via
                    # neither the success block below nor the generic handler
                    # (which would mislabel it "all providers failed"), so
                    # without this the call appears in no latency, token or SLA
                    # series at all: cost dashboards under-report real spend,
                    # and SLA availability drops starved calls from both
                    # numerator and denominator — overstating provider health
                    # during exactly the incident the tracker exists to
                    # surface. The provider did answer and the tokens were
                    # billed; only the ANSWER is unusable.
                    llm_requests.labels(
                        provider=response.provider,
                        model=response.model,
                        status="output_floor_starved",
                    ).inc()
                    llm_latency.labels(
                        provider=response.provider, model=response.model
                    ).observe(response.response_time_ms / 1000.0)
                    if response.tokens_used:
                        llm_tokens.labels(
                            provider=response.provider, model=response.model
                        ).inc(response.tokens_used)
                    sla_tracker.record_request_metrics(
                        "llm_provider", response.response_time_ms, success=False
                    )
                    raise LLMOutputFloorError(
                        f"LLM response starved below the declared output floor: "
                        f"~{visible_output_tokens} visible output tokens "
                        f"(measured over {len(visible_text)} chars of content + "
                        f"tool calls) "
                        f"< min_output_tokens={min_output_tokens}, with "
                        f"stop_reason=MAX_TOKENS at max_tokens={max_tokens} "
                        f"(provider={response.provider} model={response.model})"
                    )

            # Prometheus + SLA accounting (no-ops when metrics are disabled)
            llm_requests.labels(
                provider=response.provider, model=response.model, status="success"
            ).inc()
            llm_latency.labels(
                provider=response.provider, model=response.model
            ).observe(response.response_time_ms / 1000.0)
            if response.tokens_used:
                llm_tokens.labels(provider=response.provider, model=response.model).inc(
                    response.tokens_used
                )
            sla_tracker.record_request_metrics(
                "llm_provider", response.response_time_ms, success=True
            )

            return response

        except LLMOutputFloorError:
            # Not a provider failure — the provider answered; the answer is one
            # the caller pre-declared unusable. Its dedicated metric was
            # recorded at the raise site; re-raise without the misleading
            # "all providers failed" framing below.
            raise
        except Exception as e:
            # Provider unknown on failure (the whole fallback chain failed)
            llm_requests.labels(
                provider="unknown", model=model or "unknown", status="error"
            ).inc()
            sla_tracker.record_request_metrics(
                "llm_provider",
                (time.monotonic() - request_started) * 1000.0,
                success=False,
            )
            self.logger.error(
                f"❌ LLM Router: All providers failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    def _update_opik_span(
        self,
        response: LLMResponse,
        sanitized_prompt: Optional[str] = None,
        sanitized_messages: Optional[List[Dict[str, Any]]] = None,
        cached: bool = False,
    ) -> None:
        """Update the current Opik span with LLM call data.

        Uses opik_context.update_current_span() to attach prompt, response,
        model, provider, and token usage to whichever span is currently active.
        This works whether the span was created by @opik.track on this function
        or by a parent caller.
        """
        # Gate FIRST, warn second. Tracing being off is the configured state,
        # not a problem to report: with tracing disabled there is no span to
        # update, and update_current_span would raise (then log) once per LLM
        # call. Ordering this after the OPIK_AVAILABLE warning would silence
        # that per-call spam only for opik-installed-but-disabled, leaving the
        # far more common standalone install (opik is not in
        # requirements/dev.txt) still logging on every call — the same
        # log-spam defect #1121 is about.
        if not _opik_tracing_enabled():
            return
        if not OPIK_AVAILABLE:
            self.logger.warning("Opik SDK not available — skipping span update")
            return

        try:
            input_data = {}
            if sanitized_prompt:
                input_data["prompt"] = sanitized_prompt[:TELEMETRY_PAYLOAD_MAX_CHARS]
            if sanitized_messages:
                # Truncate messages if they become too large for telemetry stringification
                messages_str = str(sanitized_messages)
                input_data["messages"] = messages_str[:TELEMETRY_PAYLOAD_MAX_CHARS]
            output_data = {}
            if response.content:
                output_data["response"] = response.content[:TELEMETRY_PAYLOAD_MAX_CHARS]

            metadata = {
                "cached": cached or response.cached,
                "prompt_cache_hit": getattr(response, "prompt_cache_hit", False),
                "tokens_used": response.tokens_used,
                "response_time_ms": response.response_time_ms,
                "confidence": response.confidence,
                "cache_read_tokens": getattr(response, "cache_read_tokens", 0),
                "cache_write_tokens": getattr(response, "cache_write_tokens", 0),
            }

            usage = None
            if response.tokens_used:
                usage = {
                    "total_tokens": response.tokens_used,
                    "prompt_tokens": getattr(response, "input_tokens", 0),
                    "completion_tokens": getattr(response, "output_tokens", 0),
                }

            opik_context.update_current_span(
                input=input_data,
                output=output_data,
                metadata=metadata,
                model=response.model,
                provider=response.provider,
                usage=usage,
            )
        except Exception as e:
            self.logger.warning(f"Failed to update Opik span: {e}")

    async def generate(self, prompt: Optional[str] = None, **kwargs) -> LLMResponse:
        """
        ILLMProvider interface implementation - delegates to route()

        This method provides the standard ILLMProvider interface while leveraging
        all the existing functionality of the router including caching, sanitization,
        fallback strategies, and provider registry management.

        Args:
            prompt: Input prompt for text generation (optional if messages is provided in kwargs)
            **kwargs: Additional parameters including:
                - model: Specific model to use (optional)
                - max_tokens: Maximum tokens to generate (default: 1000)
                - temperature: Sampling temperature (default: 0.7)
                - data_type: Type of data being processed (optional)
                - response_format: Structured output format (optional)
                - tools: Tool/function definitions (optional)
                - tool_choice: Tool choice strategy (optional)
                - reasoning_intent: Caller-declared reasoning need (#1118, optional)
                - min_output_tokens: Visible-output floor (#1117, optional)

        Returns:
            LLMResponse with generated content

        Raises:
            TypeError: If prompt is None
            Exception: If all providers fail to generate a response
        """
        # Extract parameters from kwargs with defaults
        model = kwargs.get("model")
        max_tokens = kwargs.get("max_tokens", 1000)
        temperature = kwargs.get("temperature", 0.7)
        data_type = kwargs.get("data_type")
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        response_format = kwargs.get("response_format")
        messages = kwargs.get("messages")
        case_id = kwargs.get("case_id")
        provider_override = kwargs.get("provider_override")
        cache_prompt = kwargs.get("cache_prompt", False)
        bypass_cache = kwargs.get("bypass_cache", False)
        reasoning_intent = kwargs.get("reasoning_intent")
        min_output_tokens = kwargs.get("min_output_tokens")

        # Call existing route method with all the robust functionality
        response = await self.route(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_type=data_type,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            messages=messages,
            case_id=case_id,
            provider_override=provider_override,
            cache_prompt=cache_prompt,
            bypass_cache=bypass_cache,
            reasoning_intent=reasoning_intent,
            min_output_tokens=min_output_tokens,
        )

        # Return the full LLMResponse (milestone_engine expects this)
        return response

    async def _sanitize_if_needed(self, data: Any) -> Any:
        """
        Conditionally sanitize data based on SANITIZE_PII setting.

        Runs off the event loop via the sanitizer's async boundary so a large
        prompt / message history never blocks the loop (#654).

        Returns:
            Sanitized or original data
        """
        if self.settings.protection.sanitize_pii:
            self.logger.debug("🔒 LLM Router: Applying PII sanitization")
            return await self.sanitizer.asanitize(data)
        else:
            self.logger.debug("🔓 LLM Router: Skipping PII sanitization")
            return data

    def get_provider_status(self):
        """Get status of all providers"""
        return self.registry.get_provider_status()

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Check if the primary provider/model supports tool calling.

        Delegates to the primary provider in the fallback chain.
        """
        self.registry._ensure_initialized()

        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            return False

        primary_provider = self.registry._providers.get(fallback_chain[0])
        if not primary_provider:
            return False

        return primary_provider.supports_tool_calling(model)

    def get_structured_output_capability(self, model: Optional[str] = None):
        """
        Get the structured output capability for the primary provider/model.

        Delegates to the primary provider in the fallback chain.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability level
        """
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
        )

        # Ensure registry is initialized
        self.registry._ensure_initialized()

        # Get the primary provider (first in fallback chain)
        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            self.logger.warning("No providers available for capability detection")
            return StructuredOutputCapability.BEST_EFFORT

        primary_provider_name = fallback_chain[0]
        primary_provider = self.registry._providers.get(primary_provider_name)

        if not primary_provider:
            self.logger.warning(f"Primary provider {primary_provider_name} not found")
            return StructuredOutputCapability.BEST_EFFORT

        # Delegate to the primary provider
        capability = primary_provider.get_structured_output_capability(model)

        self.logger.debug(
            f"Router delegating capability detection to {primary_provider_name}: "
            f"{capability.value}"
        )

        return capability

    def get_structured_output_strategy(
        self, schema: Dict[str, Any], model: Optional[str] = None
    ):
        """
        Get the appropriate structured output strategy for the primary provider/model.

        Delegates to the primary provider in the fallback chain.

        Args:
            schema: Pydantic JSON schema
            model: Model name (uses default if None)

        Returns:
            StructuredOutputStrategy with configuration
        """
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputStrategy,
        )

        # Ensure registry is initialized
        self.registry._ensure_initialized()

        # Get the primary provider (first in fallback chain)
        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            self.logger.warning("No providers available for strategy creation")
            # Return a safe default strategy
            from faultmaven.infrastructure.llm.structured_output_capability import (
                StructuredOutputCapability,
                create_strategy_for_capability,
            )

            return create_strategy_for_capability(
                StructuredOutputCapability.BEST_EFFORT, schema
            )

        primary_provider_name = fallback_chain[0]
        primary_provider = self.registry._providers.get(primary_provider_name)

        if not primary_provider:
            self.logger.warning(f"Primary provider {primary_provider_name} not found")
            from faultmaven.infrastructure.llm.structured_output_capability import (
                StructuredOutputCapability,
                create_strategy_for_capability,
            )

            return create_strategy_for_capability(
                StructuredOutputCapability.BEST_EFFORT, schema
            )

        # Delegate to the primary provider
        strategy = primary_provider.get_structured_output_strategy(schema, model)

        self.logger.info(
            f"Router delegating strategy creation to {primary_provider_name}: "
            f"mode={strategy.mode.value}, include_schema_in_prompt={strategy.include_schema_in_prompt}"
        )

        return strategy
