"""Tests for SearchFileTool — Tier 2 mechanical search (v4.0)."""

import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.case.contracts import UploadedFile


@pytest.fixture
def tool():
    """SearchFileTool with a mock storage_service that returns canned bytes.

    ``SearchFileTool`` reads raw file content via
    ``self.storage_service.retrieve_file(content_ref)``.
    """
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(
        return_value=b"line 1\nline 2 ERROR timeout\nline 3\nline 4 ERROR connection\nline 5\n"
    )
    return SearchFileTool(
        storage_service=storage,
        context_lines=5,
        max_results=3,
    )


def _make_search_tool_with_storage(
    content: bytes, context_lines: int = 5, max_results: int = 3
):
    """Build a SearchFileTool wired to a mock storage_service returning *content*.

    Tests inject ``storage_service`` directly because the tool resolves
    evidence content through it.
    """
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(return_value=content)
    return SearchFileTool(
        storage_service=storage,
        context_lines=context_lines,
        max_results=max_results,
    )


def _make_evidence(
    evidence_id="ev_abc",
    content_ref="evidence/case_123/app.log",
    filename="app.log",
    upload_source="file_upload",
    data_type=None,
):
    """Build a minimal Evidence-shaped object for case-embedded lookup.

    Post-010: file-backed vs chat-extracted is decided by
    ``source_file_id`` (set when ``content_ref`` is provided, ``None``
    otherwise). Pass ``content_ref=None`` to model a chat-extracted
    evidence record with no backing file.

    The backing file is a REAL ``UploadedFile``, not a MagicMock: the tool
    reads ``display_name`` off it, and a bare mock answers that with a mock
    object, which would make the #666 leak tests unfailable.
    """
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.case_id = "case_123"
    ev.source_type.value = "logs"

    if content_ref is not None:
        # Bind a unique source_file_id per evidence so case.find_uploaded_file
        # can resolve to the right backing file when multiple evidence records
        # are present in the case. Hashed rather than derived by substitution
        # because UploadedFile validates the ^(file_|data_)[a-f0-9]{12,16}$
        # shape.
        file_id = f"file_{hashlib.md5(evidence_id.encode()).hexdigest()[:12]}"
        ev.source_file_id = file_id
        ev._uploaded_file = UploadedFile(
            file_id=file_id,
            filename=filename,
            size_bytes=100,
            uploaded_at_turn=1,
            storage_ref=content_ref,
            upload_source=upload_source,
            data_type=data_type,
        )
    else:
        ev.source_file_id = None
        ev._uploaded_file = None
    return ev


def _wire_case_lookup(case):
    """Populate `case.uploaded_files` and `case.find_uploaded_file` from the
    evidence records' stashed `_uploaded_file` attributes.

    Tests construct `case.evidence` directly, so this helper centralizes the
    lookup wiring that production reads via `case.find_uploaded_file()`.
    """
    uploaded_files = [
        ev._uploaded_file
        for ev in case.evidence
        if getattr(ev, "_uploaded_file", None) is not None
    ]
    case.uploaded_files = uploaded_files

    def _find(file_id):
        if not file_id:
            return None
        return next((f for f in uploaded_files if f.file_id == file_id), None)

    case.find_uploaded_file.side_effect = _find


@pytest.fixture
def context():
    """ToolContext with an in-memory case carrying the test evidence record."""
    case = MagicMock()
    case.case_id = "case_123"
    case.evidence = [_make_evidence()]
    _wire_case_lookup(case)

    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id="user_1",
        in_memory_case=case,
    )


