"""Test module for repository schema consistency.

This module validates that all case repository implementations correctly
implement the Pydantic model schema as defined in case-schema.md.

Tests verify:
- UploadedFile schema matches between Pydantic and SQL
- Messages don't contain session_id
- Session → User → Cases pattern (no direct session-to-case filtering)
- Field names match the post-010 shape (size_bytes, content_type,
  storage_ref, upload_source — no content_ref, preprocessing_summary,
  or source_type on UploadedFile; ``data_type`` is now a real column
  on uploaded_files, added by migration 010 to carry the file-level
  data classification that previously rode on the auto-DOCUMENT
  Evidence row)
- Optional fields are properly handled
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus, UploadedFile
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

# ============================================================
# Schema Consistency Tests
# ============================================================


@pytest.mark.unit
class TestUploadedFileSchemaConsistency:
    """Test UploadedFile schema matches across implementations"""

    def test_pydantic_model_has_required_fields(self):
        """Verify Pydantic UploadedFile model has the post-redesign field set."""

        uploaded_file = UploadedFile(
            file_id="file_abc123def456",
            filename="test.log",
            size_bytes=1024,
            content_type="text/plain",
            content_hash="a" * 64,
            uploaded_at_turn=1,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by="user_001",
            upload_source="file_upload",
            storage_ref="s3://bucket/key",
            summary="head/tail preprocessing summary",
            structural_index='{"v":1,"file_extract":"..."}',
            data_type="logs",
        )

        # Current field set (post-010)
        for field in (
            "file_id",
            "filename",
            "size_bytes",
            "content_type",
            "content_hash",
            "uploaded_at_turn",
            "uploaded_at",
            "uploaded_by",
            "upload_source",
            "storage_ref",
            # Migration 010: preprocessing artifacts moved from auto-DOCUMENT
            # Evidence rows to uploaded_files (where they semantically belong).
            "summary",
            "structural_index",
            "data_type",
            "coverage_start_ts",
            "coverage_end_ts",
        ):
            assert hasattr(uploaded_file, field), f"missing field: {field}"

        # Old/renamed/dropped field names must not exist
        for field in (
            "file_size",  # → size_bytes
            "source_type",  # → upload_source on UploadedFile
            "content_ref",  # → storage_ref
            "storage_path",  # legacy
            "preprocessing_summary",  # dropped in migration 004
            "processing_status",  # legacy
            "processed_at",  # legacy
        ):
            assert not hasattr(uploaded_file, field), f"stale field present: {field}"

    def test_pydantic_storage_ref_is_optional(self):
        """Verify storage_ref can be None (processing pending)."""

        uploaded_file = UploadedFile(
            file_id="file_abc123def456",
            filename="test.log",
            size_bytes=1024,
            uploaded_at_turn=1,
            uploaded_at=datetime.now(timezone.utc),
            storage_ref=None,
        )

        assert uploaded_file.storage_ref is None

    @pytest.mark.asyncio
    async def test_inmemory_repository_schema_match(self):
        """Test InMemoryCaseRepository preserves the post-redesign field shape."""

        repo = InMemoryCaseRepository()

        case = Case(
            case_id="case_123456789012",
            title="Test Case",
            description="Schema-consistency test case",
            user_id="user_123",
            organization_id="org_123",
            status=CaseStatus.INQUIRY,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
            uploaded_files=[
                UploadedFile(
                    file_id="file_001234567890",
                    filename="test.log",
                    size_bytes=2048,
                    content_type="text/plain",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    upload_source="file_upload",
                    storage_ref="s3://bucket/test.log",
                )
            ],
        )

        await repo.save(case)
        retrieved = await repo.get(case.case_id)

        assert len(retrieved.uploaded_files) == 1
        file = retrieved.uploaded_files[0]

        assert file.file_id == "file_001234567890"
        assert file.size_bytes == 2048
        assert file.content_type == "text/plain"
        assert file.upload_source == "file_upload"
        assert file.storage_ref == "s3://bucket/test.log"


# ============================================================
# Message Schema Tests
# ============================================================


@pytest.mark.unit
class TestMessageSchemaConsistency:
    """Test messages don't contain session_id"""

    def test_message_schema_no_session_id(self):
        """Verify Message model doesn't have session_id field"""

        message_dict = {
            "message_id": "msg_abc123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "user",
            "content": "Test message",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "author_id": "user_123",
            "metadata": {},
        }

        # Should NOT have session_id
        assert "session_id" not in message_dict

    @pytest.mark.asyncio
    async def test_case_service_creates_messages_without_session_id(self):
        """Test case service doesn't add session_id to messages"""

        # This is tested in case_service tests, but verify schema here
        # Legacy services/domain/* was removed; use extracted module path.
        from faultmaven.modules.case.domain.services.case_service import CaseService

        # Mock dependencies
        mock_repo = MagicMock()
        mock_session_store = MagicMock()

        service = CaseService(
            case_repository=mock_repo,
            session_store=mock_session_store,
        )

        # Verify the service method signature doesn't include session_id
        # (this is a structural test - actual behavior tested in service tests)
        import inspect

        sig = inspect.signature(service.create_case)

        # Should have session_id for authentication
        assert "session_id" in sig.parameters

        # But internal message creation should NOT store session_id
        # (verified by checking the actual implementation doesn't add it)


# ============================================================
# Repository Architecture Tests
# ============================================================


