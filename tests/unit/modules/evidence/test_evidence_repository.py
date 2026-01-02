"""Unit tests for EvidenceRepository (PR #46c).

Tests the repository layer with in-memory SQLite database.
"""
import json
import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from faultmaven.infrastructure.persistence.models import Base, StandaloneEvidenceModel
from faultmaven.modules.evidence.domain.models import EvidenceListFilter
from faultmaven.modules.evidence.infrastructure.persistence.evidence_repository import (
    EvidenceRepository,
)


@pytest.fixture(scope="function")
async def async_engine():
    """Create async SQLite in-memory engine for tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def async_session(async_engine):
    """Create async session for tests."""
    SessionLocal = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with SessionLocal() as session:
        yield session


@pytest.fixture
def repository(async_session) -> EvidenceRepository:
    """Create EvidenceRepository with test session."""
    return EvidenceRepository(session=async_session)


@pytest.fixture
def sample_user_id():
    """Generate sample user ID."""
    return uuid4()


class TestEvidenceRepositoryCreate:
    """Tests for creating evidence records."""

    @pytest.mark.asyncio
    async def test_create_evidence_success(self, repository, sample_user_id):
        """Test creating evidence with all fields."""
        result = await repository.create(
            filename="test.log",
            content_type="text/plain",
            size_bytes=1024,
            storage_path="evidence/path/to/file.log",
            uploaded_by=sample_user_id,
            description="Test file",
            tags=["test", "log"],
        )

        assert result is not None
        assert result.filename == "test.log"
        assert result.content_type == "text/plain"
        assert result.size_bytes == 1024
        assert result.uploaded_by == sample_user_id
        assert result.description == "Test file"
        assert "test" in result.tags
        assert "log" in result.tags

    @pytest.mark.asyncio
    async def test_create_evidence_generates_uuid(self, repository, sample_user_id):
        """Test that create generates a UUID for the evidence."""
        result = await repository.create(
            filename="test.log",
            content_type="text/plain",
            size_bytes=512,
            storage_path="evidence/path/file.log",
            uploaded_by=sample_user_id,
        )

        assert result.id is not None
        # Verify it's a valid UUID
        assert len(str(result.id)) == 36

    @pytest.mark.asyncio
    async def test_create_evidence_without_optional_fields(self, repository, sample_user_id):
        """Test creating evidence without description or tags."""
        result = await repository.create(
            filename="minimal.log",
            content_type="application/octet-stream",
            size_bytes=256,
            storage_path="evidence/minimal.log",
            uploaded_by=sample_user_id,
        )

        assert result is not None
        assert result.description is None
        assert result.tags == []

    @pytest.mark.asyncio
    async def test_create_evidence_sets_uploaded_at(self, repository, sample_user_id):
        """Test that uploaded_at is set automatically."""
        before = datetime.now(timezone.utc)

        result = await repository.create(
            filename="timestamp.log",
            content_type="text/plain",
            size_bytes=128,
            storage_path="evidence/timestamp.log",
            uploaded_by=sample_user_id,
        )

        after = datetime.now(timezone.utc)

        assert result.uploaded_at is not None
        # Should be between before and after (allowing some tolerance)
        assert result.uploaded_at >= before.replace(tzinfo=None) or result.uploaded_at >= before

    @pytest.mark.asyncio
    async def test_create_evidence_empty_linked_cases(self, repository, sample_user_id):
        """Test that linked_cases starts empty."""
        result = await repository.create(
            filename="unlinked.log",
            content_type="text/plain",
            size_bytes=64,
            storage_path="evidence/unlinked.log",
            uploaded_by=sample_user_id,
        )

        assert result.linked_cases == []


class TestEvidenceRepositoryGet:
    """Tests for retrieving evidence records."""

    @pytest.mark.asyncio
    async def test_get_evidence_success(self, repository, sample_user_id):
        """Test retrieving existing evidence."""
        created = await repository.create(
            filename="retrieve.log",
            content_type="text/plain",
            size_bytes=512,
            storage_path="evidence/retrieve.log",
            uploaded_by=sample_user_id,
            tags=["retrieve"],
        )

        result = await repository.get(created.id)

        assert result is not None
        assert result.id == created.id
        assert result.filename == "retrieve.log"
        assert "retrieve" in result.tags

    @pytest.mark.asyncio
    async def test_get_evidence_not_found(self, repository):
        """Test retrieving non-existent evidence returns None."""
        non_existent_id = uuid4()

        result = await repository.get(non_existent_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_preserves_all_fields(self, repository, sample_user_id):
        """Test that get returns all fields correctly."""
        created = await repository.create(
            filename="complete.log",
            content_type="application/json",
            size_bytes=2048,
            storage_path="evidence/complete.log",
            uploaded_by=sample_user_id,
            description="Complete test",
            tags=["tag1", "tag2", "tag3"],
        )

        result = await repository.get(created.id)

        assert result.filename == "complete.log"
        assert result.content_type == "application/json"
        assert result.size_bytes == 2048
        assert result.description == "Complete test"
        assert set(result.tags) == {"tag1", "tag2", "tag3"}


class TestEvidenceRepositoryList:
    """Tests for listing evidence records."""

    @pytest.mark.asyncio
    async def test_list_empty(self, repository):
        """Test listing when no evidence exists."""
        filters = EvidenceListFilter()

        results, total = await repository.list(filters)

        assert results == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_all(self, repository, sample_user_id):
        """Test listing all evidence without filters."""
        # Create multiple records
        for i in range(3):
            await repository.create(
                filename=f"file_{i}.log",
                content_type="text/plain",
                size_bytes=100 * (i + 1),
                storage_path=f"evidence/file_{i}.log",
                uploaded_by=sample_user_id,
            )

        filters = EvidenceListFilter()
        results, total = await repository.list(filters)

        assert len(results) == 3
        assert total == 3

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, repository, sample_user_id):
        """Test listing with limit and offset."""
        for i in range(5):
            await repository.create(
                filename=f"page_{i}.log",
                content_type="text/plain",
                size_bytes=100,
                storage_path=f"evidence/page_{i}.log",
                uploaded_by=sample_user_id,
            )

        # Get first page
        filters = EvidenceListFilter(limit=2, offset=0)
        results, total = await repository.list(filters)

        assert len(results) == 2
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_with_offset(self, repository, sample_user_id):
        """Test listing with offset skips records."""
        for i in range(5):
            await repository.create(
                filename=f"offset_{i}.log",
                content_type="text/plain",
                size_bytes=100,
                storage_path=f"evidence/offset_{i}.log",
                uploaded_by=sample_user_id,
            )

        filters = EvidenceListFilter(limit=10, offset=3)
        results, total = await repository.list(filters)

        assert len(results) == 2  # 5 - 3 = 2 remaining
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_filter_by_uploaded_by(self, repository):
        """Test filtering by uploaded_by."""
        user1 = uuid4()
        user2 = uuid4()

        await repository.create(
            filename="user1.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/user1.log",
            uploaded_by=user1,
        )
        await repository.create(
            filename="user2.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/user2.log",
            uploaded_by=user2,
        )

        filters = EvidenceListFilter(uploaded_by=user1)
        results, total = await repository.list(filters)

        assert len(results) == 1
        assert results[0].uploaded_by == user1

    @pytest.mark.asyncio
    async def test_list_filter_by_filename_contains(self, repository, sample_user_id):
        """Test filtering by filename substring (case-insensitive)."""
        await repository.create(
            filename="error_log.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/error.txt",
            uploaded_by=sample_user_id,
        )
        await repository.create(
            filename="debug_log.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/debug.txt",
            uploaded_by=sample_user_id,
        )

        filters = EvidenceListFilter(filename_contains="error")
        results, total = await repository.list(filters)

        assert len(results) == 1
        assert "error" in results[0].filename.lower()

    @pytest.mark.asyncio
    async def test_list_filter_by_tags(self, repository, sample_user_id):
        """Test filtering by tags (OR logic)."""
        await repository.create(
            filename="tagged1.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/tagged1.log",
            uploaded_by=sample_user_id,
            tags=["error", "production"],
        )
        await repository.create(
            filename="tagged2.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/tagged2.log",
            uploaded_by=sample_user_id,
            tags=["debug", "development"],
        )

        filters = EvidenceListFilter(tags=["error"])
        results, total = await repository.list(filters)

        assert len(results) == 1
        assert "error" in results[0].tags


class TestEvidenceRepositoryDelete:
    """Tests for deleting evidence records."""

    @pytest.mark.asyncio
    async def test_delete_success(self, repository, sample_user_id):
        """Test deleting existing evidence."""
        created = await repository.create(
            filename="delete_me.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/delete_me.log",
            uploaded_by=sample_user_id,
        )

        result = await repository.delete(created.id)

        assert result is True

        # Verify it's gone
        retrieved = await repository.get(created.id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, repository):
        """Test deleting non-existent evidence returns False."""
        non_existent_id = uuid4()

        result = await repository.delete(non_existent_id)

        assert result is False


class TestEvidenceRepositoryLinkToCase:
    """Tests for linking evidence to cases."""

    @pytest.mark.asyncio
    async def test_link_to_case_success(self, repository, sample_user_id):
        """Test linking evidence to a case."""
        case_id = uuid4()

        created = await repository.create(
            filename="link_me.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/link_me.log",
            uploaded_by=sample_user_id,
        )

        result = await repository.link_to_case(created.id, case_id)

        assert result is not None
        assert case_id in result.linked_cases

    @pytest.mark.asyncio
    async def test_link_to_case_multiple_cases(self, repository, sample_user_id):
        """Test linking evidence to multiple cases."""
        case_id1 = uuid4()
        case_id2 = uuid4()

        created = await repository.create(
            filename="multi_link.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/multi_link.log",
            uploaded_by=sample_user_id,
        )

        await repository.link_to_case(created.id, case_id1)
        result = await repository.link_to_case(created.id, case_id2)

        assert case_id1 in result.linked_cases
        assert case_id2 in result.linked_cases
        assert len(result.linked_cases) == 2

    @pytest.mark.asyncio
    async def test_link_to_case_idempotent(self, repository, sample_user_id):
        """Test linking same case twice doesn't create duplicate."""
        case_id = uuid4()

        created = await repository.create(
            filename="idempotent.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/idempotent.log",
            uploaded_by=sample_user_id,
        )

        await repository.link_to_case(created.id, case_id)
        result = await repository.link_to_case(created.id, case_id)

        # Should only appear once
        assert result.linked_cases.count(case_id) == 1

    @pytest.mark.asyncio
    async def test_link_to_case_not_found(self, repository):
        """Test linking non-existent evidence returns None."""
        non_existent_id = uuid4()
        case_id = uuid4()

        result = await repository.link_to_case(non_existent_id, case_id)

        assert result is None


