# PR #46 - Evidence Service Implementation Plan

## Overview

This document outlines the phased implementation of the Evidence Service module with 7 API endpoints from the microservices architecture.

## Current PR #46 (WIP) - Domain Models + Service Interface

**Status**: Work in Progress - Foundation Layer Complete

**What's Included**:
- ✅ Domain models (`modules/evidence/domain/models.py`)
  - `Evidence` - Main evidence domain model
  - `EvidenceUploadRequest` - Upload request model
  - `EvidenceLinkRequest` - Link request model
  - `EvidenceListFilter` - Filter model with pagination
- ✅ Service interface (`modules/evidence/domain/services/evidence_service.py`)
  - Business logic for upload, link, list, delete
  - Async/await patterns
  - Error handling
- ✅ API routes (`modules/evidence/api/routes.py`)
  - All 7 endpoints defined
  - Request/response models
  - Authentication integration
- ✅ Repository interface (`modules/evidence/infrastructure/persistence/evidence_repository.py`)
  - CRUD operations
  - Filtering logic
  - Case linking

**What's Missing** (to be completed in follow-up PRs):
- ❌ SQLAlchemy Evidence ORM model
- ❌ DI Container integration
- ❌ Router registration in main app
- ❌ Route order fix (`/case/{case_id}` before `/{evidence_id}`)
- ❌ Tests (20 tests target)
- ❌ Storage provider implementation

---

## Follow-Up PR Plan

### PR #46b - Infrastructure Layer + Container Integration

**Objective**: Make the Evidence Service functional with database and storage

**Files to Add/Modify**:

1. **Add SQLAlchemy Evidence Model**:
   ```python
   # faultmaven/infrastructure/database/models/evidence.py
   from sqlalchemy import Column, String, Integer, DateTime, JSON, ARRAY
   from sqlalchemy.dialects.postgresql import UUID

   class Evidence(Base):
       __tablename__ = "evidence"

       id = Column(UUID(as_uuid=True), primary_key=True)
       filename = Column(String, nullable=False)
       content_type = Column(String, nullable=False)
       size_bytes = Column(Integer, nullable=False)
       storage_path = Column(String, nullable=False)
       uploaded_by = Column(UUID(as_uuid=True), nullable=False)
       uploaded_at = Column(DateTime(timezone=True), nullable=False)
       description = Column(String, nullable=True)
       tags = Column(ARRAY(String), default=list)
       linked_cases = Column(ARRAY(String), default=list)
       metadata = Column(JSON, default=dict)
   ```

2. **Add Container Integration**:
   ```python
   # faultmaven/container/providers/services.py

   def create_evidence_service(container) -> EvidenceService:
       """Create evidence service with dependencies."""
       from faultmaven.modules.evidence.domain.services import EvidenceService
       from faultmaven.modules.evidence.infrastructure.persistence import EvidenceRepository

       session = container.get_db_session()
       repository = EvidenceRepository(session)
       storage = container.get_storage_provider()

       return EvidenceService(storage_provider=storage, repository=repository)

   # faultmaven/container.py

   def get_evidence_service(self) -> EvidenceService:
       """Get evidence service from container."""
       if not hasattr(self, '_evidence_service'):
           self._evidence_service = create_evidence_service(self)
       return self._evidence_service
   ```

3. **Register Router**:
   ```python
   # faultmaven/main.py

   from faultmaven.modules.evidence.api.routes import router as evidence_router

   app.include_router(evidence_router, prefix="/api/v1")
   ```

4. **Fix Route Order**:
   ```python
   # faultmaven/modules/evidence/api/routes.py

   # Move this BEFORE /{evidence_id}
   @router.get("/case/{case_id}", response_model=List[Evidence])
   async def get_evidence_for_case(...):
       ...

   # This should come AFTER /case/{case_id}
   @router.get("/{evidence_id}", response_model=Evidence)
   async def get_evidence(...):
       ...
   ```

5. **Alembic Migration**:
   ```bash
   alembic revision --autogenerate -m "Add evidence table"
   alembic upgrade head
   ```

**Deliverables**:
- ✅ Evidence table in database
- ✅ Container integration working
- ✅ Router registered and accessible
- ✅ Route order fixed
- ✅ Manual testing successful

**Estimated Effort**: 4-6 hours

---

### PR #46c - Tests + Documentation

**Objective**: Add comprehensive test coverage

**Files to Add**:

1. **Unit Tests** (`tests/unit/modules/evidence/test_evidence_service.py`):
   - Test upload_evidence
   - Test get_evidence
   - Test list_evidence with filters
   - Test delete_evidence
   - Test link_to_case
   - Test get_file_url
   - **Target**: 12 tests

2. **Repository Tests** (`tests/unit/modules/evidence/test_evidence_repository.py`):
   - Test create
   - Test get
   - Test list with various filters
   - Test delete
   - Test link_to_case
   - **Target**: 8 tests

3. **API Integration Tests** (`tests/integration/api/test_evidence_api.py`):
   - Test all 7 endpoints
   - Test authentication
   - Test file upload/download
   - Test error cases (404, 403)
   - **Target**: 15 tests

**Total Tests**: 35 tests (exceeds 20 target)

**Documentation**:
- Update OpenAPI spec
- Add evidence endpoint examples
- Update architecture docs

**Estimated Effort**: 6-8 hours

---

## Timeline

| PR | Focus | Estimated Effort | Dependencies |
|----|-------|------------------|--------------|
| #46 (WIP) | Domain + Interface | ✅ Complete | None |
| #46b | Infrastructure + Container | 4-6 hours | #46 merged |
| #46c | Tests + Docs | 6-8 hours | #46b merged |

**Total Estimated Time**: 10-14 hours

---

## Merge Strategy

1. **Review PR #46** (current WIP)
   - Approve domain models and service interface
   - Merge as foundation layer

2. **Implement PR #46b**
   - Complete infrastructure integration
   - Manual testing to verify endpoints work
   - Merge when functional

3. **Implement PR #46c**
   - Add comprehensive test coverage
   - Update documentation
   - Final merge completes Evidence Service

---

## Testing Checklist (PR #46c)

- [ ] Unit test: Upload evidence file
- [ ] Unit test: Get evidence by ID
- [ ] Unit test: List evidence with case filter
- [ ] Unit test: List evidence with tag filter
- [ ] Unit test: Delete evidence
- [ ] Unit test: Link evidence to case
- [ ] Unit test: Get download URL
- [ ] Repository test: Create evidence record
- [ ] Repository test: Filter by case_id
- [ ] Repository test: Filter by uploaded_by
- [ ] Repository test: Filter by tags
- [ ] Repository test: Pagination (offset/limit)
- [ ] API test: POST /evidence (upload)
- [ ] API test: GET /evidence/{id}
- [ ] API test: GET /evidence/{id}/download
- [ ] API test: DELETE /evidence/{id}
- [ ] API test: GET /evidence (list)
- [ ] API test: GET /evidence/case/{case_id}
- [ ] API test: POST /evidence/{id}/link
- [ ] API test: 404 error for missing evidence
- [ ] API test: Authentication required

---

## Success Criteria

✅ **PR #46 (Domain Layer)**:
- Clean domain models
- Service interface defined
- No runtime dependencies on missing code

✅ **PR #46b (Infrastructure)**:
- All 7 endpoints functional
- Manual testing successful
- Container integration working

✅ **PR #46c (Tests)**:
- 35+ tests passing
- 90%+ code coverage for Evidence module
- Documentation complete

---

**Document Version**: 1.0
**Created**: 2026-01-02
**Status**: Active Implementation Plan
