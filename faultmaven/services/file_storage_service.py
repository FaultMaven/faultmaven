"""File Storage Service Module (TASK-013)

Purpose: Low-level file storage operations for evidence artifacts.

This service handles actual file I/O with:
- Local filesystem storage (initial implementation)
- File path generation (organized by org/case/date)
- File validation (size, type, malware scanning placeholder)
- Future: S3/Azure/GCS support

Architecture:
    APIEvidenceArtifactService → FileStorageService → Filesystem

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import os
import re
import uuid
import aiofiles
import aiofiles.os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.services.base import BaseService
from faultmaven.exceptions import ValidationException, ServiceError


class FileStorageService(BaseService):
    """Service for file storage operations.

    Handles actual file I/O with:
    - Local filesystem storage (initial implementation)
    - File path generation (organized by org/case/date)
    - File validation (size, type, malware scanning placeholder)
    - Future: S3/Azure/GCS support

    Attributes:
        storage_root: Root directory for file storage
        max_file_size_bytes: Maximum file size allowed
        allowed_mime_types: Allowed MIME types (empty = allow all)
    """

    # Characters that are dangerous in filenames
    DANGEROUS_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    # Path traversal patterns
    PATH_TRAVERSAL_PATTERN = re.compile(r'\.\.[\\/]|[\\/]\.\.')

    def __init__(
        self,
        storage_root: str = "./data/evidence",
        max_file_size_bytes: int = 100 * 1024 * 1024,  # 100MB default
        allowed_mime_types: Optional[List[str]] = None,
    ):
        """Initialize file storage service.

        Args:
            storage_root: Root directory for file storage
            max_file_size_bytes: Maximum file size allowed
            allowed_mime_types: Allowed MIME types (None = allow all)
        """
        super().__init__("file_storage_service")
        self.storage_root = os.path.abspath(storage_root)
        self.max_file_size_bytes = max_file_size_bytes
        self.allowed_mime_types = allowed_mime_types or []

        self.log_operation(
            "initialized",
            storage_root=self.storage_root,
            max_file_size_bytes=max_file_size_bytes,
            allowed_mime_types_count=len(self.allowed_mime_types),
        )

    async def store_file(
        self,
        file_data: bytes,
        original_filename: str,
        organization_id: str,
        case_id: str,
        mime_type: str
    ) -> Dict[str, Any]:
        """Store file to filesystem.

        Generates path: {storage_root}/{org_id}/{case_id}/{date}/{uuid}_{filename}

        Args:
            file_data: Raw file bytes
            original_filename: Original filename from upload
            organization_id: Organization ID for path organization
            case_id: Case ID for path organization
            mime_type: File MIME type

        Returns:
            Dictionary with:
            - stored_filename: Filename on disk (with UUID prefix)
            - file_path: Relative path from storage_root
            - file_size: Size in bytes

        Raises:
            ValidationException: If file invalid (size, type)
            ServiceError: If storage fails
        """
        self.log_operation(
            "store_file",
            original_filename=original_filename,
            organization_id=organization_id,
            case_id=case_id,
            mime_type=mime_type,
            file_size=len(file_data),
        )

        try:
            # Validate file before storing
            self.validate_file(
                file_size=len(file_data),
                mime_type=mime_type,
                original_filename=original_filename,
            )

            # Generate storage path
            stored_filename, file_path = self._generate_storage_path(
                organization_id=organization_id,
                case_id=case_id,
                original_filename=original_filename,
            )

            # Full absolute path
            full_path = os.path.join(self.storage_root, file_path)
            directory = os.path.dirname(full_path)

            # Create directories if they don't exist
            await aiofiles.os.makedirs(directory, exist_ok=True)

            # Write file to disk
            async with aiofiles.open(full_path, 'wb') as f:
                await f.write(file_data)

            file_size = len(file_data)

            self.log_operation(
                "store_file_success",
                stored_filename=stored_filename,
                file_path=file_path,
                file_size=file_size,
            )

            return {
                "stored_filename": stored_filename,
                "file_path": file_path,
                "file_size": file_size,
            }

        except ValidationException:
            raise
        except OSError as e:
            self.log_error("store_file", e, original_filename=original_filename)
            raise ServiceError(f"Failed to store file: {e}")
        except Exception as e:
            self.log_error("store_file", e, original_filename=original_filename)
            raise ServiceError(f"Failed to store file: {e}")

    async def retrieve_file(
        self,
        file_path: str
    ) -> bytes:
        """Retrieve file from storage.

        Args:
            file_path: Relative path from storage_root

        Returns:
            Raw file bytes

        Raises:
            NotFoundError: If file doesn't exist
            ServiceError: If read fails
        """
        self.log_operation("retrieve_file", file_path=file_path)

        try:
            # Validate path doesn't contain traversal attacks
            self._validate_path(file_path)

            full_path = os.path.join(self.storage_root, file_path)

            # Check if file exists
            if not await aiofiles.os.path.exists(full_path):
                from faultmaven.exceptions import NotFoundError
                raise NotFoundError("File", file_path)

            # Read file
            async with aiofiles.open(full_path, 'rb') as f:
                data = await f.read()

            self.log_operation(
                "retrieve_file_success",
                file_path=file_path,
                file_size=len(data),
            )

            return data

        except Exception as e:
            if hasattr(e, 'resource_type'):  # NotFoundError
                raise
            self.log_error("retrieve_file", e, file_path=file_path)
            raise ServiceError(f"Failed to retrieve file: {e}")

    async def delete_file(
        self,
        file_path: str
    ) -> bool:
        """Delete file from storage.

        Args:
            file_path: Relative path from storage_root

        Returns:
            True if deleted, False if not found
        """
        self.log_operation("delete_file", file_path=file_path)

        try:
            # Validate path doesn't contain traversal attacks
            self._validate_path(file_path)

            full_path = os.path.join(self.storage_root, file_path)

            # Check if file exists
            if not await aiofiles.os.path.exists(full_path):
                self.log_operation("delete_file_not_found", file_path=file_path)
                return False

            # Delete file
            await aiofiles.os.remove(full_path)

            self.log_operation("delete_file_success", file_path=file_path)

            return True

        except Exception as e:
            self.log_error("delete_file", e, file_path=file_path)
            return False

    async def get_file_info(
        self,
        file_path: str
    ) -> Optional[Dict[str, Any]]:
        """Get file metadata without reading content.

        Args:
            file_path: Relative path from storage_root

        Returns:
            Dictionary with file_size, modified_time, etc., or None if not found
        """
        self.log_operation("get_file_info", file_path=file_path)

        try:
            # Validate path doesn't contain traversal attacks
            self._validate_path(file_path)

            full_path = os.path.join(self.storage_root, file_path)

            # Check if file exists
            if not await aiofiles.os.path.exists(full_path):
                return None

            # Get file stats
            stat = await aiofiles.os.stat(full_path)

            info = {
                "file_size": stat.st_size,
                "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                "created_time": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
                "file_path": file_path,
            }

            self.log_operation("get_file_info_success", file_path=file_path)

            return info

        except Exception as e:
            self.log_error("get_file_info", e, file_path=file_path)
            return None

    def validate_file(
        self,
        file_size: int,
        mime_type: str,
        original_filename: str
    ) -> None:
        """Validate file before storage.

        Args:
            file_size: File size in bytes
            mime_type: File MIME type
            original_filename: Original filename

        Raises:
            ValidationException: If file invalid
        """
        # Validate file size
        if file_size <= 0:
            raise ValidationException(
                "file_size: File size must be greater than 0",
                details={"file_size": file_size}
            )

        if file_size > self.max_file_size_bytes:
            max_mb = self.max_file_size_bytes / (1024 * 1024)
            file_mb = file_size / (1024 * 1024)
            raise ValidationException(
                f"file_size: File size ({file_mb:.2f}MB) exceeds maximum ({max_mb:.2f}MB)",
                details={
                    "file_size": file_size,
                    "max_file_size_bytes": self.max_file_size_bytes,
                }
            )

        # Validate MIME type (if restrictions configured)
        if self.allowed_mime_types and mime_type not in self.allowed_mime_types:
            raise ValidationException(
                f"mime_type: MIME type '{mime_type}' is not allowed",
                details={
                    "mime_type": mime_type,
                    "allowed_mime_types": self.allowed_mime_types,
                }
            )

        # Validate filename
        if not original_filename or not original_filename.strip():
            raise ValidationException(
                "original_filename: Filename is required",
                details={"original_filename": original_filename}
            )

        # Check for path traversal in filename
        if self.PATH_TRAVERSAL_PATTERN.search(original_filename):
            raise ValidationException(
                "original_filename: Filename contains invalid path traversal characters",
                details={"original_filename": original_filename}
            )

        # Check for dangerous characters
        if self.DANGEROUS_CHARS_PATTERN.search(original_filename):
            raise ValidationException(
                "original_filename: Filename contains invalid characters",
                details={"original_filename": original_filename}
            )

    def _generate_storage_path(
        self,
        organization_id: str,
        case_id: str,
        original_filename: str
    ) -> Tuple[str, str]:
        """Generate storage path and stored filename.

        Path format: {org_id}/{case_id}/{YYYY-MM-DD}/{uuid}_{filename}

        Args:
            organization_id: Organization ID
            case_id: Case ID
            original_filename: Original filename

        Returns:
            Tuple of (stored_filename, file_path)
        """
        # Sanitize filename
        safe_filename = self._sanitize_filename(original_filename)

        # Generate UUID prefix for uniqueness
        file_uuid = uuid.uuid4().hex[:12]

        # Create stored filename with UUID prefix
        stored_filename = f"{file_uuid}_{safe_filename}"

        # Generate date folder
        date_folder = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Sanitize organization_id and case_id
        safe_org_id = self._sanitize_path_component(organization_id)
        safe_case_id = self._sanitize_path_component(case_id)

        # Build relative path
        file_path = os.path.join(
            safe_org_id,
            safe_case_id,
            date_folder,
            stored_filename
        )

        return stored_filename, file_path

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe storage.

        Removes dangerous characters and limits length.

        Args:
            filename: Original filename

        Returns:
            Sanitized filename
        """
        # Remove path separators and dangerous characters
        safe = self.DANGEROUS_CHARS_PATTERN.sub('_', filename)

        # Remove leading/trailing dots and spaces
        safe = safe.strip('. ')

        # If empty after sanitization, use a default name
        if not safe:
            safe = "unnamed_file"

        # Limit filename length (preserve extension)
        max_length = 200
        if len(safe) > max_length:
            # Try to preserve extension
            if '.' in safe:
                name, ext = safe.rsplit('.', 1)
                ext = ext[:20]  # Limit extension length
                name = name[:max_length - len(ext) - 1]
                safe = f"{name}.{ext}"
            else:
                safe = safe[:max_length]

        return safe

    def _sanitize_path_component(self, component: str) -> str:
        """Sanitize a path component (org_id, case_id).

        Args:
            component: Path component to sanitize

        Returns:
            Sanitized component
        """
        # Remove path separators and dangerous characters
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f./]', '_', component)

        # Remove leading/trailing underscores
        safe = safe.strip('_')

        # If empty after sanitization, use a placeholder
        if not safe:
            safe = "unknown"

        return safe

    def _validate_path(self, file_path: str) -> None:
        """Validate file path for security.

        Args:
            file_path: Path to validate

        Raises:
            ValidationException: If path is invalid or contains traversal
        """
        if not file_path or not file_path.strip():
            raise ValidationException("file_path: Path is required")

        # Check for path traversal
        if self.PATH_TRAVERSAL_PATTERN.search(file_path):
            raise ValidationException(
                "file_path: Path contains invalid traversal sequences",
                details={"file_path": file_path}
            )

        # Ensure path doesn't start with / (absolute path)
        if file_path.startswith('/') or file_path.startswith('\\'):
            raise ValidationException(
                "file_path: Path must be relative, not absolute",
                details={"file_path": file_path}
            )

    async def ensure_storage_directory(self) -> bool:
        """Ensure storage root directory exists.

        Returns:
            True if directory exists or was created
        """
        try:
            await aiofiles.os.makedirs(self.storage_root, exist_ok=True)
            return True
        except Exception as e:
            self.log_error("ensure_storage_directory", e, storage_root=self.storage_root)
            return False

    async def get_storage_stats(self) -> Dict[str, Any]:
        """Get storage statistics.

        Returns:
            Dictionary with storage stats (total_files, total_size_bytes, etc.)
        """
        try:
            total_files = 0
            total_size_bytes = 0

            # Walk through storage directory
            for root, dirs, files in os.walk(self.storage_root):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        stat = os.stat(file_path)
                        total_files += 1
                        total_size_bytes += stat.st_size
                    except OSError:
                        pass  # Skip files we can't stat

            return {
                "storage_root": self.storage_root,
                "total_files": total_files,
                "total_size_bytes": total_size_bytes,
                "total_size_mb": total_size_bytes / (1024 * 1024),
                "max_file_size_bytes": self.max_file_size_bytes,
            }

        except Exception as e:
            self.log_error("get_storage_stats", e)
            return {
                "storage_root": self.storage_root,
                "total_files": 0,
                "total_size_bytes": 0,
                "error": str(e),
            }

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check for the file storage service.

        Returns:
            Dictionary containing health status information
        """
        try:
            # Check if storage directory is accessible
            await self.ensure_storage_directory()

            # Try to get stats
            stats = await self.get_storage_stats()

            return {
                "service": self.service_name,
                "status": "healthy",
                "storage_root": self.storage_root,
                "storage_accessible": True,
                "total_files": stats.get("total_files", 0),
                "total_size_mb": stats.get("total_size_mb", 0),
            }

        except Exception as e:
            return {
                "service": self.service_name,
                "status": "unhealthy",
                "storage_root": self.storage_root,
                "storage_accessible": False,
                "error": str(e),
            }
