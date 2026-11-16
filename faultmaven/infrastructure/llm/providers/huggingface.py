"""
Hugging Face provider implementation.

Hugging Face serves hosted models through its Inference Providers router
(``https://router.huggingface.co/v1``), an OpenAI-compatible chat-completions
API, so the wire protocol (request/response shape, stop reasons, usage) is
inherited unchanged from :class:`OpenAIProvider`. The retired serverless
text-generation API (``api-inference.huggingface.co``, ``inputs`` →
``generated_text``) no longer resolves (#2111). An override of the base URL
names another OpenAI-compatible base, such as a self-hosted TGI's ``/v1``.

What is NOT inherited is the OpenAI-specific request tuning and the capability
claims: the router forwards each call to whichever provider serves the model,
so the OpenAI model-family predicates do not apply, and tool calling and strict
schema support vary per routed provider and are unverified.
"""

from typing import Optional

from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

from .openai_provider import OpenAIProvider


class HuggingFaceProvider(OpenAIProvider):
    """Hugging Face router provider (OpenAI-compatible wire protocol)."""

    #: This provider's label: what ``provider_name`` returns, what
    #: ``llm_requests_total`` and ``llm_provider_calls_total`` carry, and what
    #: the model dispatch reads statically for its own reports (#1998).
    LABEL = "huggingface"

    @property
    def provider_name(self) -> str:
        return self.LABEL

    @classmethod
    def _uses_completion_tokens_param(cls, model_name: str) -> bool:
        """Open with the legacy ``max_tokens``, the spelling the router's
        chat-completions API documents. The inherited ``_generate()`` still
        re-sends once under the other spelling if a route rejects it (#510)."""
        return False

    @classmethod
    def _caps_reasoning_effort(cls, model_name: str) -> bool:
        """The top-level OpenAI ``reasoning_effort`` field is not part of the
        router's contract for routed models, so it is never sent."""
        return False

    @classmethod
    def _defaults_reasoning(cls, model_name: str) -> bool:
        """The direct-OpenAI gpt-5.6 accommodation (omit ``temperature``, send
        ``reasoning_effort: "none"`` with tools) is not applied to routed
        models."""
        return False

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Not claimed: tool support varies per routed provider and is
        unverified, so the engine runs single-shot without DA tool use."""
        return False

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """BEST_EFFORT (prompt-based JSON) for every model: strict schema
        enforcement varies per routed provider and is unverified."""
        return StructuredOutputCapability.BEST_EFFORT
