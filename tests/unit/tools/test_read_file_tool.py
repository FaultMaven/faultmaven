"""Unit Tests for ReadFileTool (TASK-015)

This module tests the ReadFileTool which allows agents to read
evidence artifact file contents.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock
import pytest

from faultmaven.modules.agent.tools.read_file_tool import (
    ReadFileTool,
    MAX_TEXT_SIZE,
    TEXT_MIME_TYPES,
)
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def read_file_tool():
    """Create a ReadFileTool instance."""
    return ReadFileTool()


@pytest.fixture
def sample_context():
    """Create a sample tool context with mock evidence service."""
    evidence_service = AsyncMock()
    return ToolContext(
        session_id="session_test",
        case_id="case_test",
        organization_id="org_test",
        user_id="user_test",
        evidence_service=evidence_service,
        execution_id="exec_test",
    )


@pytest.fixture
def sample_evidence():
    """Create a sample evidence artifact."""
    return EvidenceArtifact(
        evidence_id="ev_test123",
        case_id="case_test",
        user_id="user_test",
        organization_id="org_test",
        original_filename="error.log",
        stored_filename="ev_test123_error.log",
        file_path="/path/to/error.log",
        file_size=1024,
        mime_type="text/plain",
        evidence_type=EvidenceArtifactType.LOG_FILE,
        description="Error log file",
        is_primary=True,
    )


# =============================================================================
# Test: Tool Properties
# =============================================================================


class TestReadFileToolProperties:
    """Tests for ReadFileTool properties."""

    def test_name(self, read_file_tool):
        """Test tool name is correct."""
        assert read_file_tool.name == "read_file"

    def test_description(self, read_file_tool):
        """Test tool has meaningful description."""
        assert "read" in read_file_tool.description.lower()
        assert "evidence" in read_file_tool.description.lower()

    def test_parameters_schema(self, read_file_tool):
        """Test parameters schema is valid."""
        schema = read_file_tool.parameters_schema
        assert schema["type"] == "object"
        assert "evidence_id" in schema["properties"]
        assert "evidence_id" in schema["required"]

    def test_parameters_schema_includes_optional(self, read_file_tool):
        """Test parameters schema includes optional params."""
        schema = read_file_tool.parameters_schema
        assert "max_lines" in schema["properties"]
        assert "offset" in schema["properties"]


# =============================================================================
# Test: Execute with Context
# =============================================================================


class TestReadFileExecution:
    """Tests for ReadFileTool execution."""

    @pytest.mark.asyncio
    async def test_read_text_file_success(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading a text file successfully."""
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b"Error: Connection timeout\nWARNING: Retrying...\n",
            "error.log",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "Connection timeout" in result.data["content"]
        assert result.data["filename"] == "error.log"
        assert result.data["evidence_id"] == "ev_test123"

    @pytest.mark.asyncio
    async def test_read_file_missing_evidence_id(self, read_file_tool, sample_context):
        """Test reading file without evidence_id returns error."""
        result = await read_file_tool.execute_with_context(
            params={},
            context=sample_context,
        )

        assert result.success is False
        assert "evidence_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_no_evidence_service(self, read_file_tool):
        """Test reading file without evidence service returns error."""
        context = ToolContext(
            session_id="session_test",
            case_id="case_test",
            organization_id="org_test",
            user_id="user_test",
            evidence_service=None,
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=context,
        )

        assert result.success is False
        assert "service" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, read_file_tool, sample_context):
        """Test reading nonexistent file returns error."""
        sample_context.evidence_service.get_evidence.return_value = None

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_nonexistent"},
            context=sample_context,
        )

        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_wrong_case(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading file from different case returns error."""
        sample_evidence.case_id = "different_case"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is False
        assert "does not belong" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_file_with_max_lines(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading file with max_lines limit."""
        lines = "\n".join([f"Line {i}" for i in range(100)])
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            lines.encode(),
            "large.log",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123", "max_lines": 10},
            context=sample_context,
        )

        assert result.success is True
        # Should have line numbers and only 10 lines
        assert "Line 0" in result.data["content"]
        assert "Line 9" in result.data["content"]
        # Should indicate total lines
        assert "of 100" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_file_with_offset(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading file with offset."""
        lines = "\n".join([f"Line {i}" for i in range(100)])
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            lines.encode(),
            "large.log",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123", "max_lines": 10, "offset": 50},
            context=sample_context,
        )

        assert result.success is True
        # Should start from line 51
        assert "Line 50" in result.data["content"]
        assert "Line 59" in result.data["content"]
        # Should not include earlier lines
        assert "Line 49" not in result.data["content"]


# =============================================================================
# Test: File Type Handling
# =============================================================================


class TestFileTypeHandling:
    """Tests for handling different file types."""

    @pytest.mark.asyncio
    async def test_read_json_file(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading a JSON file."""
        sample_evidence.mime_type = "application/json"
        sample_evidence.original_filename = "config.json"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b'{"key": "value", "count": 42}',
            "config.json",
            "application/json",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "key" in result.data["content"]
        assert "value" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_csv_file(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading a CSV file."""
        sample_evidence.mime_type = "text/csv"
        sample_evidence.original_filename = "data.csv"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b"name,value\nalice,100\nbob,200",
            "data.csv",
            "text/csv",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "name,value" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_image_returns_summary(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading image file returns summary instead of content."""
        sample_evidence.mime_type = "image/png"
        sample_evidence.original_filename = "screenshot.png"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b"\x89PNG\r\n\x1a\n...",  # PNG magic bytes
            "screenshot.png",
            "image/png",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "image file" in result.data["content"].lower()
        assert "screenshot.png" in result.data["content"]

    @pytest.mark.asyncio
    async def test_read_pdf_returns_summary(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading PDF file returns summary instead of content."""
        sample_evidence.mime_type = "application/pdf"
        sample_evidence.original_filename = "document.pdf"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b"%PDF-1.4...",  # PDF magic bytes
            "document.pdf",
            "application/pdf",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "pdf" in result.data["content"].lower()

    @pytest.mark.asyncio
    async def test_read_small_binary_returns_base64(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading small binary file returns base64."""
        sample_evidence.mime_type = "application/octet-stream"
        sample_evidence.original_filename = "data.bin"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            b"\x00\x01\x02\x03\x04",  # Small binary data
            "data.bin",
            "application/octet-stream",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert (
            "base64" in result.data["content"].lower()
            or "binary" in result.data["content"].lower()
        )


# =============================================================================
# Test: Large File Handling
# =============================================================================


class TestLargeFileHandling:
    """Tests for handling large files."""

    @pytest.mark.asyncio
    async def test_large_file_returns_preview(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test large file returns preview with first and last lines."""
        # Create content larger than MAX_TEXT_SIZE
        large_lines = "\n".join([f"Line {i}: " + "x" * 100 for i in range(20000)])
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            large_lines.encode(),
            "huge.log",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        # Should mention it's a large file
        assert "large file" in result.data["content"].lower()
        # Should have first lines
        assert "Line 0" in result.data["content"]
        # Should have last lines
        assert "Line 19999" in result.data["content"]
        # Should mention omitted lines
        assert "omitted" in result.data["content"].lower()

    @pytest.mark.asyncio
    async def test_large_file_with_max_lines_works(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test large file with max_lines still works."""
        large_lines = "\n".join([f"Line {i}" for i in range(20000)])
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            large_lines.encode(),
            "huge.log",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123", "max_lines": 100},
            context=sample_context,
        )

        assert result.success is True
        # With max_lines specified, should return exactly that many
        assert "Line 99" in result.data["content"]
        assert "Line 100" not in result.data["content"]


# =============================================================================
# Test: UTF-8 and Encoding Handling
# =============================================================================


class TestEncodingHandling:
    """Tests for handling different encodings."""

    @pytest.mark.asyncio
    async def test_utf8_content(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test reading UTF-8 content."""
        content = "Hello, 世界! 🌍 Привет"
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            content.encode("utf-8"),
            "unicode.txt",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        assert "世界" in result.data["content"]
        assert "Привет" in result.data["content"]

    @pytest.mark.asyncio
    async def test_latin1_fallback(
        self,
        read_file_tool,
        sample_context,
        sample_evidence,
    ):
        """Test fallback to latin-1 for non-UTF-8 content."""
        # Content that's valid latin-1 but not valid UTF-8
        content = b"Caf\xe9"  # "Café" in latin-1
        sample_context.evidence_service.get_evidence.return_value = sample_evidence
        sample_context.evidence_service.download_evidence.return_value = (
            content,
            "latin.txt",
            "text/plain",
        )

        result = await read_file_tool.execute_with_context(
            params={"evidence_id": "ev_test123"},
            context=sample_context,
        )

        assert result.success is True
        # Should decode successfully with fallback
        assert "Caf" in result.data["content"]


# =============================================================================
# Test: Constants
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_max_text_size_reasonable(self):
        """Test MAX_TEXT_SIZE is reasonable."""
        assert MAX_TEXT_SIZE >= 100 * 1024  # At least 100KB
        assert MAX_TEXT_SIZE <= 10 * 1024 * 1024  # At most 10MB

    def test_text_mime_types_includes_common(self):
        """Test TEXT_MIME_TYPES includes common text types."""
        assert "text/plain" in TEXT_MIME_TYPES
        assert "application/json" in TEXT_MIME_TYPES
        assert "text/csv" in TEXT_MIME_TYPES
