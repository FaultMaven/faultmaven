# TASK-013: API Service Layer (Evidence Artifact Management)

**Phase:** Week 4, Day 6-7 (API Layer Evolution)
**Priority:** P1 (Evidence file management API)
**Estimated Time:** 6-8 hours
**Dependencies:** TASK-012 (Session Service)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement the service layer for evidence artifact management API, providing business logic for file uploads, storage management, primary evidence designation, and artifact lifecycle.

---

## Context

The Evidence Service manages file artifacts associated with cases (screenshots, logs, network traces, etc.). It coordinates file storage (local filesystem initially, cloud storage future), metadata management, and security checks.

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. File Storage Service

**File:** `faultmaven/services/file_storage_service.py`

This service handles low-level file storage operations.

```python
class FileStorageService(BaseService):
    """Service for file storage operations.

    Handles actual file I/O with:
    - Local filesystem storage (initial implementation)
    - File path generation (organized by org/case/date)
    - File validation (size, type, malware scanning placeholder)
    - Future: S3/Azure/GCS support
    """

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
        self.storage_root = storage_root
        self.max_file_size_bytes = max_file_size_bytes
        self.allowed_mime_types = allowed_mime_types or []

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

    def _generate_storage_path(
        self,
        organization_id: str,
        case_id: str,
        original_filename: str
    ) -> Tuple[str, str]:
        """Generate storage path and stored filename.

        Path format: {org_id}/{case_id}/{YYYY-MM-DD}/{uuid}_{filename}

        Returns:
            Tuple of (stored_filename, file_path)
        """
```

---

### 2. Evidence Artifact Service

**File:** `faultmaven/services/evidence_artifact_service.py`

```python
class APIEvidenceArtifactService(BaseService):
    """Service for API evidence artifact management operations."""

    def __init__(
        self,
        evidence_repo: EvidenceArtifactRepository,
        case_repo: CaseRepository,
        file_storage: FileStorageService,
    ):
        """Initialize API evidence artifact service.

        Args:
            evidence_repo: Evidence artifact repository
            case_repo: Case repository (for authorization)
            file_storage: File storage service
        """
        super().__init__("api_evidence_artifact_service")
        self.evidence_repo = evidence_repo
        self.case_repo = case_repo
        self.file_storage = file_storage
```

**Core Methods:**

```python
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
```

---

### 3. Service Factory Extension

**File:** `faultmaven/services/service_factory.py`

Add factory methods:

```python
def create_file_storage_service(self) -> FileStorageService:
    """Create file storage service."""
    return FileStorageService(
        storage_root=settings.EVIDENCE_STORAGE_ROOT,
        max_file_size_bytes=settings.MAX_EVIDENCE_FILE_SIZE,
        allowed_mime_types=settings.ALLOWED_EVIDENCE_MIME_TYPES,
    )

def create_evidence_artifact_service(self) -> APIEvidenceArtifactService:
    """Create evidence artifact service with dependencies."""
    return APIEvidenceArtifactService(
        evidence_repo=self.evidence_repo,
        case_repo=self.case_repo,
        file_storage=self.create_file_storage_service(),
    )
```

---

### 4. Configuration Settings

**File:** `faultmaven/config/settings.py`

Add settings:

```python
# Evidence Storage
EVIDENCE_STORAGE_ROOT: str = os.getenv("EVIDENCE_STORAGE_ROOT", "./data/evidence")
MAX_EVIDENCE_FILE_SIZE: int = int(os.getenv("MAX_EVIDENCE_FILE_SIZE", str(100 * 1024 * 1024)))  # 100MB
ALLOWED_EVIDENCE_MIME_TYPES: List[str] = []  # Empty = allow all (can restrict later)
```

---

### 5. FastAPI Dependency

**File:** `faultmaven/api/dependencies.py`

Add dependency:

```python
async def get_evidence_artifact_service(
    factory: ServiceFactory = Depends(get_service_factory)
) -> APIEvidenceArtifactService:
    """Get evidence artifact service for request."""
    return factory.create_evidence_artifact_service()
```

---

## Testing Requirements

### 1. File Storage Service Tests (30+ tests)

**File:** `tests/unit/services/test_file_storage_service.py`

**Test Coverage:**

