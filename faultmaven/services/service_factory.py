"""Service Factory for API Layer (TASK-011, TASK-012, TASK-013)

Purpose: Factory for creating service instances with proper dependencies.

This factory provides a centralized point for creating service instances
with their required repository dependencies. It integrates with the
database session management to ensure proper transaction handling.

Usage:
    async with get_db_session() as session:
        factory = ServiceFactory(session)
        case_service = factory.create_case_service()
        session_service = factory.create_investigation_session_service()
        evidence_service = factory.create_evidence_artifact_service()
        # Use services for operations...
"""

from typing import TYPE_CHECKING, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    EvidenceArtifactRepository,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    STORAGE_TYPE_DATABASE,
    get_case_repository,
    get_evidence_artifact_repository,
    get_investigation_session_repository,
    get_knowledge_item_repository,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.services.case_service import APICaseService
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)

# Interface imports for clean architecture compliance
if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


class ServiceFactory:
    """Factory for creating service instances with proper dependencies.

    This factory creates services with injected repository dependencies,
    ensuring proper database session handling and consistent service
    configuration across the application.

    The factory is designed to be request-scoped, meaning a new factory
    should be created for each request with a fresh database session.

    Attributes:
        db_session: Database session for repositories
        case_repo: Case repository instance
        session_repo: Investigation session repository instance
        evidence_repo: Evidence artifact repository instance
        knowledge_repo: Knowledge item repository instance

    Example:
        async with get_db_session() as session:
            factory = ServiceFactory(session)
            case_service = factory.create_case_service()
            case = await case_service.get_case(case_id, organization_id)
    """

    def __init__(
        self, db_session: AsyncSession, tenant_provider: Optional[TenantProvider] = None
    ):
        """Initialize service factory.

        Args:
            db_session: Database session for repositories
            tenant_provider: Optional tenant provider for org resolution
        """
        self.db_session = db_session
        self.tenant_provider = tenant_provider

        # Create repositories with the provided session
        self.case_repo: CaseRepository = get_case_repository(
            storage_type=STORAGE_TYPE_DATABASE,
            session=db_session,
        )
        self.session_repo: InvestigationSessionRepository = (
            get_investigation_session_repository(
                storage_type=STORAGE_TYPE_DATABASE,
                session=db_session,
            )
        )
        self.evidence_repo: EvidenceArtifactRepository = (
            get_evidence_artifact_repository(
                storage_type=STORAGE_TYPE_DATABASE,
                session=db_session,
            )
        )
        self.knowledge_repo: KnowledgeItemRepository = get_knowledge_item_repository(
            storage_type=STORAGE_TYPE_DATABASE,
            session=db_session,
        )

    def create_case_service(self) -> APICaseService:
        """Create API case service with dependencies.

        Returns:
            APICaseService instance with injected repositories
        """
        return APICaseService(
            case_repo=self.case_repo,
            session_repo=self.session_repo,
            tenant_provider=self.tenant_provider,
        )

    def create_investigation_session_service(self) -> APIInvestigationSessionService:
        """Create API investigation session service with dependencies.

        Returns:
            APIInvestigationSessionService instance with injected repositories
        """
        return APIInvestigationSessionService(
            session_repo=self.session_repo,
            case_repo=self.case_repo,
        )

    def create_file_storage_service(
        self,
        storage_root: Optional[str] = None,
        max_file_size_bytes: Optional[int] = None,
        allowed_mime_types: Optional[list] = None,
    ) -> FileStorageService:
        """Create file storage service.

        Args:
            storage_root: Optional override for storage root directory
            max_file_size_bytes: Optional override for max file size
            allowed_mime_types: Optional override for allowed MIME types

        Returns:
            FileStorageService instance
        """
        settings = get_settings()

        return FileStorageService(
            storage_root=storage_root
            or getattr(settings, "evidence_storage_root", "./data/evidence"),
            max_file_size_bytes=max_file_size_bytes
            or getattr(settings, "max_evidence_file_size", 100 * 1024 * 1024),
            allowed_mime_types=allowed_mime_types
            or getattr(settings, "allowed_evidence_mime_types", []),
        )

    def create_evidence_artifact_service(
        self,
        file_storage: Optional[FileStorageService] = None,
    ) -> APIEvidenceArtifactService:
        """Create evidence artifact service with dependencies.

        Args:
            file_storage: Optional file storage service (creates default if not provided)

        Returns:
            APIEvidenceArtifactService instance with injected dependencies
        """
        if file_storage is None:
            file_storage = self.create_file_storage_service()

        return APIEvidenceArtifactService(
            case_repo=self.case_repo,
            file_storage=file_storage,
        )

    def create_agent_orchestration_service(
        self,
        team_service=None,
    ) -> "AgentOrchestrationService":  # noqa: F821
        """Create agent orchestration service with dependencies.

        Args:
            team_service: Optional team service for resolving user team memberships.

        Returns:
            AgentOrchestrationService instance with injected dependencies
        """
        from faultmaven.modules.agent.domain.services.agent_orchestration_service import (
            AgentOrchestrationService,
        )
        from faultmaven.modules.agent.tools.base import (
            tool_registry as agent_tool_registry,
        )

        return AgentOrchestrationService(
            case_repo=self.case_repo,
            session_service=self.create_investigation_session_service(),
            evidence_service=self.create_evidence_artifact_service(),
            tool_registry=agent_tool_registry,
            team_service=team_service,
            # LLM client will be created lazily by the service
        )

    def create_user_service(self) -> "UserService":  # noqa: F821
        """Create user management service.

        Returns:
            UserService instance with injected dependencies

        Note:
            UserService uses InMemoryUserRepository for development.
            In production, this should use PostgreSQLUserRepository.
        """
        from faultmaven.api.middleware.auth import get_auth_service
        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
            PostgreSQLUserRepository,
        )
        from faultmaven.modules.auth.domain.services.user_service import UserService

        # Use PostgreSQL for production, InMemory for development
        # For now, default to InMemory
        user_repo = InMemoryUserRepository()
        auth_service = get_auth_service()

        return UserService(
            user_repo=user_repo,
            auth_service=auth_service,
        )

    # Future service factory methods:

    # def create_knowledge_service(self) -> KnowledgeService:
    #     """Create knowledge service."""
    #     return KnowledgeService(
    #         knowledge_repo=self.knowledge_repo,
    #     )
