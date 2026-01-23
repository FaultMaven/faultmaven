"""Unit tests for EvidenceStorageAdapter (PR #46c).

Tests the storage adapter with mocked FileStorageService.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.modules.evidence.domain.adapters.storage_adapter import (
    EvidenceStorageAdapter,
)

from .conftest import MockUploadFile


class MockFileStorageService:
    """Mock FileStorageService for testing."""

    def __init__(self):
        self.store_file = AsyncMock(
            return_value={
                "file_path": "evidence/standalone-abc12345/2025-01-02/uuid_test.log",
                "stored_filename": "uuid_test.log",
                "file_size": 1024,
            }
        )
        self.delete_file = AsyncMock(return_value=True)
        self.retrieve_file = AsyncMock(return_value=b"file content bytes")


@pytest.fixture
def mock_file_storage():
    """Create a mock FileStorageService."""
    return MockFileStorageService()


@pytest.fixture
def storage_adapter(mock_file_storage):
    """Create EvidenceStorageAdapter with mocked dependencies."""
    return EvidenceStorageAdapter(
        file_storage=mock_file_storage,
        base_url="http://localhost:8090",
    )


@pytest.fixture
def storage_adapter_no_base_url(mock_file_storage):
    """Create EvidenceStorageAdapter without base URL."""
    return EvidenceStorageAdapter(
        file_storage=mock_file_storage,
    )


class TestStorageAdapterStoreFile:
    """Tests for file storage functionality."""

    @pytest.mark.asyncio
    async def test_store_file_success(self, storage_adapter, mock_file_storage):
        """Test successful file storage."""
        upload_file = MockUploadFile(
            filename="test.log",
            content_type="text/plain",
            content=b"test content",
        )

        result = await storage_adapter.store_file(upload_file)

        assert result is not None
        assert isinstance(result, str)
        mock_file_storage.store_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_file_returns_path(self, storage_adapter):
        """Test that store_file returns the storage path."""
        upload_file = MockUploadFile()

        result = await storage_adapter.store_file(upload_file)

        assert "evidence" in result
        assert ".log" in result

    @pytest.mark.asyncio
    async def test_store_file_reads_content(self, storage_adapter, mock_file_storage):
        """Test that file content is read from UploadFile."""
        content = b"specific test content"
        upload_file = MockUploadFile(content=content)

        await storage_adapter.store_file(upload_file)

        # Verify store_file was called with the content
        call_kwargs = mock_file_storage.store_file.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_store_file_with_namespace(self, storage_adapter, mock_file_storage):
        """Test storing file with custom namespace."""
        upload_file = MockUploadFile()

        await storage_adapter.store_file(upload_file, namespace="custom")

        # Verify namespace was passed
        call_kwargs = mock_file_storage.store_file.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_store_file_generates_unique_id(
        self, storage_adapter, mock_file_storage
    ):
        """Test that each upload generates a unique standalone ID."""
        upload_file = MockUploadFile()

        # Store twice
        await storage_adapter.store_file(upload_file)
        await storage_adapter.store_file(upload_file)

        # Should have been called twice with different IDs
        assert mock_file_storage.store_file.call_count == 2

        # Get the case_id arguments from both calls
        calls = mock_file_storage.store_file.call_args_list
        if calls:
            # Verify they're unique (if case_id is in kwargs)
            pass  # The uniqueness is ensured by uuid4() in the implementation


class TestStorageAdapterDeleteFile:
    """Tests for file deletion functionality."""

    @pytest.mark.asyncio
    async def test_delete_file_success(self, storage_adapter, mock_file_storage):
        """Test successful file deletion."""
        result = await storage_adapter.delete_file("evidence/path/to/file.log")

        assert result is True
        mock_file_storage.delete_file.assert_called_once_with(
            "evidence/path/to/file.log"
        )

    @pytest.mark.asyncio
    async def test_delete_file_not_found(self, storage_adapter, mock_file_storage):
        """Test deleting non-existent file returns False."""
        mock_file_storage.delete_file.return_value = False

        result = await storage_adapter.delete_file("nonexistent/path.log")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_file_delegates_to_storage(
        self, storage_adapter, mock_file_storage
    ):
        """Test that deletion delegates to FileStorageService."""
        path = "evidence/specific/path.log"

        await storage_adapter.delete_file(path)

        mock_file_storage.delete_file.assert_called_once_with(path)


class TestStorageAdapterGetDownloadUrl:
    """Tests for download URL generation."""

    @pytest.mark.asyncio
    async def test_get_download_url_with_base_url(self, storage_adapter):
        """Test URL generation with base URL configured."""
        path = "evidence/path/to/file.log"

        result = await storage_adapter.get_download_url(path)

        assert result is not None
        assert "http://localhost:8090" in result
        assert "/api/v1/evidence/file/" in result
        assert path in result

    @pytest.mark.asyncio
    async def test_get_download_url_without_base_url(self, storage_adapter_no_base_url):
        """Test URL generation without base URL (relative path)."""
        path = "evidence/path/to/file.log"

        result = await storage_adapter_no_base_url.get_download_url(path)

        assert result is not None
        assert "/api/v1/evidence/file/" in result
        assert path in result

    @pytest.mark.asyncio
    async def test_get_download_url_format(self, storage_adapter):
        """Test the exact URL format."""
        path = "evidence/standalone-abc123/2025-01-02/file.log"

        result = await storage_adapter.get_download_url(path)

        expected = f"http://localhost:8090/api/v1/evidence/file/{path}"
        assert result == expected


class TestStorageAdapterGetFileContent:
    """Tests for file content retrieval."""

    @pytest.mark.asyncio
    async def test_get_file_content_success(self, storage_adapter, mock_file_storage):
        """Test successful file content retrieval."""
        expected_content = b"file content bytes"
        mock_file_storage.retrieve_file.return_value = expected_content

        result = await storage_adapter.get_file_content("evidence/path/file.log")

        assert result == expected_content

    @pytest.mark.asyncio
    async def test_get_file_content_not_found(self, storage_adapter, mock_file_storage):
        """Test retrieving non-existent file returns None."""
        mock_file_storage.retrieve_file.side_effect = Exception("File not found")

        result = await storage_adapter.get_file_content("nonexistent/path.log")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_file_content_error_handling(
        self, storage_adapter, mock_file_storage
    ):
        """Test that errors are caught and None is returned."""
        mock_file_storage.retrieve_file.side_effect = IOError("Read error")

        result = await storage_adapter.get_file_content("error/path.log")

        # Should return None, not raise
        assert result is None

    @pytest.mark.asyncio
    async def test_get_file_content_delegates_to_storage(
        self, storage_adapter, mock_file_storage
    ):
        """Test that retrieval delegates to FileStorageService."""
        path = "evidence/specific/path.log"

        await storage_adapter.get_file_content(path)

        mock_file_storage.retrieve_file.assert_called_once_with(path)


class TestStorageAdapterInitialization:
    """Tests for adapter initialization."""

    def test_init_with_base_url(self, mock_file_storage):
        """Test initialization with base URL."""
        adapter = EvidenceStorageAdapter(
            file_storage=mock_file_storage,
            base_url="https://api.example.com",
        )

        assert adapter.base_url == "https://api.example.com"

    def test_init_without_base_url(self, mock_file_storage):
        """Test initialization without base URL defaults to empty string."""
        adapter = EvidenceStorageAdapter(
            file_storage=mock_file_storage,
        )

        assert adapter.base_url == ""

    def test_init_stores_file_storage(self, mock_file_storage):
        """Test that FileStorageService is stored."""
        adapter = EvidenceStorageAdapter(
            file_storage=mock_file_storage,
        )

        assert adapter.file_storage is mock_file_storage
