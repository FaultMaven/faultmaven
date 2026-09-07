"""Unit Tests for ReadFileTool (TASK-015).

Storage redesign 2026-04 phase 2: ReadFileTool reads evidence from
`case.evidence` (via `context.in_memory_case` / `context.case_repository`)
and loads raw file content via `FileStorageService.retrieve_file`. The
deleted standalone evidence service path is no longer exercised here.
"""

import hashlib
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.read_file_tool import (
    MAX_TEXT_SIZE,
    TEXT_MIME_TYPES,
    ReadFileTool,
)
from faultmaven.modules.case.contracts import UploadedFile

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "ev_test123",
    original_filename: str = "error.log",
    content_ref: Optional[str] = "evidence/case_test/error.log",
    summary: str = "Error log file",
    case_id: str = "case_test",
    upload_source: str = "file_upload",
    data_type: Optional[str] = None,
):
    """Build a minimal Evidence-shaped object that ReadFileTool understands.

    Legacy `original_filename` and `content_ref` kwargs now wire up an
    UploadedFile stashed on the evidence; the case helper installs it on
    `case.uploaded_files` so production's `find_uploaded_file()` works. It is
    a REAL UploadedFile, not a MagicMock: the tool reads `display_name` off
    it, and a bare mock answers that with a mock object, which would make the
    #666 leak test unfailable.
    """
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.case_id = case_id
    ev.summary = summary

    if content_ref is not None:
        # Hashed rather than derived by substitution because UploadedFile
        # validates the ^(file_|data_)[a-f0-9]{12,16}$ shape.
        file_id = f"file_{hashlib.md5(evidence_id.encode()).hexdigest()[:12]}"
        ev.source_file_id = file_id
        ev._uploaded_file = UploadedFile(
            file_id=file_id,
            filename=original_filename,
            size_bytes=1024,
            uploaded_at_turn=1,
            storage_ref=content_ref,
            upload_source=upload_source,
            data_type=data_type,
        )
    else:
        ev.source_file_id = None
        ev._uploaded_file = None
    return ev


def _make_context(case_id: str = "case_test", evidence_items=None):
    """Build a ToolContext carrying an in-memory case with the given evidence."""
    case = MagicMock()
    case.case_id = case_id
    case.evidence = evidence_items if evidence_items is not None else [_make_evidence()]

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

    return ToolContext(
        session_id="session_test",
        case_id=case_id,
        enterprise_id="org_test",
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
        assert result.data["label"] == "error.log"
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
            enterprise_id="o",
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


class TestSyntheticFilenameNotReported:
    """#666: the tool result is read by the LLM, so the name it reports is the
    name that comes back at the user. Pastes have no filename they'd
    recognise."""

    MINTED = "pasted-content-20260709T105531.txt"

    @pytest.mark.asyncio
    async def test_result_reports_display_name_not_minted_filename(
        self, read_file_tool
    ):
        ev = _make_evidence(
            original_filename=self.MINTED,
            upload_source="text_paste",
            data_type="logs",
        )
        context = _make_context(evidence_items=[ev])
        with _patch_storage(b"line one\nline two\n"):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert result.data["label"] == "pasted text (turn 1)"
        assert self.MINTED not in str(result.data)

    def test_text_detection_still_uses_the_stored_name(self, read_file_tool):
        """The display name has no extension; the text-vs-binary decision has
        to keep reading the STORED one or a paste stops decoding as text.

        Driven directly rather than through ``execute``, because ``execute``
        hardcodes ``mime_type="text/plain"`` — which short-circuits
        ``_is_text_file`` before the extension is consulted, so the split is
        not observable from there. This pins the method's own contract: the
        name it sniffs and the name it prints are different arguments.
        """
        content = read_file_tool._process_file_content(
            file_data=b"line one\nline two\n",
            filename=self.MINTED,  # stored: ends .txt
            mime_type="application/octet-stream",  # mime says binary
            display_name="pasted text (turn 1)",  # shown: no extension
        )
        # Extension on the stored name wins → decoded as text.
        assert "line one" in content
        assert "Binary file" not in content

    def test_binary_placeholder_names_the_display_name(self, read_file_tool):
        """When it IS binary, the placeholder the LLM reads must not carry
        the minted name."""
        content = read_file_tool._process_file_content(
            file_data=b"\x00\x01\x02binary",
            filename="pasted-content-20260709T105531.bin",
            mime_type="application/octet-stream",
            display_name="pasted text (turn 1)",
        )
        assert "[Binary file: pasted text (turn 1)" in content
        assert "pasted-content-" not in content

    @pytest.mark.asyncio
    async def test_large_file_placeholder_names_the_display_name(self, read_file_tool):
        """The large-file preview IS reachable through ``execute`` (size
        alone triggers it), and its header names the file."""
        ev = _make_evidence(
            original_filename=self.MINTED,
            upload_source="text_paste",
            data_type="logs",
        )
        context = _make_context(evidence_items=[ev])
        big = b"a line of log text\n" * ((MAX_TEXT_SIZE // 19) + 10)
        assert len(big) > MAX_TEXT_SIZE
        with _patch_storage(big):
            result = await read_file_tool.execute_with_context(
                params={"evidence_id": "ev_test123"},
                context=context,
            )
        assert result.success is True
        assert "[Large file: pasted text (turn 1)" in result.data["content"]
        assert self.MINTED not in result.data["content"]


class TestConstants:
    def test_max_text_size_reasonable(self):
        assert MAX_TEXT_SIZE >= 100 * 1024  # at least 100KB
        assert MAX_TEXT_SIZE <= 10 * 1024 * 1024  # at most 10MB

    def test_text_mime_types_includes_common(self):
        assert "text/plain" in TEXT_MIME_TYPES
        assert "application/json" in TEXT_MIME_TYPES
        assert "text/csv" in TEXT_MIME_TYPES
