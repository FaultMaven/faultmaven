"""
ITier2SearchService — Abstract interface for interpreted search
("deep analysis") over case files. Invoked on demand by the investigation
agent via the ``deep_analysis`` tool; not driven by the preprocessing
pipeline. The ``Tier2`` token in the name refers to the agent-tools tier
(structural-index search → interpreted search → semantic-vector search),
not the preprocessing tier system.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
"""

from abc import ABC, abstractmethod

from faultmaven.core.preprocessing.models import (
    AnalysisContext,
    DeepAnalysisResult,
    UnifiedDataType,
)


class ITier2SearchService(ABC):
    """
    Interface for Tier 2 deep analysis.

    Tier 2 is NOT called by the preprocessing pipeline. It runs on-demand,
    invoked by the investigation agent via an agent tool when:
    - The Tier 1 structural index doesn't answer the user's question
    - Hypothesis validation needs raw data
    - User explicitly requests file detail
    - An image requires vision analysis

    Implementations:
    - ExternalTier2Client: HTTP call to cloud microservice
    - LocalTier2Service: In-process with local LLM (Ollama/vLLM)
    - BasicTier2Service: In-process keyword search, no LLM
    """

    @abstractmethod
    async def analyze(
        self,
        file_ref: str,
        query: str,
        context: AnalysisContext,
        data_type: UnifiedDataType,
    ) -> DeepAnalysisResult:
        """
        Analyze raw data to answer a specific question.

        Args:
            file_ref: Reference to stored raw file (S3 URI, local path, or
                      already-staged file ID for the backend)
            query: What the agent wants to know about this data
            context: Investigation context (case summary, hypotheses, stage)
            data_type: Hint from Tier 0 classification

        Returns:
            DeepAnalysisResult with answer, supporting excerpts, and metadata
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the Tier 2 backend is reachable."""
        ...
