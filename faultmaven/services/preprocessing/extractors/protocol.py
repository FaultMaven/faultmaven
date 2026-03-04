"""
Extractor Protocol — shared interface for all Tier 1 extractors.

All extractors must implement:
- strategy_name: identifies the extraction strategy for logging/metadata
- llm_calls_used: number of LLM calls (0 for all current extractors)
- extract(content) -> str: the extraction logic
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


@runtime_checkable
class Extractor(Protocol):
    """Protocol that all Tier 1 extractors must satisfy."""

    @property
    def strategy_name(self) -> str: ...

    @property
    def llm_calls_used(self) -> int: ...

    def extract(self, content: str) -> str: ...
