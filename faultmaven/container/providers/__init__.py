"""Service providers for FaultMaven DI Container.

This package contains factory modules that create and register services:
- infrastructure: Core infrastructure (LLM, tracing, security, storage)
- services: Business logic services (agent, knowledge, session)
- tools: Tool registry and creation

Each provider module exports a `register_*` function that takes
a container and registers its services.
"""

from faultmaven.container.providers.infrastructure import register_infrastructure
from faultmaven.container.providers.services import register_services
from faultmaven.container.providers.tools import register_tools

__all__ = [
    "register_infrastructure",
    "register_services",
    "register_tools",
]
