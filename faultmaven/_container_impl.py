"""Dependency Injection Container

Purpose: Centralized dependency management for the FaultMaven architecture

This container manages the lifecycle and dependencies of all components following
the interface-based dependency injection pattern.

Core Responsibilities:
- Singleton container with lazy initialization
- Dependency graph resolution for all services via DependencyRegistry
- Configuration management from environment variables
- Proper error handling with specific exceptions

Key Components:
- Infrastructure layer: LLM providers, security, observability
- Core tools: Knowledge base, web search
- Service layer: Agent, data, knowledge services
- Proper interface implementations and dependency injection
"""

import logging
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

from faultmaven.config.settings import FaultMavenSettings, get_settings
from faultmaven.container.base import BaseDIContainer
from faultmaven.container.errors import InitializationError, ServiceUnavailableError
from faultmaven.container.providers import (
    register_infrastructure,
    register_services,
    register_tools,
)
from faultmaven.utils.serialization import to_json_compatible

# Import interfaces with graceful fallback for testing environments
try:
    from faultmaven.models.interfaces import (
        BaseTool,
        ILLMProvider,
        ISanitizer,
        ISessionStore,
        ITracer,
        IVectorStore,
    )
    from faultmaven.models.interfaces_case import ICaseService, ICaseStore

    # TD-001: IReportStore removed - reports now stored via CaseRepository
    INTERFACES_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Interfaces not available: {e}")
    # Create placeholder types for testing environments
    ILLMProvider = Any
    ITracer = Any
    ISanitizer = Any
    BaseTool = Any
    IVectorStore = Any
    ISessionStore = Any
    ICaseStore = Any
    ICaseService = Any
    INTERFACES_AVAILABLE = False
# Agentic Framework Interfaces
# NOTE: The agentic framework concrete implementations (AgentStateManager,
# BusinessLogicWorkflowEngine, etc.) were archived during the modular refactoring.
# The current system uses specialized orchestration layers in modules/agent/ instead.
# These interfaces are kept for type checking only.
try:
    from faultmaven.modules.agent.domain.models.agentic import (
        IAgentStateManager,
        IBusinessLogicWorkflowEngine,
        IErrorFallbackManager,
        IGuardrailsPolicyLayer,
        IResponseSynthesizer,
        IToolSkillBroker,
    )
except ImportError:
    # Interfaces not available - use Any for type compatibility
    IAgentStateManager = Any
    IToolSkillBroker = Any
    IGuardrailsPolicyLayer = Any
    IResponseSynthesizer = Any
    IErrorFallbackManager = Any
    IBusinessLogicWorkflowEngine = Any


def _within_created_window(cases, filters):
    """Apply `[created_after, created_before)` to an in-memory list of cases.

    Shared by ``MinimalCaseService.list_user_cases`` and ``count_user_cases`` so
    the page and its count cannot apply different windows — the degraded
    service's whole job is to behave like the real one, and two copies of a
    predicate is how that stops being true.

    Both sides are normalized to UTC: an aware bound compared against a naive
    ``created_at`` raises TypeError, which the caller's blanket handler would
    turn into an empty list rather than an error. Mirrors the rule stated in
    ``modules/case/infrastructure/created_bounds.py``.
    """
    from datetime import timezone

    def _utc(value):
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    after = _utc(getattr(filters, "created_after", None))
    before = _utc(getattr(filters, "created_before", None))
    if after is None and before is None:
        return cases

    def _keep(case):
        created = _utc(getattr(case, "created_at", None))
        if created is None:
            # A case with no creation timestamp cannot be placed in the window.
            # Excluding it is the answer that does not claim a date it lacks.
            return False
        if after is not None and created < after:
            return False
        if before is not None and created >= before:
            return False
        return True

    return [case for case in cases if _keep(case)]


