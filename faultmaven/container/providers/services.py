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
    from faultmaven.config.settings import FaultMavenSettings
    from faultmaven.container.base import BaseDIContainer

logger = logging.getLogger(__name__)


def create_case_service(
    case_repository: Any | None,
    session_store: Any | None,
    case_vector_store: Any | None,
    settings: FaultMavenSettings,
    minimal_factory: callable,
    tenant_provider: Any | None = None,
) -> Any:
    """Create case service for case persistence and management."""
    if not case_repository:
        logger.debug("Case service using minimal implementation (no repository)")
        return minimal_factory()

    try:
        from faultmaven.modules.case.domain.services.case_service import CaseService

        service = CaseService(
            case_repository=case_repository,
            session_store=session_store,
            case_vector_store=case_vector_store,
            settings=settings,
            tenant_provider=tenant_provider,  # Inject TenantProvider for deployment-agnostic org resolution
        )
        logger.debug(
            f"Case service initialized with milestone-based repository and TenantProvider (tenant_provider={tenant_provider})"
        )
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
    preprocessing_service: Any | None = None,
    file_storage_service: Any | None = None,
) -> Any | None:
    """Create investigation service for workflow orchestration."""
    if not milestone_engine or not case_repository:
        logger.debug("InvestigationService skipped (missing dependencies)")
        return None

    try:
        from faultmaven.modules.agent.domain.services.investigation_service import (
            InvestigationService,
        )

        service = InvestigationService(
            milestone_engine=milestone_engine,
            case_repository=case_repository,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage_service,
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
        from faultmaven.modules.agent.domain.services.investigation_orchestrator import (
            InvestigationOrchestrator,
        )

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
    case_repository: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create evidence service for evidence management (migrated to use Case repository).

    Args:
        case_repository: Case repository (now handles standalone evidence)
        settings: Application settings

    Returns:
        EvidenceService or None if dependencies unavailable
    """
    if not case_repository:
        logger.debug("EvidenceService skipped (no Case repository)")
        return None

    try:
        from faultmaven.modules.evidence.domain.adapters import (
            EvidenceStorageAdapter,
        )
        from faultmaven.modules.evidence.domain.services import EvidenceService
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

        # Create service with Case repository (migrated from EvidenceRepository)
        service = EvidenceService(
            storage_provider=storage_adapter,
            case_repository=case_repository,
        )
        logger.info("✅ EvidenceService initialized (using Case repository)")
        return service
    except Exception as e:
        logger.warning(f"EvidenceService initialization failed: {e}")
        return None


def create_session_service(
    session_store: Any | None,
    settings: FaultMavenSettings,
    minimal_factory: callable,
) -> Any:
    """Create session service for session management."""
    if not session_store:
        logger.info("Session store unavailable; using minimal session service")
        return minimal_factory()

    try:
        from faultmaven.modules.auth.domain.services.auth_session_service import (
            AuthSessionService as SessionService,
        )

        service = SessionService(
            session_store=session_store,
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
    from faultmaven.modules.case.domain.services.case_data_ingestion_service import (
        CaseDataIngestionService,
        SimpleStorageBackend,
    )

    storage_backend = SimpleStorageBackend(settings=settings)

    return CaseDataIngestionService(
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
    knowledge_ingester: Any | None,
    sanitizer: Any,
    tracer: Any,
    llm_provider: Any | None,
    redis_client: Any | None,
    settings: FaultMavenSettings,
) -> Any:
    """Create knowledge service for knowledge base operations."""
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    return KnowledgeService(
        knowledge_ingester=knowledge_ingester,
        sanitizer=sanitizer,
        tracer=tracer,
        vector_store=vector_store,
        redis_client=redis_client,
        settings=settings,
        llm_provider=llm_provider,
    )


def create_organization_service(
    db_session: Any | None,
    settings: FaultMavenSettings,
) -> tuple[Any | None, Any | None]:
    """Create organization service and its repository.

    Returns:
        Tuple of (organization_service, organization_repository)
    """
    # Organization repository no longer needs a pre-created db_session
    # It will use get_db_session() per operation like SessionlessCaseRepository
    try:
        from faultmaven.infrastructure.persistence.sessionless_organization_repository import (
            SessionlessOrganizationRepository,
        )
        from faultmaven.modules.auth.domain.services.organization_service import (
            OrganizationService,
        )

        repository = SessionlessOrganizationRepository()
        service = OrganizationService(
            organization_repository=repository,
            audit_repository=None,
            settings=settings,
        )
        logger.debug("OrganizationService initialized with sessionless repository")
        return service, repository
    except Exception as e:
        logger.warning(f"OrganizationService initialization failed: {e}")
        import traceback

        traceback.print_exc()
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
        from faultmaven.infrastructure.persistence.team_repository import (
            PostgreSQLTeamRepository,
        )
        from faultmaven.modules.auth.domain.services.team_service import TeamService

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


def create_auth_service(
    redis_client: Any | None,
    settings: FaultMavenSettings,
) -> Any:
    """Create authentication service for JWT token operations.

    Args:
        redis_client: Redis client for token revocation tracking
        settings: Application settings

    Returns:
        AuthService instance (with or without Redis depending on availability)
    """
    try:
        from faultmaven.services.auth_service import AuthService

        # Load keys from settings if available
        private_key = None
        public_key = None
        if settings.security.jwt_private_key:
            private_key = settings.security.jwt_private_key.get_secret_value()
        if settings.security.jwt_public_key:
            public_key = settings.security.jwt_public_key

        service = AuthService(
            redis_client=redis_client,
            private_key=private_key,
            public_key=public_key,
        )
        if redis_client:
            logger.info(
                "✅ AuthService initialized with Redis (token revocation enabled)"
            )
        else:
            logger.info(
                "✅ AuthService initialized without Redis (token revocation disabled)"
            )
        return service
    except Exception as e:
        logger.warning(f"AuthService initialization failed: {e}")
        # Return a minimal AuthService without Redis
        from faultmaven.services.auth_service import AuthService

        return AuthService()


def create_user_service(
    auth_service: Any,
    redis_client: Any | None,
    db_session: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create user service for user management.

    Follows Composition Root principle: UserService receives its auth_service
    dependency via constructor injection, not via ServiceContainer.get().

    Args:
        auth_service: Auth service for JWT token operations (REQUIRED)
        redis_client: Redis client for token tracking
        db_session: Database session for persistence
        settings: Application settings

    Returns:
        UserService instance, or None if auth_service not available
    """
    if not auth_service:
        logger.warning("UserService skipped (no auth_service available)")
        return None

    try:
        from faultmaven.modules.auth.domain.services.user_service import UserService
        from faultmaven.modules.auth.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
            PostgreSQLUserRepository,
        )

        # Use PostgreSQL if db_session available, else InMemory for development
        if db_session:
            user_repo = PostgreSQLUserRepository(db_session)
            logger.debug("UserService using PostgreSQLUserRepository")
        else:
            user_repo = InMemoryUserRepository()
            logger.debug("UserService using InMemoryUserRepository (development)")

        service = UserService(
            user_repo=user_repo,
            auth_service=auth_service,  # Composition Root: injected, not fetched
            redis_client=redis_client,
        )
        logger.info("✅ UserService initialized with proper DI")
        return service
    except Exception as e:
        logger.warning(f"UserService initialization failed: {e}")
        return None


