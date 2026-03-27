"""Unit tests for DatabaseSessionRepository.

Tests CRUD operations, session lifecycle, and expiry management
for the SQLAlchemy ORM-based session repository implementation.

Run with:
    pytest tests/unit/infrastructure/persistence/test_database_session_repository.py -v

Coverage:
    pytest tests/unit/infrastructure/persistence/test_database_session_repository.py \
        --cov=faultmaven/infrastructure/persistence/session_repository \
        --cov-report=term-missing
"""

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base, SessionModel
from faultmaven.modules.auth.domain.models.session import Session
from faultmaven.modules.auth.infrastructure.repositories.session_repository import (
    DatabaseSessionRepository,
    InMemorySessionRepository,
    SessionRepositoryException,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create async engine with in-memory SQLite database."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create async session for tests."""
    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def repository(async_session) -> DatabaseSessionRepository:
    """Create repository instance for tests."""
    return DatabaseSessionRepository(async_session)


@pytest.fixture
def sample_session() -> Session:
    """Create a sample session for testing."""
    return Session(
        session_id=str(uuid4()),
        user_id="test-user-001",
        organization_id="00000000-0000-0000-0000-000000000001",  # Default org for single-tenant
        created_at=datetime.now(timezone.utc),
        last_accessed=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        metadata={"source": "web", "device": "desktop"},
    )


@pytest.fixture
def sample_session_no_expiry() -> Session:
    """Create a session without expiry for testing."""
    return Session(
        session_id=str(uuid4()),
        user_id="test-user-002",
        created_at=datetime.now(timezone.utc),
        last_accessed=datetime.now(timezone.utc),
        expires_at=None,
        metadata=None,
    )


# ============================================================
# Create Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_session(
    repository: DatabaseSessionRepository, sample_session: Session
):
    """Test creating a new session in database."""
    # Act
    created = await repository.create_session(sample_session)

    # Assert
    assert created is not None
    assert created.session_id == sample_session.session_id
    assert created.user_id == sample_session.user_id
    assert created.metadata == sample_session.metadata


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_session_no_expiry(
    repository: DatabaseSessionRepository, sample_session_no_expiry: Session
):
    """Test creating a session without expiry."""
    # Act
    created = await repository.create_session(sample_session_no_expiry)

    # Assert
    assert created is not None
    assert created.session_id == sample_session_no_expiry.session_id
    assert created.expires_at is None
    assert created.is_active() is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_session_with_metadata(repository: DatabaseSessionRepository):
    """Test creating a session with complex metadata."""
    session = Session(
        session_id=str(uuid4()),
        user_id="test-user-meta",
        metadata={
            "browser": "Chrome",
            "version": "120.0",
            "features": ["dark_mode", "notifications"],
            "preferences": {"theme": "dark"},
        },
    )

    # Act
    created = await repository.create_session(session)

    # Assert
    assert created.metadata["browser"] == "Chrome"
    assert created.metadata["features"] == ["dark_mode", "notifications"]
    assert created.metadata["preferences"]["theme"] == "dark"


# ============================================================
# Get Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session(
    repository: DatabaseSessionRepository, sample_session: Session
):
    """Test retrieving session by ID."""
    # Arrange
    await repository.create_session(sample_session)

    # Act
    retrieved = await repository.get_session(sample_session.session_id)

    # Assert
    assert retrieved is not None
    assert retrieved.session_id == sample_session.session_id
    assert retrieved.user_id == sample_session.user_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_not_found(repository: DatabaseSessionRepository):
    """Test retrieving non-existent session returns None."""
    # Act
    result = await repository.get_session("nonexistent-session-id")

    # Assert
    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_sessions_by_user(repository: DatabaseSessionRepository):
    """Test retrieving all sessions for a user."""
    # Arrange
    user_id = "test-user-multiple"
    for i in range(3):
        session = Session(
            session_id=str(uuid4()),
            user_id=user_id,
        )
        await repository.create_session(session)

    # Add session for different user
    other_session = Session(
        session_id=str(uuid4()),
        user_id="other-user",
    )
    await repository.create_session(other_session)

    # Act
    sessions = await repository.get_sessions_by_user(user_id)

    # Assert
    assert len(sessions) == 3
    assert all(s.user_id == user_id for s in sessions)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_sessions_by_user_empty(repository: DatabaseSessionRepository):
    """Test retrieving sessions for user with no sessions."""
    # Act
    sessions = await repository.get_sessions_by_user("no-sessions-user")

    # Assert
    assert sessions == []


# ============================================================
# Update Last Accessed Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_last_accessed(
    repository: DatabaseSessionRepository, sample_session: Session
):
    """Test updating session last_accessed timestamp."""
    # Arrange
    await repository.create_session(sample_session)
    original_last_accessed = sample_session.last_accessed

    # Small delay to ensure timestamp difference
    import asyncio

    await asyncio.sleep(0.01)

    # Act
    result = await repository.update_last_accessed(sample_session.session_id)

    # Assert
    assert result is True

    # Verify timestamp was updated
    retrieved = await repository.get_session(sample_session.session_id)
    assert retrieved.last_accessed > original_last_accessed


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_last_accessed_not_found(repository: DatabaseSessionRepository):
    """Test updating last_accessed for non-existent session."""
    # Act
    result = await repository.update_last_accessed("nonexistent-session-id")

    # Assert
    assert result is False


