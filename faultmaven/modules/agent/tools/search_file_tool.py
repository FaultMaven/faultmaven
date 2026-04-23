"""Search File Tool — Mechanical Search over Evidence Files

Provides two search modes over previously uploaded evidence files:

- keyword: Split query into keywords, find matching lines with context
- regex: Treat query as a regex pattern

Historical note: an ``extractor`` mode previously advertised "re-run the
domain extractor with different parameters". No extractor actually
accepted parameters, so every invocation silently degraded to a
defaults-only re-run while returning content already present in the
agent's ``<evidence>`` prompt block. Removed as part of plan
Cross-Cutting Hardening H2 — see
``docs/working/WIP-data-processing-improvement-plan.md``.

Design Reference: docs/architecture/data-processing/README.md
"""

import logging
import re
from collections import Counter
from typing import Any

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

# Default search parameters
# Context lines: ±N lines around each match. Keep small — log entries are
# typically self-contained on a single line, and large context windows cause
# the TOOL_RESULT_MAX_CHARS truncation to cut off matches.
DEFAULT_CONTEXT_LINES = 3
DEFAULT_MAX_RESULTS = 20
# Ceiling for LLM-requested max_results (prevents unbounded memory use)
MAX_RESULTS_CEILING = 200


class SearchFileTool(AgentTool):
    """Mechanical search over raw evidence file content.

    Allows the agent to search previously uploaded evidence files using
    keyword matching or regex patterns. Used within Directed Analysis as
    a supplementary tool for exact keyword/regex/timestamp matching.
    """

    def __init__(
        self,
        storage_service: Any = None,
        context_lines: int = DEFAULT_CONTEXT_LINES,
        max_results: int = DEFAULT_MAX_RESULTS,
    ):
        self.storage_service = storage_service
        self.context_lines = context_lines
        self.max_results = max_results

    @property
    def name(self) -> str:
        return "search_file"

    @property
    def description(self) -> str:
        return (
            "Search a previously uploaded evidence file for specific information. "
            "Two output formats: 'excerpts' (default) returns context windows for "
            "detailed investigation; 'count' returns compact matched lines for "
            "counting and aggregation queries (e.g. 'how many unique IPs?'). "
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
                        "regex pattern for regex mode"
                    ),
                },
                "search_type": {
                    "type": "string",
                    "enum": ["keyword", "regex"],
                    "description": (
                        "Search mode: 'keyword' splits query into keywords and finds matching lines, "
                        "'regex' treats query as a regex pattern"
                    ),
                },
                "output_format": {
                    "type": "string",
                    "enum": ["excerpts", "count"],
                    "description": (
                        "Output format. 'excerpts' (default): context windows with "
                        "surrounding lines for detailed investigation. "
                        "'count': compact matched lines only (no context) for "
                        "counting and aggregation queries. Use 'count' when you "
                        "need to know how many matches exist or enumerate all values."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return (up to 200). "
                        "Default: 20 for excerpts, 200 for count mode."
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
        output_format = params.get("output_format", "excerpts")

        # Resolve effective max_results: LLM override → format default → instance default
        requested_max = params.get("max_results")
        if requested_max is not None:
            effective_max_results = min(int(requested_max), MAX_RESULTS_CEILING)
        elif output_format == "count":
            # Count mode defaults to ceiling — completeness over detail
            effective_max_results = MAX_RESULTS_CEILING
        else:
            effective_max_results = self.max_results

        if not evidence_id:
            return ToolResult(success=False, data=None, error="evidence_id is required")

        if not query:
            return ToolResult(success=False, data=None, error="query is required")

        try:
            # Resolve evidence content from case.evidence (storage redesign
            # 2026-04 phase 2: standalone evidence path deleted)
            resolved = await self._resolve_evidence_content(
                evidence_id,
                context,
            )
            if resolved is None:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence not found or file not accessible: {evidence_id}",
                )
            content, filename, evidence_obj = resolved

            # Phase 2c — triage-to-escalation counter. Fires when this
            # tool call targets an evidence whose structural_index was
            # delivered in the same turn (collected_at_turn == current
            # turn). A high rate per data_type signals the extractor's
            # structural index is leaving the agent uninformed.
            try:
                from faultmaven.infrastructure.observability.evidence_metrics import (
                    record_triage_escalation_if_same_turn,
                )

                record_triage_escalation_if_same_turn(
                    evidence=evidence_obj,
                    case=getattr(context, "in_memory_case", None),
                    tool_name="search_file",
                )
            except Exception:
                # Telemetry is best-effort — never fail the tool for it.
                pass

            # Route by output format
            if output_format == "count":
                return self._execute_count_format(
                    content,
                    filename,
                    evidence_id,
                    query,
                    search_type,
                    effective_max_results,
                )

            # --- Excerpts format (default) ---
            # Dispatch by search type — get all merged results, then cap
            if search_type == "regex":
                all_results = self._regex_search(content, query)
            else:
                all_results = self._keyword_search(content, query)

            # Apply result cap and track truncation
            total_matches = len(all_results)
            truncated = total_matches > effective_max_results
            results = all_results[:effective_max_results]

            logger.info(
                "search_file: %s (%s), mode=%s, format=excerpts, query='%s', "
                "results=%d (total=%d, truncated=%s)",
                evidence_id,
                filename,
                search_type,
                query[:50],
                len(results),
                total_matches,
                truncated,
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
                        "output_format": "excerpts",
                        "results_count": 0,
                        "total_matches": 0,
                        "truncated": False,
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

            data = {
                "evidence_id": evidence_id,
                "filename": filename,
                "search_type": search_type,
                "query": query,
                "output_format": "excerpts",
                "results_count": len(results),
                "total_matches": total_matches,
                "truncated": truncated,
                "results": results,
            }
            if truncated:
                data["truncation_note"] = (
                    f"Showing {len(results)} of {total_matches} matches. "
                    "Results are capped — use output_format='count' for "
                    "counting/aggregation queries."
                )

            return ToolResult(success=True, data=data)

        except Exception as e:
            logger.exception("search_file failed for %s: %s", evidence_id, e)
            return ToolResult(
                success=False,
                data=None,
                error=f"Search failed: {str(e)}",
            )

    def _execute_count_format(
        self,
        content: str,
        filename: str,
        evidence_id: str,
        query: str,
        search_type: str,
        effective_max_results: int,
    ) -> ToolResult:
        """Count format: compact matched lines for counting/aggregation queries.

        Returns matched lines as "LINE: CONTENT" strings — no context windows.
        ~7x more compact than excerpts, fits far more results within the
        milestone engine's TOOL_RESULT_MAX_CHARS budget.
        """
        all_matches = self._count_search(content, query, search_type)
        match_count = len(all_matches)
        truncated = match_count > effective_max_results
        returned = all_matches[:effective_max_results]

        logger.info(
            "search_file: %s (%s), mode=%s, format=count, query='%s', "
            "match_count=%d (returned=%d, truncated=%s)",
            evidence_id,
            filename,
            search_type,
            query[:50],
            match_count,
            len(returned),
            truncated,
        )

        if not returned:
            vocabulary = self._extract_file_vocabulary(content)
            top_terms = (
                vocabulary.get("patterns", []) + vocabulary.get("frequent_tokens", [])
            )[:10]
            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "filename": filename,
                    "search_type": search_type,
                    "query": query,
                    "output_format": "count",
                    "match_count": 0,
                    "results_count": 0,
                    "truncated": False,
                    "matched_lines": [],
                    "vocabulary": vocabulary,
                    "suggestion": (
                        f"No matches found. File contains these terms: "
                        f"{', '.join(top_terms)}"
                        if top_terms
                        else "No matches found and no recognizable terms extracted."
                    ),
                },
            )

        # Format as compact "LINE: CONTENT" strings
        matched_lines = [f"{m['line']}: {m['content']}" for m in returned]

        data: dict[str, Any] = {
            "evidence_id": evidence_id,
            "filename": filename,
            "search_type": search_type,
            "query": query,
            "output_format": "count",
            "match_count": match_count,
            "results_count": len(returned),
            "truncated": truncated,
            "matched_lines": matched_lines,
        }
        if truncated:
            data["truncation_note"] = (
                f"Showing {len(returned)} of {match_count} matched lines."
            )

        return ToolResult(success=True, data=data)

    def _count_search(
        self, content: str, query: str, search_type: str
    ) -> list[dict[str, Any]]:
        """Compact search: returns matched lines only, no context windows.

        Returns list of {"line": N, "content": "..."} dicts.
        """
        lines = content.split("\n")

        if search_type == "regex":
            try:
                regex = re.compile(query, re.IGNORECASE | re.MULTILINE)
            except re.error as e:
                return [{"line": 0, "content": f"Invalid regex: {e}"}]
            return [
                {"line": i + 1, "content": line}
                for i, line in enumerate(lines)
                if regex.search(line)
            ]

        # Keyword mode
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]
        if not keywords:
            return []
        return [
            {"line": i + 1, "content": line}
            for i, line in enumerate(lines)
            if all(kw in line.lower() for kw in keywords)
        ]

    async def _resolve_evidence_content(
        self,
        evidence_id: str,
        context: ToolContext,
    ) -> tuple[str, str, Any] | None:
        """Resolve evidence content from the case-embedded evidence list.

        Storage redesign 2026-04 phase 2 deleted the standalone
        `evidence_artifacts` table and its service. Evidence is case-tied only:
        the unified ingestion pipeline creates Evidence objects embedded in the
        Case (`case.evidence`) with `content_ref` pointing to the stored file.

        Returns:
            (content_str, filename, evidence_obj) or None if not found
        """
        case = getattr(context, "in_memory_case", None)
        if case is None:
            case_repo = getattr(context, "case_repository", None)
            if case_repo is None:
                logger.warning("search_file: no case_repository on tool context")
                return None
            case = await case_repo.get(context.case_id)
            if case is None:
                logger.warning("search_file: case %s not found", context.case_id)
                return None

        # Find evidence in the case's embedded list
        case_evidence = None
        for ev in getattr(case, "evidence", []) or []:
            if getattr(ev, "evidence_id", None) == evidence_id:
                case_evidence = ev
                break

        if case_evidence is None:
            logger.warning(
                "search_file: evidence %s not found in case %s (has %d evidence items)",
                evidence_id,
                context.case_id,
                len(getattr(case, "evidence", []) or []),
            )
            return None

        # Read raw file via content_ref
        content_ref = getattr(case_evidence, "content_ref", None)
        if not content_ref:
            logger.warning(
                "search_file: evidence %s has no content_ref (file not stored)",
                evidence_id,
            )
            return None

        if self.storage_service is not None and hasattr(
            self.storage_service, "retrieve_file"
        ):
            try:
                file_data = await self.storage_service.retrieve_file(content_ref)
                filename = (
                    getattr(case_evidence, "original_filename", None) or evidence_id
                )
                content = file_data.decode("utf-8", errors="replace")
                return content, filename, case_evidence
            except Exception as e:
                logger.warning(
                    "search_file: storage_service.retrieve_file failed for %s: %s",
                    content_ref,
                    e,
                )
                return None

        logger.warning(
            "search_file: storage_service unavailable; cannot read evidence %s "
            "(content_ref=%s)",
            evidence_id,
            content_ref,
        )
        return None

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

        merged = self._merge_overlapping(matches)
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
            return self._merge_overlapping(partial_matches)

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

        return self._merge_overlapping(matches)

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
