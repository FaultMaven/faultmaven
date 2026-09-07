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

# The two protocols a self-hosted endpoint can speak to FaultMaven.
TRANSPORT_OPENAI_COMPATIBLE = "openai_compatible"  # POST {root}/v1/chat/completions
TRANSPORT_OLLAMA_NATIVE = "ollama_native"  # POST {root}/api/generate

# Path suffixes an operator may legitimately paste into LOCAL_LLM_URL. Both the
# bare API root and the full endpoint path are accepted for each protocol,
# because both are what the upstream projects print in their own docs.
_OLLAMA_NATIVE_SUFFIXES = ("/api/generate", "/api")
_OPENAI_COMPATIBLE_SUFFIXES = ("/v1/chat/completions", "/v1")


def resolve_local_transport(base_url: Optional[str]) -> tuple[str, str]:
    """Decide which protocol ``base_url`` names, and the root to build URLs from.

    THE single source of truth for that decision (#1356 review F1): the
    capability answer and ``generate()``'s dispatch both call this, so they
    cannot disagree. A capability answer that contradicts the dispatch is worse
    than either being wrong alone — it is what makes a boot gate pass and every
    request then fail.

    Decided by the URL **path**, never by the host's name. The path is where the
    operator says which API they pointed at; the hostname says nothing about it.
    Keying on the substring "ollama" in the host — the rule this replaces — read
    ``http://ollama:11434/v1`` (Ollama's own OpenAI-compatible endpoint, and the
    service name in Ollama's own compose examples and Helm chart) as toolless,
    refused to boot on it, and flipped its verdict when the host was renamed.
    That is the same "capability from a name" category error #1356 exists to fix,
    one layer down.

    Returns ``(transport, root)``. ``root`` has any recognised API suffix
    stripped, so the caller appends the full path exactly once — a base of
    ``…:11434`` and one of ``…:11434/v1`` both POST to ``…:11434/v1/chat/completions``
    rather than the second producing ``/v1/v1/…`` and a 404 (review F2).

    A bare host resolves to the OpenAI-compatible protocol: that is what
    ``.env.example`` documents, what every serving stack except Ollama offers,
    and what Ollama also serves on the same port. Selecting Ollama's native
    ``/api/generate`` is therefore explicit — point at ``/api``.
    """
    raw = (base_url or "").strip().rstrip("/")
    lowered = raw.lower()
    for suffix in _OLLAMA_NATIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return TRANSPORT_OLLAMA_NATIVE, raw[: -len(suffix)]
    for suffix in _OPENAI_COMPATIBLE_SUFFIXES:
        if lowered.endswith(suffix):
            return TRANSPORT_OPENAI_COMPATIBLE, raw[: -len(suffix)]
    return TRANSPORT_OPENAI_COMPATIBLE, raw


