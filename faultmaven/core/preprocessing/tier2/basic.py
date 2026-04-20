"""
BasicTier2Service — In-process keyword search, no LLM.

Returns raw excerpts matching the query keywords for the agent to interpret.
Lowest cost, always available, no external dependencies.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
"""

import logging
import time
from typing import Any, List, Optional

from faultmaven.core.preprocessing.models import (
    AnalysisContext,
    DataExcerpt,
    DeepAnalysisResult,
    UnifiedDataType,
)
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService

logger = logging.getLogger(__name__)


class BasicTier2Service(ITier2AnalysisService):
    """In-process keyword search, no LLM. Returns raw excerpts."""

    def __init__(
        self,
        storage_service: Any,
        context_lines: int = 20,
        max_excerpts: int = 5,
    ):
        self.storage_service = storage_service
        self.context_lines = context_lines
        self.max_excerpts = max_excerpts

    async def analyze(
        self,
        file_ref: str,
        query: str,
        context: AnalysisContext,
        data_type: UnifiedDataType,
    ) -> DeepAnalysisResult:
        start_time = time.time()

        raw_content = await self.storage_service.retrieve(file_ref)
        if isinstance(raw_content, bytes):
            raw_content = raw_content.decode("utf-8", errors="replace")

        relevant_sections = self._keyword_search(raw_content, query)

        answer = f"Found {len(relevant_sections)} matching sections."
        if not relevant_sections:
            answer = "No matching sections found for the query."

        processing_time_ms = int((time.time() - start_time) * 1000)

        return DeepAnalysisResult(
            answer=answer,
            excerpts=[
                DataExcerpt(
                    content=s["text"],
                    line_start=s["start"],
                    line_end=s["end"],
                    relevance=s.get("relevance", 0.5),
                )
                for s in relevant_sections
            ],
            confidence=0.3 if relevant_sections else 0.0,
            tokens_used=0,
            processing_time_ms=processing_time_ms,
            backend_used="basic_search",
        )

    async def is_available(self) -> bool:
        return True

    def _keyword_search(self, content: str, query: str) -> List[dict]:
        """
        Two-pass keyword search (matches the `search_file` tool v4.2 spec).

        Pass 1 (strict): a line must contain ALL query keywords. This returns
        highly specific matches for multi-word queries and suppresses noise
        from single-term hits.

        Pass 2 (fallback): only if pass 1 returns nothing, re-run with ANY
        keyword matching. This guarantees we still surface candidates for
        single-term queries or when no single line matches every term.

        Keywords shorter than 3 chars are dropped (stop-word-ish).
        """
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]

        if not keywords:
            return []

        def _collect(require_all: bool) -> list[dict]:
            out: list[dict] = []
            for i, line in enumerate(lines):
                line_lower = line.lower()
                matched = [kw for kw in keywords if kw in line_lower]
                if require_all:
                    if len(matched) < len(keywords):
                        continue
                elif not matched:
                    continue
                start = max(0, i - self.context_lines)
                end = min(len(lines), i + self.context_lines + 1)
                out.append(
                    {
                        "text": "\n".join(lines[start:end]),
                        "start": start + 1,
                        "end": end,
                        "relevance": len(matched) / len(keywords),
                    }
                )
            return out

        # Pass 1: strict (ALL keywords on same line)
        matches = _collect(require_all=True)

        # Pass 2: fallback to ANY if strict pass was empty
        if not matches:
            matches = _collect(require_all=False)

        return self._merge_overlapping(matches)[: self.max_excerpts]

    @staticmethod
    def _merge_overlapping(matches: List[dict]) -> List[dict]:
        """Merge overlapping excerpt windows."""
        if not matches:
            return []

        sorted_matches = sorted(matches, key=lambda m: m["start"])
        merged: list[dict] = [sorted_matches[0]]

        for current in sorted_matches[1:]:
            prev = merged[-1]
            if current["start"] <= prev["end"]:
                # Overlapping — extend the previous window
                if current["end"] > prev["end"]:
                    # Re-build text from the wider range
                    prev["end"] = current["end"]
                    prev["relevance"] = max(prev["relevance"], current["relevance"])
                    # Text will be a superset; keep the longer one
                    if len(current["text"]) > len(prev["text"]):
                        prev["text"] = current["text"]
            else:
                merged.append(current)

        return merged
