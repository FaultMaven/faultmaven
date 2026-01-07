"""Service layer providers.

This module contains factory functions for business logic services:
- Case management services
- Investigation services
- Session services
- Knowledge services
- Data services
- Organization/Team services
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultmaven.container.base import BaseDIContainer
    from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)


def create_case_service(
    case_repository: Any | None,
    session_store: Any | None,
    report_store: Any | None,
    case_vector_store: Any | None,
    settings: FaultMavenSettings,
    minimal_factory: callable,
) -> Any:
    """Create case service for case persistence and management."""
    if not case_repository:
        logger.debug("Case service using minimal implementation (no repository)")
        return minimal_factory()

    try:
        from faultmaven.services.domain.case_service import CaseService

        service = CaseService(
            case_repository=case_repository,
            session_store=session_store,
            report_store=report_store,
            case_vector_store=case_vector_store,
            settings=settings,
        )
        logger.debug("Case service initialized with milestone-based repository")
        return service
    except Exception as e:
        logger.warning(f"Case service initialization failed: {e}")
        return minimal_factory()


def create_milestone_engine(
    llm_provider: Any,
    case_repository: Any | None,
) -> Any | None:
    """Create milestone engine for investigation workflow."""
    if not case_repository:
        return None

    try:
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        engine = MilestoneEngine(
            llm_provider=llm_provider,
            repository=case_repository,
            trace_enabled=True,
        )
        logger.debug("MilestoneEngine initialized")
        return engine
    except Exception as e:
        logger.warning(f"MilestoneEngine initialization failed: {e}")
        return None


def create_investigation_service(
    milestone_engine: Any | None,
    case_repository: Any | None,
) -> Any | None:
    """Create investigation service for workflow orchestration."""
    if not milestone_engine or not case_repository:
        logger.debug("InvestigationService skipped (missing dependencies)")
        return None

    try:
        from faultmaven.modules.agent.domain.services.investigation_service import InvestigationService

        service = InvestigationService(
            milestone_engine=milestone_engine,
            case_repository=case_repository,
        )
        logger.debug("InvestigationService initialized")
        return service
    except Exception as e:
        logger.warning(f"InvestigationService initialization failed: {e}")
        return None


def create_investigation_orchestrator(
    hypothesis_repository: Any | None,
    solution_repository: Any | None,
) -> Any | None:
    """Create investigation orchestrator for hypothesis/solution workflow."""
    if not hypothesis_repository or not solution_repository:
        logger.debug("InvestigationOrchestrator skipped (missing repositories)")
        return None

    try:
        from faultmaven.modules.agent.domain.services.investigation_orchestrator import InvestigationOrchestrator

        orchestrator = InvestigationOrchestrator(
            hypothesis_repo=hypothesis_repository,
            solution_repo=solution_repository,
        )
        logger.debug("InvestigationOrchestrator initialized")
        return orchestrator
    except Exception as e:
        logger.warning(f"InvestigationOrchestrator initialization failed: {e}")
        return None


def create_evidence_service(
    db_session: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create evidence service for evidence management (PR #46b).

    Args:
        db_session: Database session for repository
        settings: Application settings

    Returns:
        EvidenceService or None if dependencies unavailable
    """
    if not db_session:
        logger.debug("EvidenceService skipped (no database session)")
        return None

    try:
        from faultmaven.modules.evidence.domain.services import EvidenceService
        from faultmaven.modules.evidence.infrastructure import (
            EvidenceStorageAdapter,
            EvidenceRepository,
        )
        from faultmaven.services.file_storage_service import FileStorageService

        # Create file storage service
        file_storage = FileStorageService(
            storage_root=settings.evidence_storage_root,
            max_file_size_bytes=settings.max_evidence_file_size,
        )

        # Create storage adapter
        base_url = f"http://{settings.server.host}:{settings.server.port}"
        storage_adapter = EvidenceStorageAdapter(
            file_storage=file_storage,
            base_url=base_url,
        )

        # Create repository and service
        evidence_repository = EvidenceRepository(session=db_session)
        service = EvidenceService(
            storage_provider=storage_adapter,
            repository=evidence_repository,
        )
        logger.info("✅ EvidenceService initialized")
        return service
    except Exception as e:
        logger.warning(f"EvidenceService initialization failed: {e}")
        return None


def create_session_service(
    session_store: Any | None,
    case_service: Any,
    settings: FaultMavenSettings,
    minimal_factory: callable,
) -> Any:
    """Create session service for session management."""
    if not session_store:
        logger.info("Session store unavailable; using minimal session service")
        return minimal_factory()

    try:
        from faultmaven.modules.auth.domain.services.auth_session_service import AuthSessionService as SessionService

        service = SessionService(
            session_store=session_store,
            case_service=case_service,
            settings=settings,
        )
        logger.debug("SessionService initialized")
        return service
    except Exception as e:
        logger.warning(f"SessionService initialization failed: {e}")
        return minimal_factory()


def create_data_service(
    data_classifier: Any,
    log_processor: Any,
    sanitizer: Any,
    tracer: Any,
    session_service: Any,
    settings: FaultMavenSettings,
) -> Any:
    """Create data service for data processing and analysis."""
    from faultmaven.services.domain.data_service import DataService, SimpleStorageBackend

    storage_backend = SimpleStorageBackend(settings=settings)

    return DataService(
        data_classifier=data_classifier,
        log_processor=log_processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage_backend,
        session_service=session_service,
        settings=settings,
    )


