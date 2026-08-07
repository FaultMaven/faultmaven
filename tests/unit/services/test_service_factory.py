"""Unit tests for ServiceFactory class (TASK-011, TASK-012, TASK-013).

Tests the service factory functionality including:
- Factory initialization with db_session
- Repository creation
- Service creation
- Dependency injection
- Investigation session service creation (TASK-012)
- File storage service creation (TASK-013)
- Evidence artifact service creation (TASK-013)
"""

from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.services.api_case_service import APICaseService
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

# APIEvidenceArtifactService import removed in storage redesign 2026-04 phase 2.
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

# EvidenceArtifactRepository removed - evidence now handled by ICaseRepository (TD-001)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)
from faultmaven.services.service_factory import ServiceFactory

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create in-memory SQLite engine for tests."""
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
def mock_session():
    """Create mock AsyncSession for tests that don't need a real database."""
    return MagicMock(spec=AsyncSession)


# ============================================================
# Initialization Tests
# ============================================================


class TestServiceFactoryInitialization:
    """Test ServiceFactory initialization."""

    def test_factory_initialization_with_session(self, mock_session):
        """Test factory initialization with database session."""
        factory = ServiceFactory(mock_session)

        assert factory.db_session is mock_session

    def test_factory_creates_case_repo(self, mock_session):
        """Test that factory creates case repository."""
        factory = ServiceFactory(mock_session)

        assert factory.case_repo is not None
        assert isinstance(factory.case_repo, CaseRepository)

    def test_factory_creates_session_repo(self, mock_session):
        """Test that factory creates investigation session repository."""
        factory = ServiceFactory(mock_session)

        assert factory.session_repo is not None
        assert isinstance(factory.session_repo, InvestigationSessionRepository)

    # Note: evidence_repo and execution_repo removed from ServiceFactory - evidence
    # and agent executions are now handled by case_repo (ICaseRepository) as part
    # of TD-001 migration. The factory no longer creates separate repositories for
    # these entities.

    def test_factory_creates_knowledge_repo(self, mock_session):
        """Test that factory creates knowledge item repository."""
        factory = ServiceFactory(mock_session)

        assert factory.knowledge_repo is not None
        assert isinstance(factory.knowledge_repo, KnowledgeItemRepository)


# ============================================================
# Service Creation Tests
# ============================================================


class TestServiceCreation:
    """Test service creation methods."""

    def test_create_case_service_returns_api_case_service(self, mock_session):
        """Test that create_case_service returns APICaseService."""
        factory = ServiceFactory(mock_session)

        service = factory.create_case_service()

        assert service is not None
        assert isinstance(service, APICaseService)

    def test_create_case_service_injects_case_repo(self, mock_session):
        """Test that case service has case_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_case_service()

        assert service.case_repo is not None
        assert service.case_repo is factory.case_repo

    def test_create_case_service_injects_session_repo(self, mock_session):
        """Test that case service has session_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_case_service()

        assert service.session_repo is not None
        assert service.session_repo is factory.session_repo

    # Note: evidence_repo and execution_repo removed - evidence and agent executions
    # are now handled by case_repo (ICaseRepository) as part of TD-001 migration


# ============================================================
# Investigation Session Service Creation Tests (TASK-012)
# ============================================================


class TestInvestigationSessionServiceCreation:
    """Test investigation session service creation methods."""

    def test_create_investigation_session_service_returns_service(self, mock_session):
        """Test that create_investigation_session_service returns APIInvestigationSessionService."""
        factory = ServiceFactory(mock_session)

        service = factory.create_investigation_session_service()

        assert service is not None
        assert isinstance(service, APIInvestigationSessionService)

    def test_create_investigation_session_service_injects_session_repo(
        self, mock_session
    ):
        """Test that session service has session_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_investigation_session_service()

        assert service.session_repo is not None
        assert service.session_repo is factory.session_repo

    # Note: execution_repo removed - agent executions are now handled by case_repo
    # (ICaseRepository) as part of TD-001 migration

    def test_create_investigation_session_service_injects_case_repo(self, mock_session):
        """Test that session service has case_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_investigation_session_service()

        assert service.case_repo is not None
        assert service.case_repo is factory.case_repo

    def test_multiple_investigation_session_service_instances(self, mock_session):
        """Test creating multiple investigation session service instances."""
        factory = ServiceFactory(mock_session)

        service1 = factory.create_investigation_session_service()
        service2 = factory.create_investigation_session_service()

        # Each call creates a new instance
        assert service1 is not service2

    def test_investigation_session_services_share_repos(self, mock_session):
        """Test that multiple session services share the same repositories."""
        factory = ServiceFactory(mock_session)

        service1 = factory.create_investigation_session_service()
        service2 = factory.create_investigation_session_service()

        # Repositories are shared
        assert service1.session_repo is service2.session_repo
        assert service1.case_repo is service2.case_repo


