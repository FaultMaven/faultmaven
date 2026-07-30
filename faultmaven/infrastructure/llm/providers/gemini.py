"""
Google Gemini provider implementation.

This module implements the Google Gemini LLM provider with multi-modal
capabilities for text and image processing.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

import aiohttp

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .base import BaseLLMProvider, LLMResponse, ProviderConfig, ToolCall


class GeminiProvider(BaseLLMProvider):
    """Google Gemini LLM provider implementation"""

    @property
    def provider_name(self) -> str:
        return "gemini"

    def is_available(self) -> bool:
        """Check if Gemini provider is properly configured"""
        return bool(self.config.api_key and self.config.base_url and self.config.models)

    def get_supported_models(self) -> List[str]:
        """Get list of supported Gemini models"""
        return self.config.models.copy()

    # Gemini models whose constrained-decoding budget is too small for the
    # investigation engine's stage schemas. They advertise STRICT controlled
    # generation and honour it for small schemas, then reject the large ones with
    # ``400 ... "The specified schema produces a constraint that has too many
    # states for serving"``.
    #
    # Measured 2026-07-30 against the resolved (post-``_resolve_refs_for_gemini``)
    # DIAGNOSIS schema — 31,403 bytes, object depth 3, ~102 leaf fields, 18 enums:
    #   gemini-2.5-flash  6/6 rejected, deterministic
    #   gemini-3.5-flash  accepted (same bytes, same schema)
    # Smaller stage schemas (Mitigation 23 KB, Treatment 27 KB) and INQUIRY
    # (6 KB) are accepted by both, which is why the failure only surfaces once a
    # case reaches DIAGNOSIS — several turns into a live investigation.
    #
    # Entries are matched as model-id PREFIXES, so a measured base id also covers
    # its dated / aliased / tier variants (``gemini-2.5-flash-002``,
    # ``-preview-09-2025``, ``-latest``, ``-lite``) — ``GEMINI_MODEL`` is free-form
    # and those variants share the same constrained-decoding backend. The
    # asymmetry justifies the breadth: over-matching refuses boot with an
    # actionable message and the operator names another model, while under-matching
    # silently reproduces the incident several turns into a live investigation.
    #
    # NOT a version comparison — capacity is not monotonic in version number, so a
    # newer model is never assumed capable. A prefix belongs here only with a
    # reproducible measurement, matching the Fireworks
    # ``_TOOL_CALLING_DENYLIST`` precedent.
    _SCHEMA_CAPACITY_DENYLIST_PREFIXES = ("gemini-2.5-flash",)

    def supports_engine_response_schemas(self, model: Optional[str] = None) -> bool:
        """False for models measured to reject the engine's large stage schemas.

        Resolves the model the way :meth:`generate` does — ``model or
        config.default_model``, verbatim — and deliberately NOT via
        ``get_effective_model``, which collapses anything outside
        ``config.models`` to the default. Using the latter let the gate clear a
        capable *default* while the request path went out with the denylisted model
        actually requested (e.g. a ``GEMINI_DA_MODEL`` absent from
        ``config.models``). A gate must judge the string that reaches the wire.

        See ``_SCHEMA_CAPACITY_DENYLIST_PREFIXES`` for the measurement behind each
        entry and why matching is prefix-based.
        """
        effective = (model or self.config.default_model or "").lower()
        return not effective.startswith(self._SCHEMA_CAPACITY_DENYLIST_PREFIXES)

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Determine structured output capability for Gemini models.

        Gemini's "controlled generation" (decoder-enforced schema via
        ``generation_config.response_schema``) is supported on 1.5 and
        all subsequent major versions. Only 1.0 was prompt-based only.

        The previous allow-list ("2.0 in model OR 1.5 in model") missed
        Gemini 2.5 and any future 3.x release, causing them to fall
        through to BEST_EFFORT and emit schema-invalid output (Run 18
        Variants D and E on gemini-2.5-pro). The pattern below matches
        ``gemini-1.5-*``, ``gemini-2.<n>-*``, ``gemini-3.<n>-*``, etc.
        — any major version >= 1.5.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: STRICT for 1.5+, BEST_EFFORT for 1.0
        """
        effective_model = self.get_effective_model(model)
        model_lower = effective_model.lower()

        # Match Gemini 1.5 OR any major version 2 and up (2.0, 2.5, 3.0, ...).
        # Tightened to require "gemini-" prefix to avoid false positives from
        # incidental "1.5" / "2.x" substrings elsewhere in the model string.
        if re.search(r"gemini-(?:1\.5|[2-9]\.\d+|\d{2,}\.\d+)", model_lower):
            return StructuredOutputCapability.STRICT

        # Gemini 1.0 (and any unrecognized model string) uses BEST_EFFORT
        return StructuredOutputCapability.BEST_EFFORT

    # Thinking level for structured-output calls on Gemini 3.x+ models.
    # 3.x thinking models bill hidden reasoning against maxOutputTokens; uncapped
    # thinking starves the actual JSON output (see _structured_thinking_config).
    # 3.x dropped the 2.5-era integer ``thinkingBudget`` for a string
    # ``thinkingLevel``; "low" is the lowest broadly-valid level — it bypasses
    # the heavy reasoning loop so output isn't starved, without depending on
    # whether "minimal" is in a given model's enum.
    #
    # Scope note: this is deliberately 3.x-ONLY. Gemini 2.5 also bills thinking
    # against maxOutputTokens, but the truncation→500 was only ever observed on
    # 3.x flash; 2.5-pro ran clean (the .env default during validation). Capping
    # 2.5 would change a working, reasoning-heavy path with no evidence of need,
    # so 2.5 is intentionally left at its native dynamic thinking. Revisit only
    # if 2.5 starvation is actually observed.
    _GEMINI_3X_STRUCTURED_THINKING_LEVEL = "low"

    @staticmethod
    def _gemini_major_version(model: str) -> Optional[int]:
        """Major version integer from a gemini model id (``gemini-3.5-flash`` ->
        3), or None if not parseable. Used to scope the thinking cap to 3.x+."""
        m = re.search(r"gemini-(\d+)\.", model.lower())
        return int(m.group(1)) if m else None

    def _structured_thinking_config(
        self, model: str, is_structured: bool
    ) -> Optional[Dict[str, Any]]:
        """``thinkingConfig`` payload for a structured-output call, or None to
        leave it unset.

        Caps thinking ONLY on Gemini 3.x+ models, where dynamic thinking
        starved the structured JSON output and truncated to a MAX_TOKENS 500
        (observed on gemini-3.5-flash, the shipped Gemini default). Returns
        ``{"thinkingLevel": "low"}`` there.

        Returns None everywhere else:
        - non-structured calls (partial text is still usable, so starvation is
          not fatal);
        - Gemini 1.5/2.0 (reject ``thinkingConfig`` with a 400);
        - Gemini 2.5 — bills thinking against maxOutputTokens too, but was not
          observed to starve; left at native dynamic thinking rather than
          changing a working, reasoning-heavy path without evidence.
        """
        if not is_structured:
            return None

        major = self._gemini_major_version(model)
        if major is not None and major >= 3:
            # 3.x+ uses the string thinkingLevel; the 2.5-era integer
            # thinkingBudget 400s on these models.
            return {"thinkingLevel": self._GEMINI_3X_STRUCTURED_THINKING_LEVEL}

        return None

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """
        Generate text using Google Gemini API

        Args:
            prompt: Input prompt for text generation
            model: Specific Gemini model to use
            max_tokens: Maximum tokens to generate (mapped to maxOutputTokens)
            temperature: Sampling temperature (0.0-2.0)
            **kwargs: Additional parameters

        Returns:
            LLMResponse with generated text
        """
        start_time = time.time()

        # Use specified model or default
        selected_model = model or self.config.default_model
        if not selected_model:
            selected_model = "gemini-1.5-pro"

        # Prepare generation config for Gemini API
        generation_config = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }

        # Add optional parameters
        if "top_p" in kwargs:
            generation_config["topP"] = kwargs["top_p"]
        if "top_k" in kwargs:
            generation_config["topK"] = kwargs["top_k"]
        if "stop_sequences" in kwargs:
            generation_config["stopSequences"] = kwargs["stop_sequences"]

        # Handle structured output (json_schema or json_object)
        rf = kwargs.get("response_format")
        if rf:
            if rf.get("type") == "json_schema":
                generation_config["response_mime_type"] = "application/json"
                if "json_schema" in rf and "schema" in rf["json_schema"]:
                    # Gemini's response_schema (OpenAPI-3.0 subset) rejects
                    # $ref/$defs/anyOf that Pydantic emits for nested/Optional/
                    # Union models. Inline+convert them, same as the tool path.
                    generation_config["response_schema"] = (
                        self._resolve_refs_for_gemini(rf["json_schema"]["schema"])
                    )
            elif rf.get("type") == "json_object":
                generation_config["response_mime_type"] = "application/json"

        # Extract multi-turn messages and tool calling params
        messages = kwargs.pop("messages", None)
        tools_param = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)

        # Prepare request body for Gemini API format
        if messages:
            converted = self._convert_messages_to_gemini(messages)
            request_body = {
                "contents": converted["contents"],
                "generationConfig": generation_config,
            }
            if converted.get("system_instruction"):
                request_body["systemInstruction"] = {
                    "parts": [{"text": converted["system_instruction"]}]
                }
        else:
            request_body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            }

        # Add function calling tools
        if tools_param:
            function_declarations = []
            for tool in tools_param:
                if tool.get("type") == "function":
                    func = tool["function"]
                    params = func.get("parameters", {})
                    # Gemini doesn't support $ref/$defs/anyOf — resolve them
                    params = self._resolve_refs_for_gemini(params)
                    function_declarations.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "parameters": params,
                        }
                    )
            if function_declarations:
                request_body["tools"] = [
                    {"functionDeclarations": function_declarations}
                ]

            if tool_choice == "required":
                request_body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif tool_choice == "auto":
                request_body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        # Cap thinking on structured-output calls so it can't starve the JSON
        # output. Gemini 3.x thinking models bill reasoning tokens against
        # maxOutputTokens; without a cap, deep-context turns on gemini-3.5-flash
        # consumed nearly the whole budget and the structured response truncated
        # (finishReason=MAX_TOKENS) into a 500. `rf` (JSON schema/object) or
        # `tools_param` (function calling) marks a structured call (see
        # _structured_thinking_config for the 3.x-only scope). generation_config
        # is the same object referenced by request_body["generationConfig"], so
        # mutating it here is sufficient.
        thinking_config = self._structured_thinking_config(
            selected_model, is_structured=bool(rf) or bool(tools_param)
        )
        if thinking_config is not None:
            generation_config["thinkingConfig"] = thinking_config

        # Add safety settings if provided
        if "safety_settings" in kwargs:
            request_body["safetySettings"] = kwargs["safety_settings"]
        else:
            # Default safety settings for troubleshooting use case
            request_body["safetySettings"] = [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE",
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE",
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE",
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE",
                },
            ]

        # Make API request to Gemini
        url = f"{self.config.base_url.rstrip('/')}/models/{selected_model}:generateContent"

        # Add API key as query parameter (Gemini API format)
        params = {"key": self.config.api_key}

        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    params=params,
                    headers=headers,
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        raise LLMException(
                            f"Gemini API request failed: {response.status} - {error_text}",
                            status_code=response.status,
                        )

                    response_data = await response.json()
        except asyncio.TimeoutError:
            raise LLMException(
                f"Gemini API request timed out after {self.config.timeout}s "
                f"(model: {selected_model})",
                status_code=504,  # gateway timeout — transient/retryable
            )

        # Extract content from Gemini response format
        content = ""
        tool_calls = None
        tokens_used = 0
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        raw_assistant_parts: list | None = None

        if "candidates" in response_data and response_data["candidates"]:
            candidate = response_data["candidates"][0]

            if "content" in candidate and "parts" in candidate["content"]:
                # Stash the parts array verbatim so we can echo it back on the
                # next request. Gemini 3.x with thinking enabled attaches a
                # thoughtSignature to MULTIPLE part types (text, thought,
                # functionCall) — preserving the parts as-is is the only safe
                # way to round-trip every signature. The user-visible content
                # and tool_calls projections below are derived views; the
                # source of truth for the next turn is raw_assistant_parts.
                raw_assistant_parts = candidate["content"]["parts"]
                for part in raw_assistant_parts:
                    if "text" in part:
                        # Skip thinking/thought parts — not user-visible.
                        if part.get("thought"):
                            continue
                        content += part["text"]
                    elif "functionCall" in part:
                        if tool_calls is None:
                            tool_calls = []
                        fc = part["functionCall"]
                        tool_calls.append(
                            ToolCall(
                                id=f"call_{uuid4().hex[:12]}",
                                type="function",
                                function={
                                    "name": fc["name"],
                                    "arguments": json.dumps(fc.get("args", {})),
                                },
                            )
                        )

            # Extract token usage if available. promptTokenCount is inclusive of
            # cached content, so subtract to keep buckets disjoint for cost.
            # thoughtsTokenCount (thinking models, e.g. the default
            # gemini-3.5-flash) is billed at the output rate and is NOT included
            # in candidatesTokenCount, so fold it into output_tokens — otherwise
            # cost under-reports and the buckets don't sum to totalTokenCount.
            if response_data.get("usageMetadata"):
                _usage = response_data["usageMetadata"]
                output_tokens = (_usage.get("candidatesTokenCount") or 0) + (
                    _usage.get("thoughtsTokenCount") or 0
                )
                cache_read_tokens = _usage.get("cachedContentTokenCount") or 0
                _prompt = _usage.get("promptTokenCount") or 0
                input_tokens = max(_prompt - cache_read_tokens, 0)
                tokens_used = _usage.get("totalTokenCount") or (_prompt + output_tokens)

        # Handle potential safety blocks or other issues
        if not content and "candidates" in response_data:
            candidate = response_data["candidates"][0]
            if "finishReason" in candidate:
                finish_reason = candidate["finishReason"]
                if finish_reason in ["SAFETY", "BLOCKED_REASON_UNSPECIFIED"]:
                    content = "[Content blocked by safety filters]"
                elif finish_reason == "MAX_TOKENS":
                    content = "[Response truncated due to token limit]"

        # Check for MAX_TOKENS truncation — behavior depends on request type.
        # Structured requests (JSON schema, tool calling) need complete output
        # for valid parsing. Unstructured (plain text) can use partial content.
        if content and "candidates" in response_data:
            candidate = response_data["candidates"][0]
            finish_reason = candidate.get("finishReason", "")
            if finish_reason == "MAX_TOKENS":
                is_structured = bool(rf) or bool(tools_param)
                if is_structured:
                    # Structured output: truncated JSON is unusable — raise to
                    # trigger retry or fallback
                    raise LLMException(
                        f"Response truncated due to token limit (finishReason=MAX_TOKENS). "
                        f"Response length: {len(content)} chars. "
                        "Increase max_tokens parameter or simplify prompt.",
                        retryable=True,
                    )
                else:
                    # Unstructured text: partial content is still valuable.
                    # Log warning but return the content as-is.
                    self.logger.warning(
                        f"Gemini response truncated (MAX_TOKENS) but returning "
                        f"partial content ({len(content)} chars) for unstructured request."
                    )

        # Calculate metrics
        response_time_ms = int((time.time() - start_time) * 1000)

        # Calculate confidence based on model and response quality
        has_valid_tool_calls = tool_calls is not None and len(tool_calls) > 0
        confidence = self._calculate_confidence(
            selected_model,
            content,
            response_data,
            has_valid_tool_calls=has_valid_tool_calls,
        )

        # Stash the raw parts on the response so the orchestrator can
        # round-trip them verbatim. Only set when the response actually
        # contained parts — older Gemini paths and non-thinking responses
        # leave provider_metadata as None.
        provider_metadata: dict | None = None
        if raw_assistant_parts is not None:
            provider_metadata = {"assistant_parts": raw_assistant_parts}

        return LLMResponse(
            content=content,
            confidence=confidence,
            provider=self.provider_name,
            model=selected_model,
            tokens_used=tokens_used,
            response_time_ms=response_time_ms,
            cached=False,
            tool_calls=tool_calls,
            provider_metadata=provider_metadata,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            prompt_cache_hit=bool(cache_read_tokens > 0),
        )

    def _calculate_confidence(
        self,
        model: str,
        content: str,
        response_data: dict,
        has_valid_tool_calls: bool = False,
    ) -> float:
        """
        Calculate confidence score for Gemini response

        Args:
            model: Model used for generation
            content: Generated content
            response_data: Full API response
            has_valid_tool_calls: Whether the response has valid tool calls

        Returns:
            Confidence score (0.0-1.0)
        """
        base_confidence = self.config.confidence_score

        # Gemini models have different confidence characteristics
        model_confidence_map = {
            "gemini-1.5-pro": 0.92,
            "gemini-1.5-flash": 0.87,
            "gemini-1.0-pro": 0.85,
            "gemini-pro": 0.85,
            "gemini-pro-vision": 0.90,  # Higher for multi-modal
        }

        # Find matching model confidence
        model_confidence = base_confidence
        for model_name, confidence in model_confidence_map.items():
            if model_name in model.lower():
                model_confidence = confidence
                break

        # Check if content was blocked by safety filters
        if "[Content blocked by safety filters]" in content:
            return 0.1  # Very low confidence for blocked content

        if "[Response truncated due to token limit]" in content:
            return 0.5  # Medium confidence for truncated responses

        # Adjust based on content quality
        content_length = len(content.strip())

        if content_length == 0 and not has_valid_tool_calls:
            return 0.0
        elif content_length == 0 and has_valid_tool_calls:
            return model_confidence
        elif content_length < 50:
            # Very short responses might be less reliable
            model_confidence *= 0.8
        elif content_length > 500:
            # Longer, more detailed responses are often higher quality
            model_confidence *= 1.03

        # Check for finish reason in response
        if "candidates" in response_data and response_data["candidates"]:
            candidate = response_data["candidates"][0]
            finish_reason = candidate.get("finishReason", "")

            if finish_reason == "STOP":
                # Natural completion - high confidence
                model_confidence *= 1.05
            elif finish_reason == "MAX_TOKENS":
                # Truncated - moderate confidence
                model_confidence *= 0.9
            elif finish_reason in ["SAFETY", "OTHER"]:
                # Problematic completion - low confidence
                model_confidence *= 0.4

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
                model_confidence *= 0.7
                break

        # Ensure confidence is within valid range
        return min(1.0, max(0.0, model_confidence))

    def _convert_messages_to_gemini(self, messages: list) -> Dict[str, Any]:
        """Convert OpenAI-format messages to Gemini API format.

        Handles:
        - system messages → extracted to 'system_instruction'
        - user messages → role: user with text parts
        - assistant messages → role: model with text/functionCall parts
        - tool messages → role: function with functionResponse parts

        Returns:
            Dict with 'contents' list and optional 'system_instruction' string.
        """
        system_parts: List[str] = []
        contents: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_parts.append(content)

            elif role == "user":
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": content}],
                    }
                )

            elif role == "assistant":
                # When the original response was captured verbatim (Gemini
                # 3.x with thinking — see provider_metadata.assistant_parts),
                # echo the parts as-is. This preserves every thoughtSignature
                # exactly where Gemini placed it, regardless of part type.
                # The api_response itself is the source of truth for the next
                # turn; rebuilding from `content` + `tool_calls` would drop
                # signatures attached to text/thought parts.
                msg_pmeta = msg.get("provider_metadata") or {}
                saved_parts = msg_pmeta.get("assistant_parts")
                if saved_parts:
                    contents.append({"role": "model", "parts": saved_parts})
                    continue

                parts: List[Dict[str, Any]] = []
                if content:
                    parts.append({"text": content})

                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    function_call: Dict[str, Any] = {
                        "name": func.get("name", ""),
                        "args": args,
                    }
                    # Per-tool-call signature passthrough — used when the
                    # orchestrator preserves only ToolCall.provider_metadata
                    # (e.g. test fixtures, or providers that don't preserve
                    # the full parts array). Older Gemini paths leave this
                    # absent and the conditional is a no-op.
                    pmeta = tc.get("provider_metadata") or {}
                    sig = pmeta.get("thought_signature")
                    if sig:
                        function_call["thoughtSignature"] = sig
                    parts.append({"functionCall": function_call})

                if parts:
                    contents.append({"role": "model", "parts": parts})

            elif role == "tool":
                tool_name = msg.get("name", "")
                response_content = content

                # Parse as JSON for structured response if possible
                try:
                    response_content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    response_content = {"result": content}

                fn_response_part = {
                    "functionResponse": {
                        "name": tool_name,
                        "response": response_content,
                    }
                }

                # Group consecutive function responses into one turn
                if (
                    contents
                    and contents[-1].get("role") == "function"
                    and all(
                        "functionResponse" in p for p in contents[-1].get("parts", [])
                    )
                ):
                    contents[-1]["parts"].append(fn_response_part)
                else:
                    contents.append({"role": "function", "parts": [fn_response_part]})

        result: Dict[str, Any] = {"contents": contents}
        if system_parts:
            result["system_instruction"] = "\n\n".join(system_parts)

        return result

    # Fields that Gemini's function calling API does not support.
    # Gemini only accepts: type, description, properties, required, items,
    # enum, nullable, format.  Everything else must be stripped.
    _GEMINI_UNSUPPORTED_FIELDS = frozenset(
        {
            "additionalProperties",
            "title",
            "default",
            "examples",
            "$schema",
            "minLength",
            "maxLength",
            "pattern",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minItems",
            "maxItems",
            "uniqueItems",
            "const",
            "oneOf",
        }
    )

    @staticmethod
    def _resolve_refs_for_gemini(schema: dict) -> dict:
        """Resolve $ref/$defs, convert anyOf, and strip unsupported fields for Gemini.

        Gemini's API doesn't support JSON Schema features like $ref, $defs,
        anyOf, oneOf, or additionalProperties. This method:
        1. Inlines all $ref references from $defs
        2. Converts anyOf: [{type: X}, {type: "null"}] to {type: X, nullable: true}
        3. Removes $defs from the final schema
        4. Strips unsupported fields (additionalProperties, title, default, etc.)
        """
        import copy

        schema = copy.deepcopy(schema)
        defs = schema.pop("$defs", None) or {}

        def _strip_unsupported(node: dict) -> None:
            """Remove fields that Gemini does not support."""
            for field in GeminiProvider._GEMINI_UNSUPPORTED_FIELDS:
                node.pop(field, None)

        def resolve(node):
            if not isinstance(node, dict):
                return node

            # Resolve $ref
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g. "#/$defs/InternalReasoning"
                ref_name = ref_path.rsplit("/", 1)[-1]
                if ref_name in defs:
                    resolved = copy.deepcopy(defs[ref_name])
                    return resolve(resolved)
                return {"type": "object"}

            # Convert anyOf (Pydantic Optional pattern)
            if "anyOf" in node:
                variants = node["anyOf"]
                non_null = [v for v in variants if v.get("type") != "null"]
                if len(non_null) == 1:
                    # Optional[X] pattern: anyOf: [{type: X}, {type: null}]
                    resolved = resolve(non_null[0])
                    resolved["nullable"] = True
                    # Preserve description from parent
                    if "description" in node and "description" not in resolved:
                        resolved["description"] = node["description"]
                    return resolved
                elif non_null:
                    # Union type — pick first non-null variant
                    resolved = resolve(non_null[0])
                    resolved["nullable"] = any(
                        v.get("type") == "null" for v in variants
                    )
                    return resolved
                return {"type": "string"}

            # Recurse into properties
            if "properties" in node:
                node["properties"] = {
                    k: resolve(v) for k, v in node["properties"].items()
                }

            # Recurse into items
            if "items" in node:
                node["items"] = resolve(node["items"])

            # Strip unsupported fields (additionalProperties, title, etc.)
            _strip_unsupported(node)

            return node

        return resolve(schema)