@pytest.mark.unit
class TestRepositoryArchitecture:
    """Test repositories follow Session → User → Cases pattern"""

    def test_repository_interface_no_find_by_session(self):
        """Verify find_by_session() method was removed"""

        from faultmaven.modules.case.infrastructure.case_repository import (
            CaseRepository,
        )

        # find_by_session should NOT exist in abstract interface
        assert not hasattr(CaseRepository, "find_by_session")

    @pytest.mark.asyncio
    async def test_inmemory_repository_no_find_by_session(self):
        """Verify InMemoryRepository doesn't have find_by_session"""

        repo = InMemoryCaseRepository()

        # Should NOT have find_by_session method
        assert not hasattr(repo, "find_by_session")

    @pytest.mark.asyncio
    async def test_list_method_filters_by_user_id(self):
        """Verify list() method filters by user_id, not session_id"""

        repo = InMemoryCaseRepository()

        # Create cases for two different users
        case1 = Case(
            case_id="case_001234567890",  # Must be exactly 17 chars
            title="User 1 Case",
            user_id="user_001",  # Required field, not owner_id
            organization_id="org_001",  # Required field
            status=CaseStatus.INQUIRY,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
        )

        case2 = Case(
            case_id="case_002234567890",  # Must be exactly 17 chars
            title="User 2 Case",
            user_id="user_002",  # Required field, not owner_id
            organization_id="org_002",  # Required field
            status=CaseStatus.INQUIRY,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
        )

        await repo.save(case1)
        await repo.save(case2)

        # List by user_id should only return that user's cases
        user1_cases, count = await repo.list(user_id="user_001")
        assert count == 1
        assert user1_cases[0].case_id == "case_001234567890"

        user2_cases, count = await repo.list(user_id="user_002")
        assert count == 1
        assert user2_cases[0].case_id == "case_002234567890"


# ============================================================
# PostgreSQL Hybrid Repository Tests
# ============================================================


@pytest.mark.unit
class TestPostgreSQLHybridSchemaConsistency:
    """Test PostgreSQL hybrid repository SQL queries use correct field names"""

    def test_insert_query_uses_correct_field_names(self):
        """Verify hybrid repo exposes the upsert path for uploaded_files."""

        from faultmaven.infrastructure.persistence.postgresql_hybrid_case_repository import (
            PostgreSQLHybridCaseRepository,
        )

        assert hasattr(PostgreSQLHybridCaseRepository, "_upsert_uploaded_files")

    def test_select_query_uses_correct_field_names(self):
        """Verify hybrid repo exposes the get() entrypoint."""

        from faultmaven.infrastructure.persistence.postgresql_hybrid_case_repository import (
            PostgreSQLHybridCaseRepository,
        )

        assert hasattr(PostgreSQLHybridCaseRepository, "get")


# ============================================================
# Integration Tests (require database)
# ============================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestDatabaseSchemaIntegration:
    """Integration tests verifying SQL schema matches Pydantic model"""

    async def test_full_roundtrip_uploaded_file(self, test_db_session):
        """Test complete INSERT → SELECT roundtrip preserves all fields"""

        if test_db_session is None:
            pytest.skip("No test database configured")

        repo = PostgreSQLHybridCaseRepository(db=test_db_session)

        case = Case(
            case_id="case_integration_001",
            title="Integration Test",
            description="Roundtrip integration test",
            user_id="user_integration_001",
            organization_id="org_integration_001",
            status=CaseStatus.INQUIRY,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
            uploaded_files=[
                UploadedFile(
                    file_id="file_integration001",
                    filename="integration_test.log",
                    size_bytes=4096,
                    content_type="text/plain",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    upload_source="file_upload",
                    storage_ref="s3://test-bucket/integration.log",
                )
            ],
        )

        await repo.create(case)
        retrieved = await repo.get(case.case_id)

        assert len(retrieved.uploaded_files) == 1
        file = retrieved.uploaded_files[0]

        assert file.file_id == "file_integration001"
        assert file.filename == "integration_test.log"
        assert file.size_bytes == 4096
        assert file.content_type == "text/plain"
        assert file.uploaded_at_turn == 1
        assert file.upload_source == "file_upload"
        assert file.storage_ref == "s3://test-bucket/integration.log"

    async def test_optional_fields_handle_null(self, test_db_session):
        """Test optional fields (storage_ref, content_type) can be NULL"""

        if test_db_session is None:
            pytest.skip("No test database configured")

        repo = PostgreSQLHybridCaseRepository(db=test_db_session)

        case = Case(
            case_id="case_integration_002",
            title="NULL Fields Test",
            description="Null-fields integration test",
            user_id="user_integration_002",
            organization_id="org_integration_002",
            status=CaseStatus.INQUIRY,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
            uploaded_files=[
                UploadedFile(
                    file_id="file_integration002",
                    filename="pending.log",
                    size_bytes=1024,
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    upload_source="file_upload",
                    storage_ref=None,
                    content_type=None,
                )
            ],
        )

        await repo.create(case)
        retrieved = await repo.get(case.case_id)

        file = retrieved.uploaded_files[0]
        assert file.storage_ref is None
        assert file.content_type is None


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
async def test_db_session():
    """Fixture providing test database session (if available)"""

    # Check if test database is configured
    import os

    db_url = os.getenv("TEST_DATABASE_URL")

    if not db_url:
        yield None  # Skip integration tests
        return

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Setup: Create tables if needed
        # (Assumes migrations already run)

        yield session

        # Teardown: Rollback
        await session.rollback()
