"""
Extractor Protocol — shared interface for all Tier 1 extractors.

All extractors must implement:
- strategy_name: identifies the extraction strategy for logging/metadata
- llm_calls_used: number of LLM calls (0 for all current extractors)
- extract(content) -> ExtractResult: the extraction logic
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


# Bumped only on breaking schema changes (field removal or type change).
# Additive fields (new keys in file_meta) do NOT require a bump.
SCHEMA_VERSION = 1


@dataclass
class ExtractResult:
    """Structured output from all Tier 1 extractors.

    Three structurally distinct parts, each serving a different consumer:

    file_extract  — answers the default question for this data type
                    (FILE SUMMARY + crime scene for logs). This is what the
                    agent uses to characterize the file at intake.

    search_map    — navigation for search_file calls: entity values, exact
                    search strings, event patterns. Agent uses this to find
                    specific evidence. None for data types without a search map.

    file_meta     — facts about the raw file: line count, size, time range,
                    error counts, truncation status. Agent uses this to reason
                    about completeness and scale.
    """

    file_extract: str
    search_map: str | None = None
    file_meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": SCHEMA_VERSION,
                "file_extract": self.file_extract,
                "search_map": self.search_map,
                "file_meta": self.file_meta,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, s: str) -> ExtractResult:
        d = json.loads(s)
        return cls(
            file_extract=d.get("file_extract", ""),
            search_map=d.get("search_map"),
            file_meta=d.get("file_meta") or {},
        )


@runtime_checkable
class Extractor(Protocol):
    """Protocol that all Tier 1 extractors must satisfy."""

    @property
    def strategy_name(self) -> str: ...

    @property
    def llm_calls_used(self) -> int: ...

    def extract(self, content: str) -> ExtractResult: ...
