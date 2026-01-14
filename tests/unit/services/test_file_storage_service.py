"""Unit tests for FileStorageService class (TASK-013).

Tests the file storage service functionality including:
- Store file to filesystem
- Retrieve file from storage
- Delete file from storage
- Get file info
- File validation (size, MIME type, filename)
- Path generation and sanitization
- Security (path traversal prevention)
"""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.exceptions import ValidationException, ServiceError, NotFoundError


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def temp_storage_dir():
    """Create temporary directory for file storage tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def file_storage_service(temp_storage_dir):
    """Create FileStorageService with temporary directory."""
    return FileStorageService(
        storage_root=temp_storage_dir,
        max_file_size_bytes=10 * 1024 * 1024,  # 10MB for tests
        allowed_mime_types=[],  # Allow all
    )


@pytest.fixture
def restricted_file_storage_service(temp_storage_dir):
    """Create FileStorageService with MIME type restrictions."""
    return FileStorageService(
        storage_root=temp_storage_dir,
        max_file_size_bytes=10 * 1024 * 1024,
        allowed_mime_types=["image/png", "image/jpeg", "text/plain"],
    )


@pytest.fixture
def small_limit_file_storage_service(temp_storage_dir):
    """Create FileStorageService with small size limit."""
    return FileStorageService(
        storage_root=temp_storage_dir,
        max_file_size_bytes=1024,  # 1KB limit
        allowed_mime_types=[],
    )


@pytest.fixture
def sample_file_data():
    """Create sample file data for testing."""
    return b"This is test file content for unit testing."


@pytest.fixture
def sample_image_data():
    """Create sample PNG file data (minimal valid PNG)."""
    # Minimal 1x1 pixel PNG
    return (
        b"\x89PNG\r\n\x1a\n"  # PNG signature
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


# ============================================================
# Store File Tests
# ============================================================


class TestStoreFile:
    """Test store_file method."""

    @pytest.mark.asyncio
    async def test_store_file_creates_file_on_disk(
        self, file_storage_service, sample_file_data, temp_storage_dir
    ):
        """Test that store_file creates file on disk."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        full_path = os.path.join(temp_storage_dir, result["file_path"])
        assert os.path.exists(full_path)

    @pytest.mark.asyncio
    async def test_store_file_returns_correct_path_format(
        self, file_storage_service, sample_file_data
    ):
        """Test that store_file returns path in correct format (org/case/date/uuid_filename)."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        # Check path format
        parts = result["file_path"].split(os.sep)
        assert len(parts) == 4  # org/case/date/filename
        assert parts[0] == "org_123"
        assert parts[1] == "case_456"
        # Date folder should be YYYY-MM-DD format
        assert len(parts[2]) == 10
        # Stored filename should include UUID prefix
        assert "_test.txt" in parts[3]

    @pytest.mark.asyncio
    async def test_store_file_returns_stored_filename_with_uuid_prefix(
        self, file_storage_service, sample_file_data
    ):
        """Test that stored_filename includes UUID prefix."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="document.pdf",
            organization_id="org_123",
            case_id="case_456",
            mime_type="application/pdf",
        )

        stored_filename = result["stored_filename"]
        # Should be uuid_originalname format
        assert "_" in stored_filename
        assert stored_filename.endswith("document.pdf")
        # UUID is 12 hex chars
        uuid_part = stored_filename.split("_")[0]
        assert len(uuid_part) == 12

    @pytest.mark.asyncio
    async def test_store_file_returns_correct_file_size(
        self, file_storage_service, sample_file_data
    ):
        """Test that store_file returns correct file size."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        assert result["file_size"] == len(sample_file_data)

    @pytest.mark.asyncio
    async def test_store_file_creates_directories_if_missing(
        self, file_storage_service, sample_file_data, temp_storage_dir
    ):
        """Test that store_file creates nested directories."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="new_org",
            case_id="new_case",
            mime_type="text/plain",
        )

        # Verify directory structure was created
        org_dir = os.path.join(temp_storage_dir, "new_org")
        assert os.path.isdir(org_dir)

        case_dir = os.path.join(org_dir, "new_case")
        assert os.path.isdir(case_dir)

    @pytest.mark.asyncio
    async def test_store_file_file_too_large_raises_validation_error(
        self, small_limit_file_storage_service
    ):
        """Test that oversized file raises ValidationException."""
        large_data = b"x" * 2048  # 2KB, exceeds 1KB limit

        with pytest.raises(ValidationException) as exc_info:
            await small_limit_file_storage_service.store_file(
                file_data=large_data,
                original_filename="large.bin",
                organization_id="org_123",
                case_id="case_456",
                mime_type="application/octet-stream",
            )

        assert "file_size" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_store_file_disallowed_mime_type_raises_validation_error(
        self, restricted_file_storage_service, sample_file_data
    ):
        """Test that disallowed MIME type raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await restricted_file_storage_service.store_file(
                file_data=sample_file_data,
                original_filename="script.js",
                organization_id="org_123",
                case_id="case_456",
                mime_type="application/javascript",
            )

        assert "mime_type" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_store_file_allowed_mime_type_succeeds(
        self, restricted_file_storage_service, sample_file_data
    ):
        """Test that allowed MIME type succeeds."""
        result = await restricted_file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        assert result is not None
        assert "file_path" in result


# ============================================================
# Retrieve File Tests
# ============================================================


class TestRetrieveFile:
    """Test retrieve_file method."""

    @pytest.mark.asyncio
    async def test_retrieve_file_returns_file_bytes(
        self, file_storage_service, sample_file_data
    ):
        """Test that retrieve_file returns correct bytes."""
        store_result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        retrieved_data = await file_storage_service.retrieve_file(
            store_result["file_path"]
        )

        assert retrieved_data == sample_file_data

    @pytest.mark.asyncio
    async def test_retrieve_file_not_found_raises_not_found_error(
        self, file_storage_service
    ):
        """Test that missing file raises NotFoundError."""
        with pytest.raises(NotFoundError) as exc_info:
            await file_storage_service.retrieve_file("nonexistent/path/file.txt")

        assert exc_info.value.resource_type == "File"

    @pytest.mark.asyncio
    async def test_retrieve_file_preserves_binary_data(
        self, file_storage_service, sample_image_data
    ):
        """Test that binary data is preserved correctly."""
        store_result = await file_storage_service.store_file(
            file_data=sample_image_data,
            original_filename="image.png",
            organization_id="org_123",
            case_id="case_456",
            mime_type="image/png",
        )

        retrieved_data = await file_storage_service.retrieve_file(
            store_result["file_path"]
        )

        assert retrieved_data == sample_image_data


# ============================================================
# Delete File Tests
# ============================================================


class TestDeleteFile:
    """Test delete_file method."""

    @pytest.mark.asyncio
    async def test_delete_file_removes_file_from_disk(
        self, file_storage_service, sample_file_data, temp_storage_dir
    ):
        """Test that delete_file removes file from disk."""
        store_result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        full_path = os.path.join(temp_storage_dir, store_result["file_path"])
        assert os.path.exists(full_path)

        deleted = await file_storage_service.delete_file(store_result["file_path"])

        assert deleted is True
        assert not os.path.exists(full_path)

    @pytest.mark.asyncio
    async def test_delete_file_returns_true_if_deleted(
        self, file_storage_service, sample_file_data
    ):
        """Test that delete_file returns True when file is deleted."""
        store_result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        result = await file_storage_service.delete_file(store_result["file_path"])
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_file_returns_false_if_not_found(self, file_storage_service):
        """Test that delete_file returns False for missing file."""
        result = await file_storage_service.delete_file("nonexistent/path/file.txt")
        assert result is False


# ============================================================
# Get File Info Tests
# ============================================================


class TestGetFileInfo:
    """Test get_file_info method."""

    @pytest.mark.asyncio
    async def test_get_file_info_returns_metadata(
        self, file_storage_service, sample_file_data
    ):
        """Test that get_file_info returns correct metadata."""
        store_result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        info = await file_storage_service.get_file_info(store_result["file_path"])

        assert info is not None
        assert info["file_size"] == len(sample_file_data)
        assert "modified_time" in info
        assert "created_time" in info
        assert info["file_path"] == store_result["file_path"]

    @pytest.mark.asyncio
    async def test_get_file_info_returns_none_if_not_found(self, file_storage_service):
        """Test that get_file_info returns None for missing file."""
        info = await file_storage_service.get_file_info("nonexistent/path/file.txt")
        assert info is None


# ============================================================
# Validate File Tests
# ============================================================


class TestValidateFile:
    """Test validate_file method."""

    def test_validate_file_accepts_valid_file(self, file_storage_service):
        """Test that validate_file accepts valid file parameters."""
        # Should not raise any exception
        file_storage_service.validate_file(
            file_size=1024,
            mime_type="text/plain",
            original_filename="test.txt",
        )

    def test_validate_file_zero_size_raises_validation_error(
        self, file_storage_service
    ):
        """Test that zero-size file raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            file_storage_service.validate_file(
                file_size=0,
                mime_type="text/plain",
                original_filename="test.txt",
            )

        assert "file_size" in str(exc_info.value).lower()

    def test_validate_file_negative_size_raises_validation_error(
        self, file_storage_service
    ):
        """Test that negative size raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            file_storage_service.validate_file(
                file_size=-1,
                mime_type="text/plain",
                original_filename="test.txt",
            )

        assert "file_size" in str(exc_info.value).lower()

    def test_validate_file_oversized_raises_validation_error(
        self, small_limit_file_storage_service
    ):
        """Test that oversized file raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            small_limit_file_storage_service.validate_file(
                file_size=2048,  # 2KB, exceeds 1KB limit
                mime_type="text/plain",
                original_filename="test.txt",
            )

        assert "file_size" in str(exc_info.value).lower()

    def test_validate_file_disallowed_mime_raises_validation_error(
        self, restricted_file_storage_service
    ):
        """Test that disallowed MIME type raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            restricted_file_storage_service.validate_file(
                file_size=1024,
                mime_type="application/javascript",
                original_filename="script.js",
            )

        assert "mime_type" in str(exc_info.value).lower()

    def test_validate_file_empty_filename_raises_validation_error(
        self, file_storage_service
    ):
        """Test that empty filename raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            file_storage_service.validate_file(
                file_size=1024,
                mime_type="text/plain",
                original_filename="",
            )

        assert "filename" in str(exc_info.value).lower()

    def test_validate_file_whitespace_filename_raises_validation_error(
        self, file_storage_service
    ):
        """Test that whitespace-only filename raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            file_storage_service.validate_file(
                file_size=1024,
                mime_type="text/plain",
                original_filename="   ",
            )

        assert "filename" in str(exc_info.value).lower()

    def test_validate_file_path_traversal_raises_validation_error(
        self, file_storage_service
    ):
        """Test that path traversal in filename raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            file_storage_service.validate_file(
                file_size=1024,
                mime_type="text/plain",
                original_filename="../../../etc/passwd",
            )

        assert "traversal" in str(exc_info.value).lower()


