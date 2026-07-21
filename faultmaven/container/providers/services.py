"""Service layer providers.

This module contains factory functions for business logic services:
- Case management services
- Investigation services
- Session services
- Knowledge services
- Data services
- Organization/Enterprise repositories (tenancy substrate; management lives in cloud)
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
    team_service: Any | None = None,
    share_repository: Any | None = None,
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
            team_service=team_service,  # Team-membership resolution (None in standalone)
            share_repository=share_repository,  # Case read allowlist source (ADR-013 §D4)
        )
        logger.debug("Case service initialized with milestone-based repository")
        return service
    except Exception as e:
        logger.warning(f"Case service initialization failed: {e}")
        return minimal_factory()


def create_milestone_engine(
    llm_provider: Any,
    case_repository: Any | None,
    investigation_tools: Any,
    da_provider: Any | None = None,
    da_model: str | None = None,
    sanitizer: Any | None = None,
    redis_client: Any | None = None,
) -> Any | None:
    """Create milestone engine for investigation workflow.

    Args:
        llm_provider: LLM provider (ILLMProvider interface)
        case_repository: Case persistence layer (required)
        investigation_tools: AgentToolRegistry with investigation tools (required)
        da_provider: Dedicated provider for DA (directed analysis) turns
            (configured via DA_PROVIDER). Falls back to llm_provider when None.
        da_model: Model to use with da_provider (e.g., claude-sonnet-4-5).
        sanitizer: DataSanitizer for case-scoped PII redaction at LLM boundary.
        redis_client: Async Redis client for persisting redaction registries.
    """
    if not case_repository:
        return None

    try:
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        engine = MilestoneEngine(
            llm_provider=llm_provider,
            repository=case_repository,
            investigation_tools=investigation_tools,
            da_provider=da_provider,
            da_model=da_model,
            sanitizer=sanitizer,
            redis_client=redis_client,
            trace_enabled=True,
        )
        logger.debug("MilestoneEngine initialized with investigation tools")
        return engine
    except Exception as e:
        logger.warning(f"MilestoneEngine initialization failed: {e}")
        return None


def _create_investigation_tools(container: "BaseDIContainer") -> Any | None:
    """Create an AgentToolRegistry with investigation tools for MilestoneEngine.

    Registers tools that the LLM can call during the tool loop:
    - search_file: keyword/regex search on raw evidence files
    - deep_analysis: LLM-interpreted analysis of evidence files
    - web_search: trusted domain web search (Google CSE or Tavily)
    - kb_qa: unified knowledge base Q&A (all accessible scopes)

    Returns:
        AgentToolRegistry with investigation tools, or None if no tools available.
    """
    from faultmaven.modules.agent.tools.base import AgentToolRegistry

    registry = AgentToolRegistry()
    tool_count = 0

    search_file_tool = getattr(container, "search_file_tool", None)
    if search_file_tool:
        registry.register(search_file_tool)
        tool_count += 1

    deep_analysis_tool = getattr(container, "deep_analysis_tool", None)
    if deep_analysis_tool:
        registry.register(deep_analysis_tool)
        tool_count += 1

    web_search_tool = getattr(container, "web_search_tool", None)
    if web_search_tool:
        registry.register(web_search_tool)
        tool_count += 1

    kb_adapter = getattr(container, "kb_adapter", None)
    if kb_adapter:
        registry.register(kb_adapter)
        tool_count += 1
    else:
        logger.warning(
            "kb_adapter not available — KB runbook search disabled for investigations"
        )

    vectorize_file_tool = getattr(container, "vectorize_file_tool", None)
    if vectorize_file_tool:
        registry.register(vectorize_file_tool)
        tool_count += 1

    case_evidence_qa_adapter = getattr(container, "case_evidence_qa_adapter", None)
    if case_evidence_qa_adapter:
        registry.register(case_evidence_qa_adapter)
        tool_count += 1

    if tool_count == 0:
        logger.warning("Investigation tools: none available, skipping")
        return None

    tool_names = [t.name for t in registry.get_all_tools()]
    logger.info(f"✅ DA tool registry: {tool_count} tools registered: {tool_names}")
    return registry


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
    db_session_factory: Any | None = None,
    share_repository: Any | None = None,
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
        db_session_factory=db_session_factory,
        share_repository=share_repository,
    )


def create_organization_repository() -> Any | None:
    """Create the sessionless organization repository.

    The core keeps only the organization *repository* (the substrate the
    tenant factory + SingleTenantProvider need to resolve/stamp the implicit
    default org). Organization *management* (OrganizationService + routes) is
    the hosted admin composed module (ADR-010 D4), so the core does not
    construct a service here.
    """
    try:
        from faultmaven.infrastructure.persistence.sessionless_organization_repository import (
            SessionlessOrganizationRepository,
        )

        repository = SessionlessOrganizationRepository()
        logger.debug("OrganizationRepository initialized (sessionless)")
        return repository
    except Exception as e:
        logger.warning(f"OrganizationRepository initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_enterprise_repository() -> Any | None:
    """Create the sessionless enterprise repository.

    Mirrors create_organization_repository (repository only). There is no
    EnterpriseService — enterprise CRUD in the core is bootstrap-only
    (single-tenant default-enterprise seeding); enterprise management is the
    hosted admin composed module (ADR-010 D4).
    """
    try:
        from faultmaven.infrastructure.persistence.sessionless_enterprise_repository import (
            SessionlessEnterpriseRepository,
        )

        repository = SessionlessEnterpriseRepository()
        logger.debug("EnterpriseRepository initialized (sessionless)")
        return repository
    except Exception as e:
        logger.warning(f"EnterpriseRepository initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_team_repository() -> Any | None:
    """Create the sessionless team repository.

    Mirrors create_organization_repository (repository only). The core ships the
    team repository *substrate* — used by the single-tenant default-team
    bootstrap and by KB team-scope resolution (TeamService). Team *management*
    (create/invite from a UI) is the hosted admin composed module, which drives
    the same repository (ADR-010 D4 / ADR-013).
    """
    try:
        from faultmaven.infrastructure.persistence.sessionless_team_repository import (
            SessionlessTeamRepository,
        )

        repository = SessionlessTeamRepository()
        logger.debug("TeamRepository initialized (sessionless)")
        return repository
    except Exception as e:
        logger.warning(f"TeamRepository initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_team_service(
    tenant_provider: Any | None,
    team_repository: Any | None,
) -> Any | None:
    """Create the team service (KB team-scope resolution), or None.

    Gated on multi-tenant mode (ADR-013): team collaboration is a Cloud feature,
    so the resolver is wired only when the tenant provider is multi-tenant.
    Standalone (single-tenant) leaves ``team_service`` unwired — the two
    consumers (agent retrieval + KB inventory route) then skip team resolution
    and KB scope collapses to ``personal ∪ global``.

    In Standalone (single-tenant) deployments this returns None; the resolver is
    live only under the multi-tenant provider (Cloud). The resolver itself is
    exercised directly by unit tests.
    """
    if team_repository is None or tenant_provider is None:
        return None

    from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

    if isinstance(tenant_provider, SingleTenantProvider):
        logger.debug("TeamService skipped (single-tenant; team collaboration inert)")
        return None

    from faultmaven.modules.auth.domain.services.team_service import TeamService

    logger.debug("TeamService initialized (multi-tenant)")
    return TeamService(team_repository)


def create_share_repository() -> Any | None:
    """Create the sessionless resource-share repository.

    The share table (ADR-013 §D4) is the single source of truth for team
    visibility of runbooks/cases/drafts. Unlike ``team_service``, the repository
    is created in BOTH deployment modes: the read path resolves the
    "shared-to-my-teams" allowlist arm through it (empty in standalone, where a
    user resolves to no teams) and the write path creates share rows when a
    resource is published to a team.
    """
    try:
        from faultmaven.infrastructure.persistence.sessionless_share_repository import (
            SessionlessShareRepository,
        )

        repository = SessionlessShareRepository()
        logger.debug("ShareRepository initialized (sessionless)")
        return repository
    except Exception as e:
        logger.warning(f"ShareRepository initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_auth_service(
    revocation_store: Any | None,
    settings: FaultMavenSettings,
) -> Any:
    """Create authentication service for JWT token operations.

    Args:
        revocation_store: The deployment-wide token revocation store (#767 —
            the same instance every revoke path writes to)
        settings: Application settings

    Returns:
        AuthService instance
    """
    try:
        from faultmaven.modules.auth.domain.services.auth_service import AuthService

        # Load keys from settings if available
        private_key = None
        public_key = None
        if settings.security.jwt_private_key:
            private_key = settings.security.jwt_private_key.get_secret_value()
        if settings.security.jwt_public_key:
            public_key = settings.security.jwt_public_key

        service = AuthService(
            revocation_store=revocation_store,
            private_key=private_key,
            public_key=public_key,
        )
        if revocation_store:
            logger.info(
                "✅ AuthService initialized with revocation store (token revocation enabled)"
            )
        else:
            logger.info(
                "✅ AuthService initialized without revocation store (token revocation disabled)"
            )
        return service
    except Exception as e:
        logger.warning(f"AuthService initialization failed: {e}")
        # Return a minimal AuthService without a revocation store
        from faultmaven.modules.auth.domain.services.auth_service import AuthService

        return AuthService()


def create_user_service(
    auth_service: Any,
    redis_client: Any | None,
    settings: FaultMavenSettings,
) -> Any | None:
    """Create user service for user management.

    Follows Composition Root principle: UserService receives its auth_service
    dependency via constructor injection, not a service-locator lookup.

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
        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
            SessionlessUserRepository,
        )
        from faultmaven.modules.auth.domain.services.user_service import UserService

        # Use the persistent database when one is configured, else InMemory for
        # ephemeral/no-database development. Keyed off the configured
        # DATABASE_URL — NOT a shared session handle. The sessionless repo opens
        # a fresh session per operation (Principle 5, #703 fix); it never holds
        # a process-lifetime session that could leak idle-in-transaction.
        database_url = settings.database.database_url or ""
        if database_url and database_url != ":memory:":
            user_repo = SessionlessUserRepository()
            logger.debug("UserService using SessionlessUserRepository")
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
    organization_repository: Any | None,
    settings: FaultMavenSettings,
    enterprise_repository: Any | None = None,
    team_repository: Any | None = None,
) -> Any | None:
    """Create tenant provider for deployment neutrality.

    enterprise_repository is optional; when present (single-tenant mode), the
    SingleTenantProvider uses it for default-enterprise bootstrap. Absence is
    safe — migration 006 also seeds the default enterprise idempotently, so
    bootstrap simply skips the runtime check.

    team_repository is optional; when present (single-tenant mode), the
    SingleTenantProvider uses it to seed the default team row. Absence is safe —
    bootstrap simply skips team seeding.
    """
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        TenancyConfigurationError,
        requested_tenant_provider,
    )
    from faultmaven.providers.tenancy.factory import create_tenant_provider as factory

    if not organization_repository:
        if requested_tenant_provider() == BUILTIN_MULTI:
            # Skipping here would bypass the factory's fail-closed checks and
            # leave a multi-tenant deployment without its tenant provider.
            raise TenancyConfigurationError(
                "TENANT_PROVIDER='multi' requires an organization repository; "
                "refusing to continue without one."
            )
        logger.debug("TenantProvider skipped (no organization repository)")
        return None

    try:
        provider = factory(
            organization_repository=organization_repository,
            enterprise_repository=enterprise_repository,
            team_repository=team_repository,
        )
        logger.debug(
            f"TenantProvider initialized (tenant_provider: {settings.providers.tenant_provider}, "
            f"enterprise_repo={'yes' if enterprise_repository else 'no'})"
        )
        return provider
    except TenancyConfigurationError:
        # Fatal misconfiguration (e.g. an unrecognized TENANT_PROVIDER value).
        # Never degrade to None — fail closed on EVERY container path (jobs/CLI
        # workers, not just the web lifespan that also runs the coherence gate).
        raise
    except Exception as e:
        logger.warning(f"TenantProvider initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def create_report_generation_service(
    case_repository: Any | None,
    lock_manager: Any | None,
    pii_redactor: Any | None,
) -> Any | None:
    """Create report generation service for terminal summaries."""
    if not case_repository:
        logger.debug("ReportGenerationService skipped (no case repository)")
        return None

    try:
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        service = ReportGenerationService(
            case_repository=case_repository,
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
    """Create the deployment-wide token revocation store (real or FakeRedis).

    Revoked tokens are tracked with TTL (matching token expiration). This is
    the SINGLE revocation store (#767): every revoke path writes to it and the
    request-path check reads from it, all under one key prefix.

    Args:
        settings: FaultMavenSettings instance
        cache_client: Async Redis-compatible client (always provided)

    Returns:
        RedisTokenRevocationStore instance
    """
    from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
        RedisTokenRevocationStore,
    )

    return RedisTokenRevocationStore(
        cache_client,
        key_prefix=settings.security.token_revocation_prefix,
    )


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
        issuer=settings.security.jwt_issuer,
        audience=settings.security.jwt_audience,
    )


