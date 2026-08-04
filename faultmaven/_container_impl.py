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
# The current system uses AgentOrchestrationService in modules/agent/ instead.
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

    def get_preprocessing_service(self):
        """Get the preprocessing service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "Preprocessing service requested but container not initialized"
                )
        return getattr(self, "preprocessing_service", None)

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

    def get_llm_provider(self):
        """Get the LLM provider (router) from the container."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "llm_provider", None)

    def get_sanitizer(self):
        """Get the sanitizer service."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "sanitizer", None)

    def get_tracer(self):
        """Get the tracer service."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "tracer", None)

    def get_tools(self):
        """Get the registered tools list."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "tools", [])

    def get_data_classifier(self):
        """Get the data classifier."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "data_classifier", None)

    def get_log_processor(self):
        """Get the log processor."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "log_processor", None)

    def get_vector_store(self):
        """Get the vector store."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "vector_store", None)

    def get_session_store(self):
        """Get the session store."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "session_store", None)

    def get_session_service(self):
        """Get the session service."""
        if not self._initialized and not getattr(self, "_initializing", False):
            self._ensure_initialized_for_getter()
        return getattr(self, "session_service", None)

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

    def get_report_recommendation_service(self):
        """Get the report recommendation service (TASK-024)"""
        if not self._initialized:
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "report_recommendation_service", None)

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
        from datetime import datetime, timedelta

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
                # Signature mirrors AuthSessionService.create_session so a call
                # shaped against the stand-in also binds on the real service.
                session_id = str(uuid.uuid4())
                session = MockSessionContext(session_id, user_id, metadata)
                session.client_id = client_id
                self.sessions[session_id] = session
                return session

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
                sessions = list(self.sessions.values())
                if user_id:
                    return [s for s in sessions if s.user_id == user_id]
                return sessions

            async def delete_session(self, session_id):
                if session_id in self.sessions:
                    del self.sessions[session_id]
                    return True
                return False

            async def update_last_activity(self, session_id):
                if session_id in self.sessions:
                    self.sessions[session_id].last_activity = datetime.now(timezone.utc)
                    return True
                return False

        return MinimalSessionService()

    def _create_minimal_case_service(self):
        """Create a minimal case service for testing environments"""
        import uuid
        from datetime import datetime

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
                initial_query=None,
                priority=None,
                user_id=None,
                organization_id=None,
                metadata=None,
            ):
                # Generate case_id matching required pattern ^case_[a-f0-9]{12}$
                case_id = f"case_{uuid.uuid4().hex[:12]}"

                # Validate owner_id is required (match real CaseService behavior)
                if not owner_id or not owner_id.strip():
                    from faultmaven.exceptions import ValidationException

                    raise ValidationException("Owner ID is required")

                # Create case with proper Case model structure
                final_user_id = user_id or owner_id
                final_org_id = (
                    organization_id or owner_id
                )  # Use owner_id as organization_id if not provided

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
                    organization_id=final_org_id,
                    status=CaseState.INQUIRY,
                    message_count=message_count,
                )

                self.cases[case_id] = case

                # Store initial_message as first user message if provided
                if initial_message and initial_message.strip():
                    if case_id not in self.case_messages:
                        self.case_messages[case_id] = []

                    initial_msg = {
                        "message_id": f"initial_{case_id}",
                        "case_id": case_id,
                        "message_type": "user_query",
                        "content": initial_message.strip(),
                        "timestamp": current_time,
                        "user_id": final_user_id,
                    }
                    self.case_messages[case_id].append(initial_msg)

                return case

            async def get_case(self, case_id, user_id=None):
                return self.cases.get(case_id)

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
                    and getattr(case, "message_count", 1) > 0
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

            async def list_user_cases(
                self, user_id=None, filters=None, limit=20, offset=0
            ):
                """List cases for a user with pagination - Phase 1: Core filtering implementation"""
                # Filter cases by user_id if provided
                if user_id:
                    user_cases = [
                        case for case in self.cases.values() if case.user_id == user_id
                    ]
                else:
                    # Return all cases if no user filter
                    user_cases = list(self.cases.values())

                # Phase 1: Apply core filtering - exclude deleted/archived/empty by default
                if filters:
                    # Phase 1: Default filtering behavior (exclude terminal cases)
                    if not getattr(filters, "include_deleted", False):
                        # Exclude closed cases
                        user_cases = [
                            case
                            for case in user_cases
                            if case.state != CaseState.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        user_cases = [
                            case
                            for case in user_cases
                            if case.state not in [CaseState.RESOLVED, CaseState.CLOSED]
                        ]

                    if not getattr(filters, "include_empty", False):
                        # Exclude empty cases (message_count == 0)
                        # For MinimalCaseService, we'll consider all cases as having at least 1 message unless explicitly marked
                        user_cases = [
                            case
                            for case in user_cases
                            if getattr(case, "message_count", 1) > 0
                        ]

                    # Apply other existing filters
                    if hasattr(filters, "state") and filters.state:
                        user_cases = [
                            case for case in user_cases if case.state == filters.state
                        ]
                    if hasattr(filters, "priority") and filters.priority:
                        user_cases = [
                            case
                            for case in user_cases
                            if case.priority == filters.priority
                        ]
                    if hasattr(filters, "owner_id") and filters.owner_id:
                        user_cases = [
                            case
                            for case in user_cases
                            if case.owner_id == filters.owner_id
                        ]
                else:
                    # Phase 1: No filters provided - apply default exclusions
                    # Only show active (non-terminal) cases by default
                    user_cases = [
                        case
                        for case in user_cases
                        if case.state in [CaseState.INQUIRY, CaseState.INVESTIGATING]
                    ]
                    # Exclude empty cases by default
                    user_cases = [
                        case
                        for case in user_cases
                        if getattr(case, "message_count", 1) > 0
                    ]

                # Extract pagination parameters from filters if available
                if filters and hasattr(filters, "limit"):
                    limit = filters.limit
                if filters and hasattr(filters, "offset"):
                    offset = filters.offset

                # Total match count is computed BEFORE pagination so it agrees
                # with the returned page (mirrors CaseService.list_user_cases,
                # which returns (page, total)).
                total = len(user_cases)
                paginated_cases = user_cases[offset : offset + limit]

                return paginated_cases, total

            async def list_all_cases(self, filters=None):
                """List all in-memory cases as summaries (admin cross-tenant read; degraded double).

                Mirrors ``CaseService.list_all_cases``: returns
                ``(List[CaseSummary], total)`` so the ``CaseListResponse``
                response model validates even when this fallback is active.
                """
                from faultmaven.models.api_models import CaseSummary

                all_cases = list(self.cases.values())
                if filters and getattr(filters, "state", None):
                    all_cases = [c for c in all_cases if c.state == filters.state]
                total = len(all_cases)
                limit = getattr(filters, "limit", 50) if filters else 50
                offset = getattr(filters, "offset", 0) if filters else 0
                summaries = []
                for case in all_cases[offset : offset + limit]:
                    try:
                        summaries.append(CaseSummary.from_case(case))
                    except Exception:
                        pass
                return summaries, total

            async def count_user_cases(self, user_id: str, filters=None):
                """Count cases for a user with filters - Phase 1: Mirror filtering from list_user_cases

                ``user_id`` is REQUIRED, matching CaseService.count_user_cases:
                a call that omitted it bound here and failed on the real service.
                """
                # Filter cases by user_id if provided
                if user_id:
                    user_cases = [
                        case for case in self.cases.values() if case.owner_id == user_id
                    ]
                else:
                    # Return all cases if no user filter
                    user_cases = list(self.cases.values())

                # Phase 1: Apply same core filtering as list_user_cases
                if filters:
                    # Phase 1: Default filtering behavior (exclude terminal cases)
                    if not getattr(filters, "include_deleted", False):
                        # Exclude closed cases
                        user_cases = [
                            case
                            for case in user_cases
                            if case.state != CaseState.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        user_cases = [
                            case
                            for case in user_cases
                            if case.state not in [CaseState.RESOLVED, CaseState.CLOSED]
                        ]

                    if not getattr(filters, "include_empty", False):
                        # Exclude empty cases (message_count == 0)
                        user_cases = [
                            case
                            for case in user_cases
                            if getattr(case, "message_count", 1) > 0
                        ]

                    # Apply other existing filters
                    if hasattr(filters, "state") and filters.state:
                        user_cases = [
                            case for case in user_cases if case.state == filters.state
                        ]
                    if hasattr(filters, "priority") and filters.priority:
                        user_cases = [
                            case
                            for case in user_cases
                            if case.priority == filters.priority
                        ]
                    if hasattr(filters, "owner_id") and filters.owner_id:
                        user_cases = [
                            case
                            for case in user_cases
                            if case.owner_id == filters.owner_id
                        ]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_user_cases)
                    # Only show active (non-terminal) cases by default
                    user_cases = [
                        case
                        for case in user_cases
                        if case.state in [CaseState.INQUIRY, CaseState.INVESTIGATING]
                    ]
                    # Exclude empty cases by default
                    user_cases = [
                        case
                        for case in user_cases
                        if getattr(case, "message_count", 1) > 0
                    ]

                return len(user_cases)

            async def hard_delete_case(self, case_id: str, user_id: str = None) -> bool:
                """Permanently delete a case and all associated data (idempotent)"""
                # For MinimalCaseService, just remove from memory
                # Always return True for idempotent behavior
                if case_id in self.cases:
                    del self.cases[case_id]
                return True

            async def get_case_messages(
                self, case_id: str, limit: int = 50, offset: int = 0
            ):
                """Get messages for a case"""
                if case_id not in self.case_messages:
                    return []

                messages = self.case_messages[case_id]
                # Apply pagination
                start = offset
                end = start + limit
                return messages[start:end]

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
                                msg_type = msg.get("message_type")
                                message_id = msg.get("message_id")
                                content = msg.get("content", "")
                                timestamp = msg.get("timestamp")
                            else:
                                msg_type = getattr(msg, "message_type", None)
                                message_id = getattr(msg, "message_id", None)
                                content = getattr(msg, "content", "")
                                timestamp = getattr(msg, "timestamp", None)

                            # Map message_type to role
                            role = None
                            if hasattr(msg_type, "value"):
                                msg_type = msg_type.value
                            if msg_type in ("user_query", "case_note"):
                                role = "user"
                            elif msg_type in ("agent_response",):
                                role = "assistant"  # Frontend expects "assistant", not "agent"

                            # Skip non user/assistant roles
                            if role is None:
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

            async def add_case_query(
                self, case_id: str, query_text: str, user_id: Optional[str] = None
            ) -> bool:
                """Add a query message to a case.

                Parameter is ``query_text``, matching
                ``CaseService.add_case_query`` — the stand-in previously named
                it ``query``, so a keyword call that bound here failed on the
                real service.
                """
                if case_id not in self.cases:
                    return False

                if case_id not in self.case_messages:
                    self.case_messages[case_id] = []

                # Add user query message
                query_msg = {
                    "message_id": f"query_{len(self.case_messages[case_id])}_{case_id}",
                    "case_id": case_id,
                    "message_type": "user_query",
                    "content": query_text.strip(),
                    "timestamp": datetime.now(timezone.utc),
                    "user_id": user_id or "anonymous",
                }
                self.case_messages[case_id].append(query_msg)

                # Update case metadata
                case = self.cases[case_id]
                case.message_count = len(self.case_messages[case_id])
                case.updated_at = datetime.now(timezone.utc)

                return True

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