class TestKeywordSearch:
    @pytest.mark.asyncio
    async def test_finds_matching_lines(self, tool, context):
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR timeout"},
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] > 0
        assert result.data["search_type"] == "keyword"

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty(self, tool, context):
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "nonexistent_pattern_xyz"},
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] == 0

    @pytest.mark.asyncio
    async def test_short_keywords_filtered(self, tool, context):
        """Keywords <= 2 chars are filtered out."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "a b c"},
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] == 0

    @pytest.mark.asyncio
    async def test_results_include_context_lines(self, tool, context):
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR"},
            context=context,
        )

        assert result.success is True
        for r in result.data["results"]:
            assert "excerpt" in r
            assert "line_start" in r
            assert "line_end" in r


class TestRegexSearch:
    @pytest.mark.asyncio
    async def test_regex_pattern_matches(self, tool, context):
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": r"ERROR\s+\w+",
                "search_type": "regex",
            },
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] > 0
        assert result.data["search_type"] == "regex"

    @pytest.mark.asyncio
    async def test_invalid_regex_returns_error(self, tool, context):
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "[invalid",
                "search_type": "regex",
            },
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] == 1
        assert "error" in result.data["results"][0]


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_evidence_id(self, tool, context):
        result = await tool.execute_with_context(
            params={"query": "test"},
            context=context,
        )
        assert result.success is False
        assert "evidence_id" in result.error

    @pytest.mark.asyncio
    async def test_missing_query(self, tool, context):
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )
        assert result.success is False
        assert "query" in result.error

    @pytest.mark.asyncio
    async def test_evidence_not_found(self, tool, context):
        # No evidence with the requested id is on the case.
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_missing", "query": "test"},
            context=context,
        )
        assert result.success is False
        assert (
            "not found" in result.error.lower()
            or "not accessible" in result.error.lower()
        )

    @pytest.mark.asyncio
    async def test_wrong_case_id(self, tool, context):
        # When the evidence has no content_ref, the resolver returns an error
        # string and the tool reports the file is not stored. ISS-025: error
        # text mentions "no stored file content" so the LLM can distinguish
        # this case from a missing-ID lookup.
        ev = _make_evidence(content_ref=None)
        context.in_memory_case.evidence = [ev]
        _wire_case_lookup(context.in_memory_case)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "test"},
            context=context,
        )
        assert result.success is False
        err_lower = result.error.lower()
        # Post-010: evidence with source_file_id=None is rejected as
        # chat-extracted (no stored file behind it).
        assert (
            "not found" in err_lower
            or "not accessible" in err_lower
            or "no stored file" in err_lower
        )


class TestPartialMatchFallback:
    """Tests for partial match fallback when full-keyword search returns nothing."""

    @pytest.fixture
    def multi_keyword_context(self, tool):
        """Context where only one keyword matches.

        File content is sourced from ``tool.storage_service.retrieve_file``;
        we mutate the canned content for this fixture's scenario.
        """
        tool.storage_service.retrieve_file = AsyncMock(
            return_value=(
                b"2024-01-15 server started\nprocessing request from 10.0.0.1\n"
                b"connection timeout to database\nretrying connection\n"
                b"query executed successfully\nresponse sent 200 OK\n"
            )
        )

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [_make_evidence()]
        _wire_case_lookup(case)

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_partial_match_when_full_query_misses(
        self, tool, multi_keyword_context
    ):
        """When all keywords together match nothing, individual keywords are tried."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                # "timeout" exists but "nonexistent" doesn't — full match fails,
                # partial match for "timeout" should succeed
                "query": "timeout nonexistent",
            },
            context=multi_keyword_context,
        )

        assert result.success is True
        assert result.data["results_count"] > 0
        first_result = result.data["results"][0]
        assert first_result.get("partial_match") is True
        assert "timeout" in first_result["matched_keywords"]

    @pytest.mark.asyncio
    async def test_no_partial_match_for_single_keyword(
        self, tool, multi_keyword_context
    ):
        """Single-keyword search doesn't trigger partial match fallback."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "nonexistent_xyz",
            },
            context=multi_keyword_context,
        )

        assert result.success is True
        assert result.data["results_count"] == 0

    @pytest.mark.asyncio
    async def test_partial_match_returns_all_merged(self, tool):
        """Partial match returns all merged results (cap applied externally)."""
        # Build content with a keyword appearing on many lines
        lines = [f"line {i} keyword_alpha data" for i in range(100)]
        content = "\n".join(lines)

        tool_instance = SearchFileTool(context_lines=0, max_results=3)
        results = tool_instance._keyword_search(
            content, "keyword_alpha nonexistent_zzz"
        )

        # Internal method returns all merged results; cap is in execute_with_context
        assert len(results) == 100


class TestVocabularyExtraction:
    """Tests for vocabulary extraction on zero-result searches."""

    @pytest.fixture
    def vocab_context(self, tool):
        """Context with content rich in extractable patterns."""
        log_content = (
            "2024-01-15 10:30:00 ERROR ConnectionTimeout: failed to reach 10.0.0.5\n"
            "2024-01-15 10:30:01 WARN RetryableError: attempt 2 for api-server:8080\n"
            "2024-01-15 10:30:02 INFO request to /api/v1/health returned 503\n"
            "2024-01-15 10:30:03 ERROR NullPointerException at service handler\n"
            "2024-01-15 10:30:04 DEBUG processing batch from kafka-consumer:9092\n"
        )

        # Storage redesign 2026-04 phase 2: tool reads file via
        # storage_service.retrieve_file (case-embedded path).
        tool.storage_service.retrieve_file = AsyncMock(
            return_value=log_content.encode()
        )

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [_make_evidence(content_ref="evidence/case_123/app.log")]
        _wire_case_lookup(case)

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_zero_results_returns_vocabulary(self, tool, vocab_context):
        """Zero-result search includes vocabulary for recovery."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "completely_nonexistent_term_xyz",
            },
            context=vocab_context,
        )

        assert result.success is True
        assert result.data["results_count"] == 0
        assert "vocabulary" in result.data
        assert "suggestion" in result.data

        vocab = result.data["vocabulary"]
        assert "patterns" in vocab
        assert "frequent_tokens" in vocab

    @pytest.mark.asyncio
    async def test_vocabulary_extracts_known_patterns(self, tool, vocab_context):
        """Vocabulary identifies error codes, exceptions, IPs, host:port."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "completely_nonexistent_term_xyz",
            },
            context=vocab_context,
        )

        patterns = result.data["vocabulary"]["patterns"]
        pattern_str = " ".join(patterns)

        # Should find HTTP error code
        assert "503" in pattern_str
        # Should find exception names
        assert any("Exception" in p or "Error" in p or "Timeout" in p for p in patterns)
        # Should find IP
        assert any("10.0.0.5" in p for p in patterns)

    @pytest.mark.asyncio
    async def test_suggestion_includes_terms(self, tool, vocab_context):
        """Suggestion string lists discovered terms."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "completely_nonexistent_term_xyz",
            },
            context=vocab_context,
        )

        assert "No matches found" in result.data["suggestion"]
        assert "File contains these terms" in result.data["suggestion"]

    @pytest.mark.asyncio
    async def test_successful_search_has_no_vocabulary(self, tool, context):
        """Successful searches don't include vocabulary (existing behavior preserved)."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR timeout"},
            context=context,
        )

        assert result.success is True
        assert result.data["results_count"] > 0
        assert "vocabulary" not in result.data

    def test_vocabulary_performance_on_large_content(self, tool):
        """Vocabulary extraction completes in < 500ms on ~1MB content."""
        # Generate ~1MB of log-like content with varied tokens
        # so some fall in the 2-10 frequency range
        services = [
            "auth-service",
            "payment-gateway",
            "order-processor",
            "inventory-manager",
            "notification-hub",
            "cache-layer",
        ]
        errors = [
            "ConnectionTimeout",
            "NullPointerException",
            "OutOfMemoryError",
            "SocketException",
            "DatabaseFailure",
            "AuthError",
        ]
        lines = []
        for i in range(20000):
            svc = services[i % len(services)]
            err = errors[i % len(errors)] if i % 200 == 0 else ""
            lines.append(
                f"2024-01-15 10:30:{i % 60:02d} INFO {svc} request "
                f"id={i} from 192.168.1.{i % 256} {err}"
            )
        content = "\n".join(lines)
        assert len(content) > 500_000  # Confirm substantial size

        start = time.perf_counter()
        vocab = tool._extract_file_vocabulary(content)
        elapsed = time.perf_counter() - start

        assert (
            elapsed < 0.5
        ), f"Vocabulary extraction took {elapsed:.3f}s (> 500ms budget)"
        assert len(vocab["patterns"]) > 0


class TestTruncationReporting:
    """Tests for result truncation transparency."""

    @pytest.fixture
    def many_matches_context(self, tool):
        """Context with content that produces many non-overlapping matches."""
        # Space ERROR lines 20 apart so context windows (±5) don't overlap.
        # This produces many distinct merged results that exceed max_results=3.
        lines = []
        for i in range(400):
            if i % 20 == 0:
                lines.append(f"2024-01-15 10:{i:04d} ERROR service failure #{i // 20}")
            else:
                lines.append(f"2024-01-15 10:{i:04d} INFO normal operation")
        content = "\n".join(lines)

        # Storage redesign 2026-04 phase 2: tool reads file via
        # storage_service.retrieve_file (case-embedded path).
        tool.storage_service.retrieve_file = AsyncMock(return_value=content.encode())

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [_make_evidence(content_ref="evidence/case_123/app.log")]
        _wire_case_lookup(case)

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_truncated_results_include_total_matches(
        self, tool, many_matches_context
    ):
        """When results exceed max_results, total_matches reports the full count."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR"},
            context=many_matches_context,
        )

        assert result.success is True
        assert result.data["truncated"] is True
        assert result.data["total_matches"] > result.data["results_count"]
        assert result.data["results_count"] <= 3  # fixture tool has max_results=3
        assert result.data["total_matches"] == 20  # 400 lines / 20 spacing = 20 matches

    @pytest.mark.asyncio
    async def test_truncated_results_include_truncation_note(
        self, tool, many_matches_context
    ):
        """Truncated results include a note for the LLM."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR"},
            context=many_matches_context,
        )

        assert "truncation_note" in result.data
        assert "Showing" in result.data["truncation_note"]
        assert str(result.data["total_matches"]) in result.data["truncation_note"]

    @pytest.mark.asyncio
    async def test_non_truncated_results_have_no_note(self, tool, context):
        """Non-truncated results don't include a truncation_note."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR"},
            context=context,
        )

        assert result.success is True
        assert result.data["truncated"] is False
        assert result.data["total_matches"] == result.data["results_count"]
        assert "truncation_note" not in result.data

    @pytest.mark.asyncio
    async def test_zero_results_include_truncation_fields(self, tool, context):
        """Zero-result responses include truncation fields for consistency."""
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "nonexistent_pattern_xyz"},
            context=context,
        )

        assert result.success is True
        assert result.data["total_matches"] == 0
        assert result.data["truncated"] is False