**Store File:**
- ✅ `store_file()` creates file on disk
- ✅ Generates correct path format (org/case/date/uuid_filename)
- ✅ Returns stored_filename, file_path, file_size
- ✅ Creates directories if they don't exist
- ✅ ValidationException on file too large
- ✅ ValidationException on disallowed MIME type (if configured)
- ✅ ServiceError on disk write failure

**Retrieve File:**
- ✅ `retrieve_file()` returns file bytes
- ✅ NotFoundError if file doesn't exist
- ✅ ServiceError on read failure

**Delete File:**
- ✅ `delete_file()` removes file from disk
- ✅ Returns True if deleted
- ✅ Returns False if not found

**Get File Info:**
- ✅ `get_file_info()` returns metadata
- ✅ Returns None if file doesn't exist

**Validate File:**
- ✅ `validate_file()` accepts valid files
- ✅ ValidationException on oversized files
- ✅ ValidationException on disallowed MIME types
- ✅ Handles empty filename
- ✅ Handles special characters in filename

**Path Generation:**
- ✅ `_generate_storage_path()` creates unique paths
- ✅ Includes UUID in stored_filename
- ✅ Date folder created (YYYY-MM-DD)
- ✅ Sanitizes filenames (removes dangerous characters)

---

### 2. Evidence Artifact Service Tests (60+ tests)

**File:** `tests/unit/services/test_api_evidence_artifact_service.py`

**Test Coverage:**

**Upload Evidence:**
- ✅ `upload_evidence()` stores file and creates artifact
- ✅ Evidence ID generated (UUID format)
- ✅ File stored to filesystem
- ✅ Metadata saved to repository
- ✅ is_primary flag handled correctly
- ✅ Unsets existing primary when new primary uploaded
- ✅ NotFoundError if case doesn't exist
- ✅ AuthorizationError if wrong organization
- ✅ ValidationException on invalid file

**Get Evidence:**
- ✅ `get_evidence()` success with authorization
- ✅ Returns None if not found
- ✅ Returns None if wrong organization
- ✅ Authorization via parent case check

**Download Evidence:**
- ✅ `download_evidence()` returns file data
- ✅ Returns correct filename and MIME type
- ✅ NotFoundError if evidence not found
- ✅ AuthorizationError if wrong organization
- ✅ ServiceError if file missing from disk

**Update Evidence:**
- ✅ `update_evidence()` updates metadata
- ✅ Updates description, is_primary, metadata
- ✅ Cannot update file content
- ✅ Authorization check
- ✅ NotFoundError if not found

**Delete Evidence:**
- ✅ `delete_evidence()` removes file and record
- ✅ File deleted from filesystem
- ✅ Record deleted from repository
- ✅ Returns True if deleted
- ✅ Returns False if not found
- ✅ Authorization check
- ✅ Handles missing file gracefully

**List Evidence:**
- ✅ `list_evidence_by_case()` returns all artifacts
- ✅ Filter by evidence_type
- ✅ Pagination (limit/offset)
- ✅ Authorization check
- ✅ NotFoundError if case doesn't exist

**Set Primary Evidence:**
- ✅ `set_primary_evidence()` sets is_primary=True
- ✅ Unsets existing primary for same case
- ✅ Authorization check
- ✅ NotFoundError if not found

**Get Primary Evidence:**
- ✅ `get_primary_evidence()` returns primary artifact
- ✅ Returns None if no primary set
- ✅ Authorization check

**Get Statistics:**
- ✅ `get_evidence_statistics()` returns correct counts
- ✅ By type breakdown
- ✅ Total file size calculation
- ✅ Primary evidence ID included
- ✅ Authorization check

---

### 3. Integration Tests (35+ tests)

**File:** `tests/integration/test_evidence_artifact_service_integration.py`

**Critical Tests:**

**End-to-End Upload/Download:**
```python
async def test_upload_and_download_evidence():
    """Test complete upload/download workflow."""
    # Create case
    # Upload evidence file
    # Verify file stored on disk
    # Verify metadata in database
    # Download evidence
    # Verify downloaded data matches uploaded data
```

**Authorization Enforcement:**
```python
async def test_authorization_prevents_cross_org_access():
    """Test organization-level authorization."""
    # Create case for org A
    # Upload evidence
    # Attempt to access with org B
    # Verify AuthorizationError or None returned
```