def create_tenant_provider(
    db_session: Any | None,
    organization_repository: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create tenant provider for deployment neutrality.

    Note: db_session parameter is deprecated and unused since we now use
    SessionlessOrganizationRepository. Kept for backward compatibility.
    """
    if not organization_repository:
        logger.debug("TenantProvider skipped (no organization repository)")
        return None

    try:
        from faultmaven.providers.tenancy.factory import (
            create_tenant_provider as factory,
        )

        provider = factory(organization_repository=organization_repository)
        logger.debug(
            f"TenantProvider initialized (tenant_provider: {settings.providers.tenant_provider})"
        )
        return provider
    except Exception as e:
        logger.warning(f"TenantProvider initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_report_generation_service(
    llm_router: Any | None,
    case_repository: Any | None,
    runbook_kb: Any | None,
    lock_manager: Any | None,
    pii_redactor: Any | None,
) -> Any | None:
    """Create report generation service (TD-001: migrated from IReportStore to CaseRepository)."""
    if not case_repository:
        logger.debug("ReportGenerationService skipped (no case repository)")
        return None

    try:
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        service = ReportGenerationService(
            llm_router=llm_router,
            case_repository=case_repository,
            runbook_kb=runbook_kb,
            lock_manager=lock_manager,
            pii_redactor=pii_redactor,
        )
        logger.debug("ReportGenerationService initialized")
        return service
    except Exception as e:
        logger.warning(f"ReportGenerationService initialization failed: {e}")
        return None


def create_oauth_code_repository(
    settings: FaultMavenSettings,
    cache_client: Any = None,
) -> Any:
    """Create OAuth code repository based on deployment.

    Authorization codes are ephemeral (10 min) and should use cache layer only.
    Database persistence is optional for compliance/audit (write-only).

    Args:
        settings: FaultMavenSettings instance
        cache_client: Redis client for cloud, None for local (uses in-memory)

    Returns:
        OAuth code repository instance (cache layer only)
    """
    # Determine if we're in cloud or local deployment
    is_cloud = cache_client is not None

    if is_cloud:
        # Cloud deployment: Use Redis cache
        from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
            RedisOAuthCodeRepository,
        )

        return RedisOAuthCodeRepository(cache_client)
    else:
        # Local deployment: Use in-memory cache
        from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
            InMemoryOAuthCodeRepository,
        )

        return InMemoryOAuthCodeRepository()


def create_token_revocation_store(
    settings: FaultMavenSettings,
    cache_client: Any = None,
) -> Any:
    """Create token revocation store based on deployment.

    Revoked tokens are tracked with TTL (matching token expiration).
    Uses cache layer only for ephemeral tracking.

    Args:
        settings: FaultMavenSettings instance
        cache_client: Redis client for cloud, None for local (uses in-memory)

    Returns:
        Token revocation store instance (cache layer only)
    """
    # Determine if we're in cloud or local deployment
    is_cloud = cache_client is not None

    if is_cloud:
        # Cloud deployment: Use Redis cache
        from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
            RedisTokenRevocationStore,
        )

        return RedisTokenRevocationStore(cache_client)
    else:
        # Local deployment: Use in-memory cache
        from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
            InMemoryTokenRevocationStore,
        )

        return InMemoryTokenRevocationStore()


def create_jwt_token_generator(
    settings: FaultMavenSettings,
    revocation_store: Any,
) -> Any:
    """Create JWT token generator with RS256 signing.

    Args:
        settings: FaultMavenSettings instance
        revocation_store: Token revocation tracking store

    Returns:
        RS256JWTTokenGenerator instance
    """
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        RS256JWTTokenGenerator,
    )

    # Load RSA key pair from settings (from security section, not auth)
    private_key = None
    public_key = None
    if settings.security.jwt_private_key:
        private_key = settings.security.jwt_private_key.get_secret_value()
    if settings.security.jwt_public_key:
        public_key = settings.security.jwt_public_key

    return RS256JWTTokenGenerator(
        private_key=private_key,
        public_key=public_key,
        revocation_store=revocation_store,
        settings=settings.security,  # Pass security settings, not auth settings
    )


def create_oauth_service(
    settings: FaultMavenSettings,
    user_repository: Any,
    code_repository: Any,
    token_generator: Any,
) -> Any | None:
    """Create OAuth service based on configuration.

    Args:
        settings: FaultMavenSettings instance
        user_repository: User repository for user lookups
        code_repository: OAuth code storage (cache layer: in-memory or Redis)
        token_generator: JWT token generator (RS256)

    Returns:
        OAuthServiceImpl instance or None if OAuth disabled
    """
    from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl

    # Check if OAuth enabled
    if not settings.auth.oauth_enabled:
        logger.info("OAuth service disabled (using dev-login)")
        return None

    return OAuthServiceImpl(
        code_repository=code_repository,
        user_repository=user_repository,
        token_generator=token_generator,
        settings=settings.auth,
    )


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
    case_vector_store = getattr(container, "case_vector_store", None)
    hypothesis_repository = getattr(container, "hypothesis_repository", None)
    solution_repository = getattr(container, "solution_repository", None)
    db_session = getattr(container, "db_session", None)
    vector_store = container.get_service("vector_store")
    knowledge_ingester = getattr(container, "knowledge_ingester", None)
    redis_client = getattr(container, "redis_client", None)

    # Auth Service (JWT token operations with optional Redis for revocation)
    auth_service = create_auth_service(redis_client, settings)
    container.auth_service = auth_service
    container._register_service("auth_service", auth_service)

    # User Service (Composition Root: auth_service injected via constructor)
    user_service = create_user_service(auth_service, redis_client, db_session, settings)
    container.user_service = user_service
    if user_service:
        container._register_service("user_service", user_service)

    # OAuth Service (if enabled)
    if settings.auth.oauth_enabled:
        logger.info("Registering OAuth service...")

        # Create OAuth code repository (cache layer only)
        oauth_code_repository = create_oauth_code_repository(
            settings,
            cache_client=redis_client,
        )
        container.oauth_code_repository = oauth_code_repository
        container._register_service("oauth_code_repository", oauth_code_repository)

        # Create token revocation store (cache layer only)
        token_revocation_store = create_token_revocation_store(
            settings,
            cache_client=redis_client,
        )
        container.token_revocation_store = token_revocation_store
        container._register_service("token_revocation_store", token_revocation_store)

        # Create JWT token generator
        jwt_token_generator = create_jwt_token_generator(
            settings,
            revocation_store=token_revocation_store,
        )
        container.jwt_token_generator = jwt_token_generator
        container._register_service("jwt_token_generator", jwt_token_generator)

        # Create OAuth service (uses user_store for dev-login compatibility)
        # Note: user_store is the same store used by dev-login authentication
        # This ensures OAuth can find users created via dev-login
        user_store = getattr(container, "user_store", None)
        if user_store:
            oauth_service = create_oauth_service(
                settings,
                user_repository=user_store,  # Use user_store, not user_repo
                code_repository=oauth_code_repository,
                token_generator=jwt_token_generator,
            )
            container.oauth_service = oauth_service
            container._register_service("oauth_service", oauth_service)

            logger.info(
                "✅ OAuth service registered (cache: %s)",
                "Redis" if redis_client else "in-memory",
            )
        else:
            logger.warning("OAuth service skipped (no user_store available)")
    else:
        logger.info("OAuth service disabled (using dev-login mode)")

    # Organization Service (create first, before TenantProvider which depends on organization_repository)
    organization_service, organization_repository = create_organization_service(
        db_session, settings
    )
    container.organization_service = organization_service
    if organization_service:
        container._register_service("organization_service", organization_service)

    # Tenant Provider (create after OrganizationService, before CaseService)
    tenant_provider = create_tenant_provider(
        db_session, organization_repository, settings
    )
    container.tenant_provider = tenant_provider
    if tenant_provider:
        container._register_service("tenant_provider", tenant_provider)

    # Case Service (with TenantProvider injection for deployment-agnostic org resolution)
    case_service = create_case_service(
        case_repository,
        session_store,
        case_vector_store,
        settings,
        container._create_minimal_case_service,
        tenant_provider=tenant_provider,  # Inject TenantProvider
    )
    container._register_service("case_service", case_service)

    # Milestone Engine
    llm_provider = container.get_service("llm_provider")
    milestone_engine = create_milestone_engine(llm_provider, case_repository)
    container.milestone_engine = milestone_engine
    if milestone_engine:
        container._register_service("milestone_engine", milestone_engine)

    # Investigation Service
    preprocessing_service = container.get_service("preprocessing_service")
    investigation_service = create_investigation_service(
        milestone_engine, case_repository, preprocessing_service
    )
    container.investigation_service = investigation_service
    if investigation_service:
        container._register_service("investigation_service", investigation_service)

    # Investigation Orchestrator
    investigation_orchestrator = create_investigation_orchestrator(
        hypothesis_repository, solution_repository
    )
    container.investigation_orchestrator = investigation_orchestrator
    if investigation_orchestrator:
        container._register_service(
            "investigation_orchestrator", investigation_orchestrator
        )

    # Evidence Service (migrated to use Case repository)
    evidence_service = create_evidence_service(case_repository, settings)
    container.evidence_service = evidence_service
    if evidence_service:
        container._register_service("evidence_service", evidence_service)

    # Team Service (organization_repository already created above)
    team_service = create_team_service(db_session, organization_repository, settings)
    container.team_service = team_service
    if team_service:
        container._register_service("team_service", team_service)

    # Session Service
    session_service = create_session_service(
        session_store,
        settings,
        container._create_minimal_session_service,
    )
    container._register_service("session_service", session_service)

    # Agent Service
    # Backward-compatible alias: expose InvestigationService as agent_service.
    # If the investigation service can't be constructed (missing deps), still provide a
    # minimal non-null placeholder so callers/tests don't explode during init.
    agent_service = (
        investigation_service if investigation_service is not None else object()
    )
    container.agent_service = agent_service
    container._register_service("agent_service", agent_service)

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
        knowledge_ingester,
        container.get_service("sanitizer", required=True),
        container.get_service("tracer", required=True),
        container.get_service("llm_provider", required=False),
        redis_client,
        settings,
    )
    container._register_service("knowledge_service", knowledge_service)

    # Report Generation Service (TD-001: migrated from IReportStore to CaseRepository)
    llm_provider = container.get_service("llm_provider")
    runbook_kb = getattr(container, "runbook_kb", None)
    lock_manager = getattr(container, "lock_manager", None)
    pii_redactor = getattr(container, "pii_redactor", None)
    report_generation_service = create_report_generation_service(
        llm_router=llm_provider,
        case_repository=case_repository,
        runbook_kb=runbook_kb,
        lock_manager=lock_manager,
        pii_redactor=pii_redactor,
    )
    container.report_generation_service = report_generation_service
    if report_generation_service:
        container._register_service(
            "report_generation_service", report_generation_service
        )

    # Report Recommendation Service (optional - may not be implemented yet)
    # TODO: Implement create_report_recommendation_service if needed
    container.report_recommendation_service = None
    container._register_service("report_recommendation_service", None)

    logger.info("✅ Service layer registered")
