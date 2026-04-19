"""Unit Tests for ReadFileTool (TASK-015).

Storage redesign 2026-04 phase 2: ReadFileTool reads evidence from
`case.evidence` (via `context.in_memory_case` / `context.case_repository`)
and loads raw file content via `FileStorageService.retrieve_file`. The
deleted standalone evidence service path is no longer exercised here.
"""

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.read_file_tool import (
    MAX_TEXT_SIZE,
    TEXT_MIME_TYPES,
    ReadFileTool,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev_test123",
    original_filename: str = "error.log",
    content_ref: Optional[str] = "evidence/case_test/error.log",
    summary: str = "Error log file",
    case_id: str = "case_test",
):
    """Build a minimal Evidence-shaped object that ReadFileTool understands."""
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.case_id = case_id
    ev.original_filename = original_filename
    ev.content_ref = content_ref
    ev.summary = summary
    ev.content_size_bytes = 1024
    return ev


def _make_context(case_id: str = "case_test", evidence_items=None):
    """Build a ToolContext carrying an in-memory case with the given evidence."""
    case = MagicMock()
    case.case_id = case_id
    case.evidence = evidence_items if evidence_items is not None else [_make_evidence()]
    return ToolContext(
        session_id="session_test",
        case_id=case_id,
        organization_id="org_test",
        user_id="user_test",
        in_memory_case=case,
        execution_id="exec_test",
    )


def _patch_storage(content: bytes):
    """Patch FileStorageService.retrieve_file inside read_file_tool to return *content*."""
    return patch(
        "faultmaven.modules.evidence.domain.services.file_storage_service.FileStorageService.retrieve_file",
        new=AsyncMock(return_value=content),
    )


@pytest.fixture
def read_file_tool():
    return ReadFileTool()


# ---------------------------------------------------------------------------
# Tool properties
# ---------------------------------------------------------------------------


class TestReadFileToolProperties:
    def test_name(self, read_file_tool):
        assert read_file_tool.name == "read_file"

    def test_description_mentions_evidence(self, read_file_tool):
        assert "evidence" in read_file_tool.description.lower()

    def test_parameters_schema_requires_evidence_id(self, read_file_tool):
        schema = read_file_tool.parameters_schema
        assert schema["type"] == "object"
        assert "evidence_id" in schema["properties"]
        assert "evidence_id" in schema["required"]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestReadFileExecution:
    @pytest.mark.asyncio
    async def test_read_text_file_success(self, read_file_tool):
        context = _make_context()
        with _patch_storage(b"Error: Connection timeout\nWARNING: Retrying...\n"):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "Connection timeout" in result.data["content"]
        assert result.data["filename"] == "error.log"
        assert result.data["evidence_id"] == "ev_test123"

    @pytest.mark.asyncio
    async def test_read_file_missing_evidence_id(self, read_file_tool):
        context = _make_context()
        result = await read_file_tool.execute_with_context(params={}, context=context)
        assert result.success is False
        assert "evidence_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_no_case_available(self, read_file_tool):
        # Storage redesign 2026-04 phase 2: when neither in_memory_case nor
        # case_repository is on the context, the lookup fails cleanly.
        context = ToolContext(
            session_id="s",
            case_id="case_test",
            organization_id="o",
            user_id="u",
        )
        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=context,
        )
        assert result.success is False
        assert "case" in result.error.lower() or "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_evidence_not_found(self, read_file_tool):
        context = _make_context(evidence_items=[])
        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_nonexistent"},
            context=context,
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_wrong_case(self, read_file_tool):
        ev = _make_evidence(case_id="different_case")
        context = _make_context(evidence_items=[ev])
        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=context,
        )
        assert result.success is False
        assert "does not belong" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_no_content_ref(self, read_file_tool):
        ev = _make_evidence(content_ref=None)
        context = _make_context(evidence_items=[ev])
        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=context,
        )
        assert result.success is False
        assert (
            "content_ref" in result.error.lower()
            or "not stored" in result.error.lower()
        )

    @pytest.mark.asyncio
    async def test_read_file_with_max_lines(self, read_file_tool):
        lines = "\n".join([f"Line {i}" for i in range(100)])
        context = _make_context()
        with _patch_storage(lines.encode()):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123", "max_lines": 10},
                context=context,
            )
        assert result.success is True
        assert "Line 0" in result.data["content"]
        assert "Line 9" in result.data["content"]
        assert "of 100" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_file_with_offset(self, read_file_tool):
        lines = "\n".join([f"Line {i}" for i in range(100)])
        context = _make_context()
        with _patch_storage(lines.encode()):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123", "max_lines": 10, "offset": 50},
                context=context,
            )
        assert result.success is True
        assert "Line 50" in result.data["content"]
        assert "Line 59" in result.data["content"]
        assert "Line 49" not in result.data["content"]


