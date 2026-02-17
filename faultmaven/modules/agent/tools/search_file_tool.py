"""Search File Tool — Tier 2 Mechanical Search (v4.0)

Provides three search modes over previously uploaded evidence files:
- keyword: Split query into keywords, find matching lines with context
- regex: Treat query as a regex pattern
- extractor: Re-run domain-specific extractor with different parameters

Design Reference: docs/working/DRAFT-data-preprocessing-spec-v4.md Section 3
"""

import logging
import re
from typing import Any, Dict, List, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

# Default search parameters
DEFAULT_CONTEXT_LINES = 20
DEFAULT_MAX_RESULTS = 10


class SearchFileTool(AgentTool):
    """Tier 2 mechanical search over raw file content.

    Allows the agent to search previously uploaded evidence files using
    keyword matching, regex patterns, or re-running domain extractors
    with different parameters.
    """

    def __init__(
        self,
        storage_service: Any = None,
        preprocessing_service: Any = None,
        context_lines: int = DEFAULT_CONTEXT_LINES,
        max_results: int = DEFAULT_MAX_RESULTS,
    ):
        self.storage_service = storage_service
        self.preprocessing_service = preprocessing_service
        self.context_lines = context_lines
        self.max_results = max_results

    @property
    def name(self) -> str:
        return "search_file"

    @property
    def description(self) -> str:
        return (
            "Search a previously uploaded evidence file for specific information. "
            "Use when the evidence summary mentions something relevant but lacks detail, "
            "or you need specific lines, values, or patterns from the raw file. "
            "Supports keyword search, regex patterns, and extractor re-runs. "
            "Use list_evidence first to find the evidence_id."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": "The ID of the evidence artifact to search",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Search query: keywords for keyword mode, "
                        "regex pattern for regex mode, "
                        "or description of what to find for extractor mode"
                    ),
                },
                "search_type": {
                    "type": "string",
                    "enum": ["keyword", "regex", "extractor"],
                    "description": (
                        "Search mode: 'keyword' splits query into keywords and finds matching lines, "
                        "'regex' treats query as a regex pattern, "
                        "'extractor' re-runs the domain extractor with different parameters"
                    ),
                },
                "extractor_params": {
                    "type": "object",
                    "description": (
                        "Parameters for extractor re-run mode. "
                        'E.g., {"min_severity": "WARN"} for log extractor, '
                        '{"z_score_threshold": 2.0} for metrics extractor'
                    ),
                },
            },
            "required": ["evidence_id", "query"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute file search."""
        evidence_id = params.get("evidence_id")
        query = params.get("query", "")
        search_type = params.get("search_type", "keyword")
        extractor_params = params.get("extractor_params", {})

        if not evidence_id:
            return ToolResult(success=False, data=None, error="evidence_id is required")

        if not query:
            return ToolResult(success=False, data=None, error="query is required")

        if not context.evidence_service:
            return ToolResult(
                success=False, data=None, error="Evidence service not available"
            )

        try:
            # Get evidence metadata
            evidence = await context.evidence_service.get_evidence(
                evidence_id=evidence_id,
                organization_id=context.organization_id,
            )

            if not evidence:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence not found: {evidence_id}",
                )

            if evidence.case_id != context.case_id:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence {evidence_id} does not belong to case {context.case_id}",
                )

            # Download raw file content
            file_data, filename, mime_type = (
                await context.evidence_service.download_evidence(
                    evidence_id=evidence_id,
                    organization_id=context.organization_id,
                )
            )

            content = file_data.decode("utf-8", errors="replace")

            # Dispatch by search type
            if search_type == "regex":
                results = self._regex_search(content, query)
            elif search_type == "extractor":
                results = await self._extractor_rerun(
                    content, evidence, extractor_params
                )
            else:
                results = self._keyword_search(content, query)

            logger.info(
                f"search_file: {evidence_id} ({filename}), "
                f"mode={search_type}, query='{query[:50]}', "
                f"results={len(results)}"
            )

            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "filename": filename,
                    "search_type": search_type,
                    "query": query,
                    "results_count": len(results),
                    "results": results,
                },
            )

        except Exception as e:
            logger.exception(f"search_file failed for {evidence_id}: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=f"Search failed: {str(e)}",
            )

    def _keyword_search(self, content: str, query: str) -> List[Dict[str, Any]]:
        """Keyword search: tokenize query, find matching lines, return context windows."""
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]

        if not keywords:
            return []

        matches: list[dict] = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            matched_keywords = [kw for kw in keywords if kw in line_lower]
            if matched_keywords:
                start = max(0, i - self.context_lines)
                end = min(len(lines), i + self.context_lines + 1)
                matches.append(
                    {
                        "excerpt": "\n".join(
                            f"{j + 1}: {lines[j]}" for j in range(start, end)
                        ),
                        "line_start": start + 1,
                        "line_end": end,
                        "matched_keywords": matched_keywords,
                        "relevance": len(matched_keywords) / len(keywords),
                    }
                )

        return self._merge_overlapping(matches)[: self.max_results]

    def _regex_search(self, content: str, pattern: str) -> List[Dict[str, Any]]:
        """Regex search: compile pattern, find all matches, return with context."""
        try:
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return [{"error": f"Invalid regex pattern: {e}"}]

        lines = content.split("\n")
        context_lines = min(self.context_lines, 10)  # Smaller context for regex
        matches: list[dict] = []

        for i, line in enumerate(lines):
            if regex.search(line):
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                matches.append(
                    {
                        "excerpt": "\n".join(
                            f"{j + 1}: {lines[j]}" for j in range(start, end)
                        ),
                        "line_start": start + 1,
                        "line_end": end,
                        "match_line": i + 1,
                    }
                )

        return self._merge_overlapping(matches)[: self.max_results]

    async def _extractor_rerun(
        self,
        content: str,
        evidence: Any,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Re-run domain-specific extractor with different parameters."""
        if not self.preprocessing_service:
            return [
                {"error": "Preprocessing service not available for extractor re-run"}
            ]

        # Get the detailed data type from evidence metadata
        data_type_str = getattr(evidence, "data_type", None)
        if not data_type_str:
            return [{"error": "Evidence has no data_type — cannot determine extractor"}]

        from faultmaven.models.api import DataType

        try:
            # Map unified type back to detailed type for extractor lookup
            data_type = DataType(data_type_str)
        except ValueError:
            # Try mapping unified type name to a detailed type
            unified_to_detailed = {
                "logs": DataType.LOGS_AND_ERRORS,
                "metrics": DataType.METRICS_AND_PERFORMANCE,
                "configuration": DataType.STRUCTURED_CONFIG,
                "code": DataType.SOURCE_CODE,
                "text": DataType.UNSTRUCTURED_TEXT,
                "image": DataType.VISUAL_EVIDENCE,
            }
            data_type = unified_to_detailed.get(data_type_str)
            if not data_type:
                return [{"error": f"Unknown data_type: {data_type_str}"}]

        extractor = self.preprocessing_service.extractors.get(data_type)
        if not extractor:
            return [{"error": f"No extractor available for {data_type.value}"}]

        try:
            result = extractor.extract(content, **params)
            return [
                {
                    "extractor": extractor.strategy_name,
                    "data_type": data_type.value,
                    "params": params,
                    "content": result[:10000] if len(result) > 10000 else result,
                    "truncated": len(result) > 10000,
                }
            ]
        except TypeError:
            # Extractor doesn't accept extra params — run without them
            result = extractor.extract(content)
            return [
                {
                    "extractor": extractor.strategy_name,
                    "data_type": data_type.value,
                    "params": {},
                    "content": result[:10000] if len(result) > 10000 else result,
                    "truncated": len(result) > 10000,
                    "note": "Extractor does not support custom parameters; ran with defaults",
                }
            ]

    @staticmethod
    def _merge_overlapping(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge overlapping excerpt windows."""
        if not matches:
            return []

        sorted_matches = sorted(matches, key=lambda m: m.get("line_start", 0))
        merged: list[dict] = [sorted_matches[0]]

        for current in sorted_matches[1:]:
            prev = merged[-1]
            if current.get("line_start", 0) <= prev.get("line_end", 0):
                # Overlapping — extend
                if current.get("line_end", 0) > prev.get("line_end", 0):
                    prev["line_end"] = current["line_end"]
                    prev["excerpt"] = current["excerpt"]  # Use the wider window
                    prev["relevance"] = max(
                        prev.get("relevance", 0), current.get("relevance", 0)
                    )
            else:
                merged.append(current)

        return merged
