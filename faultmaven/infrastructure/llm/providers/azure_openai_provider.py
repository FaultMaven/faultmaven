"""
Azure OpenAI provider implementation.

Azure OpenAI's v1 API (``https://<resource>.openai.azure.com/openai/v1``) is
the OpenAI wire protocol: the same ``/chat/completions`` request and response
shapes, tool calling and ``response_format``. So the request body, the
token-cap spelling, the reasoning-effort rules and structured-output detection
are all inherited from :class:`OpenAIProvider` unchanged.

Two things differ, and only these are overridden here:

- **Authentication.** Azure takes the resource key in an ``api-key`` header
  rather than ``Authorization: Bearer``.
- **The model id is a deployment name.** ``AZURE_OPENAI_MODEL`` names the
  deployment, which the operator chose when creating it. The inherited
  capability and reasoning rules key off OpenAI model names (``gpt-5.6``,
  ``o3``, …), so they apply when the deployment is named after its model, the
  common convention; a deployment named otherwise is classified as an unknown
  OpenAI model (FUNCTION_CALLING, legacy ``max_tokens`` as the opening guess,
  corrected by the endpoint's own 400 as on direct OpenAI).
"""

from typing import Dict

from .openai_provider import OpenAIProvider


class AzureOpenAIProvider(OpenAIProvider):
    """Azure OpenAI v1 endpoint (OpenAI-compatible wire protocol)."""

    #: This provider's label: what ``provider_name`` returns, what
    #: ``llm_requests_total`` and ``llm_provider_calls_total`` carry, and what
    #: the model dispatch reads statically for its own reports (#1998).
    LABEL = "azure_openai"

    @property
    def provider_name(self) -> str:
        return self.LABEL

    def _request_headers(self) -> Dict[str, str]:
        """Azure authenticates the resource key with the ``api-key`` header."""
        return {
            "api-key": f"{self.config.api_key}",
            "Content-Type": "application/json",
        }
