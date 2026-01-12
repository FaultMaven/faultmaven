"""File storage backend interface with presigned URL support.

This module defines the interface for file storage backends that support
both direct file operations and presigned URL generation for upload/download.

Design Goals:
- Storage neutrality: Same interface for filesystem and S3
- Presigned URLs: Enable direct client-to-storage transfers
- Security: Configurable URL expiration and access controls

Usage:
    from faultmaven.infrastructure.storage import get_storage_backend

    backend = get_storage_backend()
    upload_url = await backend.generate_upload_url("evidence/file.log")
    download_url = await backend.generate_download_url("evidence/file.log")
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional
import logging


logger = logging.getLogger(__name__)


class StorageType(str, Enum):
    """Storage backend type."""
    FILESYSTEM = "filesystem"
    S3 = "s3"


@dataclass
class PresignedUrl:
    """Presigned URL with metadata.

    Attributes:
        url: The presigned URL for upload/download
        expires_at: When the URL expires
        method: HTTP method (PUT for upload, GET for download)
        headers: Optional headers required for the request
    """
    url: str
    expires_at: datetime
    method: str
    headers: Optional[Dict[str, str]] = None

    @property
    def is_expired(self) -> bool:
        """Check if the URL has expired."""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def seconds_until_expiry(self) -> int:
        """Seconds until URL expires (0 if already expired)."""
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds()))


@dataclass
class StoredFile:
    """Metadata for a stored file.

    Attributes:
        key: Storage key/path
        size_bytes: File size in bytes
        content_type: MIME type
        created_at: When the file was stored
        metadata: Additional file metadata
    """
    key: str
    size_bytes: int
    content_type: str
    created_at: datetime
    metadata: Optional[Dict[str, Any]] = None


class IFileStorageBackend(ABC):
    """Interface for file storage backends with presigned URL support.

    This interface enables storage-neutral file operations. Implementations
    must support both direct file operations and presigned URL generation.

    Presigned URLs allow clients to upload/download directly to/from storage
    without proxying through the application server, reducing bandwidth and
    improving performance for large files.

    Implementations:
        - FilesystemStorageBackend: Local filesystem with API-based URLs
        - S3StorageBackend: AWS S3 with native presigned URLs
    """

    @abstractmethod
    async def generate_upload_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expires_in: timedelta = timedelta(hours=1),
        metadata: Optional[Dict[str, str]] = None,
    ) -> PresignedUrl:
        """Generate a presigned URL for file upload.

        Args:
            key: Storage key/path for the file
            content_type: Expected MIME type of the upload
            expires_in: URL validity duration (default: 1 hour)
            metadata: Optional metadata to attach to the file

        Returns:
            PresignedUrl with PUT method for uploading

        Example:
            url = await backend.generate_upload_url(
                key="evidence/org123/case456/error.log",
                content_type="text/plain",
                expires_in=timedelta(minutes=15),
            )
            # Client uses url.url with PUT request
        """
        pass

    @abstractmethod
    async def generate_download_url(
        self,
        key: str,
        expires_in: timedelta = timedelta(hours=1),
        filename: Optional[str] = None,
    ) -> PresignedUrl:
        """Generate a presigned URL for file download.

        Args:
            key: Storage key/path for the file
            expires_in: URL validity duration (default: 1 hour)
            filename: Optional filename for Content-Disposition header

        Returns:
            PresignedUrl with GET method for downloading

        Raises:
            FileNotFoundError: If the file doesn't exist

        Example:
            url = await backend.generate_download_url(
                key="evidence/org123/case456/error.log",
                filename="error.log",
            )
            # Client uses url.url with GET request
        """
        pass

    @abstractmethod
    async def store_file(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> StoredFile:
        """Store a file directly (for server-side operations).

        Args:
            key: Storage key/path for the file
            data: File content as bytes
            content_type: MIME type of the file
            metadata: Optional metadata to attach

        Returns:
            StoredFile with file metadata

        Example:
            stored = await backend.store_file(
                key="evidence/org123/case456/error.log",
                data=file_bytes,
                content_type="text/plain",
            )
        """
        pass

    @abstractmethod
    async def retrieve_file(self, key: str) -> Optional[bytes]:
        """Retrieve file content directly (for server-side operations).

        Args:
            key: Storage key/path for the file

        Returns:
            File content as bytes, or None if not found
        """
        pass

    @abstractmethod
    async def delete_file(self, key: str) -> bool:
        """Delete a file.

        Args:
            key: Storage key/path for the file

        Returns:
            True if file was deleted, False if not found
        """
        pass

    @abstractmethod
    async def file_exists(self, key: str) -> bool:
        """Check if a file exists.

        Args:
            key: Storage key/path for the file

        Returns:
            True if file exists, False otherwise
        """
        pass

    @abstractmethod
    async def get_file_info(self, key: str) -> Optional[StoredFile]:
        """Get file metadata without downloading content.

        Args:
            key: Storage key/path for the file

        Returns:
            StoredFile with metadata, or None if not found
        """
        pass

    @abstractmethod
    def get_storage_type(self) -> StorageType:
        """Get the storage backend type.

        Returns:
            StorageType enum value
        """
        pass