class TestMaxResultsOverride:
    """Tests for LLM-requested max_results parameter."""

    @pytest.fixture
    def many_lines_context(self, tool):
        """Context with many matching lines."""
        lines = [
            f"2024-01-15 10:30:{i % 60:02d} ERROR failure #{i}" for i in range(100)
        ]
        content = "\n".join(lines)

        # Storage redesign 2026-04 phase 2: tool reads file via
        # storage_service.retrieve_file (case-embedded path).
        tool.storage_service.retrieve_file = AsyncMock(return_value=content.encode())

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [_make_evidence(content_ref="evidence/case_123/app.log")]
        _wire_case_lookup(case)

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_max_results_override_increases_limit(self, many_lines_context):
        """LLM can request more results via max_results parameter."""
        content = "\n".join(
            f"2024-01-15 10:30:{i % 60:02d} ERROR failure #{i}" for i in range(100)
        ).encode()
        tool = _make_search_tool_with_storage(content, context_lines=0, max_results=3)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR", "max_results": 50},
            context=many_lines_context,
        )

        assert result.success is True
        assert result.data["results_count"] == 50

    @pytest.mark.asyncio
    async def test_max_results_capped_at_ceiling(self, many_lines_context):
        """max_results is capped at MAX_RESULTS_CEILING (200)."""
        content = "\n".join(
            f"2024-01-15 10:30:{i % 60:02d} ERROR failure #{i}" for i in range(100)
        ).encode()
        tool = _make_search_tool_with_storage(content, context_lines=0, max_results=3)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR", "max_results": 999},
            context=many_lines_context,
        )

        assert result.success is True
        # 100 lines total, ceiling is 200, so we get all 100
        assert result.data["results_count"] == 100
        assert result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_default_max_results_when_not_specified(self, many_lines_context):
        """Without max_results param, uses the tool's default."""
        content = "\n".join(
            f"2024-01-15 10:30:{i % 60:02d} ERROR failure #{i}" for i in range(100)
        ).encode()
        tool = _make_search_tool_with_storage(content, context_lines=0, max_results=5)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "ERROR"},
            context=many_lines_context,
        )

        assert result.success is True
        assert result.data["results_count"] == 5
        assert result.data["truncated"] is True
        assert result.data["total_matches"] == 100


