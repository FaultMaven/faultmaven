"""Evidence Storage Adapter

Wraps FileStorageService to provide the interface expected by EvidenceService.
Handles file upload, download, and deletion with proper path management.
"""

import logging
from typing import Optional
from uuid import uuid4

from fastapi import UploadFile

from faultmaven.services.file_storage_service import FileStorageService

logger = logging.getLogger(__name__)


class EvidenceStorageAdapter:
    """Adapter that wraps FileStorageService for EvidenceService interface.

    The EvidenceService expects:
    - store_file(file, namespace) -> storage_path
    - delete_file(storage_path) -> bool
    - get_download_url(storage_path) -> url

    FileStorageService provides:
    - store_file(bytes, filename, org_id, case_id, mime_type) -> dict
    - delete_file(path) -> bool
    - retrieve_file(path) -> bytes
    """

    def __init__(
        self,
        file_storage: FileStorageService,
        base_url: Optional[str] = None,
    ):
        """Initialize storage adapter.

        Args:
            file_storage: Underlying FileStorageService instance
            base_url: Base URL for generating download URLs (e.g., "http://localhost:8000")
        """
        self.file_storage = file_storage
        self.base_url = base_url or ""

    async def store_file(
        self,
        file: UploadFile,
        namespace: str = "evidence",
    ) -> str:
        """Store uploaded file and return storage path.

        Args:
            file: FastAPI UploadFile object
            namespace: Storage namespace (used as organization folder)

        Returns:
            Relative storage path for the file
        """
        # Read file content
        content = await file.read()

        # Generate a unique case_id for standalone evidence uploads
        # This allows evidence to exist outside of case context
        standalone_id = f"standalone-{uuid4().hex[:8]}"

        # Store using FileStorageService
        result = await self.file_storage.store_file(
            file_data=content,
            original_filename=file.filename or "unnamed",
            organization_id=namespace,
            case_id=standalone_id,
            mime_type=file.content_type or "application/octet-stream",
        )

        logger.info(f"Evidence file stored: {file.filename} -> {result['file_path']}")

        return result["file_path"]

    async def delete_file(self, storage_path: str) -> bool:
        """Delete file from storage.

        Args:
            storage_path: Relative path returned from store_file

        Returns:
            True if deleted, False if not found
        """
        return await self.file_storage.delete_file(storage_path)

    async def get_download_url(self, storage_path: str) -> str:
        """Get download URL for evidence file.

        For local storage, returns an API endpoint URL.
        For cloud storage (future), would return pre-signed URL.

        Args:
            storage_path: Relative path returned from store_file

        Returns:
            URL for downloading the file
        """
        # For local storage, construct API download URL
        # The actual file serving is handled by the download endpoint
        # which retrieves the file and serves it
        if self.base_url:
            return f"{self.base_url}/api/v1/evidence/file/{storage_path}"

        # Relative URL if no base URL configured
        return f"/api/v1/evidence/file/{storage_path}"

    async def get_file_content(self, storage_path: str) -> Optional[bytes]:
        """Retrieve file content for download.

        Args:
            storage_path: Relative path to file

        Returns:
            File bytes or None if not found
        """
        try:
            return await self.file_storage.retrieve_file(storage_path)
        except Exception as e:
            logger.warning(f"Failed to retrieve file {storage_path}: {e}")
            return None