# ============================================================
# Path Generation Tests
# ============================================================


class TestGenerateStoragePath:
    """Test _generate_storage_path method."""

    def test_generate_storage_path_creates_unique_paths(self, file_storage_service):
        """Test that paths are unique (UUID-based)."""
        path1 = file_storage_service._generate_storage_path(
            organization_id="org_123",
            case_id="case_456",
            original_filename="test.txt",
        )

        path2 = file_storage_service._generate_storage_path(
            organization_id="org_123",
            case_id="case_456",
            original_filename="test.txt",
        )

        # Paths should be different due to UUID
        assert path1[1] != path2[1]

    def test_generate_storage_path_includes_uuid_in_filename(
        self, file_storage_service
    ):
        """Test that stored filename includes UUID prefix."""
        stored_filename, file_path = file_storage_service._generate_storage_path(
            organization_id="org_123",
            case_id="case_456",
            original_filename="document.pdf",
        )

        # UUID should be 12 hex chars followed by underscore
        parts = stored_filename.split("_", 1)
        assert len(parts) == 2
        assert len(parts[0]) == 12  # UUID hex

    def test_generate_storage_path_includes_date_folder(self, file_storage_service):
        """Test that date folder is created (YYYY-MM-DD format)."""
        stored_filename, file_path = file_storage_service._generate_storage_path(
            organization_id="org_123",
            case_id="case_456",
            original_filename="test.txt",
        )

        parts = file_path.split(os.sep)
        date_folder = parts[2]

        # Verify YYYY-MM-DD format
        assert len(date_folder) == 10
        assert date_folder.count("-") == 2

    def test_generate_storage_path_sanitizes_filename(self, file_storage_service):
        """Test that dangerous characters are sanitized."""
        stored_filename, file_path = file_storage_service._generate_storage_path(
            organization_id="org_123",
            case_id="case_456",
            original_filename='<script>alert("xss")</script>.txt',
        )

        # Should not contain dangerous characters
        assert "<" not in stored_filename
        assert ">" not in stored_filename
        assert '"' not in stored_filename


