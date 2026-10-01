"""File storage backend interface.

This module defines the interface for file storage backends: server-side
store, retrieve, delete, list and metadata operations.

Design Goals:
- Storage neutrality: Same interface for filesystem and S3

Usage:
    from faultmaven.infrastructure.storage import get_storage_backend

    backend = get_storage_backend()
    await backend.store_file("evidence/file.log", data, content_type="text/plain")
    data = await backend.retrieve_file("evidence/file.log")
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StorageType(str, Enum):
    """Storage backend type."""

    FILESYSTEM = "filesystem"
    S3 = "s3"


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
    """Interface for file storage backends.

    This interface enables storage-neutral, server-side file operations:
    store, retrieve, delete, list and metadata lookup. The application
    server performs every transfer itself.

    Implementations:
        - FilesystemStorageBackend: Local filesystem
        - S3StorageBackend: AWS S3
    """

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
    async def get_file_info(self, key: str) -> Optional[StoredFile]:
        """Get file metadata without downloading content.

        Args:
            key: Storage key/path for the file

        Returns:
            StoredFile with metadata, or None if not found
        """
        pass

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> List[str]:
        """List the keys of every stored object under a prefix.

        Required by sweep-style maintenance (orphan cleanup, storage stats),
        which cannot walk a local directory once the backend may be remote.

        Args:
            prefix: Only return keys starting with this string. Empty string
                lists everything the backend holds.

        Returns:
            Storage keys, in unspecified order. Empty list if nothing matches
            or the backing store does not exist yet.
        """
        pass

    @abstractmethod
    def get_storage_type(self) -> StorageType:
        """Get the storage backend type.

        Returns:
            StorageType enum value
        """
        pass