def create_knowledge_service(
    vector_store: Any | None,
    session_store: Any | None,
    knowledge_ingester: Any | None,
    settings: FaultMavenSettings,
) -> Any:
    """Create knowledge service for knowledge base operations."""
    from faultmaven.modules.knowledge.domain.services.knowledge_service import KnowledgeService

    return KnowledgeService(
        vector_store=vector_store,
        session_store=session_store,
        knowledge_ingester=knowledge_ingester,
        settings=settings,
    )


def create_organization_service(
    db_session: Any | None,
    settings: FaultMavenSettings,
) -> tuple[Any | None, Any | None]:
    """Create organization service and its repository.

    Returns:
        Tuple of (organization_service, organization_repository)
    """
    if not db_session:
        logger.debug("OrganizationService skipped (no database session)")
        return None, None

    try:
        from faultmaven.services.domain.organization_service import OrganizationService
        from faultmaven.infrastructure.persistence.organization_repository import PostgreSQLOrganizationRepository

        repository = PostgreSQLOrganizationRepository(db_session)
        service = OrganizationService(
            organization_repository=repository,
            audit_repository=None,
            settings=settings,
        )
        logger.debug("OrganizationService initialized")
        return service, repository
    except Exception as e:
        logger.warning(f"OrganizationService initialization failed: {e}")
        return None, None


def create_team_service(
    db_session: Any | None,
    organization_repository: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create team service for team collaboration."""
    if not db_session or not organization_repository:
        logger.debug("TeamService skipped (missing dependencies)")
        return None

    try:
        from faultmaven.services.domain.team_service import TeamService
        from faultmaven.infrastructure.persistence.team_repository import PostgreSQLTeamRepository

        team_repository = PostgreSQLTeamRepository(db_session)
        service = TeamService(
            team_repository=team_repository,
            organization_repository=organization_repository,
            audit_repository=None,
            settings=settings,
        )
        logger.debug("TeamService initialized")
        return service
    except Exception as e:
        logger.warning(f"TeamService initialization failed: {e}")
        return None


def create_tenant_provider(
    db_session: Any | None,
    organization_repository: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create tenant provider for deployment neutrality."""
    if not db_session:
        logger.debug("TenantProvider skipped (no database session)")
        return None

    try:
        from faultmaven.providers.tenancy.factory import create_tenant_provider as factory

        provider = factory(organization_repository=organization_repository)
        logger.debug(f"TenantProvider initialized (mode: {settings.deployment_mode})")
        return provider
    except Exception as e:
        logger.warning(f"TenantProvider initialization failed: {e}")
        return None


def register_services(container: BaseDIContainer) -> None:
    """Register all services with the container.

    Args:
        container: The DI container to register services with
    """
    settings = container.settings

    logger.info("🔍 Services: Registering services...")

    # Get dependencies from container
    case_repository = getattr(container, "case_repository", None)
    session_store = container.get_service("session_store")
    report_store = getattr(container, "report_store", None)
    case_vector_store = getattr(container, "case_vector_store", None)
    hypothesis_repository = getattr(container, "hypothesis_repository", None)
    solution_repository = getattr(container, "solution_repository", None)
    db_session = getattr(container, "db_session", None)
    vector_store = container.get_service("vector_store")
    knowledge_ingester = getattr(container, "knowledge_ingester", None)

    # Case Service
    case_service = create_case_service(
        case_repository,
        session_store,
        report_store,
        case_vector_store,
        settings,
        container._create_minimal_case_service,
    )
    container._register_service("case_service", case_service)

    # Milestone Engine
    llm_provider = container.get_service("llm_provider")
    milestone_engine = create_milestone_engine(llm_provider, case_repository)
    container.milestone_engine = milestone_engine
    if milestone_engine:
        container._register_service("milestone_engine", milestone_engine)

    # Investigation Service
    investigation_service = create_investigation_service(milestone_engine, case_repository)
    container.investigation_service = investigation_service
    if investigation_service:
        container._register_service("investigation_service", investigation_service)

    # Investigation Orchestrator
    investigation_orchestrator = create_investigation_orchestrator(
        hypothesis_repository, solution_repository
    )
    container.investigation_orchestrator = investigation_orchestrator
    if investigation_orchestrator:
        container._register_service("investigation_orchestrator", investigation_orchestrator)

    # Evidence Service (PR #46b)
    evidence_service = create_evidence_service(db_session, settings)
    container.evidence_service = evidence_service
    if evidence_service:
        container._register_service("evidence_service", evidence_service)

    # Organization Service
    organization_service, organization_repository = create_organization_service(db_session, settings)
    container.organization_service = organization_service
    if organization_service:
        container._register_service("organization_service", organization_service)

    # Tenant Provider
    tenant_provider = create_tenant_provider(db_session, organization_repository, settings)
    container.tenant_provider = tenant_provider
    if tenant_provider:
        container._register_service("tenant_provider", tenant_provider)

    # Team Service
    team_service = create_team_service(db_session, organization_repository, settings)
    container.team_service = team_service
    if team_service:
        container._register_service("team_service", team_service)

    # Session Service
    session_service = create_session_service(
        session_store,
        case_service,
        settings,
        container._create_minimal_session_service,
    )
    container._register_service("session_service", session_service)

    # Agent Service (explicitly None for clean architecture - use InvestigationService)
    container.agent_service = None

    # Data Service
    data_service = create_data_service(
        container.get_service("data_classifier", required=True),
        container.get_service("log_processor", required=True),
        container.get_service("sanitizer", required=True),
        container.get_service("tracer", required=True),
        session_service,
        settings,
    )
    container._register_service("data_service", data_service)

    # Knowledge Service
    knowledge_service = create_knowledge_service(
        vector_store,
        session_store,
        knowledge_ingester,
        settings,
    )
    container._register_service("knowledge_service", knowledge_service)

    logger.info("✅ Service layer registered")
