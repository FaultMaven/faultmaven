"""
Tier 2 Service Factory

Creates the appropriate Tier 2 backend based on configuration.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md Section 9.3
"""

import logging
from typing import Any, Optional

from faultmaven.core.preprocessing.tier2.basic import BasicTier2Service
from faultmaven.core.preprocessing.tier2.external import ExternalTier2Client
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service

logger = logging.getLogger(__name__)


def create_tier2_service(
    backend: str = "disabled",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    llm_client: Optional[Any] = None,
    storage_service: Optional[Any] = None,
    timeout_seconds: int = 30,
) -> Optional[ITier2AnalysisService]:
    """
    Factory: create Tier 2 service based on configuration.

    Args:
        backend: One of "external", "local", "basic", "disabled"
        base_url: URL for external backend
        api_key: API key for external backend
        llm_client: LLM client for local backend
        storage_service: Storage service for file retrieval
        timeout_seconds: Timeout for Tier 2 calls

    Returns:
        ITier2AnalysisService instance, or None if disabled

    Configuration (env vars):
        TIER2_BACKEND=disabled    # external | local | basic | disabled
        TIER2_URL=                # URL for external backend
        TIER2_API_KEY=            # API key for external backend
        TIER2_TIMEOUT_SECONDS=30  # Timeout for Tier 2 calls
    """
    if backend == "disabled":
        logger.info("Tier 2 deep analysis: disabled")
        return None

    if backend == "external":
        if not base_url:
            raise ValueError(
                "TIER2_URL required when TIER2_BACKEND=external"
            )
        logger.info(f"Tier 2 deep analysis: external ({base_url})")
        return ExternalTier2Client(
            base_url=base_url,
            api_key=api_key,
            storage_service=storage_service,
            timeout_seconds=timeout_seconds,
        )

    if backend == "local":
        if not llm_client:
            raise ValueError(
                "LLM client required when TIER2_BACKEND=local"
            )
        logger.info("Tier 2 deep analysis: local LLM")
        return LocalTier2Service(
            llm_client=llm_client,
            storage_service=storage_service,
        )

    if backend == "basic":
        if not storage_service:
            raise ValueError(
                "Storage service required when TIER2_BACKEND=basic"
            )
        logger.info("Tier 2 deep analysis: basic keyword search")
        return BasicTier2Service(
            storage_service=storage_service,
        )

    raise ValueError(
        f"Unknown TIER2_BACKEND: {backend}. "
        f"Expected: external | local | basic | disabled"
    )
