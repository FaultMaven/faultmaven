"""Unit tests for ServiceFactory class (TASK-011).

Tests the service factory functionality including:
- Factory initialization with db_session
- Repository creation
- Service creation
- Dependency injection
"""

from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.services.service_factory import ServiceFactory
from faultmaven.services.case_service import APICaseService
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    EvidenceArtifactRepository,
)
from faultmaven.infrastructure.persistence.agent_execution_repository import (
    AgentExecutionRepository,
)
from faultmaven.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)


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

    def test_factory_creates_evidence_repo(self, mock_session):
        """Test that factory creates evidence artifact repository."""
        factory = ServiceFactory(mock_session)

        assert factory.evidence_repo is not None
        assert isinstance(factory.evidence_repo, EvidenceArtifactRepository)

    def test_factory_creates_execution_repo(self, mock_session):
        """Test that factory creates agent execution repository."""
        factory = ServiceFactory(mock_session)

        assert factory.execution_repo is not None
        assert isinstance(factory.execution_repo, AgentExecutionRepository)

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

    def test_create_case_service_injects_evidence_repo(self, mock_session):
        """Test that case service has evidence_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_case_service()

        assert service.evidence_repo is not None
        assert service.evidence_repo is factory.evidence_repo

    def test_create_case_service_injects_execution_repo(self, mock_session):
        """Test that case service has execution_repo injected."""
        factory = ServiceFactory(mock_session)

        service = factory.create_case_service()

        assert service.execution_repo is not None
        assert service.execution_repo is factory.execution_repo


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
        assert service1.evidence_repo is service2.evidence_repo
        assert service1.execution_repo is service2.execution_repo


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
