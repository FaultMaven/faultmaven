"""API Evidence Artifact Service Module (TASK-013)

Purpose: API service layer for evidence artifact management operations.

This service provides business logic orchestration between FastAPI controllers
and domain repositories. It handles:
- Evidence file upload and storage
- Organization-level authorization
- Primary evidence management
- Evidence statistics and analytics

Architecture:
    FastAPI Routes → APIEvidenceArtifactService → Repositories/FileStorage → Database/Filesystem

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from uuid import uuid4

from faultmaven.services.base import BaseService

# Interface imports for clean architecture compliance
if TYPE_CHECKING:
    from faultmaven.models.interfaces import IVectorStore, ITracer, ISanitizer
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)
from faultmaven.modules.case.contracts import ICaseRepository
from faultmaven.modules.evidence.domain.models import EvidenceListFilter
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ValidationException,
    ServiceError,
)


class APIEvidenceArtifactService(BaseService):
    """Service for API evidence artifact management operations.

    This service provides a clean API layer for evidence management with:
    - Organization-level authorization on all operations
    - File storage management
    - Unified logging and error handling
    - Transaction coordination between file storage and database

    Attributes:
        case_repo: Case repository (handles evidence persistence - migrated from EvidenceArtifactRepository)
        file_storage: File storage service
    """

    def __init__(
        self,
        case_repo: ICaseRepository,
        file_storage: Any,
    ):
        """Initialize API evidence artifact service.

        Args:
            case_repo: Case repository (handles evidence persistence and authorization - migrated from EvidenceArtifactRepository)
            file_storage: File storage service (required)

        Raises:
            ValueError: If required dependencies are not provided
        """
        super().__init__("api_evidence_artifact_service")
        self.case_repo = case_repo

        # Require explicit dependency injection
        if file_storage is None:
            raise ValueError("file_storage is required for APIEvidenceArtifactService")

        self.file_storage = file_storage

    # ============================================================
    # Authorization Helpers
    # ============================================================

    async def _verify_case_access(
        self,
        case_id: str,
        organization_id: str,
    ) -> None:
        """Verify that organization has access to case.

        Args:
            case_id: Case ID to check
            organization_id: Organization ID for authorization

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        case = await self.case_repo.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)

        if case.organization_id != organization_id:
            raise AuthorizationError(
                f"Case {case_id} not accessible by organization {organization_id}"
            )

    async def _verify_evidence_access(
        self,
        evidence_id: str,
        organization_id: str,
    ) -> EvidenceArtifact:
        """Verify organization has access to evidence artifact.

        Gets the evidence and checks authorization via parent case.

        Args:
            evidence_id: Evidence ID to check
            organization_id: Organization ID for authorization

        Returns:
            EvidenceArtifact if authorized

        Raises:
            NotFoundError: If evidence not found
            AuthorizationError: If organization doesn't own parent case
        """
        from uuid import UUID
        evidence_id_uuid = UUID(evidence_id) if isinstance(evidence_id, str) else evidence_id
        evidence = await self.case_repo.get_standalone_evidence(evidence_id_uuid)
        if not evidence:
            raise NotFoundError("Evidence", evidence_id)

        # Check organization matches
        if evidence.organization_id != organization_id:
            raise AuthorizationError(
                f"Evidence {evidence_id} not accessible by organization {organization_id}"
            )

        return evidence

    # ============================================================
    # Core Evidence Operations
    # ============================================================

    async def upload_evidence(
        self,
        case_id: str,
        organization_id: str,
        user_id: str,
        file_data: bytes,
        original_filename: str,
        mime_type: str,
        evidence_type: EvidenceArtifactType,
        description: Optional[str] = None,
        is_primary: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ) -> EvidenceArtifact:
        """Upload evidence artifact for a case.

        Workflow:
        1. Verify case exists and belongs to organization
        2. Validate file (size, type)
        3. Store file to filesystem
        4. Create EvidenceArtifact record
        5. If is_primary=True, unset existing primary
        6. Save artifact to repository
        7. Return created artifact

        Args:
            case_id: Case to attach evidence to
            organization_id: Organization for authorization
            user_id: User uploading the evidence
            file_data: Raw file bytes
            original_filename: Original filename
            mime_type: File MIME type
            evidence_type: Type of evidence (screenshot, log, etc.)
            description: Optional description
            is_primary: Whether this is primary evidence for case
            metadata: Optional metadata

        Returns:
            Created EvidenceArtifact

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If file invalid
            ServiceError: If storage or database fails
        """
        self.log_operation(
            "upload_evidence",
            case_id=case_id,
            organization_id=organization_id,
            user_id=user_id,
            original_filename=original_filename,
            mime_type=mime_type,
            evidence_type=evidence_type.value,
            file_size=len(file_data),
            is_primary=is_primary,
        )

        try:
            # Step 1: Verify case access
            await self._verify_case_access(case_id, organization_id)

            # Steps 2 & 3: Validate and store file
            storage_result = await self.file_storage.store_file(
                file_data=file_data,
                original_filename=original_filename,
                organization_id=organization_id,
                case_id=case_id,
                mime_type=mime_type,
            )

            # Step 4: Create evidence artifact
            evidence_id = f"evd_{uuid4().hex[:12]}"
            now = datetime.now(timezone.utc)

            evidence = EvidenceArtifact(
                evidence_id=evidence_id,
                case_id=case_id,
                user_id=user_id,
                organization_id=organization_id,
                original_filename=original_filename,
                stored_filename=storage_result["stored_filename"],
                file_path=storage_result["file_path"],
                evidence_type=evidence_type,
                mime_type=mime_type,
                file_size=storage_result["file_size"],
                storage_backend=StorageBackend.LOCAL_FILESYSTEM,
                created_at=now,
                updated_at=now,
                metadata=metadata,
                description=description,
                is_primary=False,  # Will set after checking existing primary
            )

            # Step 5: Handle is_primary flag
            if is_primary:
                # Use repository's set_primary_evidence which handles unsetting others
                evidence.is_primary = True

            # Step 6: Save to repository using create_standalone_evidence
            # Extract parameters from EvidenceArtifact object
            from uuid import UUID
            uploaded_by_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
            saved_evidence = await self.case_repo.create_standalone_evidence(
                filename=evidence.original_filename,
                content_type=evidence.mime_type,
                size_bytes=evidence.file_size,
                storage_path=evidence.file_path,
                uploaded_by=uploaded_by_uuid,
                description=evidence.description,
                tags=getattr(evidence, 'tags', []),
            )
            
            # Link to case
            from uuid import UUID
            evidence_id_uuid = UUID(saved_evidence.evidence_id) if isinstance(saved_evidence.evidence_id, str) else saved_evidence.evidence_id
            case_id_uuid = UUID(case_id) if isinstance(case_id, str) else case_id
            saved_evidence = await self.case_repo.link_standalone_evidence_to_case(evidence_id_uuid, case_id_uuid)
            if not saved_evidence:
                raise ServiceError(f"Failed to link evidence {saved_evidence.evidence_id} to case {case_id}")

            # If is_primary, ensure it's set properly via repository
            if is_primary:
                await self.case_repo.set_primary_evidence(case_id, saved_evidence.evidence_id)
                # Refresh to get updated state
                from uuid import UUID
                evidence_id_uuid = UUID(saved_evidence.evidence_id) if isinstance(saved_evidence.evidence_id, str) else saved_evidence.evidence_id
                saved_evidence = await self.case_repo.get_standalone_evidence(evidence_id_uuid)

            self.log_operation(
                "upload_evidence_success",
                evidence_id=evidence_id,
                case_id=case_id,
                file_path=storage_result["file_path"],
            )

            return saved_evidence

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            self.log_error("upload_evidence", e, case_id=case_id, user_id=user_id)
            raise ServiceError(f"Failed to upload evidence: {e}")

    async def get_evidence(
        self,
        evidence_id: str,
        organization_id: str
    ) -> Optional[EvidenceArtifact]:
        """Get evidence artifact by ID with authorization.

        Verifies organization owns the parent case.

        Args:
            evidence_id: Evidence ID to retrieve
            organization_id: Organization for authorization

        Returns:
            Evidence artifact if found and authorized, None otherwise
        """
        self.log_operation(
            "get_evidence",
            evidence_id=evidence_id,
            organization_id=organization_id,
        )

        if not evidence_id or not evidence_id.strip():
            return None

        try:
            from uuid import UUID
            evidence_id_uuid = UUID(evidence_id) if isinstance(evidence_id, str) else evidence_id
            evidence = await self.case_repo.get_standalone_evidence(evidence_id_uuid)

            if not evidence:
                return None

            # Authorization check
            if evidence.organization_id != organization_id:
                self.log_operation(
                    "get_evidence_unauthorized",
                    evidence_id=evidence_id,
                    organization_id=organization_id,
                    evidence_org_id=evidence.organization_id,
                )
                return None

            return evidence

        except Exception as e:
            self.log_error("get_evidence", e, evidence_id=evidence_id)
            return None

    async def download_evidence(
        self,
        evidence_id: str,
        organization_id: str
    ) -> Tuple[bytes, str, str]:
        """Download evidence artifact file.

        Args:
            evidence_id: Evidence ID to download
            organization_id: Organization for authorization

        Returns:
            Tuple of (file_data, original_filename, mime_type)

        Raises:
            NotFoundError: If evidence not found
            AuthorizationError: If organization doesn't own case
            ServiceError: If file read fails
        """
        self.log_operation(
            "download_evidence",
            evidence_id=evidence_id,
            organization_id=organization_id,
        )

        try:
            # Verify access and get evidence
            evidence = await self._verify_evidence_access(evidence_id, organization_id)

            # Retrieve file from storage
            file_data = await self.file_storage.retrieve_file(evidence.file_path)

            self.log_operation(
                "download_evidence_success",
                evidence_id=evidence_id,
                file_size=len(file_data),
            )

            return file_data, evidence.original_filename, evidence.mime_type

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            self.log_error("download_evidence", e, evidence_id=evidence_id)
            raise ServiceError(f"Failed to download evidence: {e}")

    async def update_evidence(
        self,
        evidence_id: str,
        organization_id: str,
        updates: Dict[str, Any]
    ) -> EvidenceArtifact:
        """Update evidence artifact metadata.

        Allowed updates:
        - description
        - is_primary
        - metadata

        Note: Cannot update file content (must re-upload)

        Args:
            evidence_id: Evidence ID to update
            organization_id: Organization for authorization
            updates: Fields to update

        Returns:
            Updated EvidenceArtifact

        Raises:
            NotFoundError: If evidence not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If updates invalid
        """
        self.log_operation(
            "update_evidence",
            evidence_id=evidence_id,
            organization_id=organization_id,
            update_fields=list(updates.keys()) if updates else [],
        )

        if not updates:
            raise ValidationException("updates: At least one update field is required")

        try:
            # Verify access and get evidence
            evidence = await self._verify_evidence_access(evidence_id, organization_id)

            # Validate and apply updates
            allowed_fields = {'description', 'is_primary', 'metadata'}
            applied_updates = []

            for key, value in updates.items():
                if key not in allowed_fields:
                    continue

                if key == 'description':
                    evidence.description = value.strip() if value else None
                    applied_updates.append('description')

                elif key == 'is_primary':
                    if value:
                        # Use repository method to handle unsetting others
                        await self.case_repo.set_primary_evidence(
                            evidence.case_id, evidence_id
                        )
                    else:
                        evidence.is_primary = False
                    applied_updates.append('is_primary')

                elif key == 'metadata':
                    if value is not None and not isinstance(value, dict):
                        raise ValidationException("metadata: Must be a dictionary")
                    evidence.metadata = value
                    applied_updates.append('metadata')

            if not applied_updates:
                raise ValidationException(
                    f"updates: No valid update fields provided. Allowed: {allowed_fields}"
                )

            # Update timestamp
            evidence.updated_at = datetime.now(timezone.utc)

            # Save updated evidence (if not just is_primary update handled by set_primary_evidence)
            if 'is_primary' not in applied_updates or len(applied_updates) > 1:
                saved_evidence = await self.case_repo.update_standalone_evidence(evidence)
            else:
                # Refresh evidence to get updated state
                from uuid import UUID
                evidence_id_uuid = UUID(evidence_id) if isinstance(evidence_id, str) else evidence_id
                saved_evidence = await self.case_repo.get_standalone_evidence(evidence_id_uuid)
            self.log_operation(
                "update_evidence_success",
                evidence_id=evidence_id,
                applied_updates=applied_updates,
            )

            return saved_evidence

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            self.log_error("update_evidence", e, evidence_id=evidence_id)
            raise ServiceError(f"Failed to update evidence: {e}")

    async def delete_evidence(
        self,
        evidence_id: str,
        organization_id: str
    ) -> bool:
        """Delete evidence artifact and file.

        Workflow:
        1. Verify authorization
        2. Get evidence artifact
        3. Delete file from storage
        4. Delete artifact from repository
        5. Return success status

        Args:
            evidence_id: Evidence ID to delete
            organization_id: Organization for authorization

        Returns:
            True if deleted, False if not found

        Raises:
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "delete_evidence",
            evidence_id=evidence_id,
            organization_id=organization_id,
        )

        if not evidence_id or not evidence_id.strip():
            return False

        try:
            # Get evidence to check authorization and get file path
            from uuid import UUID
            evidence_id_uuid = UUID(evidence_id) if isinstance(evidence_id, str) else evidence_id
            evidence = await self.case_repo.get_standalone_evidence(evidence_id_uuid)

            if not evidence:
                return False

            # Authorization check
            if evidence.organization_id != organization_id:
                raise AuthorizationError(
                    f"Evidence {evidence_id} not accessible by organization {organization_id}"
                )

            # Delete file from storage (gracefully handle missing files)
            file_deleted = await self.file_storage.delete_file(evidence.file_path)
            if not file_deleted:
                self.log_operation(
                    "delete_evidence_file_missing",
                    evidence_id=evidence_id,
                    file_path=evidence.file_path,
                )

            # Delete record from repository
            from uuid import UUID
            evidence_id_uuid = UUID(evidence_id) if isinstance(evidence_id, str) else evidence_id
            deleted = await self.case_repo.delete_standalone_evidence(evidence_id_uuid)

            if deleted:
                self.log_operation(
                    "delete_evidence_success",
                    evidence_id=evidence_id,
                    file_deleted=file_deleted,
                )

            return deleted

        except AuthorizationError:
            raise
        except Exception as e:
            self.log_error("delete_evidence", e, evidence_id=evidence_id)
            return False

    # ============================================================
    # List and Query Operations
    # ============================================================

    async def list_evidence_by_case(
        self,
        case_id: str,
        organization_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[EvidenceArtifact]:
        """List evidence artifacts for a case.

        Args:
            case_id: Case ID to list evidence for
            organization_id: Organization for authorization
            evidence_type: Optional filter by type
            limit: Max results
            offset: Pagination offset

        Returns:
            List of evidence artifacts

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "list_evidence_by_case",
            case_id=case_id,
            organization_id=organization_id,
            evidence_type=evidence_type.value if evidence_type else None,
            limit=limit,
            offset=offset,
        )

        try:
            # Verify case access
            await self._verify_case_access(case_id, organization_id)

            # Get evidence from repository
            filters = EvidenceListFilter(case_id=case_id, limit=limit, offset=offset)
            # Note: evidence_type filtering not yet supported in EvidenceListFilter, all evidence returned
            evidence_list, total = await self.case_repo.list_standalone_evidence(filters)
            # Filter by evidence_type if specified (client-side filter for now)
            if evidence_type:
                evidence_list = [e for e in evidence_list if getattr(e, 'evidence_type', None) == evidence_type]
                total = len(evidence_list)

            self.log_operation(
                "list_evidence_by_case_result",
                case_id=case_id,
                count=len(evidence_list),
                total=total,
            )

            return evidence_list

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            self.log_error("list_evidence_by_case", e, case_id=case_id)
            raise ServiceError(f"Failed to list evidence: {e}")

    # ============================================================
    # Primary Evidence Management
    # ============================================================

    async def set_primary_evidence(
        self,
        evidence_id: str,
        organization_id: str
    ) -> EvidenceArtifact:
        """Set artifact as primary evidence for case.

        Unsets any existing primary evidence for the same case.

        Args:
            evidence_id: Evidence ID to set as primary
            organization_id: Organization for authorization

        Returns:
            Updated evidence artifact with is_primary=True

        Raises:
            NotFoundError: If evidence not found
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "set_primary_evidence",
            evidence_id=evidence_id,
            organization_id=organization_id,
        )

        try:
            # Verify access and get evidence
            evidence = await self._verify_evidence_access(evidence_id, organization_id)

            # Set as primary (repository handles unsetting others)
            success = await self.case_repo.set_primary_evidence(
                evidence.case_id, evidence_id
            )

            if not success:
                raise ServiceError(f"Failed to set primary evidence for {evidence_id}")

            # Refresh evidence to get updated state
            updated_evidence = await self.case_repo.get_standalone_evidence(evidence_id)

            self.log_operation(
                "set_primary_evidence_success",
                evidence_id=evidence_id,
                case_id=evidence.case_id,
            )

            return updated_evidence

        except (NotFoundError, AuthorizationError):
            raise
        except ServiceError:
            raise
        except Exception as e:
            self.log_error("set_primary_evidence", e, evidence_id=evidence_id)
            raise ServiceError(f"Failed to set primary evidence: {e}")

    async def get_primary_evidence(
        self,
        case_id: str,
        organization_id: str
    ) -> Optional[EvidenceArtifact]:
        """Get primary evidence artifact for a case.

        Args:
            case_id: Case ID to get primary evidence for
            organization_id: Organization for authorization

        Returns:
            Primary evidence artifact if set, None otherwise

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "get_primary_evidence",
            case_id=case_id,
            organization_id=organization_id,
        )

        try:
            # Verify case access
            await self._verify_case_access(case_id, organization_id)

            # Get primary evidence from repository
            primary = await self.case_repo.get_primary_evidence(case_id)

            if primary:
                self.log_operation(
                    "get_primary_evidence_found",
                    case_id=case_id,
                    evidence_id=primary.evidence_id,
                )
            else:
                self.log_operation(
                    "get_primary_evidence_not_set",
                    case_id=case_id,
                )

            return primary

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            self.log_error("get_primary_evidence", e, case_id=case_id)
            return None

    # ============================================================
    # Statistics and Analytics
    # ============================================================

    async def get_evidence_statistics(
        self,
        case_id: str,
        organization_id: str
    ) -> Dict[str, Any]:
        """Get evidence statistics for a case.

        Returns:
            Statistics including:
            - total_artifacts
            - by_type (screenshot, log_file, etc.)
            - total_file_size_bytes
            - primary_evidence_id (if set)
        """
        self.log_operation(
            "get_evidence_statistics",
            case_id=case_id,
            organization_id=organization_id,
        )

        try:
            # Verify case access
            await self._verify_case_access(case_id, organization_id)

            # Get all evidence for case (no pagination for stats)
            from uuid import UUID
            case_id_uuid = UUID(case_id) if isinstance(case_id, str) else case_id
            filters = EvidenceListFilter(case_id=case_id_uuid, limit=10000, offset=0)
            evidence_list, total = await self.case_repo.list_standalone_evidence(filters)

            # Calculate statistics
            by_type: Dict[str, int] = {}
            total_file_size_bytes = 0
            primary_evidence_id = None

            for evidence in evidence_list:
                # Count by type
                type_key = evidence.evidence_type.value
                by_type[type_key] = by_type.get(type_key, 0) + 1

                # Sum file sizes
                total_file_size_bytes += evidence.file_size

                # Find primary evidence
                if evidence.is_primary:
                    primary_evidence_id = evidence.evidence_id

            statistics = {
                "case_id": case_id,
                "organization_id": organization_id,
                "total_artifacts": total,
                "by_type": by_type,
                "total_file_size_bytes": total_file_size_bytes,
                "total_file_size_mb": total_file_size_bytes / (1024 * 1024),
                "primary_evidence_id": primary_evidence_id,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            self.log_operation(
                "get_evidence_statistics_success",
                case_id=case_id,
                total_artifacts=total,
            )

            return statistics

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            self.log_error("get_evidence_statistics", e, case_id=case_id)
            return {
                "case_id": case_id,
                "organization_id": organization_id,
                "total_artifacts": 0,
                "by_type": {},
                "total_file_size_bytes": 0,
                "total_file_size_mb": 0,
                "primary_evidence_id": None,
                "error": str(e),
            }

    # ============================================================
    # Bulk Operations
    # ============================================================

    async def delete_all_evidence_for_case(
        self,
        case_id: str,
        organization_id: str
    ) -> int:
        """Delete all evidence artifacts for a case.

        Note: This is typically called when deleting a case (CASCADE).

        Args:
            case_id: Case ID to delete evidence for
            organization_id: Organization for authorization

        Returns:
            Number of evidence artifacts deleted

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "delete_all_evidence_for_case",
            case_id=case_id,
            organization_id=organization_id,
        )

        try:
            # Verify case access
            await self._verify_case_access(case_id, organization_id)

            # Get all evidence for case
            from uuid import UUID
            case_id_uuid = UUID(case_id) if isinstance(case_id, str) else case_id
            filters = EvidenceListFilter(case_id=case_id_uuid, limit=10000, offset=0)
            evidence_list, total = await self.case_repo.list_standalone_evidence(filters)

            deleted_count = 0

            for evidence in evidence_list:
                # Delete file from storage
                await self.file_storage.delete_file(evidence.file_path)

                # Delete record from repository
                from uuid import UUID
                evidence_id_uuid = UUID(evidence.evidence_id) if isinstance(evidence.evidence_id, str) else evidence.evidence_id
                if await self.case_repo.delete_standalone_evidence(evidence_id_uuid):
                    deleted_count += 1

            self.log_operation(
                "delete_all_evidence_for_case_success",
                case_id=case_id,
                deleted_count=deleted_count,
                total=total,
            )

            return deleted_count

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            self.log_error("delete_all_evidence_for_case", e, case_id=case_id)
            raise ServiceError(f"Failed to delete evidence for case: {e}")

    # ============================================================
    # Health Check
    # ============================================================

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check for the evidence artifact service.

        Returns:
            Dictionary containing health status information
        """
        try:
            # Check file storage health
            file_storage_health = await self.file_storage.health_check()

            return {
                "service": self.service_name,
                "status": "healthy" if file_storage_health.get("status") == "healthy" else "degraded",
                "file_storage": file_storage_health,
            }

        except Exception as e:
            return {
                "service": self.service_name,
                "status": "unhealthy",
                "error": str(e),
            }