# ============================================================
# Sanitize Filename Tests
# ============================================================


class TestSanitizeFilename:
    """Test _sanitize_filename method."""

    def test_sanitize_filename_removes_dangerous_chars(self, file_storage_service):
        """Test that dangerous characters are removed."""
        result = file_storage_service._sanitize_filename('file<>:"/\\|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "/" not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_sanitize_filename_removes_leading_trailing_dots(
        self, file_storage_service
    ):
        """Test that leading/trailing dots are removed."""
        result = file_storage_service._sanitize_filename("...file.txt...")
        assert not result.startswith(".")
        assert not result.endswith(".")

    def test_sanitize_filename_empty_returns_default(self, file_storage_service):
        """Test that empty filename returns default name."""
        result = file_storage_service._sanitize_filename("")
        assert result == "unnamed_file"

    def test_sanitize_filename_limits_length(self, file_storage_service):
        """Test that very long filenames are limited."""
        long_name = "a" * 300 + ".txt"
        result = file_storage_service._sanitize_filename(long_name)
        assert len(result) <= 200

    def test_sanitize_filename_preserves_extension_on_truncation(
        self, file_storage_service
    ):
        """Test that extension is preserved when truncating."""
        long_name = "a" * 300 + ".pdf"
        result = file_storage_service._sanitize_filename(long_name)
        assert result.endswith(".pdf")


