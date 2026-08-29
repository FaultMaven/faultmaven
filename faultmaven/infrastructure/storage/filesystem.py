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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import aiofiles.os

from faultmaven.infrastructure.storage.base import (
    IFileStorageBackend,
    PresignedUrl,
    StorageType,
    StoredFile,
)
from faultmaven.utils.path_containment import PathEscape, resolve_within_root

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

        # No mkdir here. Construction happens lazily via get_storage_backend(),
        # which agent tools reach on a request path, and the storage root may
        # be a network mount where a synchronous mkdir blocks the event loop.
        # store_file creates directories asynchronously when it needs them;
        # every read path already tolerates a root that does not exist yet.
        logger.info(f"Filesystem storage initialized at {self.storage_root}")

    def _get_full_path(self, key: str) -> Path:
        """Resolve ``key`` to a path, refusing anything outside the root.

        Every filesystem operation in this backend goes through here first, and
        acts on the **resolved** path this returns — so containment is checked
        once, before the first ``makedirs``/``open``/``remove``, not after.

        This used to be a denylist (``".." in key or key.startswith("/")``).
        Two properties the allowlist has that it did not (#1235):

        - A substring check does not resolve symlinks. Given a symlink
          ``linked`` inside the root pointing anywhere outside it, the key
          ``linked/evidence.log`` contains no ``..`` and does not start with
          ``/`` — the denylist admitted it and the write landed outside the
          root (measured; see the tests). ``resolve()`` + ``is_relative_to``
          refuses it, because containment is about where a path *lands*, not
          what it is named.
        - It couples "safe" to the positive property "the resolved path is
          inside the root" rather than to the absence of two textual markers,
          which is the invariant that can actually be asserted.

        The rule is ``utils.path_containment.resolve_within_root``, shared with
        the runbook tree (#1213/#1215/#1225) so the two subsystems cannot drift
        apart. No known key reaches here from user input — ``storage_key`` is
        minted by ``FileStorageService._generate_storage_key`` — so this is
        defense in depth and cross-subsystem consistency, not a live traversal.

        **What it returns is the UNRESOLVED join, deliberately.** Containment
        is decided on the resolved path; the filesystem operation is then
        performed on ``storage_root / key`` itself. For a read or a write the
        two are equivalent — ``open`` follows the link to the same in-root
        target — but for ``delete_file`` they are not: unlinking the RESOLVED
        path removes a symlink's target and leaves the key dangling, so the
        object disappears from ``list_keys`` (``is_file()`` is False on a broken
        link) and becomes unreachable by both the orphan sweep and
        ``fm-wipe-deployment``. In-root symlinks are legal here, so that is a
        silent data-loss shape, not a corner case. Unlinking the key's own path
        is also what this backend did before the guard existed.

        **The raised message is REDACTED, deliberately.** ``PathEscape``'s
        message carries resolved absolute paths and its docstring says that must
        never reach a client — but this backend's exceptions do:
        ``FileStorageService.retrieve_file`` re-wraps them into a ``ServiceError``
        and ``read_file_tool`` puts that string in a ``ToolResult``, which enters
        the LLM context and the case transcript. So the detail is logged
        server-side and the exception names only the key, which the caller
        supplied and already has. The runbook tree keeps its detailed message
        because it translates at its service seams instead.

        Args:
            key: Storage key/path, relative to the storage root.

        Returns:
            ``storage_root / key`` — unresolved, and proven to resolve to a
            location strictly inside the storage root.

        Raises:
            PathEscape: the key resolves outside the storage root, resolves to
                the root itself, or cannot be resolved at all. Subclasses
                ``ValueError``, which is what this raised before. It is a plain
                ``PathEscape`` and never ``RunbookPathEscape`` — the knowledge
                scan catches that type in order to skip a file and continue, and
                must not do so for an unrelated subsystem's refusal.
        """
        candidate = self.storage_root / key
        try:
            resolve_within_root(
                candidate,
                root=self.storage_root,
                source=f"key={key!r}",
                subject="storage key",
                tree="storage",
            )
        except PathEscape as exc:
            logger.warning("Refusing storage key %r: %s", key, exc)
            raise PathEscape(f"Invalid storage key: {key!r}") from exc
        return candidate

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
        # Containment is checked HERE, before the makedirs below. A mkdir on an
        # escaped path materialises attacker-chosen directories outside the tree
        # whatever the write then does, and a containment check anchored on the
        # directory just created is circular (#1215's round-1 defect, #1235).
        full_path = self._get_full_path(key)

        # Create parent directories. Async, not Path.mkdir: the storage root
        # may be a network mount (an RWX volume shared between replicas), where
        # a synchronous mkdir is a blocking round-trip on the event loop.
        await aiofiles.os.makedirs(str(full_path.parent), exist_ok=True)

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

        def _walk() -> List[str]:
            # The existence check belongs in here too: on a network mount even
            # a stat is a blocking round-trip.
            if not self.storage_root.exists():
                return []

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
