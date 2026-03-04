"""Search File Tool — Tier 2 Mechanical Search (v4.0)

Provides three search modes over previously uploaded evidence files:
- keyword: Split query into keywords, find matching lines with context
- regex: Treat query as a regex pattern
- extractor: Re-run domain-specific extractor with different parameters

Design Reference: docs/working/DRAFT-data-preprocessing-spec-v4.md Section 3
"""

import logging
import re
from collections import Counter
from typing import Any

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
    def parameters_schema(self) -> dict[str, Any]:
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
        params: dict[str, Any],
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
                "search_file: %s (%s), mode=%s, query='%s', results=%d",
                evidence_id,
                filename,
                search_type,
                query[:50],
                len(results),
            )

            if not results:
                vocabulary = self._extract_file_vocabulary(content)
                top_terms = (
                    vocabulary.get("patterns", [])
                    + vocabulary.get("frequent_tokens", [])
                )[:10]
                return ToolResult(
                    success=True,
                    data={
                        "evidence_id": evidence_id,
                        "filename": filename,
                        "search_type": search_type,
                        "query": query,
                        "results_count": 0,
                        "results": [],
                        "vocabulary": vocabulary,
                        "suggestion": (
                            f"No matches found. File contains these terms: "
                            f"{', '.join(top_terms)}"
                            if top_terms
                            else "No matches found and no recognizable terms extracted."
                        ),
                    },
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
            logger.exception("search_file failed for %s: %s", evidence_id, e)
            return ToolResult(
                success=False,
                data=None,
                error=f"Search failed: {str(e)}",
            )

    def _keyword_search(self, content: str, query: str) -> list[dict[str, Any]]:
        """Keyword search: tokenize query, find matching lines, return context windows.

        Two-pass strategy:
        1. Find lines matching ALL keywords (high relevance).
        2. If no all-keyword matches and multiple keywords exist, fall back to
           individual keyword matching (lower cap, marked partial_match).
        """
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]

        if not keywords:
            return []

        # Pass 1: lines matching ALL keywords
        matches: list[dict] = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            matched_keywords = [kw for kw in keywords if kw in line_lower]
            if len(matched_keywords) == len(keywords):
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
                        "relevance": 1.0,
                    }
                )

        merged = self._merge_overlapping(matches)[: self.max_results]
        if merged:
            return merged

        # Pass 2: partial match fallback — individual keywords
        if len(keywords) > 1:
            partial_matches: list[dict] = []
            for kw in keywords:
                for i, line in enumerate(lines):
                    if kw in line.lower():
                        start = max(0, i - self.context_lines)
                        end = min(len(lines), i + self.context_lines + 1)
                        partial_matches.append(
                            {
                                "excerpt": "\n".join(
                                    f"{j + 1}: {lines[j]}" for j in range(start, end)
                                ),
                                "line_start": start + 1,
                                "line_end": end,
                                "matched_keywords": [kw],
                                "relevance": 1 / len(keywords),
                                "partial_match": True,
                            }
                        )
            return self._merge_overlapping(partial_matches)[:5]

        return []

    def _regex_search(self, content: str, pattern: str) -> list[dict[str, Any]]:
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
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
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

    # --- Vocabulary extraction (for zero-result recovery) ---

    # Compiled once at class level
    _VOCAB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("http_errors", re.compile(r"\b[45]\d{2}\b")),
        (
            "exceptions",
            re.compile(r"\b[A-Z][a-zA-Z]*(?:Error|Exception|Failure|Fault|Timeout)\b"),
        ),
        ("host_port", re.compile(r"\b[\w-]+:\d{2,5}\b")),
        ("ip_addresses", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
        ("file_paths", re.compile(r"/[\w/.-]+")),
    ]

    _STOP_WORDS = frozenset(
        {
            "the",
            "and",
            "for",
            "are",
            "but",
            "not",
            "you",
            "all",
            "can",
            "had",
            "her",
            "was",
            "one",
            "our",
            "out",
            "has",
            "have",
            "from",
            "with",
            "they",
            "been",
            "said",
            "each",
            "which",
            "their",
            "will",
            "other",
            "about",
            "many",
            "then",
            "them",
            "these",
            "some",
            "would",
            "make",
            "like",
            "into",
            "time",
            "very",
            "when",
            "come",
            "could",
            "more",
            "than",
            "first",
            "also",
            "its",
            "over",
            "such",
            "after",
            "this",
            "that",
            "what",
            "there",
            "where",
            "just",
            "most",
            "only",
            # Log noise
            "info",
            "debug",
            "warn",
            "warning",
            "error",
            "trace",
            "level",
            "timestamp",
            "date",
            "log",
            "logger",
            "message",
            "msg",
            "null",
            "none",
            "true",
            "false",
            "undefined",
        }
    )

    def _extract_file_vocabulary(self, content: str) -> dict[str, list[str]]:
        """Extract vocabulary from file content for zero-result recovery.

        Three-pass heuristic:
        1. Known patterns (HTTP errors, exceptions, IPs, paths)
        2. Frequent tokens (statistical)
        3. Structural hints (from preprocessed content if available)
        """
        # Budget: first 100KB
        sample = content[:102400]

        result: dict[str, list[str]] = {"patterns": [], "frequent_tokens": []}

        # Pass 1: Known patterns
        seen: set[str] = set()
        for _label, pattern in self._VOCAB_PATTERNS:
            for match in pattern.finditer(sample):
                val = match.group(0)
                if val not in seen:
                    seen.add(val)
                    result["patterns"].append(val)
                    if len(result["patterns"]) >= 30:
                        break

        # Pass 2: Frequent tokens
        tokens = re.split(r"[\s=:,;|\[\]{}()\"\'+]+", sample)
        counts: Counter[str] = Counter()
        for tok in tokens:
            tok_lower = tok.lower().strip(".-_/")
            if (
                len(tok_lower) > 2
                and not tok_lower.isdigit()
                and tok_lower not in self._STOP_WORDS
                and tok_lower.isalnum()
            ):
                counts[tok_lower] += 1

        # Tokens appearing 2-10 times (not too rare, not too common)
        frequent = [tok for tok, count in counts.most_common(50) if 2 <= count <= 10][
            :20
        ]
        result["frequent_tokens"] = frequent

        return result

    @staticmethod
    def _merge_overlapping(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
