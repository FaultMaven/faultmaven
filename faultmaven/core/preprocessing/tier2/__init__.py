"""
Tier 3 Deep LLM Analysis backends (legacy package name `tier2`).

On-demand interpreted analysis of raw data, invoked by the investigation
agent's `deep_analysis` tool — NOT during upload preprocessing. The
package is named `tier2` for historical reasons (pre-v5.0 naming); the
current spec places this capability at Tier 3.

Backends:
- external: HTTP call to cloud microservice (Gemini, OpenAI, custom)
- local: In-process with local LLM (Ollama/vLLM)
- basic: In-process keyword search, no LLM
- disabled: Agent works from Tier 1 index only

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
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
