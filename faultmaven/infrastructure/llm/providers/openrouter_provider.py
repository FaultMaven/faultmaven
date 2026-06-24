"""
OpenRouter provider implementation.

OpenRouter is an OpenAI-compatible multi-model gateway, so the wire protocol
(request/response shape, tool calling, response_format) is identical to
:class:`OpenAIProvider` and is inherited unchanged.

What is NOT inherited is structured-output capability detection. OpenRouter
model ids are namespaced as ``vendor/model`` (e.g. ``anthropic/claude-sonnet-4-6``,
``google/gemini-2.5-flash``, ``meta-llama/llama-3.3-70b``). OpenAIProvider's
capability heuristics key off OpenAI model *names* (gpt-4o, gpt-5, o1, ...),
which never match a namespaced id — so every routed model would silently
collapse to FUNCTION_CALLING regardless of what the underlying model actually
supports. This subclass detects capability from the routed family instead.
"""

from typing import Optional

from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .openai_provider import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter gateway provider (OpenAI-compatible wire protocol)."""

    @property
    def provider_name(self) -> str:
        return "openrouter"

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """Detect capability from the routed ``vendor/model`` id.

        - ``openai/*`` — reuse OpenAI's strict-json_schema heuristics on the
          routed suffix (so ``openai/gpt-5`` correctly reports STRICT).
        - everything else — FUNCTION_CALLING. OpenRouter normalizes tool
          calling across vendors, and the engine's FUNCTION_CALLING strategy
          forces ``tool_choice=required``; the inherited OpenAI-compatible
          response path extracts ``tool_calls`` and tolerates empty content.
          This is the reliable, gateway-agnostic enforcement path. We do NOT
          claim STRICT for non-OpenAI vendors because json_schema strict-mode
          passthrough is model-dependent and unverified per route (an
          empirical probe, not a static guarantee).

        Note: ``response_schema``-style native enforcement (Gemini direct)
        is intentionally not assumed here — through OpenRouter's OpenAI shim
        those models are driven via tool calling.
        """
        effective_model = self.get_effective_model(model)
        vendor, sep, suffix = effective_model.lower().partition("/")

        if sep and vendor == "openai":
            # Classify the routed suffix with OpenAI's own name heuristics.
            return self._capability_for_model_name(suffix)

        return StructuredOutputCapability.FUNCTION_CALLING
