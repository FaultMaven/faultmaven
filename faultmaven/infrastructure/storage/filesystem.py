"""Filesystem storage backend implementation.

Provides local filesystem storage with API-based URLs for upload/download.
For filesystem storage, "presigned URLs" are actually API endpoints that
handle the upload/download operations.

Usage:
    backend = FilesystemStorageBackend(
        storage_root="./data/evidence",
        base_url="http://localhost:8090",
    )
    url = await backend.generate_download_url("org123/case456/file.log")
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import aiofiles.os

from faultmaven.infrastructure.storage.base import (
    IFileStorageBackend,
    PresignedUrl,
    StorageType,
    StoredFile,
)

logger = logging.getLogger(__name__)


class FilesystemStorageBackend(IFileStorageBackend):
    """Filesystem-based storage backend.

    For filesystem storage, presigned URLs are API endpoints that handle
    the actual file operations. This allows the same interface to work
    for both local development and production S3 deployments.

    URL Format:
        - Upload: POST {base_url}/api/v1/storage/upload/{key}
        - Download: GET {base_url}/api/v1/storage/download/{key}

    Attributes:
        storage_root: Root directory for file storage
        base_url: Base URL for API endpoints (e.g., "http://localhost:8090")
    """

    def __init__(
        self,
        storage_root: str = "./data/storage",
        base_url: str = "http://localhost:8090",
    ):
        """Initialize filesystem storage backend.

        Args:
            storage_root: Root directory for file storage
            base_url: Base URL for generating API endpoint URLs
        """
        self.storage_root = Path(storage_root)
        self.base_url = base_url.rstrip("/")

        # Ensure storage root exists
        self.storage_root.mkdir(parents=True, exist_ok=True)
        logger.info(f"Filesystem storage initialized at {self.storage_root}")

    def _get_full_path(self, key: str) -> Path:
        """Get full filesystem path for a key.

        Args:
            key: Storage key/path

        Returns:
            Full filesystem path

        Raises:
            ValueError: If key contains path traversal
        """
        # Prevent path traversal attacks
        if ".." in key or key.startswith("/"):
            raise ValueError(f"Invalid storage key: {key}")

        return self.storage_root / key

    async def generate_upload_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expires_in: timedelta = timedelta(hours=1),
        metadata: Optional[Dict[str, str]] = None,
    ) -> PresignedUrl:
        """Generate an API endpoint URL for file upload.

        For filesystem storage, this returns an API endpoint that accepts
        the file upload via POST request.

        Args:
            key: Storage key/path for the file
            content_type: Expected MIME type
            expires_in: URL validity duration (honored by API, not URL itself)
            metadata: Optional metadata (stored with file)

        Returns:
            PresignedUrl with POST method for uploading
        """
        # Encode key for URL
        encoded_key = key.replace("/", "%2F")
        url = f"{self.base_url}/api/v1/storage/upload/{encoded_key}"

        expires_at = datetime.now(timezone.utc) + expires_in

        logger.debug(f"Generated upload URL for key={key}")

        return PresignedUrl(
            url=url,
            expires_at=expires_at,
            method="POST",
            headers={"Content-Type": content_type},
        )

    async def generate_download_url(
        self,
        key: str,
        expires_in: timedelta = timedelta(hours=1),
        filename: Optional[str] = None,
    ) -> PresignedUrl:
        """Generate an API endpoint URL for file download.

        For filesystem storage, this returns an API endpoint that serves
        the file via GET request.

        Args:
            key: Storage key/path for the file
            expires_in: URL validity duration (honored by API, not URL itself)
            filename: Optional filename for Content-Disposition

        Returns:
            PresignedUrl with GET method for downloading

        Raises:
            FileNotFoundError: If the file doesn't exist
        """
        full_path = self._get_full_path(key)

        if not await aiofiles.os.path.exists(str(full_path)):
            raise FileNotFoundError(f"File not found: {key}")

        # Encode key for URL
        encoded_key = key.replace("/", "%2F")
        url = f"{self.base_url}/api/v1/storage/download/{encoded_key}"

        if filename:
            url += f"?filename={filename}"

        expires_at = datetime.now(timezone.utc) + expires_in

        logger.debug(f"Generated download URL for key={key}")

        return PresignedUrl(
            url=url,
            expires_at=expires_at,
            method="GET",
        )

    async def store_file(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> StoredFile:
        """Store a file to the filesystem.

        Args:
            key: Storage key/path for the file
            data: File content as bytes
            content_type: MIME type of the file
            metadata: Optional metadata (stored as sidecar JSON)

        Returns:
            StoredFile with file metadata
        """
        full_path = self._get_full_path(key)

        # Create parent directories
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Write file
        async with aiofiles.open(full_path, "wb") as f:
            await f.write(data)

        # Store metadata as sidecar file if provided
        if metadata:
            import json

            metadata_path = full_path.with_suffix(full_path.suffix + ".meta")
            async with aiofiles.open(metadata_path, "w") as f:
                await f.write(
                    json.dumps(
                        {
                            "content_type": content_type,
                            "metadata": metadata,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                )

        logger.info(f"Stored file: {key} ({len(data)} bytes)")

        return StoredFile(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            created_at=datetime.now(timezone.utc),
            metadata=metadata,
        )

    async def retrieve_file(self, key: str) -> Optional[bytes]:
        """Retrieve file content from filesystem.

        Args:
            key: Storage key/path for the file

        Returns:
            File content as bytes, or None if not found
        """
        full_path = self._get_full_path(key)

        if not await aiofiles.os.path.exists(str(full_path)):
            return None

        async with aiofiles.open(full_path, "rb") as f:
            data = await f.read()

        logger.debug(f"Retrieved file: {key} ({len(data)} bytes)")
        return data

    async def delete_file(self, key: str) -> bool:
        """Delete a file from filesystem.

        Args:
            key: Storage key/path for the file

        Returns:
            True if file was deleted, False if not found
        """
        full_path = self._get_full_path(key)

        if not await aiofiles.os.path.exists(str(full_path)):
            return False

        await aiofiles.os.remove(str(full_path))

        # Also remove metadata sidecar if exists
        metadata_path = full_path.with_suffix(full_path.suffix + ".meta")
        if await aiofiles.os.path.exists(str(metadata_path)):
            await aiofiles.os.remove(str(metadata_path))

        logger.info(f"Deleted file: {key}")
        return True

    async def file_exists(self, key: str) -> bool:
        """Check if a file exists on filesystem.

        Args:
            key: Storage key/path for the file

        Returns:
            True if file exists, False otherwise
        """
        full_path = self._get_full_path(key)
        return await aiofiles.os.path.exists(str(full_path))

    async def get_file_info(self, key: str) -> Optional[StoredFile]:
        """Get file metadata without downloading content.

        Args:
            key: Storage key/path for the file

        Returns:
            StoredFile with metadata, or None if not found
        """
        full_path = self._get_full_path(key)

        if not await aiofiles.os.path.exists(str(full_path)):
            return None

        stat = await aiofiles.os.stat(str(full_path))

        # Try to load metadata from sidecar file
        content_type = "application/octet-stream"
        metadata = None
        created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)

        metadata_path = full_path.with_suffix(full_path.suffix + ".meta")
        if await aiofiles.os.path.exists(str(metadata_path)):
            import json

            async with aiofiles.open(metadata_path, "r") as f:
                meta_data = json.loads(await f.read())
                content_type = meta_data.get("content_type", content_type)
                metadata = meta_data.get("metadata")
                if "created_at" in meta_data:
                    created_at = datetime.fromisoformat(meta_data["created_at"])

        return StoredFile(
            key=key,
            size_bytes=stat.st_size,
            content_type=content_type,
            created_at=created_at,
            metadata=metadata,
        )

    async def list_keys(self, prefix: str = "") -> List[str]:
        """List stored keys under a prefix by walking the storage root.

        Args:
            prefix: Only return keys starting with this string.

        Returns:
            Storage keys relative to the storage root, POSIX-separated so
            they round-trip through the same form ``store_file`` accepted.
        """
        if not self.storage_root.exists():
            return []

        def _walk() -> List[str]:
            keys = []
            for path in self.storage_root.rglob("*"):
                if not path.is_file():
                    continue
                key = path.relative_to(self.storage_root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
            return keys

        # rglob over a large evidence tree is blocking I/O — keep it off the
        # event loop like every other sweep in this codebase.
        return await asyncio.to_thread(_walk)

    def get_storage_type(self) -> StorageType:
        """Get the storage backend type.

        Returns:
            StorageType.FILESYSTEM
        """
        return StorageType.FILESYSTEM
