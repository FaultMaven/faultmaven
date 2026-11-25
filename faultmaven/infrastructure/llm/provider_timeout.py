"""One provider's request timeout ``T``: the one resolver (#2983).

Shared by the client a provider is built with (``ProviderRegistry``), the
deadline each attempt of a routed walk runs under (``route_request``) and the
retry-ladder report (``config/retry_budget``). Its own module because the
router imports the registry, so the registry cannot import a resolver from it.
"""

import os
from typing import Optional

from faultmaven.config.settings import get_settings


def provider_timeout(name: Optional[str], settings=None) -> float:
    """``name``'s request timeout in seconds, from ``settings`` (else the live ones).

    An explicit ``LLM_PROVIDER_TIMEOUT_OVERRIDES`` entry is that provider's
    ``T``, below the base as well as above it; otherwise the base,
    ``LLM_REQUEST_TIMEOUT`` (else ``LLMSettings.request_timeout``). An override
    ``<= 0`` is refused when the settings load.
    """
    llm = (settings if settings is not None else get_settings()).llm
    overrides = llm.provider_timeout_overrides or {}
    if name and name in overrides:
        return float(overrides[name])
    return float(os.getenv("LLM_REQUEST_TIMEOUT", str(llm.request_timeout)))