# ============================================================
# Multiple Service Creation Tests
# ============================================================


class TestMultipleServiceCreation:
    """Test creating multiple services."""

    def test_multiple_case_service_instances(self, mock_session):
        """Test creating multiple case service instances."""
        factory = ServiceFactory(mock_session)

        service1 = factory.create_case_service()
        service2 = factory.create_case_service()

        # Each call creates a new instance
        assert service1 is not service2

    def test_services_share_same_repos(self, mock_session):
        """Test that multiple services share the same repositories."""
        factory = ServiceFactory(mock_session)

        service1 = factory.create_case_service()
        service2 = factory.create_case_service()

        # Repositories are shared
        assert service1.case_repo is service2.case_repo
        assert service1.session_repo is service2.session_repo
        # Note: evidence_repo removed - evidence now handled by case_repo (TD-001)


# ============================================================
# Integration with Real Session Tests
# ============================================================


class TestServiceFactoryIntegration:
    """Test ServiceFactory with real database session."""

    @pytest.mark.asyncio
    async def test_factory_with_real_session(self, async_session):
        """Test factory works with real async session."""
        factory = ServiceFactory(async_session)

        assert factory.db_session is async_session
        assert factory.case_repo is not None

    @pytest.mark.asyncio
    async def test_case_service_with_real_session(self, async_session):
        """Test case service works with real session."""
        factory = ServiceFactory(async_session)
        service = factory.create_case_service()

        # Service should be functional
        assert service is not None
        assert service.service_name == "api_case_service"

    @pytest.mark.asyncio
    async def test_case_service_can_list_cases(self, async_session):
        """Test case service can list cases (empty db)."""
        factory = ServiceFactory(async_session)
        service = factory.create_case_service()

        # Should not raise, returns empty list
        cases = await service.list_cases("test_org")
        assert cases == []

    @pytest.mark.asyncio
    async def test_investigation_session_service_with_real_session(self, async_session):
        """Test investigation session service works with real session."""
        factory = ServiceFactory(async_session)
        service = factory.create_investigation_session_service()

        # Service should be functional
        assert service is not None
        assert service.service_name == "api_investigation_session_service"

    @pytest.mark.asyncio
    async def test_investigation_session_service_has_all_repos(self, async_session):
        """Test investigation session service has all required repositories."""
        factory = ServiceFactory(async_session)
        service = factory.create_investigation_session_service()

        assert service.session_repo is not None
        assert service.case_repo is not None


# ============================================================
# File Storage Service Creation Tests (TASK-013)
# ============================================================


class TestFileStorageServiceCreation:
    """Test file storage service creation methods."""

    def test_create_file_storage_service_returns_service(self, mock_session):
        """Test that create_file_storage_service returns FileStorageService."""
        factory = ServiceFactory(mock_session)

        service = factory.create_file_storage_service()

        assert service is not None
        assert isinstance(service, FileStorageService)

    def test_create_file_storage_service_uses_configured_backend(self, mock_session):
        """The factory must not choose a storage root of its own.

        Storage location is the backend's business, resolved from
        STORAGE_BACKEND — a factory that passed its own root is how
        STORAGE_BACKEND=s3 became inert (#689).

        The real factory is patched rather than invoked: building the true
        singleton would bind this unit test to global state shared with every
        other test in the session.
        """
        factory = ServiceFactory(mock_session)
        sentinel = MagicMock()

        with patch(
            "faultmaven.infrastructure.storage.factory.get_storage_backend",
            return_value=sentinel,
        ):
            service = factory.create_file_storage_service()

        assert service.backend is sentinel

    def test_create_file_storage_service_with_custom_max_size(self, mock_session):
        """Test file storage service with custom max file size."""
        factory = ServiceFactory(mock_session)
        custom_size = 50 * 1024 * 1024  # 50MB

        service = factory.create_file_storage_service(max_file_size_bytes=custom_size)

        assert service.max_file_size_bytes == custom_size

    def test_create_file_storage_service_with_mime_types(self, mock_session):
        """Test file storage service with allowed MIME types."""
        factory = ServiceFactory(mock_session)
        mime_types = ["image/png", "image/jpeg"]

        service = factory.create_file_storage_service(allowed_mime_types=mime_types)

        assert service.allowed_mime_types == mime_types

    def test_create_file_storage_service_multiple_instances(self, mock_session):
        """Test that each call creates a new file storage instance."""
        factory = ServiceFactory(mock_session)

        service1 = factory.create_file_storage_service()
        service2 = factory.create_file_storage_service()

        assert service1 is not service2
