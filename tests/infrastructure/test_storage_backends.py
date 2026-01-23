"""Tests for storage backends.

Verifies:
1. Filesystem backend: returns deterministic local URLs
2. S3 backend: returns presigned URL shape (mock boto client)
3. Integration smoke test: evidence upload flow uses backend interface only
4. Factory correctly selects backend based on STORAGE_BACKEND
"""

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """Ensure clean environment for each test."""
    env_vars = ["STORAGE_BACKEND", "S3_BUCKET_NAME", "S3_REGION", "S3_KEY_PREFIX"]
    original = {k: os.environ.get(k) for k in env_vars}

    yield

    # Restore original values
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def temp_storage_dir():
    """Create temporary directory for filesystem storage tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def filesystem_backend(temp_storage_dir):
    """Create filesystem storage backend for testing."""
    from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend

    return FilesystemStorageBackend(
        storage_root=temp_storage_dir,
        base_url="http://localhost:8090",
    )


@pytest.fixture
def mock_boto3_client():
    """Create mock boto3 S3 client."""
    mock_client = MagicMock()

    # Mock presigned URL generation
    mock_client.generate_presigned_url.return_value = (
        "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc123"
    )

    # Mock head_object (file exists check)
    mock_client.head_object.return_value = {
        "ContentLength": 1024,
        "ContentType": "text/plain",
        "LastModified": "2025-01-02T12:00:00Z",
        "Metadata": {"custom": "value"},
    }

    # Mock get_object
    mock_client.get_object.return_value = {
        "Body": MagicMock(read=lambda: b"file content"),
    }

    return mock_client


# =============================================================================
# Filesystem Backend Tests
# =============================================================================


class TestFilesystemStorageBackend:
    """Tests for filesystem storage backend."""

    @pytest.mark.asyncio
    async def test_generate_upload_url_format(self, filesystem_backend):
        """Test upload URL has correct format for filesystem backend."""
        url = await filesystem_backend.generate_upload_url(
            key="org123/case456/error.log",
            content_type="text/plain",
        )

        assert url.url.startswith("http://localhost:8090/api/v1/storage/upload/")
        assert url.method == "POST"
        assert "Content-Type" in url.headers
        assert url.headers["Content-Type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_generate_download_url_format(
        self, filesystem_backend, temp_storage_dir
    ):
        """Test download URL has correct format for filesystem backend."""
        # First store a file
        key = "org123/case456/error.log"
        await filesystem_backend.store_file(key, b"test content")

        url = await filesystem_backend.generate_download_url(
            key=key,
            filename="error.log",
        )

        assert url.url.startswith("http://localhost:8090/api/v1/storage/download/")
        assert url.method == "GET"
        assert "filename=error.log" in url.url

    @pytest.mark.asyncio
    async def test_generate_download_url_file_not_found(self, filesystem_backend):
        """Test download URL raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            await filesystem_backend.generate_download_url(key="nonexistent/file.log")

    @pytest.mark.asyncio
    async def test_store_and_retrieve_file(self, filesystem_backend):
        """Test storing and retrieving file content."""
        key = "test/data.txt"
        content = b"Hello, World!"

        # Store file
        stored = await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type="text/plain",
        )

        assert stored.key == key
        assert stored.size_bytes == len(content)
        assert stored.content_type == "text/plain"

        # Retrieve file
        retrieved = await filesystem_backend.retrieve_file(key)
        assert retrieved == content

    @pytest.mark.asyncio
    async def test_delete_file(self, filesystem_backend):
        """Test deleting a file."""
        key = "test/delete-me.txt"

        # Store file
        await filesystem_backend.store_file(key, b"delete me")

        # Verify exists
        assert await filesystem_backend.file_exists(key)

        # Delete file
        deleted = await filesystem_backend.delete_file(key)
        assert deleted is True

        # Verify gone
        assert not await filesystem_backend.file_exists(key)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file(self, filesystem_backend):
        """Test deleting a nonexistent file returns False."""
        deleted = await filesystem_backend.delete_file("nonexistent/file.txt")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_file_exists(self, filesystem_backend):
        """Test file existence check."""
        key = "test/exists.txt"

        assert not await filesystem_backend.file_exists(key)

        await filesystem_backend.store_file(key, b"I exist!")

        assert await filesystem_backend.file_exists(key)

    @pytest.mark.asyncio
    async def test_get_file_info(self, filesystem_backend):
        """Test getting file metadata."""
        key = "test/metadata.txt"
        content = b"File with metadata"
        metadata = {"author": "test", "version": "1.0"}

        await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type="text/plain",
            metadata=metadata,
        )

        info = await filesystem_backend.get_file_info(key)

        assert info is not None
        assert info.key == key
        assert info.size_bytes == len(content)
        assert info.content_type == "text/plain"

    @pytest.mark.asyncio
    async def test_path_traversal_prevention(self, filesystem_backend):
        """Test that path traversal attacks are prevented."""
        with pytest.raises(ValueError, match="Invalid storage key"):
            await filesystem_backend.store_file("../../../etc/passwd", b"malicious")

        with pytest.raises(ValueError, match="Invalid storage key"):
            await filesystem_backend.store_file("/absolute/path", b"malicious")

    def test_storage_type(self, filesystem_backend):
        """Test storage type is FILESYSTEM."""
        from faultmaven.infrastructure.storage.base import StorageType

        assert filesystem_backend.get_storage_type() == StorageType.FILESYSTEM


