"""Tests for SearchFileTool — Tier 2 mechanical search (v4.0)."""

import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool


@pytest.fixture
def tool():
    return SearchFileTool(
        storage_service=MagicMock(),
        preprocessing_service=MagicMock(),
        context_lines=5,
        max_results=3,
    )


@pytest.fixture
def context():
    evidence = MagicMock()
    evidence.case_id = "case_123"
    evidence.data_type = "logs"

    evidence_service = AsyncMock()
    evidence_service.get_evidence.return_value = evidence
    evidence_service.download_evidence.return_value = (
        b"line 1\nline 2 ERROR timeout\nline 3\nline 4 ERROR connection\nline 5\n",
        "app.log",
        "text/plain",
    )

    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_1",
        evidence_service=evidence_service,
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
        context.evidence_service.get_evidence.return_value = None
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_missing", "query": "test"},
            context=context,
        )
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_wrong_case_id(self, tool, context):
        evidence = MagicMock()
        evidence.case_id = "case_other"
        context.evidence_service.get_evidence.return_value = evidence
        # download_evidence should not be attempted for wrong-case evidence
        context.evidence_service.download_evidence.return_value = None
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "test"},
            context=context,
        )
        assert result.success is False
        assert (
            "not found" in result.error.lower()
            or "not accessible" in result.error.lower()
        )


class TestPartialMatchFallback:
    """Tests for partial match fallback when full-keyword search returns nothing."""

    @pytest.fixture
    def multi_keyword_context(self):
        """Context where only one keyword matches."""
        evidence = MagicMock()
        evidence.case_id = "case_123"
        evidence.data_type = "logs"

        evidence_service = AsyncMock()
        evidence_service.get_evidence.return_value = evidence
        evidence_service.download_evidence.return_value = (
            b"2024-01-15 server started\nprocessing request from 10.0.0.1\n"
            b"connection timeout to database\nretrying connection\n"
            b"query executed successfully\nresponse sent 200 OK\n",
            "app.log",
            "text/plain",
        )

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            organization_id="org_1",
            user_id="user_1",
            evidence_service=evidence_service,
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
    async def test_partial_match_capped_at_five(self, tool):
        """Partial match results are capped at 5."""
        # Build content with a keyword appearing on many lines
        lines = [f"line {i} keyword_alpha data" for i in range(100)]
        content = "\n".join(lines)

        tool_instance = SearchFileTool(context_lines=0, max_results=3)
        results = tool_instance._keyword_search(
            content, "keyword_alpha nonexistent_zzz"
        )

        assert len(results) <= 5


class TestVocabularyExtraction:
    """Tests for vocabulary extraction on zero-result searches."""

    @pytest.fixture
    def vocab_context(self):
        """Context with content rich in extractable patterns."""
        evidence = MagicMock()
        evidence.case_id = "case_123"
        evidence.data_type = "logs"

        log_content = (
            "2024-01-15 10:30:00 ERROR ConnectionTimeout: failed to reach 10.0.0.5\n"
            "2024-01-15 10:30:01 WARN RetryableError: attempt 2 for api-server:8080\n"
            "2024-01-15 10:30:02 INFO request to /api/v1/health returned 503\n"
            "2024-01-15 10:30:03 ERROR NullPointerException at service handler\n"
            "2024-01-15 10:30:04 DEBUG processing batch from kafka-consumer:9092\n"
        )

        evidence_service = AsyncMock()
        evidence_service.get_evidence.return_value = evidence
        evidence_service.download_evidence.return_value = (
            log_content.encode(),
            "app.log",
            "text/plain",
        )

        return ToolContext(
            session_id="sess_1",
            case_id="case_123",
            organization_id="org_1",
            user_id="user_1",
            evidence_service=evidence_service,
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


class TestToolProperties:
    def test_name(self, tool):
        assert tool.name == "search_file"

    def test_schema_has_required_fields(self, tool):
        schema = tool.parameters_schema
        assert "evidence_id" in schema["properties"]
        assert "query" in schema["properties"]
        assert "search_type" in schema["properties"]
        assert schema["required"] == ["evidence_id", "query"]