class TestCountOutputFormat:
    """Tests for output_format='count' — compact results for aggregation."""

    @pytest.fixture
    def ip_log_context(self, tool):
        """Context simulating HDFS log with many IPs."""
        # 200 log lines, 50 with IPs in 10.251.x.x range
        lines = []
        for i in range(200):
            if i % 4 == 0:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.DataNode: "
                    f"Receiving block from /10.251.{i % 256}.{(i * 7) % 256}:50010"
                )
            else:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.NameSystem: "
                    f"addStoredBlock: blockMap updated"
                )
        content = "\n".join(lines)

        # Storage redesign 2026-04 phase 2: tool reads file via
        # storage_service.retrieve_file (case-embedded path).
        tool.storage_service.retrieve_file = AsyncMock(return_value=content.encode())

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [_make_evidence(content_ref="evidence/case_123/hdfs.log")]
        _wire_case_lookup(case)

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_count_returns_match_count(self, tool, ip_log_context):
        """Count mode returns accurate match_count for all matching lines."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "10.251",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        assert result.success is True
        assert result.data["output_format"] == "count"
        assert result.data["match_count"] == 50  # every 4th line out of 200

    @pytest.mark.asyncio
    async def test_count_returns_compact_matched_lines(self, tool, ip_log_context):
        """Count mode returns compact 'LINE: CONTENT' strings, no context windows."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "10.251",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        assert "matched_lines" in result.data
        for line_str in result.data["matched_lines"]:
            # Each entry is "LINE_NUM: CONTENT" — a flat string, not a dict
            assert isinstance(line_str, str)
            assert ": " in line_str
            assert "10.251" in line_str

    @pytest.mark.asyncio
    async def test_count_no_context_windows(self, tool, ip_log_context):
        """Count mode does NOT return excerpt-style results with context."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "10.251",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        assert "results" not in result.data  # No excerpt-style results
        assert "matched_lines" in result.data  # Compact lines instead

    @pytest.mark.asyncio
    async def test_count_defaults_to_high_max_results(self, ip_log_context):
        """Count mode defaults to MAX_RESULTS_CEILING, not the tool's default."""
        # Build matching content (must mirror ip_log_context fixture).
        lines = []
        for i in range(200):
            if i % 4 == 0:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.DataNode: "
                    f"Receiving block from /10.251.{i % 256}.{(i * 7) % 256}:50010"
                )
            else:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.NameSystem: "
                    f"addStoredBlock: blockMap updated"
                )
        tool = _make_search_tool_with_storage("\n".join(lines).encode(), max_results=3)
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "10.251",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        # Should return all 50 matches, not cap at 3
        assert result.data["results_count"] == 50
        assert result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_count_with_regex(self, tool, ip_log_context):
        """Count mode works with regex search."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": r"10\.251\.\d+\.\d+",
                "search_type": "regex",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        assert result.success is True
        assert result.data["match_count"] == 50

    @pytest.mark.asyncio
    async def test_count_zero_matches(self, tool, context):
        """Count mode with zero matches returns vocabulary."""
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "nonexistent_xyz",
                "output_format": "count",
            },
            context=context,
        )

        assert result.success is True
        assert result.data["match_count"] == 0
        assert result.data["results_count"] == 0
        assert "vocabulary" in result.data

    @pytest.mark.asyncio
    async def test_count_preserves_individual_matches(self, ip_log_context):
        """Count mode returns each match individually; excerpts merge them.

        This is the key advantage: for 'how many unique IPs?' the LLM gets
        all 50 matched lines individually, not merged into a few context blobs.
        """
        # Build matching content (must mirror ip_log_context fixture).
        lines = []
        for i in range(200):
            if i % 4 == 0:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.DataNode: "
                    f"Receiving block from /10.251.{i % 256}.{(i * 7) % 256}:50010"
                )
            else:
                lines.append(
                    f"081109 2035{i:02d} INFO dfs.NameSystem: "
                    f"addStoredBlock: blockMap updated"
                )
        tool = _make_search_tool_with_storage(
            "\n".join(lines).encode(), context_lines=3, max_results=200
        )

        # Excerpts mode: adjacent matches merge into fewer windows
        excerpts_result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "10.251"},
            context=ip_log_context,
        )
        # Count mode: every match is a separate line
        count_result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": "10.251",
                "output_format": "count",
            },
            context=ip_log_context,
        )

        # Count mode: 50 individual matched lines, one per match
        assert len(count_result.data["matched_lines"]) == 50

        # Excerpts mode: merging collapses 50 matches into far fewer results
        assert excerpts_result.data["results_count"] < 50

        # Count mode's match_count is accurate regardless
        assert count_result.data["match_count"] == 50


class TestToolProperties:
    def test_name(self, tool):
        assert tool.name == "search_file"

    def test_schema_has_required_fields(self, tool):
        schema = tool.parameters_schema
        assert "evidence_id" in schema["properties"]
        assert "query" in schema["properties"]
        assert "search_type" in schema["properties"]
        assert "output_format" in schema["properties"]
        assert "max_results" in schema["properties"]
        assert schema["required"] == ["evidence_id", "query"]


class TestNonFileBackedEvidenceGuard:
    """ISS-025 regression tests.

    The agent creates symptom_evidence records whose extracts are
    derived from chat content rather than a stored file (no
    ``source_file_id``). When the LLM passes one of those evidence_ids
    to search_file, the tool must:
      1. Reject the call with a non-cryptic error.
      2. Enumerate file-backed evidence_ids the LLM should retry with.
    Without these signals the LLM was concluding "the file is inaccessible"
    after the first failure and giving up on follow-up questions.
    """

    @pytest.mark.asyncio
    async def test_chat_extracted_evidence_is_rejected_with_redirect(self, tool):
        """A chat-extracted evidence record (no source_file_id) must be
        rejected before storage retrieval, with the error message naming
        the actual file-backed evidence in the case so the LLM can
        retry correctly. Post-010: the guard is based on source_file_id
        presence; the form column is gone."""
        chat_ev = _make_evidence(
            evidence_id="ev_chat",
            content_ref=None,  # No source_file_id → chat-extracted shape
        )
        document_ev = _make_evidence(
            evidence_id="ev_upload",
            content_ref="evidence/case_123/healthapp.log",
            filename="HealthApp_2k.log",
        )

        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [chat_ev, document_ev]
        _wire_case_lookup(case)
        ctx = ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_chat", "query": "ERROR"},
            context=ctx,
        )

        assert result.success is False
        err = result.error
        # The error explains chat-extracted evidence has no stored file
        assert "chat" in err.lower() or "no stored file" in err
        # Names the file-backed alternative so the LLM can redirect
        assert "ev_upload" in err
        assert "HealthApp_2k.log" in err
        # Must NOT have hit storage at all — the guard fires before retrieval
        tool.storage_service.retrieve_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_evidence_id_lists_alternatives(self, tool):
        """When the LLM passes an evidence_id that doesn't exist at all, the
        error should still list the file-backed evidence in the case."""
        document_ev = _make_evidence(
            evidence_id="ev_actual",
            content_ref="evidence/case_123/system.log",
            filename="system.log",
        )
        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [document_ev]
        _wire_case_lookup(case)
        ctx = ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_typo_or_stale", "query": "ERROR"},
            context=ctx,
        )

        assert result.success is False
        assert "not found" in result.error.lower()
        # Redirect: names the actual upload so the LLM can retry
        assert "ev_actual" in result.error
        assert "system.log" in result.error

    @pytest.mark.asyncio
    async def test_no_alternatives_when_only_chat_extracted_evidence_exists(self, tool):
        """Edge case: a case with ONLY chat-extracted evidence (no
        file-backed uploads) should still produce a clean error, no
        spurious 'available' line. Post-010: non-searchable evidence
        is identified by source_file_id IS NULL."""
        only_chat = _make_evidence(
            evidence_id="ev_only_chat",
            content_ref=None,  # chat-extracted: no source_file_id
        )
        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [only_chat]
        _wire_case_lookup(case)
        ctx = ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_only_chat", "query": "anything"},
            context=ctx,
        )

        assert result.success is False
        # The error explains why the evidence is non-searchable
        err_lower = result.error.lower()
        assert "no stored file" in err_lower or "chat" in err_lower
        # No misleading "Available file-backed evidence:" line when
        # there genuinely isn't any
        assert "Available file-backed evidence" not in result.error


class TestSyntheticFilenameNotReported:
    """#666: the engine appends citation guidance quoting whatever this tool
    puts under ``filename``, so a minted ``pasted-content-<ts>.txt`` reported
    here is what the user reads back. The tool reports display names."""

    MINTED = "pasted-content-20260709T105531.txt"

    def _paste_context(self):
        ev = _make_evidence(
            evidence_id="ev_paste",
            filename=self.MINTED,
            upload_source="text_paste",
            data_type="logs",
        )
        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [ev]
        _wire_case_lookup(case)
        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

    @pytest.mark.asyncio
    async def test_excerpts_result_reports_display_name(self, tool):
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_paste", "query": "ERROR timeout"},
            context=self._paste_context(),
        )
        assert result.success is True
        assert result.data["label"] == "pasted text (turn 1)"
        assert self.MINTED not in json.dumps(result.data)

    @pytest.mark.asyncio
    async def test_count_result_reports_display_name(self, tool):
        result = await tool.execute_with_context(
            params={
                "evidence_id": "ev_paste",
                "query": "ERROR",
                "output_format": "count",
            },
            context=self._paste_context(),
        )
        assert result.success is True
        assert result.data["label"] == "pasted text (turn 1)"
        assert self.MINTED not in json.dumps(result.data)

    @pytest.mark.asyncio
    async def test_alternatives_hint_reports_display_name(self, tool):
        """The redirect hint listing other file-backed evidence is model-
        visible too, and named files by their raw filename."""
        chat_ev = _make_evidence(evidence_id="ev_chat", content_ref=None)
        paste_ev = _make_evidence(
            evidence_id="ev_paste",
            filename=self.MINTED,
            upload_source="text_paste",
            data_type="logs",
        )
        case = MagicMock()
        case.case_id = "case_123"
        case.evidence = [chat_ev, paste_ev]
        _wire_case_lookup(case)
        ctx = ToolContext(
            session_id="sess_1",
            case_id="case_123",
            enterprise_id="org_1",
            user_id="user_1",
            in_memory_case=case,
        )

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_chat", "query": "anything"},
            context=ctx,
        )

        assert result.success is False
        assert self.MINTED not in result.error
        assert "ev_paste (pasted text (turn 1))" in result.error