# ============================================================
# Update Metadata Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_session_metadata(
    repository: DatabaseSessionRepository, sample_session: Session
):
    """Test updating session metadata."""
    # Arrange
    await repository.create_session(sample_session)
    new_metadata = {"updated": True, "new_field": "value"}

    # Act
    result = await repository.update_session_metadata(
        sample_session.session_id, new_metadata
    )

    # Assert
    assert result is True

    # Verify metadata was updated
    retrieved = await repository.get_session(sample_session.session_id)
    assert retrieved.metadata["updated"] is True
    assert retrieved.metadata["new_field"] == "value"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_session_metadata_not_found(repository: DatabaseSessionRepository):
    """Test updating metadata for non-existent session."""
    # Act
    result = await repository.update_session_metadata(
        "nonexistent-session-id", {"key": "value"}
    )

    # Assert
    assert result is False


# ============================================================
# Delete Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_session(
    repository: DatabaseSessionRepository, sample_session: Session
):
    """Test deleting session."""
    # Arrange
    await repository.create_session(sample_session)

    # Act
    result = await repository.delete_session(sample_session.session_id)

    # Assert
    assert result is True

    # Verify deletion
    retrieved = await repository.get_session(sample_session.session_id)
    assert retrieved is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_session_not_found(repository: DatabaseSessionRepository):
    """Test deleting non-existent session."""
    # Act
    result = await repository.delete_session("nonexistent-session-id")

    # Assert
    assert result is False


# ============================================================
# Session Expiry Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cleanup_expired_sessions(repository: DatabaseSessionRepository):
    """Test cleanup removes only expired sessions."""
    # Arrange
    # Create expired session
    expired_session = Session(
        session_id=str(uuid4()),
        user_id="test-user-expired",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await repository.create_session(expired_session)

    # Create active session
    active_session = Session(
        session_id=str(uuid4()),
        user_id="test-user-active",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    await repository.create_session(active_session)

    # Create session without expiry (never expires)
    no_expiry_session = Session(
        session_id=str(uuid4()),
        user_id="test-user-no-expiry",
        expires_at=None,
    )
    await repository.create_session(no_expiry_session)

    # Act
    deleted_count = await repository.cleanup_expired_sessions()

    # Assert
    assert deleted_count == 1

    # Verify expired session was deleted
    expired_retrieved = await repository.get_session(expired_session.session_id)
    assert expired_retrieved is None

    # Verify active session still exists
    active_retrieved = await repository.get_session(active_session.session_id)
    assert active_retrieved is not None

    # Verify no-expiry session still exists
    no_expiry_retrieved = await repository.get_session(no_expiry_session.session_id)
    assert no_expiry_retrieved is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cleanup_expired_sessions_none_expired(
    repository: DatabaseSessionRepository,
):
    """Test cleanup when no sessions are expired."""
    # Arrange
    session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    await repository.create_session(session)

    # Act
    deleted_count = await repository.cleanup_expired_sessions()

    # Assert
    assert deleted_count == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_expiry_check():
    """Test session expiry logic in domain model."""
    # Create expired session
    expired_session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    # Create active session
    active_session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    # Create no-expiry session
    no_expiry_session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=None,
    )

    # Assert
    assert expired_session.is_expired() is True
    assert expired_session.is_active() is False

    assert active_session.is_expired() is False
    assert active_session.is_active() is True

    assert no_expiry_session.is_expired() is False
    assert no_expiry_session.is_active() is True


# ============================================================
# In-Memory Repository Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inmemory_repository_create():
    """Test in-memory repository create."""
    repo = InMemorySessionRepository()
    session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
    )

    created = await repo.create_session(session)
    assert created.session_id == session.session_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inmemory_repository_get():
    """Test in-memory repository get."""
    repo = InMemorySessionRepository()
    session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
    )
    await repo.create_session(session)

    retrieved = await repo.get_session(session.session_id)
    assert retrieved is not None
    assert retrieved.session_id == session.session_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inmemory_repository_delete():
    """Test in-memory repository delete."""
    repo = InMemorySessionRepository()
    session = Session(
        session_id=str(uuid4()),
        user_id="test-user",
    )
    await repo.create_session(session)

    result = await repo.delete_session(session.session_id)
    assert result is True

    retrieved = await repo.get_session(session.session_id)
    assert retrieved is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inmemory_repository_clear():
    """Test in-memory repository clear."""
    repo = InMemorySessionRepository()

    # Create multiple sessions
    for i in range(3):
        session = Session(
            session_id=str(uuid4()),
            user_id="test-user",
        )
        await repo.create_session(session)

    # Clear all
    repo.clear()

    # Verify all cleared
    sessions = await repo.get_sessions_by_user("test-user")
    assert len(sessions) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inmemory_repository_cleanup_expired():
    """Test in-memory repository expired session cleanup."""
    repo = InMemorySessionRepository()

    # Create expired session
    expired = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await repo.create_session(expired)

    # Create active session
    active = Session(
        session_id=str(uuid4()),
        user_id="test-user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    await repo.create_session(active)

    # Cleanup
    deleted_count = await repo.cleanup_expired_sessions()

    # Assert
    assert deleted_count == 1
    assert await repo.get_session(expired.session_id) is None
    assert await repo.get_session(active.session_id) is not None
