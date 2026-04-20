"""
LocalTier2Service — In-process Tier 3 deep analysis using local LLM (Ollama/vLLM).

Reads raw file from local storage, finds relevant sections via single-pass
keyword search (ANY-match, no Pass-2 fallback), then sends them to a local
LLM for analysis.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
"""

import logging
import time
from typing import Any, List

from faultmaven.core.preprocessing.models import (
    AnalysisContext,
    DataExcerpt,
    DeepAnalysisResult,
    UnifiedDataType,
)
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService

logger = logging.getLogger(__name__)


class LocalTier2Service(ITier2AnalysisService):
    """In-process Tier 2 using local filesystem + local LLM (Ollama/vLLM)."""

    def __init__(
        self,
        llm_client: Any,
        storage_service: Any,
        context_lines: int = 20,
        max_excerpts: int = 5,
        max_tokens: int = 2000,
    ):
        self.llm_client = llm_client
        self.storage_service = storage_service
        self.context_lines = context_lines
        self.max_excerpts = max_excerpts
        self.max_tokens = max_tokens

    async def analyze(
        self,
        file_ref: str,
        query: str,
        context: AnalysisContext,
        data_type: UnifiedDataType,
    ) -> DeepAnalysisResult:
        start_time = time.time()

        # Read raw file from local filesystem (no staging needed)
        raw_content = await self.storage_service.retrieve(file_ref)
        if isinstance(raw_content, bytes):
            raw_content = raw_content.decode("utf-8", errors="replace")

        # Use keyword search to narrow down relevant sections
        relevant_sections = self._find_relevant_sections(raw_content, query)

        # Build prompt and send to local LLM
        prompt = self._build_analysis_prompt(
            query, relevant_sections, context, data_type
        )

        tokens_used = 0
        try:
            response = await self.llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.max_tokens,
            )
            answer = response.content if hasattr(response, "content") else str(response)
            tokens_used = getattr(response, "tokens_used", 0)
        except Exception as e:
            logger.error(f"Local LLM analysis failed: {e}")
            answer = f"LLM analysis failed. Raw excerpts below:\n\n" + "\n---\n".join(
                s["text"][:500] for s in relevant_sections
            )

        processing_time_ms = int((time.time() - start_time) * 1000)

        return DeepAnalysisResult(
            answer=answer,
            excerpts=[
                DataExcerpt(
                    content=s["text"],
                    line_start=s["start"],
                    line_end=s["end"],
                )
                for s in relevant_sections
            ],
            confidence=0.6 if relevant_sections else 0.2,
            tokens_used=tokens_used,
            processing_time_ms=processing_time_ms,
            backend_used="local_llm",
        )

    async def is_available(self) -> bool:
        try:
            # Ping local LLM
            if hasattr(self.llm_client, "is_available"):
                return await self.llm_client.is_available()
            return True
        except Exception:
            return False

    def _find_relevant_sections(self, content: str, query: str) -> List[dict]:
        """Keyword/regex search to find sections relevant to the query."""
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]
        matches: list[dict] = []

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(kw in line_lower for kw in keywords):
                start = max(0, i - self.context_lines)
                end = min(len(lines), i + self.context_lines + 1)
                matches.append(
                    {
                        "text": "\n".join(lines[start:end]),
                        "start": start + 1,
                        "end": end,
                    }
                )

        return self._merge_overlapping(matches)[: self.max_excerpts]

    @staticmethod
    def _merge_overlapping(matches: List[dict]) -> List[dict]:
        """Deduplicate overlapping windows."""
        if not matches:
            return []

        sorted_matches = sorted(matches, key=lambda m: m["start"])
        merged: list[dict] = [sorted_matches[0]]

        for current in sorted_matches[1:]:
            prev = merged[-1]
            if current["start"] <= prev["end"]:
                if current["end"] > prev["end"]:
                    prev["end"] = current["end"]
                    if len(current["text"]) > len(prev["text"]):
                        prev["text"] = current["text"]
            else:
                merged.append(current)

        return merged

    @staticmethod
    def _build_analysis_prompt(
        query: str,
        sections: List[dict],
        context: AnalysisContext,
        data_type: UnifiedDataType,
    ) -> str:
        """Build a prompt for the local LLM with query and relevant sections."""
        parts = [
            f"You are analyzing a {data_type.value} file for a troubleshooting investigation.",
        ]

        if context.case_summary:
            parts.append(f"\nCase context: {context.case_summary}")

        if context.active_hypotheses:
            parts.append(
                "\nActive hypotheses:\n"
                + "\n".join(f"- {h}" for h in context.active_hypotheses)
            )

        parts.append(f"\nQuestion: {query}")

        if sections:
            parts.append("\nRelevant sections from the file:")
            for i, s in enumerate(sections, 1):
                parts.append(
                    f"\n--- Section {i} (lines {s['start']}-{s['end']}) ---\n"
                    f"{s['text'][:2000]}"
                )

        parts.append(
            "\nProvide a concise analysis answering the question. "
            "Reference specific line numbers when possible."
        )

        return "\n".join(parts)
