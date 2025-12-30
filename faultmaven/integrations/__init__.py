"""FaultMaven integrations module.

This module contains integrations with external services and APIs,
including LLM providers (Anthropic, OpenAI), and other third-party services.
"""

from faultmaven.integrations.llm_client import (
    LLMClient,
    LLMProvider,
    create_llm_client,
)

__all__ = [
    "LLMClient",
    "LLMProvider",
    "create_llm_client",
]