**Primary Evidence Management:**
```python
async def test_primary_evidence_enforcement():
    """Test primary evidence management."""
    # Upload evidence A, set as primary
    # Upload evidence B, set as primary
    # Verify evidence A no longer primary
    # Get primary evidence
    # Verify evidence B is primary
```

**CASCADE Delete:**
```python
async def test_evidence_cascade_delete_with_case():
    """Test CASCADE delete when case deleted."""
    # Create case
    # Upload multiple evidence files
    # Delete case
    # Verify all evidence records CASCADE deleted
    # Verify files deleted from disk (cleanup job)
```

**File Storage Persistence:**
```python
async def test_file_persists_across_service_restart():
    """Test file persistence."""
    # Upload evidence
    # Create new service instance (simulates restart)
    # Download evidence
    # Verify file still accessible
```

**Multiple Evidence Types:**
```python
async def test_multiple_evidence_types_per_case():
    """Test different evidence types."""
    # Upload screenshot
    # Upload log file
    # Upload network trace
    # List evidence
    # Verify all types present
    # Filter by type
    # Verify filtering works
```

---

### 4. Service Factory Tests (5+ tests)

**File:** `tests/unit/services/test_service_factory.py` (extend existing)

**Test Coverage:**
- ✅ `create_file_storage_service()` returns service
- ✅ `create_evidence_artifact_service()` returns service
- ✅ Service has correct dependencies
- ✅ file_storage not None

---

### 5. Performance Benchmarks (12+ benchmarks)

**File:** `tests/benchmarks/test_evidence_artifact_service_operations.py`

**Benchmarks:**
- Upload evidence (1MB file, target: <500ms p95)
- Upload evidence (10MB file, target: <2000ms p95)
- Get evidence metadata (target: <100ms p95)
- Download evidence (1MB file, target: <400ms p95)
- Delete evidence (target: <200ms p95)
- List evidence (50 artifacts, target: <300ms p95)
- Set primary evidence (target: <150ms p95)
- Get statistics (100 artifacts, target: <400ms p95)

**File Storage Benchmarks:**
- Store file (1MB, target: <300ms p95)
- Retrieve file (1MB, target: <200ms p95)
- Delete file (target: <100ms p95)

---

## Acceptance Criteria

- ✅ FileStorageService implemented with local filesystem support
- ✅ APIEvidenceArtifactService implemented with 10+ methods
- ✅ Service factory extended
- ✅ FastAPI dependency added
- ✅ Configuration settings added
- ✅ 140+ tests (30 file storage + 60 evidence service + 35 integration + 5 factory + 12 benchmarks)
- ✅ 80%+ test coverage
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns from TASK-011/012
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests
- [ ] File storage directory created and writable
- [ ] Authorization checks verified in all methods

---

## Notes

**File Storage Strategy:**
- **Phase 1 (TASK-013):** Local filesystem storage
- **Phase 2 (Future):** Cloud storage abstraction (S3, Azure Blob, GCS)
- Directory structure: `{storage_root}/{org_id}/{case_id}/{YYYY-MM-DD}/{uuid}_{filename}`

**Security Considerations:**
1. **File Validation:**
   - Size limits enforced (default 100MB)
   - MIME type validation (optional allow list)
   - Future: Malware scanning integration (ClamAV, VirusTotal)

2. **Path Traversal Prevention:**
   - Sanitize filenames (remove `../`, absolute paths)
   - UUID prefix ensures uniqueness
   - Store relative paths only

3. **Authorization:**
   - All operations require organization ownership check via parent case
   - No direct file access without going through service layer

**Primary Evidence Pattern:**
- Only one primary evidence artifact per case
- Setting new primary automatically unsets previous primary
- Primary evidence displayed prominently in UI

**File Cleanup:**
- Evidence records CASCADE deleted with parent case
- Files on disk should be cleaned up (future: background job)
- For now: Files remain on disk when record deleted (orphan cleanup future)

**MIME Type Detection:**
- Use `python-magic` library for reliable MIME type detection
- Don't trust client-provided MIME type alone
- Verify file signature matches declared type

**No Database Migration:**
- Service layer is pure Python logic
- No schema changes required
- Uses existing repositories and domain models

**Evolution Path:**
```
TASK-011: Case Service ✅
TASK-012: Session Service ✅
TASK-013: Evidence Service ← Current
TASK-014: FastAPI Controllers (REST API)
TASK-015: Agent Orchestration Service
```