class LocalProvider(BaseLLMProvider):
    """Local LLM provider implementation"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        # One-shot latch for the "declaration not honoured" warning (review F4).
        # `supports_tool_calling` is on the /health path via
        # `resolve_investigation_capability`, so an unlatched warning is emitted
        # by every liveness probe, forever, on a documented configuration.
        self._declared_tool_calling_warned = False

    def _transport(self) -> tuple[str, str]:
        """This endpoint's ``(transport, root)`` — see ``resolve_local_transport``."""
        return resolve_local_transport(self.config.base_url)

    @property
    def provider_name(self) -> str:
        return "local"

    def is_available(self) -> bool:
        """Check if local provider is properly configured"""
        return bool(self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    # --- Tool calling ------------------------------------------------------
    #
    # A model's tool-calling capability is a property of the ENDPOINT serving
    # it, not of its name (#1356). For a self-hosted endpoint exactly two such
    # properties are knowable from configuration, and this rule uses both:
    #
    # 1. The TRANSPORT, as named by the URL PATH — ``resolve_local_transport``,
    #    the same function ``generate()`` dispatches on, so the two can never
    #    disagree. Ollama's native ``/api/generate`` has no ``tool_calls`` field
    #    at all, so NO model can do tool calling over it — a protocol-level
    #    fact, not a model-level one, and not a fact about the hostname. Every
    #    other path reaches ``/v1/chat/completions``, which does carry them.
    #    (The raw llama.cpp ``/completion`` fallback is entered only when that
    #    path answers 404 at runtime; a tool call that then fails is Layer 2's
    #    job — ``ToolCallingUnsupportedError``.)
    # 2. The OPERATOR'S DECLARATION (``LOCAL_LLM_TOOL_CALLING`` →
    #    ``ProviderConfig.tool_calling``). Only the person who built the serving
    #    stack knows whether it was started with tool support (vLLM
    #    ``--enable-auto-tool-choice``, a llama.cpp build with a tool-capable
    #    chat template, …).
    #
    # So the OpenAI-compatible transport defaults to CAPABLE — the same default
    # ``BaseLLMProvider`` gives every other OpenAI-compatible provider — and the
    # operator narrows it when their stack cannot. Until #1356 the rule was
    # instead "the model name contains functionary or hermes", which refused to
    # boot on gpt-oss, Qwen, Mistral or Llama 3.3 served over vLLM with full
    # native tool calling, and told the self-hoster to adopt a cloud vendor.
    #
    # Why a declaration rather than the two alternatives:
    #
    # * A DENYLIST (the ``FireworksProvider._TOOL_CALLING_DENYLIST`` pattern)
    #   is keyed on a model id that names ONE serving stack, because Fireworks
    #   hosts the catalogue: ``…/minimax-m2p7`` is the same deployment for
    #   every user, so its incompatibility reproduces for every user. Self-
    #   hosting has no catalogue — ``qwen3-32b`` does tools under vLLM and does
    #   not under a llama.cpp build with no chat template — so a shipped
    #   denylist would be the same name-substring inference this fix removes,
    #   merely inverted, and could never be right for every operator. The
    #   declaration IS that denylist, re-keyed to the only identifier that
    #   distinguishes local endpoints: the deployment's own configuration.
    # * CLASSIFYING BY HOSTNAME (the rule this replaced, and the shape the
    #   first cut of this fix kept) is the same category error one layer down:
    #   it read Ollama's own OpenAI-compatible endpoint as toolless because the
    #   service was called "ollama", and renaming the host flipped the verdict
    #   on a byte-identical endpoint. Honouring the declaration on every
    #   transport is not the repair either — ``generate()`` dispatches on the
    #   same predicate, so a declaration the dispatch ignores just moves the
    #   failure from boot to every request.
    # * A STARTUP PROBE asks the right question but cannot answer it at boot. A
    #   local server routinely starts alongside or after the API, so "not up
    #   yet" is indistinguishable from "not capable" and the gate would fail
    #   closed on a transient — the reported symptom again, from a new cause.
    #   It would also put a live LLM call behind ``/health`` (which calls the
    #   same resolver, documented pure) and force an infrastructure import into
    #   the config-layer gate.

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Whether this local endpoint can do tool calling for *model*.

        The rule and the reasoning behind it are in the note above. In short:
        the Ollama ``/api/generate`` transport is never capable and no
        declaration can make it so — the protocol has nowhere to put a tool
        call — and the OpenAI-compatible transport is capable unless the
        operator declares otherwise.
        """
        declared = getattr(self.config, "tool_calling", None)
        transport, _root = self._transport()

        if transport != TRANSPORT_OPENAI_COMPATIBLE:
            if declared and not self._declared_tool_calling_warned:
                # Latched: /health resolves capability on every probe (F4).
                self._declared_tool_calling_warned = True
                self.logger.warning(
                    "LOCAL_LLM_TOOL_CALLING=true is not honoured for %r: this "
                    "URL names Ollama's native /api/generate API, whose response "
                    "has no tool_calls field, so tool calling is impossible "
                    "there for every model. Drop the /api suffix from "
                    "LOCAL_LLM_URL — the same port also serves the "
                    "OpenAI-compatible API, which does carry tool calls.",
                    self.config.base_url,
                )
            return False

        if declared is not None:
            return bool(declared)

        return True

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for local models.

        - FUNCTION_CALLING: a functionary/hermes model on an endpoint that can
          actually carry tool calls (see ``supports_tool_calling``).
        - BEST_EFFORT: everything else (prompt-based JSON generation).

        The model-name signal stays on THIS axis, where the boot gate's defect
        does not apply. Here the name is a PROMOTION above the safe default
        rather than a refusal: an unrecognised but capable model gets
        BEST_EFFORT, which works. Promoting every local model to
        FUNCTION_CALLING would instead change the schema path of every existing
        local deployment on no evidence — the engine would begin forcing a tool
        call for its response schema where prompt-requested JSON serves today.

        It is subordinate to ``supports_tool_calling`` so the two axes cannot
        contradict each other: an endpoint whose transport or operator says it
        cannot carry tool calls is BEST_EFFORT even when the model is named
        ``hermes``.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: FUNCTION_CALLING or BEST_EFFORT
        """
        model_lower = self.get_effective_model(model).lower()

        if self.supports_tool_calling(model) and (
            "functionary" in model_lower or "hermes" in model_lower
        ):
            return StructuredOutputCapability.FUNCTION_CALLING

        # Everything else uses BEST_EFFORT (prompt-based JSON generation)
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

        # Which protocol this endpoint speaks, decided by the SAME function
        # `supports_tool_calling` uses so the two cannot drift (#1356 review F1).
        transport, root = self._transport()

        if transport == TRANSPORT_OLLAMA_NATIVE:
            # Ollama-specific API
            try:
                return await self._call_ollama_api(
                    prompt, effective_model, max_tokens, temperature, **kwargs
                )
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                raise self._transport_error("Ollama", e) from e

        # First try OpenAI-compatible API (most common for modern local LLM servers)
        try:
            return await self._call_openai_compatible_api(
                prompt, effective_model, max_tokens, temperature, **kwargs
            )
        except Exception as openai_error:
            # Fall back to the raw llama.cpp completion endpoint only when the
            # server actually answered 404 — i.e. it is up, and this particular
            # path is not the one it serves.
            #
            # Keyed on the STATUS, not on ``"404" in str(...)``. That substring
            # matched the port in "Cannot connect to host localhost:4040", so an
            # unreachable vLLM on port 4040 was read as "wrong endpoint" and
            # sent down the fallback for a second full timeout. Same
            # bare-number-substring defect as the classifier chain in #1287,
            # one layer down. ``_call_*_api`` raise ``LLMException`` carrying
            # ``status_code``; a non-HTTP failure has none and is not a 404.
            if getattr(openai_error, "status_code", None) != 404:
                # Not a wrong-endpoint error — the fallback cannot help.
                raise openai_error
            try:
                return await self._call_llamacpp_api(
                    prompt, effective_model, max_tokens, temperature, **kwargs
                )
            except Exception as llamacpp_error:
                # Both formats failed. Report BOTH: the llama.cpp error alone
                # never explains why llama.cpp was contacted in the first place,
                # which is the 404 above.
                #
                # A transport failure on this leg is typed first so the
                # composite has a declaration to carry forward. Retryability is
                # taken from whichever underlying failure declared it —
                # otherwise this composite declares nothing, defaults to
                # non-retryable, and converts a TRANSIENT transport failure into
                # a permanent one: the #1287 shape, one layer up.
                if isinstance(
                    llamacpp_error, (asyncio.TimeoutError, aiohttp.ClientError)
                ):
                    llamacpp_error = self._transport_error("llama.cpp", llamacpp_error)
                raise LLMException(
                    f"Local LLM server failed with both API formats. "
                    f"OpenAI-compatible: {openai_error}. "
                    f"Raw llama.cpp: {llamacpp_error}",
                    retryable=(
                        getattr(openai_error, "retryable", None) is True
                        or getattr(llamacpp_error, "retryable", None) is True
                    ),
                ) from llamacpp_error

    def _transport_error(self, transport: str, error: BaseException) -> LLMException:
        """Type a transport-layer failure from one of the three local transports.

        ``_call_ollama_api`` and ``_call_llamacpp_api`` do no error handling of
        their own, so a hung or restarting local server surfaced as a bare
        ``asyncio.TimeoutError`` — whose ``str()`` is the EMPTY STRING — or as
        raw aiohttp wording. Neither can be classified downstream by message,
        and both were therefore treated as permanent (#1287). Retryability is
        declared here instead, matching what ``_call_openai_compatible_api``
        already does inline for the third transport.
        """
        if isinstance(error, asyncio.TimeoutError):
            return LLMException(
                f"Local LLM ({transport}) request timed out after "
                f"{self.config.timeout} seconds",
                status_code=504,  # gateway timeout — transient/retryable
            )
        return LLMException(
            f"Local LLM ({transport}) connection error: {str(error)}", retryable=True
        )

    async def _call_ollama_api(
        self, prompt: str, model: str, max_tokens: int, temperature: float, **kwargs
    ) -> LLMResponse:
        """Call Ollama-style API"""

        _transport, root = self._transport()

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
                f"{root}/api/generate",
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

        # Normalised root: a base already ending in /v1 must not become /v1/v1
        # and 404 (#1356 review F2).
        _transport, root = self._transport()
        self.logger.debug(f"Starting OpenAI-compatible API call to {root}")
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
                    f"{root}/v1/chat/completions",
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

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                # Routed through the SAME helper the other two transports use,
                # so this file emits one message shape rather than three. It
                # previously typed its own timeout inline and its own transport
                # error inline, both worded differently from the helper, which
                # made "what does a local transport failure look like" a
                # three-way answer in a single file.
                response_time = self._get_response_time_ms()
                self.logger.warning(
                    f"{type(e).__name__} after {response_time}ms "
                    f"(limit: {self.config.timeout * 1000}ms); "
                    f"model={model}, max_tokens={max_tokens}, "
                    f"temperature={temperature}"
                )
                raise self._transport_error("OpenAI-compatible", e) from e

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

        _transport, root = self._transport()

        # llama.cpp server uses completions endpoint, not chat/completions
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "stop": ["\\n\\n"],  # Basic stop tokens
            "stream": False,
        }

        # ``cache_prompt`` is a REAL llama.cpp body field (it reuses the KV
        # cache for a shared prompt prefix), so this transport genuinely
        # consumes the knob rather than discarding it — the one condition the
        # merge seam sets for popping something yourself.
        #
        # It has to be lifted out BEFORE the merge, because
        # ``_merge_extra_kwargs`` drops it for everyone: it is Anthropic's
        # caching hint, and the providers that do not implement it reject
        # unknown body fields. Routing this transport through that seam without
        # this line silently dropped the field that had reached the wire at
        # merge-base, costing llama.cpp-fallback deployments their prompt-prefix
        # caching on the DA tool loop (``milestone_engine`` passes
        # ``cache_prompt=True`` there on every iteration).
        cache_prompt = kwargs.pop("cache_prompt", None)
        if cache_prompt is not None:
            payload["cache_prompt"] = cache_prompt

        # Add any additional options (knobs discarded, None values filtered).
        if kwargs:
            self._merge_extra_kwargs(payload, kwargs, model=model)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{root}/completion",
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