class DIContainer(BaseDIContainer):
    """Singleton dependency injection container for centralized component management.

    Extends BaseDIContainer to inherit:
    - DependencyRegistry for service lifecycle tracking
    - Standardized service access patterns
    - Health check infrastructure
    """

    def __new__(cls):
        # Use parent's singleton implementation
        instance = super().__new__(cls)
        # Initialize settings if not already present
        if not hasattr(instance, "settings"):
            instance.settings = None
        return instance

    async def initialize(self, allow_degraded: bool = False):
        """Initialize all dependencies with proper error handling (async for proper event loop handling).

        Args:
            allow_degraded: opt in to the lenient path under pytest, where a
                composition failure otherwise raises so it names itself (#823).
                Never an escape from ``settings.must_not_degrade`` — a
                deployment that must not degrade refuses either way.
        """
        logger = logging.getLogger(__name__)

        if self._initialized:
            logger.debug("Container already initialized, skipping")
            return

        if self._initializing:
            logger.debug("Container initialization already in progress, skipping")
            return

        self._initializing = True
        logger.info("Initializing DI Container with unified settings system")

        # Initialize settings as the single source of truth
        try:
            self.settings = get_settings()
            self._register_service("settings", self.settings)
            logger.info("✅ Unified settings system initialized")
        except Exception as e:
            logger.error(f"❌ Failed to initialize settings system: {e}")
            self._initializing = False
            raise InitializationError("Failed to initialize settings", cause=e)

        try:
            # Use providers for layer initialization
            # Infrastructure layer: LLM, storage, security, observability
            await register_infrastructure(self)

            # Tools layer: Tool registry, document Q&A tools
            register_tools(self)

            # Service layer: Business logic services
            register_services(self)

            self._initialized = True
            self._initializing = False
            logger.info("✅ DI Container initialized successfully")

        except Exception as e:
            logger.error(f"❌ DI Container initialization failed: {e}")
            self._initializing = False

            # A tenancy configuration refusal (e.g. TENANT_PROVIDER=multi
            # outside DEPLOYMENT_MODE=cloud) is a deliberate fail-closed
            # decision, not an infrastructure hiccup: it must terminate every
            # path — jobs/CLI included — never degrade to a half-initialized
            # container that would run against tenanted data unchecked.
            try:
                from faultmaven.providers.tenancy.factory import (
                    TenancyConfigurationError,
                )
            except ImportError:
                # The tenancy module itself is unimportable — the same shape as
                # the failure that motivated #885 (a package missing from the
                # image), and `register_services` imports that factory inside
                # the function, so this handler is where it lands. Then `e`
                # cannot be a tenancy refusal, and the handler must not raise a
                # *second* error that escapes the cloud guard below.
                is_tenancy_refusal = False
            else:
                is_tenancy_refusal = isinstance(e, TenancyConfigurationError)

            if is_tenancy_refusal:
                raise

            # A deployment that must not degrade never serves a half-composed
            # container (#885). Composition is ordered — infrastructure, then
            # tools, then services — so an exception part-way through leaves
            # every service registered after the failing line absent, while the
            # pod keeps serving: the #629 flip rehearsal had readiness green and
            # /health "healthy" with the whole service layer missing. Refuse the
            # boot instead, so uvicorn exits, the pod CrashLoops and the rollout
            # rolls back. RuntimeError is the container's established fail-fast
            # channel: both the web lifespan and the jobs runner treat it as
            # terminal. Deliberately NOT gated on SKIP_SERVICE_CHECKS or pytest
            # — those escapes would defeat the guarantee exactly where it has to
            # hold. Anywhere else the lenient posture below applies, which dev
            # ergonomics rely on.
            if self.settings.must_not_degrade:
                # The two fields do NOT behave alike. ``use_enum_values`` is set
                # on FaultMavenSettings, so ``deployment_mode`` holds the plain
                # str "cloud" and unwrapping it is defensive only. It is not set
                # on ServerSettings, so ``server.environment`` holds the
                # Environment MEMBER: formatting it unwrapped logs
                # "Environment.PRODUCTION" at an operator (#827). Comparisons
                # work either way — Environment subclasses str — which is
                # exactly why the difference goes unnoticed until it is in a
                # message.
                mode = getattr(
                    self.settings.deployment_mode,
                    "value",
                    self.settings.deployment_mode,
                )
                env = getattr(
                    self.settings.server.environment,
                    "value",
                    self.settings.server.environment,
                )
                logger.critical(
                    "FAIL-FAST: DI container could not be composed under "
                    f"DEPLOYMENT_MODE={mode}/ENVIRONMENT={env}. "
                    "Refusing to serve a partial API."
                )
                raise RuntimeError(
                    "DI Container initialization failed under "
                    f"DEPLOYMENT_MODE={mode}/ENVIRONMENT={env}: {e}. A partially "
                    "composed container would serve an API missing whole "
                    "service layers."
                ) from e

            # Check if interfaces are available - if not, use minimal container
            if not INTERFACES_AVAILABLE:
                logger.warning(
                    "Interfaces not available - creating minimal container for testing"
                )
                self._create_minimal_container()
                self._initialized = True
            else:
                import traceback

                logger.error(f"Critical initialization error: {traceback.format_exc()}")

                # Under pytest the lenient path costs more than it buys: the
                # container returns normally with `_initialized` still False,
                # and the real error is only in captured logs, so the failure
                # re-surfaces as an unrelated `assert False is True` in
                # whichever test reads container state next (#823). A test that
                # wants the degraded container asks for it by name.
                if "pytest" in sys.modules and not allow_degraded:
                    raise RuntimeError(
                        f"DI Container initialization failed: {e}. Pass "
                        "allow_degraded=True to exercise the degraded container."
                    ) from e

                self._initialized = False

    def _ensure_initialized_for_getter(self) -> None:
        """Best-effort lazy initialization for sync getter methods.

        Tests and sync call sites expect getters to trigger initialization.

        Behavior:
        - If initialize() is mocked (not a coroutine function), call it directly.
        - If no event loop is running, run async initialize() to completion via asyncio.run.
        - If an event loop is running, schedule initialize() as a background task.
        """
        if self._initialized or getattr(self, "_initializing", False):
            return

        logger = logging.getLogger(__name__)
        logger.warning(
            "Service requested but container not initialized - triggering lazy initialization"
        )

        import asyncio
        import inspect

        init = getattr(self, "initialize", None)
        if init is None:
            return

        # If patched/mocked in tests, just call it so assertions see the call.
        if not inspect.iscoroutinefunction(init):
            try:
                init()
            except Exception:
                # Getter should not raise due to failed lazy init
                return
            return

        # Normal path: initialize is an async function
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (common in sync tests)
            try:
                asyncio.run(init())
            except Exception:
                return
        else:
            try:
                loop.create_task(init())
            except Exception:
                return

    def _create_minimal_container(self):
        """Create minimal container for testing environments without dependencies"""
        # Create mock objects for testing
        from unittest.mock import MagicMock

        # Infrastructure layer mocks
        self.llm_provider = MagicMock()
        self.sanitizer = MagicMock()
        self.tracer = MagicMock()
        self.data_classifier = MagicMock()
        self.log_processor = MagicMock()

        # Tools layer
        self.tools = []

        # Service layer mocks
        self.agent_service = MagicMock()
        self.data_service = MagicMock()
        # No stand-in: a KnowledgeService without a database cannot answer a
        # single KB question truthfully, and the stub that used to sit here
        # fabricated documents for any plausible id (#899). None is the honest
        # answer; every caller of get_knowledge_service() handles it.
        self.knowledge_service = None
        self.session_service = self._create_minimal_session_service()

        logging.getLogger(__name__).info("Created minimal container for testing")

    def get_settings(self) -> FaultMavenSettings:
        """Get the unified settings instance"""
        if not hasattr(self, "settings") or self.settings is None:
            self.settings = get_settings()
        return self.settings

    def get_agent_service(self):
        """Get the agent service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "Agent service requested but container not initialized - this should not happen after startup"
                )
                self._ensure_initialized_for_getter()
        return getattr(self, "agent_service", None)

    def get_data_service(self):
        """Get the data service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "Data service requested but container not initialized - this should not happen after startup"
                )
                self._ensure_initialized_for_getter()
        return getattr(self, "data_service", None)

    def get_knowledge_service(self):
        """Get the knowledge service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "Knowledge service requested but container not initialized - this should not happen after startup"
                )
                self._ensure_initialized_for_getter()
        # Returns None when composition did not produce one. It used to
        # substitute an in-memory stub, which made a partially composed
        # container indistinguishable from a working one: `is None` guards in
        # kb_seed and fm-reset-kb never fired, and the stub answered document
        # reads with fabricated content (#899). A missing service is now
        # visible to the caller, as with every sibling getter.
        return getattr(self, "knowledge_service", None)

    def get_suggestion_service(self):
        """Get the knowledge suggestion service (#1214).

        Returns None when composition did not produce one, like every sibling
        getter — the composition root decides whether that is survivable, and
        the routes answer 503 rather than silently building a store-less
        replacement (which is exactly how the extract → approve loop came to be
        broken).
        """
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "suggestion_service", None)

    def get_oauth_service(self):
        """Get the OAuth service (if enabled)."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "oauth_service", None)

    def get_metrics_collector(self):
        """Get the metrics collector service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Metrics collector requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "metrics_collector", None)

    def get_intelligent_cache(self):
        """Get the intelligent cache service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Intelligent cache requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "intelligent_cache", None)

    def get_analytics_dashboard_service(self):
        """Get the analytics dashboard service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Analytics dashboard service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "analytics_dashboard_service", None)

    def get_sla_monitor(self):
        """Get the SLA monitor service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("SLA monitor requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "sla_monitor", None)

    def get_performance_monitor(self):
        """Get the performance monitor"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Performance monitor requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "performance_monitor", None)

    def get_enhanced_agent_service(self):
        """Get the enhanced agent service with memory and planning capabilities"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Enhanced agent service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        enhanced_service = getattr(self, "enhanced_agent_service", None)
        if enhanced_service is None:
            # Fallback to standard agent service
            return self.get_agent_service()
        return enhanced_service

    def get_orchestration_service(self):
        """Get the orchestration service for multi-step workflows"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Orchestration service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "orchestration_service", None)

    def get_llm_provider(self):
        """Get the LLM provider interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "LLM provider requested but container not initialized - this should not happen after startup"
                )

        # LLM provider must be initialized - fail hard if not available
        llm_provider = getattr(self, "llm_provider", None)
        if llm_provider is None:
            # CRITICAL: LLM provider is required for core functionality
            # This should NEVER happen in production - it's a configuration error
            logger = logging.getLogger(__name__)
            logger.critical(
                "FATAL: LLM provider not initialized. "
                "This is a critical configuration error that prevents core functionality. "
                "Application cannot operate without a working LLM provider."
            )

            # Fail hard - raise exception instead of silently degrading
            raise RuntimeError(
                "LLM provider not initialized. "
                "This is a critical configuration error. "
                "Please check: (1) API keys are set in environment, "
                "(2) Network connectivity to LLM provider, "
                "(3) LLM provider settings are correct. "
                "Application cannot start without a working LLM provider."
            )
        return llm_provider

    def get_sanitizer(self):
        """Get the data sanitizer interface implementation."""
        return getattr(self, "sanitizer", None)

    def get_tracer(self):
        """Get the tracer interface implementation."""
        return getattr(self, "tracer", None)

    def get_tools(self):
        """Get list of available tools."""
        return getattr(self, "tools", [])

    def get_data_classifier(self):
        """Get the data classifier interface implementation."""
        return getattr(self, "data_classifier", None)

    def get_log_processor(self):
        """Get the log processor interface implementation."""
        return getattr(self, "log_processor", None)

    def get_preprocessing_service(self):
        """Get the preprocessing service (new Phase 1 pipeline)."""
        return self.get_service("preprocessing_service", required=True)

    def get_vector_store(self):
        """Get the vector store interface implementation."""
        return getattr(self, "vector_store", None)

    def get_knowledge_ingester(self):
        """Get the knowledge ingester interface implementation."""
        return getattr(self, "knowledge_ingester", None)

    def get_session_store(self):
        """Get the session store interface implementation."""
        return getattr(self, "session_store", None)

    def get_session_service(self):
        """Get the session service implementation."""
        return self.get_service("session_service")

    def get_case_service(self) -> Optional[ICaseService]:
        """Get the case service implementation (optional feature)."""
        return self.get_service("case_service")

    def get_investigation_service(self):
        """Get the investigation service implementation (v2.0 milestone-based)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Investigation service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "investigation_service", None)

    def get_milestone_engine(self):
        """Get the milestone engine implementation (v2.0 core investigation)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Milestone engine requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "milestone_engine", None)

    def get_case_store(self) -> Optional[ICaseStore]:
        """Get the case store implementation (optional feature)"""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "case_store", None)

    def get_tenant_provider(self):
        """Get the tenant provider for multi-tenant isolation (TASK-023/024)"""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "tenant_provider", None)

    def get_organization_repository(self):
        """Get the organization repository (the tenant-row substrate, ADR-010 D4).

        ``None`` before the container is initialized. Callers that only need a
        display label should degrade rather than refuse — see
        ``modules/auth/api/auth._resolve_organization_summary``.
        """
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "organization_repository", None)

    def get_team_service(self):
        """Get the team service for KB team-scope resolution (None in standalone)."""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "team_service", None)

    def get_report_generation_service(self):
        """Get the report generation service (TASK-024)"""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "report_generation_service", None)

    def get_config(self):
        """Get the configuration manager instance"""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "config", None)

    def _create_minimal_session_service(self):
        """Create a minimal session service.

        Reachable in PRODUCTION, not only under test: ``create_session_service``
        falls back to this stand-in whenever the session store is unavailable
        (Redis down), so its semantics must mirror ``AuthSessionService`` — a
        degraded deployment must not answer differently from a healthy one.
        """
        import uuid
        from datetime import timedelta

        # Session TTL, sourced exactly as AuthSessionService.__init__ sources it
        # (settings.session.ttl_hours when present, else the 24h default), so
        # the stand-in expires sessions on the same clock as the real service.
        _settings = getattr(self, "settings", None)
        _session_ttl = timedelta(
            hours=getattr(getattr(_settings, "session", None), "ttl_hours", 24)
        )

        class MockSessionContext:
            def __init__(self, session_id, user_id=None, metadata=None):
                self.session_id = session_id
                self.user_id = user_id
                self.metadata = metadata or {}
                self.created_at = datetime.now(timezone.utc)
                self.last_activity = datetime.now(timezone.utc)
                # Sessions carry an explicit expiry from creation: without one,
                # `validate=True` could never distinguish a live session from an
                # expired one and would silently behave as `validate=False`.
                self.expires_at = self.created_at + _session_ttl
                self.data_uploads = []
                self.case_history = []

        class MockSessionManager:
            """Mock session manager for testing (spec-compliant v2.0)"""

            def __init__(self):
                self.sessions = {}

        class MinimalSessionService:
            def __init__(self):
                self.sessions = {}  # Store sessions in memory for testing
                self.session_manager = MockSessionManager()  # Add mock session manager
                self.session_manager.sessions = self.sessions  # Share session storage

            async def create_session(self, user_id, client_id=None, metadata=None):
                """Create or resume a session.

                Mirrors ``AuthSessionService.create_session``, including the
                RETURN SHAPE: ``(SessionContext, resumed)``. Callers unpack that
                pair unconditionally — ``sso_login_service`` does
                ``session, _resumed = await ...create_session(...)`` — so a bare
                session raised TypeError and 500'd SSO login whenever the
                degraded fallback was active.
                """
                if not user_id or not str(user_id).strip():
                    from faultmaven.exceptions import ValidationException

                    raise ValidationException(
                        "user_id is required for session creation"
                    )

                # Session resumption for a known (user_id, client_id) pair,
                # mirroring the real service: extend the existing session and
                # report it as resumed rather than minting a second one.
                if client_id:
                    for existing in self.sessions.values():
                        if (
                            existing.user_id == user_id
                            and getattr(existing, "client_id", None) == client_id
                        ):
                            existing.expires_at = (
                                datetime.now(timezone.utc) + _session_ttl
                            )
                            existing.last_activity = datetime.now(timezone.utc)
                            existing.session_resumed = True
                            return existing, True

                session_id = str(uuid.uuid4())
                session = MockSessionContext(session_id, user_id, metadata)
                session.client_id = client_id
                session.session_resumed = False
                self.sessions[session_id] = session
                return session, False

            async def get_session(self, session_id, validate=True):
                """Get session by ID, optionally enforcing expiry.

                Mirrors ``AuthSessionService.get_session``: ``validate=True``
                treats an expired session as absent AND removes it;
                ``validate=False`` returns the stored session as-is with no
                expiry check and no delete side effect — a read must never
                destroy what it reads.
                """
                session = self.sessions.get(session_id)
                if not session:
                    return None

                if not validate:
                    return session

                expires_at = getattr(session, "expires_at", None)
                if expires_at and datetime.now(timezone.utc) > expires_at:
                    await self.delete_session(session_id)
                    return None

                return session

            async def validate_session(self, session_id):
                """Whether the session exists and has not expired."""
                return await self.get_session(session_id) is not None

            async def list_sessions(self, user_id=None):
                """Mirrors ``AuthSessionService.list_sessions``, fail-closed half
                included: ``is not None``, not truthiness, so a blank caller id
                yields nothing rather than every session (#1447 review). This is
                the implementation the enumeration was measured against, so the
                two must not differ here of all places."""
                sessions = list(self.sessions.values())
                if user_id is not None:
                    return [s for s in sessions if s.user_id == user_id]
                return sessions

            async def delete_session(self, session_id):
                if session_id in self.sessions:
                    del self.sessions[session_id]
                    return True
                return False

            async def update_last_activity(self, session_id):
                """Touch a session's last_activity, honouring expiry.

                Resolves through ``get_session`` exactly as
                ``AuthSessionService.update_last_activity`` does, so an EXPIRED
                session answers False (and is evicted) instead of being
                silently refreshed. Reading the store directly bypassed the
                expiry semantics and let a heartbeat on a dead session return
                200 in degraded mode where the real service returns 404.
                """
                session = await self.get_session(session_id)
                if not session:
                    return False

                now = datetime.now(timezone.utc)
                session.last_activity = now
                session.updated_at = now
                return True

        return MinimalSessionService()

    def _create_minimal_case_service(self):
        """Create a minimal case service for testing environments"""
        import uuid

        from faultmaven.config.tenant_context import (
            get_current_billing_organization_id,
            get_current_enterprise_id,
        )
        from faultmaven.modules.case.domain.models import Case, CaseState

        class MinimalCaseService:
            def __init__(self):
                self.cases = {}  # Store cases in memory for testing
                self.case_messages = (
                    {}
                )  # Store messages per case: {case_id: [messages]}

            async def create_case(
                self,
                title=None,
                description=None,
                owner_id=None,
                session_id=None,
                initial_message=None,
                source="copilot",
            ):
                # Signature mirrors CaseService.create_case. `source` is not
                # optional in practice: modules/case/api/routes.py passes it on
                # every POST /api/v1/cases, so a stand-in without it raised
                # TypeError -> 500 whenever the degraded fallback was active.
                # The dropped parameters (initial_query, priority, user_id,
                # organization_id, metadata) had no caller anywhere.
                # Generate case_id matching required pattern ^case_[a-f0-9]{12}$
                case_id = f"case_{uuid.uuid4().hex[:12]}"

                # Validate owner_id is required (match real CaseService behavior)
                if not owner_id or not owner_id.strip():
                    from faultmaven.exceptions import ValidationException

                    raise ValidationException("Owner ID is required")

                # Create case with proper Case model structure.
                #
                # Isolation from the request BINDING, billing from the actor's
                # organization — the two columns come from two different places
                # and mean two different things (ADR-017 D1/D2). This used to
                # stamp ``organization_id = owner_id``, which is neither: a user
                # id in an organization FK, and no ``enterprise_id`` at all, so
                # the degraded path 500'd on a required field the moment
                # ``Case`` gained one.
                final_user_id = owner_id
                final_enterprise_id = get_current_enterprise_id()
                final_org_id = get_current_billing_organization_id()

                # Phase 2: Handle initial_message transactionally
                current_time = datetime.now(timezone.utc)
                message_count = 0

                # Phase 2: If initial_message provided, set message_count=1 and update timestamp
                if initial_message and initial_message.strip():
                    message_count = 1
                    current_time = datetime.now(
                        timezone.utc
                    )  # Refresh timestamp for message creation

                # Phase 3: Handle auto-title generation
                provided_title = title or "New Chat"

                # Phase 3: Auto-title generation after first committed message
                should_auto_title = (
                    initial_message
                    and initial_message.strip()
                    and provided_title == "New Chat"
                )

                if should_auto_title:
                    # Generate auto-title: chat-<UTC ISO 8601 Z>
                    provided_title = f"chat-{current_time.isoformat()}Z"

                case = Case(
                    case_id=case_id,
                    title=provided_title,
                    description=description or "",
                    user_id=final_user_id,
                    enterprise_id=final_enterprise_id,
                    organization_id=final_org_id,
                    # `state`, the field `Case` declares. This said
                    # `status=`, which `extra='ignore'` dropped: the INQUIRY it
                    # appeared to set came from the field DEFAULT, so the kwarg
                    # was decorative and a different value here would have been
                    # discarded in silence. #1431's mechanism, inside the class
                    # this branch sweeps.
                    state=CaseState.INQUIRY,
                    message_count=message_count,
                    # `current_turn` moves WITH `message_count`. It did not, so
                    # a case created with an initial message sat at turn 0 —
                    # and `include_empty=False`, which the repository applies as
                    # `current_turn > 0`, hid a case that had content. The two
                    # fields describe the same fact and must not disagree.
                    current_turn=message_count,
                    # Mirrors CaseService: an unrecognised source falls back to
                    # "copilot" rather than being stored verbatim.
                    source=(
                        source if source in ("copilot", "slack", "api") else "copilot"
                    ),
                )

                self.cases[case_id] = case

                # Store initial_message as first user message if provided
                if initial_message and initial_message.strip():
                    if case_id not in self.case_messages:
                        self.case_messages[case_id] = []

                    initial_msg = {
                        "message_id": f"initial_{case_id}",
                        "case_id": case_id,
                        # `role`, not `message_type` (#1397). This stand-in
                        # wrote that key INSTEAD of `role` and mapped it back on
                        # the way out — the last place the second name carried
                        # information rather than duplicating the first.
                        "role": "user",
                        # The initial message IS turn 1. Without it every
                        # `Message(...)` built from this row failed validation
                        # (`turn_number` is required) and was swallowed by the
                        # parse handler, so the degraded transcript answered 200
                        # with an EMPTY list for a case that had a message —
                        # which is also why the mapping above had no coverage.
                        "turn_number": 1,
                        "content": initial_message.strip(),
                        "timestamp": current_time,
                        "user_id": final_user_id,
                    }
                    self.case_messages[case_id].append(initial_msg)

                return case

            async def get_case(self, case_id, user_id=None, *, owner_only=False):
                case = self.cases.get(case_id)
                # Ownership is applied on BOTH arms, not just under
                # ``owner_only``. The stand-in must honour the gate rather than
                # merely accept it: a degraded path that widened a caller's
                # reach would be worse than one that 500s.
                #
                # The read arm of the real resolver is owner ∪ shared-to-my-
                # teams, and this stand-in cannot consult the share allowlist —
                # ``resource_shares`` lives in the repository it is standing in
                # for, so in this mode no share can exist to honour. Refusing a
                # non-owner is therefore the narrow answer AND the accurate
                # one. Gating this on ``owner_only`` meant every read-arm
                # caller — which is most of them, including the session resume
                # (#1393) — got any case from any caller.
                # ``user_id`` truthiness, not ``is not None``: the real
                # ``CaseService.get_case`` writes ``if user_id and ...``, so an
                # empty-string caller id takes the unscoped path there. A
                # stand-in that diverges refuses a read the real service serves.
                if case is not None and user_id and case.user_id != user_id:
                    return None
                return case

            def _active_session_cases(self, session_id):
                """Non-terminal, non-empty cases for a session.

                CaseService.list_cases_by_session/count_cases_by_session take no
                filters, so neither does this stand-in; the default exclusions
                below are the only behaviour a caller can reach.
                """
                return [
                    case
                    for case in self.cases.values()
                    if case.current_session_id == session_id
                    and case.state in [CaseState.INQUIRY, CaseState.INVESTIGATING]
                    and case.current_turn > 0
                ]

            async def list_cases_by_session(self, session_id, limit=50, offset=0):
                """List active cases for a session (mirrors CaseService)."""
                session_cases = self._active_session_cases(session_id)
                return session_cases[offset : offset + limit]

            async def count_cases_by_session(self, session_id):
                """Count active cases for a session (mirrors CaseService)."""
                return len(self._active_session_cases(session_id))

            async def close_case(self, case_id, user_id):
                # Mirrors CaseService.close_case: one closure rule — the
                # engine executor derives closure_reason and stamps closed_at
                # atomically (a bare `state = CLOSED` assignment trips the
                # terminal-state validator, #915).
                from faultmaven.core.investigation.terminal_transitions import (
                    execute_user_closure,
                )
                from faultmaven.exceptions import ConflictError, NotFoundError

                case = self.cases.get(case_id)
                if not case or case.user_id != user_id:
                    raise NotFoundError("Case", case_id)
                if case.state.is_terminal:
                    raise ConflictError(
                        f"Case {case_id} is already {case.state.value}",
                        resource_type="Case",
                        resource_id=case_id,
                        conflict_reason="already_closed",
                    )
                execute_user_closure(case, user_id)
                return case

            async def list_user_cases(self, user_id, filters=None):
                """List cases for a user with pagination.

                Signature mirrors CaseService.list_user_cases: ``user_id`` is
                REQUIRED and pagination comes from ``filters`` only. The
                stand-in previously also took ``limit``/``offset`` directly,
                which the real service rejects, and defaulted ``user_id`` to
                None where the real service raises.
                """
                if not user_id:
                    from faultmaven.exceptions import ValidationException

                    raise ValidationException("User ID cannot be empty")

                # Defaults mirror CaseListFilter when no filters are given.
                limit = 50
                offset = 0
                user_cases = [
                    case for case in self.cases.values() if case.user_id == user_id
                ]

                # Only the filters `CaseListFilter` actually declares, because
                # the stand-in must answer what `CaseService.list_user_cases`
                # answers. Two `getattr(filters, "include_deleted"/
                # "include_terminal", False)` blocks used to sit here (#1431):
                # `CaseListFilter` declares neither field, so both defaults
                # always fired and this path DROPPED every RESOLVED and CLOSED
                # case — a filter applied that nobody requested, while the real
                # service excludes no terminal state at all. Same defect as the
                # declared-and-never-applied filters, opposite sign.
                if filters:
                    # `filters.include_empty`, not `getattr(..., False)`. The
                    # declared default is **True**, so the getattr default
                    # contradicted it: rename the field and `not False` becomes
                    # true, and the stand-in starts dropping every empty case
                    # with no error — which is the silence this whole branch is
                    # about. Read the field; let a missing one raise.
                    #
                    # And the predicate is `current_turn`, not `message_count`.
                    # The repository applies `current_turn > 0` and the route
                    # publishes "Include cases with current_turn == 0", so a
                    # stand-in keyed on `message_count` returned, in degraded
                    # mode, a case the real service hides.
                    if not filters.include_empty:
                        user_cases = [
                            case for case in user_cases if case.current_turn > 0
                        ]

                    # Only the fields `CaseListFilter` DECLARES, reached
                    # directly. The `hasattr(filters, "priority")` and
                    # `hasattr(filters, "owner_id")` blocks that sat here named
                    # fields the model does not have, so neither could ever
                    # fire — and four filter blocks in a row is exactly what
                    # made the MISSING one below easy to miss.
                    if filters.state:
                        user_cases = [
                            case for case in user_cases if case.state == filters.state
                        ]
                    # `source`, for the same reason as the date window below:
                    # the real service forwards `filters.source` to
                    # `repository.list`, and a stand-in that dropped it answered
                    # `GET /cases?source=slack` with 200 and EVERY case — the
                    # #1424 defect one layer up, in the code that replaces the
                    # layer it lives in.
                    if filters.source:
                        user_cases = [
                            case for case in user_cases if case.source == filters.source
                        ]
                    # `team_id` resolves to NOTHING here, and that is the
                    # faithful mirror rather than a shortcut. The real service
                    # turns a team filter into an allowlist of shared case ids
                    # and short-circuits `return [], 0` when it resolves empty;
                    # this stand-in cannot consult that allowlist, because
                    # `resource_shares` lives in the repository it is standing
                    # in for — so in this mode no share can exist to match, and
                    # the empty page is the honest answer. Returning the
                    # caller's OWN cases instead is a filter accepted and
                    # applied to nothing.
                    if filters.team_id:
                        return [], 0
                    # Creation-date window `[created_after, created_before)`.
                    # Honoured HERE too: every line of this stand-in runs in
                    # production the moment the repository is missing, and a
                    # bound it ignored would answer 200 with the UNFILTERED
                    # list — the exact silence the date bounds were added to
                    # end. Both `list_user_cases` and `count_user_cases` apply
                    # it, so the page and the count cannot disagree.
                    user_cases = _within_created_window(user_cases, filters)
                # No `else` branch: with no filters `CaseService.list_user_cases`
                # narrows on nothing (state=None, source=None, team=None,
                # include_empty=True — `CaseListFilter`'s own default), so the
                # stand-in must not either. It used to exclude every terminal
                # case AND every empty one here, which is the #1431 defect once
                # more with no filter object to blame it on.

                # Declared fields, read directly — `hasattr` guards on
                # `limit`/`offset` could only ever be True, and would have
                # turned a rename into a silent fall-back to the defaults.
                if filters:
                    limit = filters.limit
                    offset = filters.offset

                # Total match count is computed BEFORE pagination so it agrees
                # with the returned page (mirrors CaseService.list_user_cases,
                # which returns (page, total)).
                total = len(user_cases)
                paginated_cases = user_cases[offset : offset + limit]

                # `CaseSummary`, converted PER CASE and best-effort — what the
                # real service returns and how it returns it. This handed back
                # raw `Case` entities, and the route compensated with an
                # UNGUARDED `CaseSummary.from_case(item)` inside a handler-wide
                # `except Exception`: one case whose conversion failed turned
                # `GET /cases` from "one case missing" into 500, no cases at
                # all. That line was unreachable for terminal cases on this
                # path until the previous commit stopped hiding them, so the
                # blast radius grew exactly when the filter was fixed. The real
                # service catches per case and continues; so does this.
                from faultmaven.models.api_models import CaseSummary

                summaries = []
                for case in paginated_cases:
                    try:
                        summaries.append(CaseSummary.from_case(case))
                    except Exception as exc:  # pragma: no cover - defensive
                        # `logging.getLogger`, not a bare `logger`: this module
                        # binds its logger inside functions, so a bare name here
                        # would be a NameError on the one path that needs it.
                        logging.getLogger(__name__).error(
                            "Failed to convert case %s to summary: %s",
                            getattr(case, "case_id", "<unknown>"),
                            exc,
                        )

                return summaries, total

            async def list_all_cases(self, filters=None):
                """List all in-memory cases as summaries (admin cross-tenant read; degraded double).

                Mirrors ``CaseService.list_all_cases``: returns
                ``(List[CaseSummary], total)`` so the ``CaseListResponse``
                response model validates even when this fallback is active.
                """
                from faultmaven.models.api_models import CaseSummary

                all_cases = list(self.cases.values())
                # `state` AND `source` — the two `CaseService.list_all_cases`
                # forwards to the repository. `GET /api/v1/admin/cases`
                # declares `source` and builds the filter with it, so a
                # stand-in reading only `state` answered an operator's
                # source-filtered cross-tenant list with every case.
                # `include_empty` stays unhonoured, deliberately and for the
                # reason the real method gives in its own docstring: filtering
                # after pagination would make the page and `total` disagree.
                if filters and filters.state:
                    all_cases = [c for c in all_cases if c.state == filters.state]
                if filters and filters.source:
                    all_cases = [c for c in all_cases if c.source == filters.source]
                total = len(all_cases)
                # Declared fields read directly; the defaults belong to the
                # no-filters case, not to a `getattr` fall-back that would
                # survive the field being renamed away.
                limit = filters.limit if filters else 50
                offset = filters.offset if filters else 0
                summaries = []
                for case in all_cases[offset : offset + limit]:
                    try:
                        summaries.append(CaseSummary.from_case(case))
                    except Exception:
                        pass
                return summaries, total

            async def count_user_cases(self, user_id: str, filters=None):
                """Count cases for a user — by ASKING list_user_cases, as the real service does.

                ``CaseService.count_user_cases`` is ``_, count = await
                self.list_user_cases(user_id, filters)``. Agreement there is
                STRUCTURAL: there is no second copy of the predicates to drift.

                This was a hand-maintained duplicate of all six, and every
                defect the duplicate ever had was one the original did not:
                it selected on ``case.owner_id``, a field ``Case`` does not
                declare, for the life of the product; and — the reason this is
                a delegation now rather than another patch — it answered a
                FALSY ``user_id`` by counting **every user's cases**, under a
                comment reading "Return all cases if no user filter", while
                ``list_user_cases`` twenty lines up raised
                ``ValidationException`` for the same input and the docstring
                here claimed ``user_id`` was "REQUIRED, matching
                CaseService.count_user_cases". Measured before the change:
                one case for alice and two for bob, and ``count_user_cases("")``
                returned 3.

                Delegating inherits the guard, the predicates and the
                page/total contract at once, and leaves nothing to keep in step
                by hand.
                """
                _, total = await self.list_user_cases(user_id, filters)
                return total

            async def hard_delete_case(self, case_id: str, user_id: str = None) -> bool:
                """Permanently delete a case and all associated data (idempotent)"""
                # Ownership, like ``get_case`` above. This took ``user_id`` and
                # never read it, so in degraded mode any authenticated caller
                # could destroy any case — and ``DELETE /cases/{case_id}``
                # reaches it with no route-level pre-gate. Accepting the
                # argument and ignoring it is the shape that makes a gate look
                # present; the real service resolves through ``get_case`` here.
                if case_id in self.cases:
                    if user_id and self.cases[case_id].user_id != user_id:
                        return False
                    del self.cases[case_id]
                # Idempotent for a case that is already gone.
                return True

            async def get_case_messages_enhanced(
                self,
                case_id: str,
                limit: int = 50,
                offset: int = 0,
                include_debug: bool = False,
            ):
                """Enhanced message retrieval with debugging support."""
                import time

                from faultmaven.models.api import (
                    CaseMessagesResponse,
                    Message,
                    MessageRetrievalDebugInfo,
                )

                start_time = time.time()
                debug_info = None
                storage_errors = []
                message_parsing_errors = 0

                # Mock Redis key for debugging
                redis_key = f"case_messages:{case_id}"

                try:
                    # Get case messages
                    if case_id not in self.case_messages:
                        total_count = 0
                        raw_messages = []
                    else:
                        total_count = len(self.case_messages[case_id])
                        raw_messages = self.case_messages[case_id]

                    # Apply pagination
                    start = offset
                    end = start + limit
                    paginated_messages = raw_messages[start:end]

                    # Convert to Message format
                    messages = []
                    for msg in paginated_messages:
                        try:
                            # Handle both dict and object formats
                            if isinstance(msg, dict):
                                role = msg.get("role")
                                message_id = msg.get("message_id")
                                content = msg.get("content", "")
                                timestamp = msg.get("timestamp")
                                turn_number = msg.get("turn_number", 0)
                            else:
                                role = getattr(msg, "role", None)
                                message_id = getattr(msg, "message_id", None)
                                content = getattr(msg, "content", "")
                                timestamp = getattr(msg, "timestamp", None)
                                turn_number = getattr(msg, "turn_number", 0)

                            # `role` is read directly now (#1397): it used to be
                            # derived from a `message_type` this stand-in was
                            # the last writer and reader of, through a mapping
                            # ("user_query"/"case_note" -> user, "agent_response"
                            # -> assistant) that the real rows never needed.
                            if hasattr(role, "value"):
                                role = role.value

                            # Skip anything that is not one of the two the
                            # conversation renders, as before.
                            if role not in ("user", "assistant"):
                                continue

                            # Format timestamp
                            created_at = None
                            if timestamp:
                                try:
                                    if hasattr(timestamp, "isoformat"):
                                        created_at = to_json_compatible(timestamp)
                                    else:
                                        created_at = str(timestamp)
                                except Exception:
                                    created_at = str(timestamp)

                            messages.append(
                                Message(
                                    message_id=message_id or f"msg_{len(messages)}",
                                    turn_number=turn_number,
                                    role=role,
                                    content=content,
                                    created_at=created_at
                                    or to_json_compatible(datetime.now(timezone.utc)),
                                )
                            )
                        except Exception as e:
                            message_parsing_errors += 1
                            storage_errors.append(f"Failed to parse message: {str(e)}")

                    retrieved_count = len(messages)
                    has_more = (start + limit) < total_count
                    next_offset = (start + limit) if has_more else None

                except Exception as e:
                    storage_errors.append(f"Storage error: {str(e)}")
                    total_count = 0
                    retrieved_count = 0
                    messages = []
                    has_more = False
                    next_offset = None

                # Calculate operation time
                operation_time_ms = (time.time() - start_time) * 1000

                # Create debug info if requested
                if include_debug:
                    debug_info = MessageRetrievalDebugInfo(
                        redis_key=redis_key,
                        redis_operation_time_ms=operation_time_ms,
                        storage_errors=storage_errors,
                        message_parsing_errors=message_parsing_errors,
                    )

                return CaseMessagesResponse(
                    messages=messages,
                    total_count=total_count,
                    retrieved_count=retrieved_count,
                    has_more=has_more,
                    next_offset=next_offset,
                    debug_info=debug_info,
                )

            async def get_case_conversation_context(
                self, case_id: str, limit: int = 10
            ) -> str:
                """Get formatted conversation context for LLM injection"""
                if case_id not in self.cases:
                    return ""

                # For minimal implementation, return a simple context format
                # In full implementation, this would retrieve actual messages from storage
                case = self.cases[case_id]

                context_lines = []
                context_lines.append(f"Previous conversation for case: {case.title}")
                context_lines.append(f"Case status: {case.state.value}")
                context_lines.append(f"Created: {case.created_at}")
                context_lines.append(f"Last updated: {case.updated_at}")
                context_lines.append(
                    f"Message count: {getattr(case, 'message_count', 0)}"
                )

                if case.description:
                    context_lines.append(f"Description: {case.description}")

                # Add placeholder for actual messages
                if getattr(case, "message_count", 0) > 0:
                    context_lines.append(
                        "\n--- Recent conversation history would appear here ---"
                    )
                    context_lines.append(
                        "(In full implementation, this would show actual messages)"
                    )
                else:
                    context_lines.append("\n--- No conversation history yet ---")

                return "\n".join(context_lines)

            async def update_case(
                self, case_id: str, updates: dict, user_id: str = None
            ) -> bool:
                """Update a case with new data - Phase 3: Handle manual title flag changes"""
                if case_id not in self.cases:
                    return False

                # Ownership, for the same reason as ``hard_delete_case``: this
                # accepted ``user_id`` and never read it, so a stranger could
                # rewrite the owner's case in degraded mode.
                if user_id and self.cases[case_id].user_id != user_id:
                    return False

                case = self.cases[case_id]
                current_time = datetime.now(timezone.utc)

                # Phase 3: Handle manual title updates
                if "title" in updates:
                    new_title = updates["title"]
                    if new_title and new_title.strip():
                        case.title = new_title
                        # Phase 3: Mark title as manually set to prevent auto-title override
                        case.title_manually_set = True
                    elif new_title == "":
                        # Allow clearing title (reset to "New Chat")
                        case.title = "New Chat"
                        # Reset manual flag when clearing title
                        case.title_manually_set = False

                # Update other fields
                if "description" in updates:
                    case.description = updates.get("description", "")
                if "status" in updates:
                    status_value = updates["status"]
                    if status_value:
                        # Validate status before setting
                        valid_statuses = {
                            "inquiry",
                            "investigating",
                            "resolved",
                            "closed",
                        }
                        if status_value not in valid_statuses:
                            raise ValueError(
                                f"Invalid case status '{status_value}'. Valid statuses: {valid_statuses}"
                            )
                        case.state = CaseState(status_value)
                # Always update timestamp when any field is modified
                case.updated_at = current_time

                return True

        # Cache the instance to maintain state across requests
        if not hasattr(self, "_cached_minimal_case_service"):
            self._cached_minimal_case_service = MinimalCaseService()
        return self._cached_minimal_case_service

    # Phase 3: Enhanced Data Processing Services Getters

    def get_pattern_learner(self):
        """Get the pattern learner service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Pattern learner requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "pattern_learner", None)

    def get_enhanced_data_classifier(self):
        """Get the enhanced data classifier service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Enhanced data classifier requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        enhanced_classifier = getattr(self, "enhanced_data_classifier", None)
        if enhanced_classifier is None:
            # Fallback to standard classifier
            return self.get_data_classifier()
        return enhanced_classifier

    def get_enhanced_log_processor(self):
        """Get the enhanced log processor service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Enhanced log processor requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        enhanced_processor = getattr(self, "enhanced_log_processor", None)
        if enhanced_processor is None:
            # Fallback to standard processor
            return self.get_log_processor()
        return enhanced_processor

    def get_enhanced_data_service(self):
        """Get the enhanced data service with memory integration and pattern learning"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Enhanced data service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        enhanced_service = getattr(self, "enhanced_data_service", None)
        if enhanced_service is None:
            # Fallback to standard data service
            return self.get_data_service()
        return enhanced_service

    # Phase A: Microservice Foundation Services Getters

    def get_confidence_service(self):
        """Get the global confidence service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Confidence service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "confidence_service", None)

    def get_decision_recorder(self):
        """Get the decision records & telemetry service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Decision recorder requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "decision_recorder", None)

    def get_microservice_session_service(self):
        """Get the microservice session service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Microservice session service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        enhanced_service = getattr(self, "microservice_session_service", None)
        if enhanced_service is None:
            # Fallback to standard session service
            return self.get_session_service()
        return enhanced_service

    def get_policy_service(self):
        """Get the policy/safety service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Policy service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "policy_service", None)

    def get_unified_retrieval_service(self):
        """Get the unified retrieval service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Unified retrieval service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "unified_retrieval_service", None)

    # Phase B: Orchestration and Coordination Services Getters

    def get_gateway_service(self):
        """Get the gateway processing service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Gateway service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "gateway_service", None)

    def get_redis_client(self):
        """Get the Redis client for job persistence and caching"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            if not getattr(self, "_initializing", False):
                logger.warning("Redis client requested but container not initialized")
        return getattr(self, "redis_client", None)

    def get_job_service(self):
        """Get the job service for async operation management"""
        logger = logging.getLogger(__name__)
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                logger.warning("Job service requested but container not initialized")

        # Create job service if not already created
        if not hasattr(self, "_job_service"):
            try:
                from faultmaven.infrastructure.jobs.job_service import JobService

                redis_client = self.get_redis_client()
                self._job_service = JobService(redis_client=redis_client)
                logger.info("✅ Job service initialized")
            except Exception as e:
                logger.warning(f"Job service initialization failed: {e}")
                self._job_service = None

        return self._job_service

    # Agentic Framework Services Getters

    def get_business_logic_workflow_engine(
        self,
    ) -> Optional[IBusinessLogicWorkflowEngine]:
        """Get the business logic workflow engine for plan-execute-observe-adapt orchestration"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Business Logic Workflow Engine requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "business_logic_workflow_engine", None)

    def get_agent_state_manager(self) -> Optional[IAgentStateManager]:
        """Get the agent state manager for persistent memory and execution state management"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Agent State Manager requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "agent_state_manager", None)

        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Query Classification Engine requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "query_classification_engine", None)

    def get_tool_skill_broker(self) -> Optional[IToolSkillBroker]:
        """Get the tool skill broker for dynamic orchestration of tools and skills"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Tool Skill Broker requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "tool_skill_broker", None)

    def get_guardrails_policy_layer(self) -> Optional[IGuardrailsPolicyLayer]:
        """Get the guardrails policy layer for safety, security, and compliance enforcement"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Guardrails Policy Layer requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "guardrails_policy_layer", None)

    def get_response_synthesizer(self) -> Optional[IResponseSynthesizer]:
        """Get the response synthesizer for intelligent response generation and formatting"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Response Synthesizer requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "response_synthesizer", None)

    def get_error_fallback_manager(self) -> Optional[IErrorFallbackManager]:
        """Get the error fallback manager for robust error recovery and graceful degradation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Error Fallback Manager requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "error_fallback_manager", None)

    # Authentication Services

    def get_auth_service(self):
        """Get the authentication service for JWT token operations.

        Returns:
            AuthService instance from DI container, or None if not available
        """
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Auth service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "auth_service", None)

    def get_user_store(self):
        """Get the user store for user account management"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("User store requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "user_store", None)

    def get_user_service(self):
        """Get the user service for user management operations.

        Returns UserService with auth_service injected via Composition Root pattern
        (constructor injection, not a service-locator lookup).
        """
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("User service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "user_service", None)

    def health_check(self) -> dict:
        """Check health of all container dependencies.

        Uses the registry to get service status information.
        """
        # Get base health from registry
        base_health = self.get_health()

        if not self._initialized:
            return {"status": "not_initialized", "components": {}}

        # Build component status from registry
        all_services = self._registry.get_all_services()
        components = {}

        for name, info in all_services.items():
            components[name] = info.is_available()

        # Add tools count
        components["tools_count"] = (
            len(self.tools) if hasattr(self, "tools") and self.tools else 0
        )

        # Determine overall health
        failed_services = self._registry.get_failed_services()
        if failed_services:
            status = "degraded"
        elif all(v if isinstance(v, bool) else v > 0 for v in components.values()):
            status = "healthy"
        else:
            status = "degraded"

        return {
            "status": status,
            "initialized": self._initialized,
            "components": components,
            "registry": base_health,
        }

    def reset(self):
        """Reset container state (useful for testing).

        Delegates to BaseDIContainer.reset() which clears the registry.
        """
        # Clear common attributes that might not be in registry
        common_attrs = [
            "tools",
            "llm_provider",
            "sanitizer",
            "tracer",
            "data_classifier",
            "log_processor",
            "vector_store",
            "session_store",
            "agent_service",
            "data_service",
            "knowledge_service",
            "session_service",
            "case_service",
        ]
        for attr in common_attrs:
            if hasattr(self, attr):
                delattr(self, attr)

        # Clear settings
        self.settings = None

        # Use parent's reset which clears all registered services
        super().reset()


# Global container access - always returns the current singleton instance
class GlobalContainer:
    """Proxy class that always returns the current singleton DIContainer instance"""

    def __getattr__(self, name):
        """Delegate all attribute access to the current singleton instance"""
        current_instance = DIContainer()
        return getattr(current_instance, name)

    def __call__(self, *args, **kwargs):
        """Make the proxy callable like DIContainer"""
        return DIContainer(*args, **kwargs)

    def __repr__(self):
        """Return representation of current singleton instance"""
        current_instance = DIContainer()
        return repr(current_instance)

    def __str__(self):
        """Return string representation of current singleton instance"""
        current_instance = DIContainer()
        return str(current_instance)

    def __eq__(self, other):
        """Compare with other objects based on current singleton instance"""
        current_instance = DIContainer()
        # Handle identity comparison with DIContainer instances
        if isinstance(other, DIContainer):
            return current_instance is other
        return current_instance == other

    def __hash__(self):
        """Return hash of current singleton instance"""
        current_instance = DIContainer()
        return hash(current_instance)

    def __class_getitem__(cls, item):
        """Support for isinstance checks"""
        return DIContainer.__class_getitem__(item)

    def __instancecheck__(cls, instance):
        """Make isinstance work with GlobalContainer"""
        return isinstance(instance, DIContainer)

    @property
    def __class__(self):
        """Return DIContainer class for isinstance checks"""
        return DIContainer


# Global container instance - always points to current singleton
container = GlobalContainer()
