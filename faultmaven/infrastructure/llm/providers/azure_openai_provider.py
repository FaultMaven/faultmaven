"""
Azure OpenAI provider implementation.

Azure OpenAI's v1 API (``https://<resource>.openai.azure.com/openai/v1``) is
the OpenAI wire protocol: the same ``/chat/completions`` request and response
shapes, tool calling and ``response_format``. So the request body, the
token-cap spelling, the shape-based reasoning-effort rules and
structured-output detection are all inherited from :class:`OpenAIProvider`.

Two things differ, and only these are handled here:

- **Authentication.** Azure takes the resource key in an ``api-key`` header
  rather than ``Authorization: Bearer``.
- **The model id is a deployment name.** ``AZURE_OPENAI_MODEL`` names the
  deployment, which the operator chose when creating it. The inherited
  capability and reasoning rules key off OpenAI model names (``gpt-5.6``,
  ``o3``, …), so they apply when the deployment is named after its model, the
  common convention; a deployment named otherwise is classified as an unknown
  OpenAI model (FUNCTION_CALLING, legacy ``max_tokens`` as the opening guess,
  corrected by the endpoint's own 400 as on direct OpenAI).

The OPERATOR reasoning default is not shared: ``OPENAI_REASONING_EFFORT`` is
the OpenAI provider's knob and is not applied to Azure deployments, which run
the shape-based defaults.

The endpoint is accepted in either spelling Azure documents — the resource URL
the portal shows, or its v1 endpoint — and normalized by :func:`v1_base_url`.
"""

from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .openai_provider import OpenAIProvider

#: The path of Azure OpenAI's v1 API under a resource URL.
V1_PATH = "/openai/v1"


def v1_base_url(endpoint: Optional[str]) -> str:
    """The v1 API base URL for ``endpoint``, or ``""`` when it is unset.

    ``AZURE_OPENAI_ENDPOINT`` is, in Azure's portal and SDKs, the bare
    resource URL (``https://<resource>.openai.azure.com/``); FaultMaven
    posts to ``<base>/chat/completions`` under the v1 API. A bare resource
    URL (or one ending ``/openai``) gets the v1 path appended; a URL with any
    other path is taken as the operator wrote it.
    """
    value = (endpoint or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if path in ("", "/openai"):
        path = V1_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


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