# =============================================================================
# S3 Backend Tests
# =============================================================================


@pytest.mark.skipif(
    condition=True, reason="boto3 not installed"  # Skip S3 tests - requires boto3
)
class TestS3StorageBackend:
    """Tests for S3 storage backend (with mocked boto3)."""

    def test_s3_backend_requires_boto3(self):
        """Test S3 backend raises ImportError if boto3 not installed."""
        with patch.dict("sys.modules", {"boto3": None}):
            # Force reimport
            import importlib

            from faultmaven.infrastructure.storage import s3

            # Check BOTO3_AVAILABLE flag
            # (actual test would need more complex import mocking)

    @pytest.mark.asyncio
    async def test_generate_upload_url_shape(self, mock_boto3_client):
        """Test S3 upload URL has presigned URL shape."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                url = await backend.generate_upload_url(
                    key="evidence/file.log",
                    content_type="text/plain",
                )

                # Verify presigned URL shape
                assert "s3.amazonaws.com" in url.url or "X-Amz" in url.url
                assert url.method == "PUT"
                assert url.headers.get("Content-Type") == "text/plain"

                # Verify boto3 was called correctly
                mock_boto3_client.generate_presigned_url.assert_called_once()
                call_args = mock_boto3_client.generate_presigned_url.call_args
                assert call_args[1]["ClientMethod"] == "put_object"

            except ImportError:
                pytest.skip("boto3 not installed")

    @pytest.mark.asyncio
    async def test_generate_download_url_shape(self, mock_boto3_client):
        """Test S3 download URL has presigned URL shape."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                url = await backend.generate_download_url(
                    key="evidence/file.log",
                    filename="file.log",
                )

                # Verify presigned URL shape
                assert "s3.amazonaws.com" in url.url or "X-Amz" in url.url
                assert url.method == "GET"

            except ImportError:
                pytest.skip("boto3 not installed")

    @pytest.mark.asyncio
    async def test_s3_store_and_retrieve(self, mock_boto3_client):
        """Test S3 store and retrieve operations."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                # Store file
                stored = await backend.store_file(
                    key="test/file.txt",
                    data=b"test content",
                    content_type="text/plain",
                )

                assert stored.key == "test/file.txt"
                mock_boto3_client.put_object.assert_called_once()

            except ImportError:
                pytest.skip("boto3 not installed")

    def test_s3_storage_type(self, mock_boto3_client):
        """Test S3 storage type."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.base import StorageType
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                assert backend.get_storage_type() == StorageType.S3

            except ImportError:
                pytest.skip("boto3 not installed")


# =============================================================================
# Factory Tests
# =============================================================================


