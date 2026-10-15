"""
LLM Provider Package

This package contains the centralized provider registry and implementations
for various LLM providers used by FaultMaven.

The registry is imported from its own module,
``faultmaven.infrastructure.llm.providers.registry``, never through this
package: the registry sends through ``faultmaven.infrastructure.llm.dispatch``,
which imports ``providers.base``, so a package that imported the registry
would make importing the dispatch first a cycle (#1998).
"""

from .base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    ReasoningIntent,
    StopReason,
    normalize_stop_reason,
)
from .fireworks_provider import FireworksProvider
from .groq_provider import GroqProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider
from .openrouter_provider import OpenRouterProvider

__all__ = [
    "BaseLLMProvider",
    "LLMResponse",
    "ReasoningIntent",
    "StopReason",
    "normalize_stop_reason",
    "ProviderConfig",
    "FireworksProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "GroqProvider",
    "LocalProvider",
]