# ============================================================
# Path Validation Tests
# ============================================================


class TestValidatePath:
    """Test _validate_path method."""

    def test_validate_path_rejects_empty_path(self, file_storage_service):
        """Test that empty path raises ValidationException."""
        with pytest.raises(ValidationException):
            file_storage_service._validate_path("")

    def test_validate_path_rejects_path_traversal(self, file_storage_service):
        """Test that path traversal raises ValidationException."""
        with pytest.raises(ValidationException):
            file_storage_service._validate_path("../etc/passwd")

        with pytest.raises(ValidationException):
            file_storage_service._validate_path("foo/../../bar")

    def test_validate_path_rejects_absolute_path(self, file_storage_service):
        """Test that absolute path raises ValidationException."""
        with pytest.raises(ValidationException):
            file_storage_service._validate_path("/etc/passwd")


# ============================================================
# Storage Stats Tests
# ============================================================


class TestGetStorageStats:
    """Test get_storage_stats method."""

    @pytest.mark.asyncio
    async def test_get_storage_stats_counts_files(
        self, file_storage_service, sample_file_data
    ):
        """Test that storage stats count files correctly."""
        # Store a few files
        for i in range(3):
            await file_storage_service.store_file(
                file_data=sample_file_data,
                original_filename=f"file{i}.txt",
                organization_id="org_123",
                case_id="case_456",
                mime_type="text/plain",
            )

        stats = await file_storage_service.get_storage_stats()

        assert stats["total_files"] == 3
        assert stats["total_size_bytes"] == len(sample_file_data) * 3


# ============================================================
# Health Check Tests
# ============================================================


class TestHealthCheck:
    """Test health_check method."""

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy(self, file_storage_service):
        """Test that health check returns healthy status."""
        health = await file_storage_service.health_check()

        assert health["status"] == "healthy"
        assert health["storage_accessible"] is True


# ============================================================
# Edge Cases and Error Handling
# ============================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_store_file_with_special_chars_in_org_id(
        self, file_storage_service, sample_file_data
    ):
        """Test storing file with special characters in organization ID."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="test.txt",
            organization_id="org/with/slashes",
            case_id="case_456",
            mime_type="text/plain",
        )

        # Should sanitize the org_id in path
        assert "/" not in result["file_path"].split(os.sep)[0]

    @pytest.mark.asyncio
    async def test_store_file_with_unicode_filename(
        self, file_storage_service, sample_file_data
    ):
        """Test storing file with Unicode filename."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="日本語ファイル.txt",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        assert result is not None
        assert "file_path" in result

    @pytest.mark.asyncio
    async def test_store_and_retrieve_empty_extension(
        self, file_storage_service, sample_file_data
    ):
        """Test storing file without extension."""
        result = await file_storage_service.store_file(
            file_data=sample_file_data,
            original_filename="README",
            organization_id="org_123",
            case_id="case_456",
            mime_type="text/plain",
        )

        data = await file_storage_service.retrieve_file(result["file_path"])
        assert data == sample_file_data

    @pytest.mark.asyncio
    async def test_multiple_stores_same_filename(
        self, file_storage_service, sample_file_data
    ):
        """Test that multiple stores of same filename create unique files."""
        paths = []
        for _ in range(3):
            result = await file_storage_service.store_file(
                file_data=sample_file_data,
                original_filename="same_name.txt",
                organization_id="org_123",
                case_id="case_456",
                mime_type="text/plain",
            )
            paths.append(result["file_path"])

        # All paths should be unique
        assert len(set(paths)) == 3