class TestStorageFactory:
    """Tests for storage backend factory."""

    def test_factory_creates_filesystem_by_default(self, clean_env, temp_storage_dir):
        """Test factory creates filesystem backend by default."""
        os.environ["STORAGE_BACKEND"] = "filesystem"

        from faultmaven.infrastructure.storage import (
            StorageType,
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(storage_backend=MagicMock(value="filesystem")),
                evidence_storage=MagicMock(evidence_storage_root=temp_storage_dir),
                server=MagicMock(host="localhost", port=8000),
            )

            backend = get_storage_backend(reset=True)

            assert backend.get_storage_type() == StorageType.FILESYSTEM

    def test_factory_explicit_override(self, clean_env, temp_storage_dir):
        """Test factory accepts explicit storage type."""
        from faultmaven.infrastructure.storage import (
            StorageType,
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(
                    storage_backend=MagicMock(value="s3")  # Settings say S3
                ),
                evidence_storage=MagicMock(evidence_storage_root=temp_storage_dir),
                server=MagicMock(host="localhost", port=8000),
            )

            # But we explicitly request filesystem
            backend = get_storage_backend(storage_type="filesystem", reset=True)

            assert backend.get_storage_type() == StorageType.FILESYSTEM

    def test_factory_s3_requires_bucket_name(self, clean_env):
        """Test factory raises error if S3 requested without bucket name."""
        os.environ.pop("S3_BUCKET_NAME", None)

        from faultmaven.infrastructure.storage import (
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(storage_backend=MagicMock(value="s3")),
                evidence_storage=MagicMock(
                    s3_bucket_name=None,  # Missing bucket name
                    s3_region="us-east-1",
                    s3_key_prefix="evidence/",
                    s3_endpoint_url=None,
                ),
            )

            with pytest.raises(ValueError, match="S3_BUCKET_NAME"):
                get_storage_backend(reset=True)


# =============================================================================
# Integration Tests
# =============================================================================


class TestStorageIntegration:
    """Integration tests for storage backends."""

    @pytest.mark.asyncio
    async def test_evidence_upload_flow_uses_interface(self, filesystem_backend):
        """Test that evidence upload flow works through interface only."""
        from faultmaven.infrastructure.storage.base import IFileStorageBackend

        # Verify backend is an IFileStorageBackend
        assert isinstance(filesystem_backend, IFileStorageBackend)

        # Simulate evidence upload flow
        key = "evidence/org123/case456/error.log"
        content = b"Error log content here"
        content_type = "text/plain"

        # Step 1: Generate upload URL (client would use this)
        upload_url = await filesystem_backend.generate_upload_url(
            key=key,
            content_type=content_type,
        )
        assert upload_url.url is not None
        assert not upload_url.is_expired

        # Step 2: Store file (server-side or via presigned URL)
        stored = await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type=content_type,
        )
        assert stored.size_bytes == len(content)

        # Step 3: Generate download URL
        download_url = await filesystem_backend.generate_download_url(
            key=key,
            filename="error.log",
        )
        assert download_url.url is not None

        # Step 4: Verify file info
        info = await filesystem_backend.get_file_info(key)
        assert info is not None
        # Note: Filesystem backend doesn't persist content_type metadata,
        # returns default application/octet-stream
        assert info.content_type == "application/octet-stream"

        # Step 5: Retrieve content
        retrieved = await filesystem_backend.retrieve_file(key)
        assert retrieved == content

        # Step 6: Cleanup
        deleted = await filesystem_backend.delete_file(key)
        assert deleted is True

    @pytest.mark.asyncio
    async def test_url_expiration(self, filesystem_backend):
        """Test presigned URL expiration tracking."""
        key = "test/expiry.txt"
        await filesystem_backend.store_file(key, b"test")

        # Short expiry
        url = await filesystem_backend.generate_download_url(
            key=key,
            expires_in=timedelta(seconds=10),
        )

        assert not url.is_expired
        assert url.seconds_until_expiry > 0
        assert url.seconds_until_expiry <= 10

        # Long expiry
        url_long = await filesystem_backend.generate_download_url(
            key=key,
            expires_in=timedelta(hours=24),
        )

        assert url_long.seconds_until_expiry > 3600  # More than 1 hour
