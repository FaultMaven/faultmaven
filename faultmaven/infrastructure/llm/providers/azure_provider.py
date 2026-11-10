"""
Azure provider: models deployed on an Azure AI Foundry resource.

Requests go to the resource's OpenAI v1 route, ``https://<resource>.services.
ai.azure.com/openai/v1`` (the ``*.openai.azure.com`` host serves the same
route). Microsoft directs every Foundry deployment to it, OpenAI and non-OpenAI
models alike (DeepSeek, Grok, Llama, …), replacing the Azure AI Model Inference
``/models`` endpoint, whose SDK retired on 2026-08-26. The route is the OpenAI
wire protocol with implicit versioning (no ``api-version``) and the resource
key as a bearer token, so the request, the response, the token-cap spelling,
the shape-based reasoning-effort rules and structured-output detection are all
inherited from :class:`OpenAIProvider`.

What is Azure's own:

- **The endpoint is per resource.** There is no default host;
  ``AZURE_API_BASE`` names it, in any spelling :func:`v1_base_url` accepts.
- **The model id is a deployment name.** ``AZURE_MODEL`` names the deployment,
  chosen by the operator. The inherited capability and reasoning rules key off
  OpenAI model names (``gpt-5.6``, ``o3``, …), so they apply when the
  deployment is named after an OpenAI model; any other name is classified as
  an unknown model (FUNCTION_CALLING, legacy ``max_tokens`` as the opening
  guess, corrected by the endpoint's own 400 as on direct OpenAI).

``OPENAI_REASONING_EFFORT`` is the OpenAI provider's knob and is not applied
to Azure deployments, which run the shape-based defaults.
"""

from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from .openai_provider import OpenAIProvider

#: The OpenAI v1 route under a Foundry (or Azure OpenAI) resource URL.
V1_PATH = "/openai/v1"

#: Paths that name the resource rather than a route, and the retired Model
#: Inference route (``/models``), all of which mean "this resource's v1 route".
_RESOURCE_PATHS = ("", "/openai", "/models", "/model")


def v1_base_url(endpoint: Optional[str]) -> str:
    """The v1 route base URL for ``endpoint``, or ``""`` when it is unset.

    Accepts the resource URL the portal shows
    (``https://<resource>.services.ai.azure.com/``), the v1 route itself
    (``…/openai/v1``) and the Model Inference URL (``…/models``), and returns
    ``https://<resource>…/openai/v1`` for each: FaultMaven posts to
    ``<base>/chat/completions``, which only the v1 route answers without an
    ``api-version``. A URL with any other path (a gateway in front of the
    resource) is taken as written.
    """
    value = (endpoint or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if path in _RESOURCE_PATHS:
        path = V1_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class AzureProvider(OpenAIProvider):
    """Azure AI Foundry deployments over the resource's OpenAI v1 route."""

    #: This provider's label: what ``provider_name`` returns, what
    #: ``llm_requests_total`` and ``llm_provider_calls_total`` carry, and what
    #: the model dispatch reads statically for its own reports (#1998).
    LABEL = "azure"

    @property
    def provider_name(self) -> str:
        return self.LABEL
