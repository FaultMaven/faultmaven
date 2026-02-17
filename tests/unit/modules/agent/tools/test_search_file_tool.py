"""Tests for SearchFileTool — Tier 2 mechanical search (v4.0)."""

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
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc", "query": "test"},
            context=context,
        )
        assert result.success is False
        assert "does not belong" in result.error


class TestToolProperties:
    def test_name(self, tool):
        assert tool.name == "search_file"

    def test_schema_has_required_fields(self, tool):
        schema = tool.parameters_schema
        assert "evidence_id" in schema["properties"]
        assert "query" in schema["properties"]
        assert "search_type" in schema["properties"]
        assert schema["required"] == ["evidence_id", "query"]
