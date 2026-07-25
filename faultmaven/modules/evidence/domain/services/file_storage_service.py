"""File Storage Service Module

Purpose: Evidence-domain file storage — validation, key naming, and the
orphan-tracking sidecar protocol.

This service owns the *domain* half of evidence storage and is deliberately
backend-agnostic: it holds no filesystem paths and performs no file I/O of its
own. Raw bytes cross into infrastructure through ``IFileStorageBackend``, which
``STORAGE_BACKEND`` resolves to a filesystem or S3 implementation.

Architecture:
    InvestigationService / agent tools
        → FileStorageService  (validation, key naming, sidecars)
        → IFileStorageBackend (bytes; filesystem or S3)

Design Reference: docs/architecture/data-and-storage/evidence-file-storage.md
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from faultmaven.exceptions import ServiceError, ValidationException
from faultmaven.services.base import BaseService

# Sidecar metadata suffix for orphan-file tracking
# (evidence-failure-modes.md).
# Stored as a companion object under `{key}.meta.json`; the orphan-cleanup job
# reads these to decide what's safe to delete. Being an ordinary backend object
# rather than a local file is what lets cleanup work on S3 too.
SIDECAR_SUFFIX = ".meta.json"

# Interface imports for clean architecture compliance
if TYPE_CHECKING:
    from faultmaven.infrastructure.storage.base import IFileStorageBackend


class FileStorageService(BaseService):
    """Service for evidence file storage operations.

    Owns the backend-independent concerns:
    - File validation (size, MIME type, filename safety)
    - Storage-key generation (organized by org/case/date)
    - The orphan-tracking sidecar protocol

    Byte I/O is delegated to the injected ``IFileStorageBackend``.

    Attributes:
        backend: The storage backend bytes are read from and written to
        max_file_size_bytes: Maximum file size allowed
        allowed_mime_types: Allowed MIME types (empty = allow all)
    """

    # Characters that are dangerous in filenames
    DANGEROUS_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    # Path traversal patterns
    PATH_TRAVERSAL_PATTERN = re.compile(r"\.\.[\\/]|[\\/]\.\.")

    def __init__(
        self,
        backend: Optional["IFileStorageBackend"] = None,
        max_file_size_bytes: int = 100 * 1024 * 1024,  # 100MB default
        allowed_mime_types: Optional[List[str]] = None,
    ):
        """Initialize file storage service.

        Args:
            backend: Storage backend to use. Defaults to the configured
                backend from ``get_storage_backend()`` — so every construction
                site honours ``STORAGE_BACKEND`` without having to plumb one
                through. Tests inject an explicit backend.
            max_file_size_bytes: Maximum file size allowed
            allowed_mime_types: Allowed MIME types (None = allow all)
        """
        super().__init__("file_storage_service")

        if backend is None:
            # Lazy import: keeps the evidence domain free of an import-time
            # dependency on the storage infrastructure package.
            from faultmaven.infrastructure.storage.factory import get_storage_backend

            backend = get_storage_backend()

        self.backend = backend
        self.max_file_size_bytes = max_file_size_bytes
        self.allowed_mime_types = allowed_mime_types or []

        self.log_operation(
            "initialized",
            storage_backend=self.backend.get_storage_type().value,
            max_file_size_bytes=max_file_size_bytes,
            allowed_mime_types_count=len(self.allowed_mime_types),
        )

    async def store_file(
        self,
        file_data: bytes,
        original_filename: str,
        organization_id: str,
        case_id: str,
        mime_type: str,
    ) -> Dict[str, Any]:
        """Store a file via the configured storage backend.

        Generates key: {organization_id}/{case_id}/{date}/{uuid}_{filename}

        Args:
            file_data: Raw file bytes
            original_filename: Original filename from upload
            organization_id: Organization ID for key organization
            case_id: Case ID for key organization
            mime_type: File MIME type

        Returns:
            Dictionary with:
            - stored_filename: Filename component (with UUID prefix)
            - storage_key: Backend key for the stored object
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

            # Generate storage key
            stored_filename, storage_key = self._generate_storage_key(
                organization_id=organization_id,
                case_id=case_id,
                original_filename=original_filename,
            )

            await self.backend.store_file(
                key=storage_key,
                data=file_data,
                content_type=mime_type,
            )

            # Write orphan-tracking sidecar beside the file.
            # Cleanup job reads these to decide what's safe to delete.
            await self._write_sidecar(
                storage_key=storage_key,
                case_id=case_id,
                organization_id=organization_id,
                linked=False,
            )

            file_size = len(file_data)

            self.log_operation(
                "store_file_success",
                stored_filename=stored_filename,
                storage_key=storage_key,
                file_size=file_size,
            )

            return {
                "stored_filename": stored_filename,
                "storage_key": storage_key,
                "file_size": file_size,
            }

        except ValidationException:
            raise
        except Exception as e:
            self.log_error("store_file", e, original_filename=original_filename)
            raise ServiceError(f"Failed to store file: {e}")

    async def _write_sidecar(
        self,
        *,
        storage_key: str,
        case_id: str,
        organization_id: str,
        linked: bool,
    ) -> None:
        """Write the sidecar metadata object beside a stored file.

        Sidecar format (stable schema — read by the orphan-cleanup job):
            {
                "case_id": "case_abc",
                "organization_id": "org_xyz",
                "uploaded_at": "2026-04-18T10:00:00+00:00",
                "linked": false,
                "schema_version": 1
            }

        Write failures are logged but non-fatal — the file itself stored
        successfully, and worst-case the cleanup job won't know this file
        is linked. Safer to have a file without a sidecar than to fail the
        upload.
        """
        sidecar_key = f"{storage_key}{SIDECAR_SUFFIX}"
        payload = {
            "case_id": case_id,
            "organization_id": organization_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "linked": linked,
            "schema_version": 1,
        }
        try:
            await self.backend.store_file(
                key=sidecar_key,
                data=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            self.log_error("write_sidecar", e, sidecar_key=sidecar_key)

    async def mark_linked(self, storage_key: str) -> bool:
        """Flip a stored file's sidecar `linked` flag to True.

        Called after Evidence is created referencing this file so the
        orphan-cleanup job knows not to delete it.

        Args:
            storage_key: Backend key (as returned by `store_file` in the
                `storage_key` field).

        Returns:
            True if the sidecar was successfully updated, False if no
            sidecar was found or the update failed. Non-fatal — callers
            shouldn't depend on the return value.
        """
        try:
            payload = await self.read_sidecar(storage_key)
            if payload is None:
                # No sidecar — file was stored before the sidecar protocol
                # landed, or the sidecar write failed at store time.
                # Either way: nothing to update. Not an error.
                self.log_operation("mark_linked_no_sidecar", storage_key=storage_key)
                return False

            if payload.get("linked") is True:
                return True  # already linked — idempotent

            payload["linked"] = True
            await self.backend.store_file(
                key=f"{storage_key}{SIDECAR_SUFFIX}",
                data=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )

            self.log_operation("mark_linked_success", storage_key=storage_key)
            return True
        except Exception as e:
            self.log_error("mark_linked", e, storage_key=storage_key)
            return False

    async def read_sidecar(self, storage_key: str) -> Optional[Dict[str, Any]]:
        """Read a stored file's sidecar metadata.

        Returns None only when the sidecar genuinely does not exist. A backend
        failure or corrupt payload RAISES.

        The distinction is load-bearing. The orphan-cleanup job deletes any
        file whose sidecar does not say ``linked=true``, so collapsing "the
        backend is erroring" into "no metadata" would let a transient S3 fault
        present a live, referenced evidence file as an unlinked orphan.
        Callers must be able to tell "this file has no metadata" from "I could
        not find out".

        Raises:
            ServiceError: If the sidecar exists but cannot be read or parsed
        """
        self._validate_key(storage_key)

        try:
            raw = await self.backend.retrieve_file(f"{storage_key}{SIDECAR_SUFFIX}")
        except Exception as e:
            self.log_error("read_sidecar", e, storage_key=storage_key)
            raise ServiceError(f"Failed to read sidecar: {e}")

        if raw is None:
            return None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self.log_error("read_sidecar", e, storage_key=storage_key)
            raise ServiceError(f"Corrupt sidecar for {storage_key}: {e}")

        # Valid JSON is not necessarily a sidecar. A bare list or scalar would
        # reach the sweep and blow up on .get(), aborting the entire run over
        # one bad object — so reject the shape here, where it is counted as a
        # single skipped file.
        if not isinstance(payload, dict):
            raise ServiceError(
                f"Corrupt sidecar for {storage_key}: expected an object, got "
                f"{type(payload).__name__}"
            )

        return payload

    async def list_sidecar_keys(self) -> List[str]:
        """List the storage keys of every stored file that has a sidecar.

        Returns file keys (sidecar suffix stripped), so callers can feed them
        straight back into `read_sidecar` / `delete_file`. Used by the
        orphan-cleanup job, which must enumerate candidates without assuming
        it can walk a local directory.
        """
        keys = await self.backend.list_keys()
        stored = set(keys)

        # A stripped base names a real file only if that file is actually
        # stored. Without this check, an object whose own key ends in
        # SIDECAR_SUFFIX yields a PHANTOM base, and the sweep then reads that
        # object's user-controlled content as the phantom's orphan metadata and
        # deletes the object as its companion.
        #
        # `_sanitize_filename` reserves the suffix so no NEW upload can create
        # that shape, but this is what covers objects already stored before
        # that reservation existed — the mangle is generation-time only and
        # cannot reach them. It is also what would have caught the truncation
        # ordering bug that briefly reopened the hole.
        return [
            base
            for k in keys
            if k.endswith(SIDECAR_SUFFIX)
            and (base := k[: -len(SIDECAR_SUFFIX)]) in stored
        ]

    async def retrieve_file(self, storage_key: str) -> bytes:
        """Retrieve a file from the configured storage backend.

        Args:
            storage_key: Backend key for the stored object

        Returns:
            Raw file bytes

        Raises:
            NotFoundError: If file doesn't exist
            ServiceError: If read fails
        """
        self.log_operation("retrieve_file", storage_key=storage_key)

        try:
            # Reject traversal / absolute keys before the backend sees them
            self._validate_key(storage_key)

            data = await self.backend.retrieve_file(storage_key)

            if data is None:
                from faultmaven.exceptions import NotFoundError

                raise NotFoundError("File", storage_key)

            self.log_operation(
                "retrieve_file_success",
                storage_key=storage_key,
                file_size=len(data),
            )

            return data

        except Exception as e:
            if hasattr(e, "resource_type"):  # NotFoundError
                raise
            self.log_error("retrieve_file", e, storage_key=storage_key)
            raise ServiceError(f"Failed to retrieve file: {e}")

    async def delete_file(self, storage_key: str) -> bool:
        """Delete a file and its sidecar from storage.

        The sidecar is deleted regardless of whether the file itself was
        still present: a sidecar outliving its file would otherwise be swept
        forever by the cleanup job, which reads it and finds nothing to do.

        A backend failure RAISES rather than returning False. The two outcomes
        are not interchangeable: the orphan-cleanup job counts a return value
        as reclaimed storage, so swallowing (say) an S3 AccessDenied here would
        report deletions that never happened and increment the deletion metric
        for files still sitting in the bucket.

        Args:
            storage_key: Backend key for the stored object

        Returns:
            True if the file was deleted, False if it was already gone

        Raises:
            ValidationException: If the key is invalid
            ServiceError: If the backend fails to delete
        """
        self.log_operation("delete_file", storage_key=storage_key)

        self._validate_key(storage_key)

        try:
            deleted = await self.backend.delete_file(storage_key)
            # The sidecar is what makes a file discoverable to the sweep, so
            # its removal is part of the delete, not a best-effort extra.
            await self.backend.delete_file(f"{storage_key}{SIDECAR_SUFFIX}")
        except Exception as e:
            self.log_error("delete_file", e, storage_key=storage_key)
            raise ServiceError(f"Failed to delete file: {e}")

        if not deleted:
            self.log_operation("delete_file_not_found", storage_key=storage_key)
            return False

        self.log_operation("delete_file_success", storage_key=storage_key)

        return True

    def validate_file(
        self, file_size: int, mime_type: str, original_filename: str
    ) -> None:
        """Validate file before storage.

        Args:
            file_size: File size in bytes
            mime_type: File MIME type
            original_filename: Original filename

        Raises:
            ValidationException: If file invalid
        """
        # Validate file size.
        # 0-byte files are accepted: emptiness is itself diagnostic information
        # (e.g. a log file confirmed to be empty). Downstream classification
        # routes empty content to the UNANALYZABLE path with a clear rationale,
        # so the pipeline degrades gracefully instead of rejecting at the API.
        if file_size < 0:
            raise ValidationException(
                "file_size: File size cannot be negative",
                details={"file_size": file_size},
            )

        if file_size > self.max_file_size_bytes:
            max_mb = self.max_file_size_bytes / (1024 * 1024)
            file_mb = file_size / (1024 * 1024)
            raise ValidationException(
                f"file_size: File size ({file_mb:.2f}MB) exceeds maximum ({max_mb:.2f}MB)",
                details={
                    "file_size": file_size,
                    "max_file_size_bytes": self.max_file_size_bytes,
                },
            )

        # Validate MIME type (if restrictions configured)
        if self.allowed_mime_types and mime_type not in self.allowed_mime_types:
            raise ValidationException(
                f"mime_type: MIME type '{mime_type}' is not allowed",
                details={
                    "mime_type": mime_type,
                    "allowed_mime_types": self.allowed_mime_types,
                },
            )

        # Validate filename
        if not original_filename or not original_filename.strip():
            raise ValidationException(
                "original_filename: Filename is required",
                details={"original_filename": original_filename},
            )

        # Check for path traversal in filename
        if self.PATH_TRAVERSAL_PATTERN.search(original_filename):
            raise ValidationException(
                "original_filename: Filename contains invalid path traversal characters",
                details={"original_filename": original_filename},
            )

        # Check for dangerous characters
        if self.DANGEROUS_CHARS_PATTERN.search(original_filename):
            raise ValidationException(
                "original_filename: Filename contains invalid characters",
                details={"original_filename": original_filename},
            )

    def _generate_storage_key(
        self, organization_id: str, case_id: str, original_filename: str
    ) -> Tuple[str, str]:
        """Generate the storage key and stored filename.

        Key format: {organization_id}/{case_id}/{YYYY-MM-DD}/{uuid}_{filename}

        Keys always use forward slashes: they are backend keys, not local
        paths, and S3 has no notion of an OS-specific separator.

        Args:
            organization_id: Organization ID
            case_id: Case ID
            original_filename: Original filename

        Returns:
            Tuple of (stored_filename, storage_key)
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

        # Build the backend key (always POSIX-separated)
        storage_key = "/".join(
            (safe_org_id, safe_case_id, date_folder, stored_filename)
        )

        return stored_filename, storage_key

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe storage.

        Removes dangerous characters and limits length.

        Args:
            filename: Original filename

        Returns:
            Sanitized filename
        """
        # Remove path separators and dangerous characters
        safe = self.DANGEROUS_CHARS_PATTERN.sub("_", filename)

        # Remove leading/trailing dots and spaces
        safe = safe.strip(". ")

        # If empty after sanitization, use a default name
        if not safe:
            safe = "unnamed_file"

        # Limit filename length (preserve extension)
        max_length = 200
        if len(safe) > max_length:
            # Try to preserve extension
            if "." in safe:
                name, ext = safe.rsplit(".", 1)
                ext = ext[:20]  # Limit extension length
                name = name[: max_length - len(ext) - 1]
                safe = f"{name}.{ext}"
            else:
                safe = safe[:max_length]

        # The sidecar protocol reserves names ending in SIDECAR_SUFFIX. An
        # upload actually named "notes.meta.json" would otherwise be
        # enumerated by list_sidecar_keys() as some other file's sidecar, its
        # user-controlled content parsed as orphan metadata, and the file
        # itself deleted as that phantom's companion.
        #
        # This MUST be the last step. Truncation above rebuilds the name from
        # `{name[:N]}.{ext}` and can reconstitute the very suffix a earlier
        # an earlier mangle removed — a >200-char name ending `.metaXXXX.json`
        # truncates
        # straight back to `.meta.json`. The substitution is length-preserving,
        # so applying it here cannot push the name back over the limit.
        if safe.lower().endswith(SIDECAR_SUFFIX):
            safe = f"{safe[: -len(SIDECAR_SUFFIX)]}.meta_json"

        return safe

    def _sanitize_path_component(self, component: str) -> str:
        """Sanitize a path component (organization_id, case_id).

        Args:
            component: Path component to sanitize

        Returns:
            Sanitized component
        """
        # Remove path separators and dangerous characters
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f./]', "_", component)

        # Remove leading/trailing underscores
        safe = safe.strip("_")

        # If empty after sanitization, use a placeholder
        if not safe:
            safe = "unknown"

        return safe

    def _validate_key(self, storage_key: str) -> None:
        """Validate a storage key for security.

        Defence in depth: the filesystem backend rejects traversal too, but a
        key that escapes its prefix is a domain-level violation regardless of
        which backend is configured.

        Args:
            storage_key: Key to validate

        Raises:
            ValidationException: If the key is invalid or contains traversal
        """
        if not storage_key or not storage_key.strip():
            raise ValidationException("storage_key: Key is required")

        # Check for path traversal
        if self.PATH_TRAVERSAL_PATTERN.search(storage_key):
            raise ValidationException(
                "storage_key: Key contains invalid traversal sequences",
                details={"storage_key": storage_key},
            )

        # Ensure the key is relative, not absolute
        if storage_key.startswith("/") or storage_key.startswith("\\"):
            raise ValidationException(
                "storage_key: Key must be relative, not absolute",
                details={"storage_key": storage_key},
            )