# ---------------------------------------------------------------------------
# File type handling
# ---------------------------------------------------------------------------


class TestFileTypeHandling:
    @pytest.mark.asyncio
    async def test_read_json_file(self, read_file_tool):
        ev = _make_evidence(original_filename="config.json")
        context = _make_context(evidence_items=[ev])
        with _patch_storage(b'{"key": "value", "count": 42}'):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "key" in result.data["content"]
        assert "value" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_csv_file(self, read_file_tool):
        ev = _make_evidence(original_filename="data.csv")
        context = _make_context(evidence_items=[ev])
        with _patch_storage(b"name,value\nalice,100\nbob,200"):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "name,value" in result.data["content"]


# ---------------------------------------------------------------------------
# Large-file handling
# ---------------------------------------------------------------------------


class TestLargeFileHandling:
    @pytest.mark.asyncio
    async def test_large_file_returns_preview(self, read_file_tool):
        # Content larger than MAX_TEXT_SIZE
        large_lines = "\n".join([f"Line {i}: " + "x" * 100 for i in range(20000)])
        context = _make_context()
        with _patch_storage(large_lines.encode()):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "large file" in result.data["content"].lower()
        assert "Line 0" in result.data["content"]
        assert "Line 19999" in result.data["content"]
        assert "omitted" in result.data["content"].lower()

    @pytest.mark.asyncio
    async def test_large_file_with_max_lines_works(self, read_file_tool):
        large_lines = "\n".join([f"Line {i}" for i in range(20000)])
        context = _make_context()
        with _patch_storage(large_lines.encode()):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123", "max_lines": 100},
                context=context,
            )
        assert result.success is True
        assert "Line 99" in result.data["content"]
        assert "Line 100" not in result.data["content"]


# ---------------------------------------------------------------------------
# Encoding handling
# ---------------------------------------------------------------------------


class TestEncodingHandling:
    @pytest.mark.asyncio
    async def test_utf8_content(self, read_file_tool):
        text = "Hello, \u4e16\u754c! \U0001f30d \u041f\u0440\u0438\u0432\u0435\u0442"
        ev = _make_evidence(original_filename="unicode.txt")
        context = _make_context(evidence_items=[ev])
        with _patch_storage(text.encode("utf-8")):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "\u4e16\u754c" in result.data["content"]
        assert "\u041f\u0440\u0438\u0432\u0435\u0442" in result.data["content"]

    @pytest.mark.asyncio
    async def test_latin1_fallback(self, read_file_tool):
        # Content that's valid latin-1 but not valid UTF-8
        ev = _make_evidence(original_filename="latin.txt")
        context = _make_context(evidence_items=[ev])
        with _patch_storage(b"Caf\xe9"):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "Caf" in result.data["content"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_text_size_reasonable(self):
        assert MAX_TEXT_SIZE >= 100 * 1024  # at least 100KB
        assert MAX_TEXT_SIZE <= 10 * 1024 * 1024  # at most 10MB

    def test_text_mime_types_includes_common(self):
        assert "text/plain" in TEXT_MIME_TYPES
        assert "application/json" in TEXT_MIME_TYPES
        assert "text/csv" in TEXT_MIME_TYPES
