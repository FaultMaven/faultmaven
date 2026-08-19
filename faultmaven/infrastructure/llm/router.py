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

from .cache import LLMResponseCache
from .providers import LLMResponse, get_registry

# Opik native tracing for LLM calls
try:
    import opik
    from opik import opik_context

    OPIK_AVAILABLE = True
except ImportError:
    OPIK_AVAILABLE = False

TELEMETRY_PAYLOAD_MAX_CHARS = 8000


def _opik_track_llm(name: str):
    """Decorator: wraps function with @opik.track(type='llm') when Opik is available."""
    if OPIK_AVAILABLE:
        return opik.track(
            name=name, type="llm", capture_input=False, capture_output=False
        )

    # No-op passthrough when Opik is not installed
    def identity(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)

        return wrapper

    return identity


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
        provider_name = getattr(self.settings.llm, "chat_provider", None) or os.getenv(
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
                sanitized_prompt, cache_model, case_id=case_id
            )
            if cached_response:
                self.logger.info("✅ Using cached response")
                if cached_response.is_truncated:
                    # The entry survived a store, so a one-time cut is now the
                    # permanent answer for this key: there is no TTL and no
                    # eviction API, and only a `bypass_cache` retry ever
                    # overwrites an entry. Consumers still see `is_truncated`
                    # and act on it, but the replay itself is worth saying out
                    # loud (#1094).
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
            # entry already under this key is unusable: the truncated body the
            # first attempt stored before the caller discovered it would not
            # parse. max_tokens is not part of the cache key, so the retry lands
            # on that same key, and the cache has no eviction API — overwriting
            # it with the complete response is the only thing that stops the
            # poisoned entry being served to every later identical call (#513).
            if (
                model
                and not messages
                and sanitized_prompt
                and response.confidence >= self.confidence_threshold
            ):
                self.cache.store(sanitized_prompt, model, response, case_id=case_id)

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
