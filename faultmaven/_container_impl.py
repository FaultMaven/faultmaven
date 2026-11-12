"""Dependency Injection Container

Purpose: Centralized dependency management for the FaultMaven architecture

This container manages the lifecycle and dependencies of all components following
the interface-based dependency injection pattern.

Core Responsibilities:
- Singleton container, composed once by an awaited initialize()
- Dependency graph resolution for all services via DependencyRegistry
- Configuration management from environment variables
- Proper error handling with specific exceptions

Key Components:
- Infrastructure layer: LLM providers, security, observability
- Core tools: Knowledge base, web search
- Service layer: Agent, data, knowledge services
- Proper interface implementations and dependency injection
"""

import functools
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, ClassVar, List, Optional

from faultmaven.config.settings import FaultMavenSettings, get_settings
from faultmaven.container.base import BaseDIContainer
from faultmaven.container.errors import (
    InitializationError,
    RequiredServiceConstructionError,
    ServiceUnavailableError,
)
from faultmaven.container.providers import (
    register_infrastructure,
    register_services,
    register_tools,
)
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.concurrency.teardown import (
    run_teardown,
    teardown_step,
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


class DIContainer(BaseDIContainer):
    """Singleton dependency injection container for centralized component management.

    Extends BaseDIContainer to inherit:
    - DependencyRegistry for service lifecycle tracking
    - Standardized service access patterns
    - Health check infrastructure
    """

    def __new__(cls):
        # The parent's singleton, built from ``_CONSTRUCTED_STATE``.
        return super().__new__(cls)

    async def initialize(self, allow_degraded: bool = False):
        """Initialize all dependencies with proper error handling (async for proper event loop handling).

        A call that leaves the container initialized takes one hold for its
        caller's context, which the same context's ``shutdown()`` releases: the
        composition, and a later call that finds it initialized and shares it
        (#1843). A call that raises, returns degraded or finds a composition in
        progress takes none: its composer's shutdown closes nothing beside a
        live holder in another context, and closes what is recorded when no
        hold exists.

        Args:
            allow_degraded: opt in to the lenient path under pytest, where a
                composition failure otherwise raises so it names itself (#823).
                Never an escape from ``settings.must_not_degrade`` — a
                deployment that must not degrade refuses either way.
        """
        logger = logging.getLogger(__name__)

        if self._initialized:
            logger.debug("Container already initialized, skipping")
            self._holds.take()
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
            self._holds.take()
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

            # Nor does a ChromaDB client that must not be built (a malformed
            # credential, a chromadb with no session to bound, an HTTP client
            # built outside the door): no caller may answer it with a degraded
            # mode, and a container serving without its service layer is a
            # worse one than the fallback it replaces (#2056).
            from faultmaven.infrastructure.chroma_client import (
                ChromaClientConfigError,
            )

            if isinstance(e, ChromaClientConfigError):
                raise

            # Nor does a service the deployment configured and could not
            # construct (#2103), in any environment, pytest and allow_degraded
            # included: serving without it, or with an in-memory stand-in, is
            # what the refusal replaces.
            if isinstance(e, RequiredServiceConstructionError):
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
                self._holds.take()
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
        # None for the same reason: the session modules cannot import without
        # the interfaces this container exists for, and a second, hand-written
        # session service fell behind the real one (#1855).
        self.session_service = None

        logging.getLogger(__name__).info("Created minimal container for testing")

    def get_settings(self) -> FaultMavenSettings:
        """Get the unified settings instance"""
        if not hasattr(self, "settings") or self.settings is None:
            self.settings = get_settings()
        return self.settings

    # A getter never composes the container (#2249). Four used to, lazily:
    # asyncio.run(initialize()) with no loop running bound the composition, its
    # Redis pool included, to a loop that closed as the getter returned, and
    # create_task(initialize()) with one running answered from a half-composed
    # container while nothing awaited the task. Every process initializes
    # before its first getter (the lifespan, the jobs runner, the fm-*
    # commands), so a getter answers from what is composed.

    def get_agent_service(self):
        """Get the agent service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "Agent service requested but container not initialized - this should not happen after startup"
                )
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
        # Returns None when composition did not produce one. It used to
        # substitute an in-memory stub, which made a partially composed
        # container indistinguishable from a working one: `is None` guards in
        # kb_seed and fm-reset-kb never fired, and the stub answered document
        # reads with fabricated content (#899). A missing service is now
        # visible to the caller, as with every sibling getter.
        return getattr(self, "knowledge_service", None)

    def get_oauth_service(self):
        """Get the OAuth service (if enabled)."""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "OAuth service requested but container not initialized - this should not happen after startup"
                )
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

    #: Whether the calling context's latest ``shutdown()`` was the last release,
    #: once it decided. On the class: a reset rebuilds what is on the instance,
    #: and a last release's closes come after its reset.
    _last_release: ClassVar[ContextVar[Optional[bool]]] = ContextVar(
        "container_last_release", default=None
    )

    def released_last(self) -> Optional[bool]:
        """Whether this context's latest :meth:`shutdown` decided it was the last
        release, ``None`` if it ended before deciding. Read when it raised."""
        return self._last_release.get()

    async def shutdown(self) -> bool:
        """Release the calling context's hold. With none left anywhere, close and reset.

        Returns ``False`` while another context still holds the container: two
        live lifespans share it (the second ``initialize()`` returns early), so
        the first to end closes nothing, nor may its composer dispose the
        process-global database engine or stop the serving state (#1843). A
        context that took no hold releases nothing.

        With no hold left anywhere it first drains the work in flight
        (``inflight.drain``, #2490): that work still reads the container and
        writes through what it owns. A hold taken during the drain makes this
        release not the last, even if it is released before the drain ends:
        this release then cancels, resets and closes nothing, and returns
        ``False`` (or raises the drain's interruption); that hold's own release
        drains and closes.
        Otherwise it stops the case-lease renewals, unless the drain was
        interrupted: it then cancels the cancellable rest with the grace, which
        a second interruption ends, and the leases of the work still running
        keep renewing, through the Redis client it closes. A hold taken during
        that grace makes it not the last, even once it is released: it resets
        and closes nothing, and raises the interruption, read as not the last.
        Else it resets, so an ``initialize()`` during the closes composes
        afresh and keeps its hold.
        Then it closes everything recorded with :meth:`own`, newest first,
        through ``run_teardown`` (every close runs, a failed one is logged, an
        interruption, the drain's included, is held until all have run), and
        returns ``True``. A last release keeps ``inflight.draining()`` raised
        from its drain until its closes end, whatever composes or resets
        meanwhile, so no evidence vector write starts under a client it closes
        (#2532); a second composition's are refused during the closes too. The
        knowledge base's vector write reads no such flag (#2562). Once it has
        decided, it records for the calling context whether it was the last
        (:meth:`released_last`): a composer whose shutdown raised reads that
        decision. After a last release, whether it returned or raised, the
        composer yields to any hold still held when it reads (all taken since
        its reset); after one that failed before deciding, it reads only whether
        the container is held. Whoever composes shuts down on every path, while
        the loop the clients are bound to still runs.
        """
        self._last_release.set(None)
        if not self._holds.release():
            self._last_release.set(False)
            return False
        holds = self._holds
        taken = holds.takes
        # No hold taken on this instance since this release began. Sticky: once
        # false it stays false, so the drain's flag and this release agree.
        last = lambda: self._holds is holds and holds.takes == taken
        with inflight.raised(last):  # the draining flag, to the reset (#2532)
            interrupted = await teardown_step(
                "Draining the work in flight",
                functools.partial(_drain_work_in_flight, last),
            )
            if not last():
                self._last_release.set(False)
                if interrupted is not None:
                    raise interrupted
                return False
            if interrupted is None:
                # Synchronous: no hold can be taken between the check and the reset.
                interrupted = await teardown_step(
                    "Letting the case leases lapse", _let_leases_lapse
                )
            else:
                await teardown_step(
                    "Cancelling the cancellable rest", _cancel_cancellable
                )
                if not last():  # a hold taken during the grace
                    self._last_release.set(False)
                    raise interrupted
            self._last_release.set(True)
            owned = self._owned[::-1]
            self._owned.clear()  # closed once, whatever the closes do
            self.reset()
        # Then until the closes end, whatever composes or resets meanwhile: the
        # flag is the process's, so it cannot tell this composition's writes
        # from a later one's. The reset is synchronous, so there is no gap.
        with inflight.raised(lambda: True):
            try:
                await run_teardown(
                    [
                        (f"Closing {what}", functools.partial(close, resource))
                        for what, resource, close in owned
                    ]
                )
            finally:
                if interrupted is not None:
                    raise interrupted
        return True

    async def release_every_hold(self) -> bool:
        """Void every hold on the container, then shut it down. For tests only.

        A test's holds can sit in contexts its fixtures never enter (a fixture's
        setup, the test's own task), and they all end with the test. The
        shutdown is a last release, so it drains the work in flight first.
        """
        self._holds = type(self._holds)()
        return await self.shutdown()

    def reset(self):
        """Return the container to what construction built.

        Keeps exactly the names ``BaseDIContainer._CONSTRUCTED_STATE`` lists,
        the table construction builds from, and builds each again from its
        factory, so ``settings`` is ``None`` once more. Every attribute
        ``initialize``, a provider or a getter's cache put on the instance is
        removed, whatever its name: tools, adapters and extractors set as plain
        attributes, and the ``hasattr`` cache of ``get_job_service``. One left
        behind is handed to the next composition as-is, built over the
        previous one's Redis client, which the shutdown that preceded this
        reset closed.

        Refuses while anything is owned (``own``), naming it: whatever it
        dropped could no longer be closed. Only ``shutdown()`` and tests call it.
        """
        super().reset()
        constructed = {name for name, _factory in self._CONSTRUCTED_STATE}
        for name in [name for name in vars(self) if name not in constructed]:
            delattr(self, name)
        for name, factory in self._CONSTRUCTED_STATE:
            setattr(self, name, factory())


async def _drain_work_in_flight(last) -> None:
    """The last release's drain."""
    await inflight.drain(last=last)


def _let_leases_lapse() -> None:
    inflight.let_leases_lapse()


async def _cancel_cancellable() -> None:
    await inflight.cancel_cancellable()


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
