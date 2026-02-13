"""
Tier 2: Deep Analysis Service

On-demand LLM-powered analysis of raw data. Invoked by the investigation
agent, NOT during upload preprocessing.

Backends:
- external: HTTP call to cloud microservice (Gemini, OpenAI, custom)
- local: In-process with local LLM (Ollama/vLLM)
- basic: In-process keyword search, no LLM
- disabled: Agent works from Tier 1 index only

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md Section 6
"""

from faultmaven.core.preprocessing.tier2.basic import BasicTier2Service
from faultmaven.core.preprocessing.tier2.external import ExternalTier2Client
from faultmaven.core.preprocessing.tier2.factory import create_tier2_service
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service

__all__ = [
    "ITier2AnalysisService",
    "BasicTier2Service",
    "LocalTier2Service",
    "ExternalTier2Client",
    "create_tier2_service",
]