class TestEvidenceRepositoryJsonSerialization:
    """Tests for JSON field serialization/deserialization."""

    @pytest.mark.asyncio
    async def test_tags_json_serialization(self, repository, sample_user_id):
        """Test tags are properly serialized/deserialized as JSON."""
        tags = ["tag1", "tag2", "special-tag", "tag with spaces"]

        created = await repository.create(
            filename="json_tags.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/json_tags.log",
            uploaded_by=sample_user_id,
            tags=tags,
        )

        retrieved = await repository.get(created.id)

        assert set(retrieved.tags) == set(tags)

    @pytest.mark.asyncio
    async def test_linked_cases_json_serialization(self, repository, sample_user_id):
        """Test linked_cases are properly serialized/deserialized as JSON."""
        case_ids = [uuid4(), uuid4(), uuid4()]

        created = await repository.create(
            filename="json_cases.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/json_cases.log",
            uploaded_by=sample_user_id,
        )

        # Link all cases
        result = created
        for case_id in case_ids:
            result = await repository.link_to_case(result.id, case_id)

        # Retrieve and verify
        retrieved = await repository.get(created.id)

        assert len(retrieved.linked_cases) == 3
        for case_id in case_ids:
            assert case_id in retrieved.linked_cases

    @pytest.mark.asyncio
    async def test_empty_tags_serialization(self, repository, sample_user_id):
        """Test empty tags list is properly handled."""
        created = await repository.create(
            filename="no_tags.log",
            content_type="text/plain",
            size_bytes=100,
            storage_path="evidence/no_tags.log",
            uploaded_by=sample_user_id,
            tags=[],
        )

        retrieved = await repository.get(created.id)

        assert retrieved.tags == []