def create_sso_identity_provider(
    settings: FaultMavenSettings,
) -> Any | None:
    """Create the cloud SSO identity provider, or None when SSO is unconfigured.

    Returns None unless ``auth_mode=oauth`` and WorkOS is fully configured, so
    standalone/local deployments never import the vendor SDK (the adapter is
    imported lazily only on the configured path). See ADR-015.

    Args:
        settings: FaultMavenSettings instance

    Returns:
        An ISSOIdentityProvider (WorkOS AuthKit), or None when SSO is off.
    """
    auth = settings.auth
    if not auth.sso_configured:
        return None

    from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
        WorkOSIdentityProvider,
    )

    # sso_configured guarantees these are present and non-empty.
    provider = WorkOSIdentityProvider.from_config(
        api_key=auth.workos_api_key.get_secret_value(),
        client_id=auth.workos_client_id,
        redirect_uri=auth.workos_redirect_uri,
    )
    logger.info("✅ SSO identity provider initialized (WorkOS AuthKit)")
    return provider


def create_sso_login_service(
    settings: FaultMavenSettings,
    identity_provider: Any,
    redis_client: Any,
    token_generator: Any,
    session_service: Any,
) -> Any | None:
    """Create the SSO login orchestration service, or None when SSO is off.

    Only constructed when the identity provider exists (i.e. ``sso_configured``),
    which also guarantees oauth mode — so the RS256 token generator is present.
    User lookup uses a sessionless repository (per-operation sessions), the same
    pattern the user store uses.

    Args:
        settings: FaultMavenSettings instance
        identity_provider: ISSOIdentityProvider from create_sso_identity_provider
        redis_client: Async Redis-compatible client (real Redis in cloud)
        token_generator: RS256 JWT token generator
        session_service: AuthSessionService for session creation at exchange

    Returns:
        An SSOLoginService, or None when SSO is not configured.
    """
    if identity_provider is None:
        return None
    if token_generator is None or session_service is None:
        logger.warning(
            "SSO login service skipped (missing token generator or session service)"
        )
        return None

    from faultmaven.infrastructure.persistence.sessionless_audit_repository import (
        SessionlessAuditRepository,
    )
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.modules.auth.domain.services.sso_login_service import (
        SSOLoginService,
    )
    from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
        SSOEphemeralStore,
    )

    service = SSOLoginService(
        identity_provider=identity_provider,
        ephemeral_store=SSOEphemeralStore(redis_client),
        user_repository=SessionlessUserRepository(),
        token_generator=token_generator,
        session_service=session_service,
        dashboard_url=settings.auth.dashboard_url,
        access_token_expires_in=settings.auth.jwt_access_token_expire_minutes * 60,
        audit_log=SessionlessAuditRepository(),
    )
    logger.info("✅ SSO login service initialized")
    return service


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
    vector_store = container.get_service("vector_store")
    knowledge_ingester = getattr(container, "knowledge_ingester", None)
    redis_client = getattr(container, "redis_client", None)

    # Token revocation store — created unconditionally (#767): both auth modes
    # revoke tokens (OAuth /revoke, refresh rotation, logout), and the
    # request-path check in AuthService must read the same store instance.
    token_revocation_store = create_token_revocation_store(
        settings,
        cache_client=redis_client,
    )
    container.token_revocation_store = token_revocation_store
    container._register_service("token_revocation_store", token_revocation_store)

    # Auth Service (JWT token operations; revocation via the shared store)
    auth_service = create_auth_service(token_revocation_store, settings)
    container.auth_service = auth_service
    container._register_service("auth_service", auth_service)

    # User Service (Composition Root: auth_service injected via constructor)
    user_service = create_user_service(auth_service, redis_client, settings)
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

        # Create JWT token generator (shares the deployment-wide revocation
        # store, so tokens revoked here are seen by the request-path check)
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

    # Organization Repository (create before TenantProvider, which resolves the
    # implicit org through it via constructor injection below). Org/team
    # *management* is the hosted admin composed module (ADR-010 D4); the core
    # keeps only the repository substrate.
    organization_repository = create_organization_repository()

    # Enterprise Repository (create before TenantProvider; SingleTenantProvider
    # uses it for default-enterprise bootstrap).
    enterprise_repository = create_enterprise_repository()
    container.enterprise_repository = enterprise_repository
    if enterprise_repository:
        container._register_service("enterprise_repository", enterprise_repository)

    # Team Repository (create before TenantProvider; SingleTenantProvider uses
    # it to seed the default team row. Also the substrate for TeamService.)
    team_repository = create_team_repository()
    container.team_repository = team_repository
    if team_repository:
        container._register_service("team_repository", team_repository)

    # Tenant Provider (create after the Organization + Enterprise + Team
    # repositories, before CaseService)
    tenant_provider = create_tenant_provider(
        organization_repository,
        settings,
        enterprise_repository=enterprise_repository,
        team_repository=team_repository,
    )
    container.tenant_provider = tenant_provider
    if tenant_provider:
        container._register_service("tenant_provider", tenant_provider)

    # Team Service (KB team-scope resolution). Gated on multi-tenant mode —
    # None in standalone (team collaboration is a Cloud feature).
    team_service = create_team_service(tenant_provider, team_repository)
    container.team_service = team_service
    if team_service:
        container._register_service("team_service", team_service)

    # Share Repository (resource→scope visibility source of truth, ADR-013 §D4).
    # Created in both modes: the read allowlist resolves through it (empty in
    # standalone) and the KB write path creates share rows on team publish.
    share_repository = create_share_repository()
    container.share_repository = share_repository
    if share_repository:
        container._register_service("share_repository", share_repository)

    # Case Service. Org resolution is request-scoped (tenant_scope middleware ->
    # config.tenant_context contextvar), so no TenantProvider injection is needed
    # here; the service reads the bound org at write time.
    case_service = create_case_service(
        case_repository,
        session_store,
        case_vector_store,
        settings,
        container._create_minimal_case_service,
        team_service=team_service,  # Case read allowlist: team-membership resolution
        share_repository=share_repository,  # Case read allowlist: share source (§D4)
    )
    container._register_service("case_service", case_service)

    # Evidence service removed in storage redesign 2026-04 phase 2 (standalone path deletion).
    # Milestone Engine (with investigation tools for evidence searching).
    # Agent tools read evidence directly from case.evidence via case_repository.
    llm_provider = container.get_service("llm_provider")
    investigation_tools = _create_investigation_tools(container)

    # Dedicated DA provider for directed analysis tool loop (DA_PROVIDER in .env)
    from faultmaven.container.providers.infrastructure import create_da_provider

    da_provider, da_model = create_da_provider()

    # Sanitizer + Redis for case-scoped PII redaction at LLM boundary
    sanitizer = container.get_service("sanitizer", required=False)
    redis_client = getattr(container, "redis_client", None)

    milestone_engine = create_milestone_engine(
        llm_provider,
        case_repository,
        investigation_tools=investigation_tools,
        da_provider=da_provider,
        da_model=da_model,
        sanitizer=sanitizer,
        redis_client=redis_client,
    )
    container.milestone_engine = milestone_engine
    if milestone_engine:
        container._register_service("milestone_engine", milestone_engine)

    # Investigation Service
    preprocessing_service = container.get_service("preprocessing_service")
    # File storage was already constructed and registered in
    # register_infrastructure — retrieve the singleton here instead of
    # constructing a second instance. Centralizing construction in the
    # infrastructure layer ensures a single source of truth and correct
    # init ordering (Tier 2 in infrastructure.py consumes it before this
    # function runs).
    file_storage_service = container.get_service("file_storage_service")

    investigation_service = create_investigation_service(
        milestone_engine,
        case_repository,
        preprocessing_service,
        file_storage_service,
    )
    container.investigation_service = investigation_service
    if investigation_service:
        container._register_service("investigation_service", investigation_service)

    # Session Service
    session_service = create_session_service(
        session_store,
        settings,
        container._create_minimal_session_service,
    )
    container._register_service("session_service", session_service)

    # SSO login orchestration (cloud WorkOS AuthKit; ADR-015). Registered after
    # the session service because exchange mints a session; the JWT generator
    # was registered in the oauth block above (sso_configured implies oauth).
    sso_identity_provider = create_sso_identity_provider(settings)
    if sso_identity_provider:
        container._register_service("sso_identity_provider", sso_identity_provider)
        sso_login_service = create_sso_login_service(
            settings,
            identity_provider=sso_identity_provider,
            redis_client=redis_client,
            token_generator=container.get_service("jwt_token_generator"),
            session_service=session_service,
        )
        if sso_login_service:
            container._register_service("sso_login_service", sso_login_service)

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

    # Knowledge Service — prefer KnowledgeVectorStore (scope-enforcing) over the
    # generic ChromaDBVectorStore. Fall back to vector_store if not registered.
    knowledge_vector_store = getattr(container, "knowledge_vector_store", None)
    knowledge_service = create_knowledge_service(
        knowledge_vector_store or vector_store,
        knowledge_ingester,
        container.get_service("sanitizer", required=True),
        container.get_service("tracer", required=True),
        container.get_service("llm_provider", required=False),
        redis_client,
        settings,
        share_repository=share_repository,
    )
    container._register_service("knowledge_service", knowledge_service)

    # Report Generation Service (TD-001: migrated from IReportStore to CaseRepository)
    llm_provider = container.get_service("llm_provider")
    lock_manager = getattr(container, "lock_manager", None)
    pii_redactor = getattr(container, "pii_redactor", None)
    report_generation_service = create_report_generation_service(
        case_repository=case_repository,
        lock_manager=lock_manager,
        pii_redactor=pii_redactor,
    )
    container.report_generation_service = report_generation_service
    if report_generation_service:
        container._register_service(
            "report_generation_service", report_generation_service
        )

    # Wire services into milestone engine (created earlier in the registration order)
    if report_generation_service and milestone_engine:
        milestone_engine.report_service = report_generation_service
    if knowledge_service and milestone_engine:
        milestone_engine.knowledge_service = knowledge_service
        logger.info("✅ Knowledge service wired to MilestoneEngine")
    # KB seeder pre-fetch owner-team arm (ADR-013 §D4): resolves the case
    # owner's team-shared runbooks. None in standalone (arm resolves empty).
    if milestone_engine:
        milestone_engine.team_service = team_service
        milestone_engine.share_repository = share_repository

    # Report Recommendation Service (optional - may not be implemented yet)
    # TODO: Implement create_report_recommendation_service if needed
    container.report_recommendation_service = None
    container._register_service("report_recommendation_service", None)

    logger.info("✅ Service layer registered")
