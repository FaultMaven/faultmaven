"""
Interpreted-search ("deep analysis") backends.

On-demand interpretation of raw data, invoked by the investigation
agent's ``deep_analysis`` tool — not during upload preprocessing. The
package name ``tier2`` refers to the agent-tools tier system: Tier 2 is
keyword/regex search (``search_file``), Tier 3 is interpreted analysis.

Backends:

- ``external``: HTTP call to a remote analysis service.
- ``local``: In-process with the configured LLM (Ollama/vLLM).
- ``basic``: In-process keyword search, no LLM.
- ``disabled``: Agent works from the Tier 1 structural index only.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
"""

from faultmaven.core.preprocessing.tier2.basic import BasicTier2Service
from faultmaven.core.preprocessing.tier2.external import ExternalTier2Client
from faultmaven.core.preprocessing.tier2.factory import create_tier2_service
from faultmaven.core.preprocessing.tier2.interface import ITier2SearchService
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service

__all__ = [
    "ITier2SearchService",
    "BasicTier2Service",
    "LocalTier2Service",
    "ExternalTier2Client",
    "create_tier2_service",
]
