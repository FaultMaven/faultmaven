"""Dependency Injection Container

Purpose: Centralized dependency management for the FaultMaven architecture

This container manages the lifecycle and dependencies of all components following
the interface-based dependency injection pattern.

Core Responsibilities:
- Singleton container with lazy initialization
- Dependency graph resolution for all services
- Configuration management from environment variables
- Proper error handling and fallback mechanisms

Key Components:
- Infrastructure layer: LLM providers, security, observability
- Core tools: Knowledge base, web search
- Service layer: Agent, data, knowledge services
- Proper interface implementations and dependency injection
"""

from typing import List, Optional, Any
import logging
from faultmaven.config.settings import FaultMavenSettings, get_settings

# Import interfaces with graceful fallback for testing environments
try:
    from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool, IVectorStore, ISessionStore
    from faultmaven.models.interfaces_case import ICaseStore, ICaseService
    from faultmaven.models.interfaces_report import IReportStore
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
    IReportStore = Any
    INTERFACES_AVAILABLE = False
# Agentic Framework Components
try:
    from faultmaven.services.agentic import (
        AgentStateManager,
        ToolSkillBroker,
        GuardrailsPolicyLayer,
        ResponseSynthesizer,
        ErrorFallbackManager,
        BusinessLogicWorkflowEngine
    )
    from faultmaven.models.agentic import (
        IAgentStateManager,
        IToolSkillBroker,
        IGuardrailsPolicyLayer,
        IResponseSynthesizer,
        IErrorFallbackManager,
        IBusinessLogicWorkflowEngine
    )
    AGENTIC_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Agentic framework not available: {e}")
    # Create placeholder types for testing environments
    IAgentStateManager = Any
    # Removed: IQueryClassificationEngine (superseded by doctor/patient)
    IToolSkillBroker = Any
    IGuardrailsPolicyLayer = Any
    IResponseSynthesizer = Any
    IErrorFallbackManager = Any
    IBusinessLogicWorkflowEngine = Any
    AGENTIC_AVAILABLE = False



class DIContainer:
    """Singleton dependency injection container for centralized component management"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._initializing = False  # Prevent re-entrant initialization
            cls._instance.settings = None  # Will be initialized on first access
        return cls._instance
    
    async def initialize(self):
        """Initialize all dependencies with proper error handling (async for proper event loop handling)"""
        logger = logging.getLogger(__name__)

        if self._initialized:
            logger.debug("Container already initialized, skipping")
            return

        if getattr(self, '_initializing', False):
            logger.debug("Container initialization already in progress, skipping")
            return

        self._initializing = True
        logger.info("Initializing DI Container with unified settings system")

        # Initialize settings as the single source of truth
        try:
            self.settings = get_settings()
            logger.info("✅ Unified settings system initialized")
        except Exception as e:
            logger.error(f"❌ Failed to initialize settings system: {e}")
            self._initializing = False
            raise

        try:
            # Always try to create infrastructure layer first - even if interfaces not available
            # This allows tests to mock the infrastructure layer creation
            await self._create_infrastructure_layer()

            # Core tools - Domain-specific functionality
            self._create_tools_layer()

            # Service layer - Business logic orchestration
            self._create_service_layer()

            self._initialized = True
            self._initializing = False
            logger.info("✅ DI Container initialized successfully")

        except Exception as e:
            logger.error(f"❌ DI Container initialization failed: {e}")
            # Always reset _initializing flag regardless of error type
            self._initializing = False

            # Check if interfaces are available - if not, this is expected and we use minimal container
            if not INTERFACES_AVAILABLE:
                logger.warning("Interfaces not available - creating minimal container for testing")
                self._create_minimal_container()
                self._initialized = True
            else:
                # Real error with interfaces available - don't initialize
                import traceback
                logger.error(f"Critical initialization error: {traceback.format_exc()}")
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
        self.knowledge_service = self._create_minimal_knowledge_service()
        self.session_service = self._create_minimal_session_service()
        
        logging.getLogger(__name__).info("Created minimal container for testing")
    
    async def _create_infrastructure_layer(self):
        """Create infrastructure components with interface implementations using unified settings (async for Redis)"""
        logger = logging.getLogger(__name__)
        
        # Ensure settings are available
        if not hasattr(self, 'settings') or self.settings is None:
            self.settings = get_settings()
        
        # Log current configuration using settings system
        logger.info(f"🔍 Container: Configuration check during infrastructure creation:")
        logger.info(f"🔍 Container: CHAT_PROVIDER = {self.settings.llm.provider}")
        logger.info(f"🔍 Container: LLM_REQUEST_TIMEOUT = {self.settings.llm.request_timeout}")
        logger.info(f"🔍 Container: SKIP_SERVICE_CHECKS = {self.settings.server.skip_service_checks}")
        
        # Legacy config removed - now using unified settings system exclusively
        
        # Data sanitization for PII protection
        from faultmaven.infrastructure.security.redaction import DataSanitizer
        logger.debug(f"Protection config loaded: enabled={self.settings.protection.protection_enabled}")
        self.sanitizer: ISanitizer = DataSanitizer(settings=self.settings)
        
        # Distributed tracing (initialize first to set up environment variables)
        from faultmaven.infrastructure.observability.tracing import OpikTracer
        logger.debug(f"Observability config loaded: enabled={self.settings.observability.tracing_enabled}")
        self.tracer: ITracer = OpikTracer(settings=self.settings)
        
        # LLM Provider (initialize after Opik tracer to ensure environment is properly set up)
        from faultmaven.infrastructure.llm.router import LLMRouter
        # LLMRouter does not accept settings; it reads runtime config internally
        self.llm_provider: ILLMProvider = LLMRouter()
        
        # Core processing interfaces (legacy log processor)
        from faultmaven.core.processing.log_analyzer import LogProcessor
        self.log_processor = LogProcessor()

        # New preprocessing pipeline (Phase 1-4)
        from faultmaven.services.preprocessing.classifier import DataClassifier
        from faultmaven.services.preprocessing.extractors import LogsAndErrorsExtractor, StructuredConfigExtractor, MetricsAndPerformanceExtractor, UnstructuredTextExtractor, SourceCodeExtractor, VisualEvidenceExtractor
        from faultmaven.services.preprocessing.chunking_service import ChunkingService
        from faultmaven.services.preprocessing.preprocessing_service import PreprocessingService
        from faultmaven.infrastructure.security.redaction import DataSanitizer

        self.data_classifier = DataClassifier()
        self.logs_extractor = LogsAndErrorsExtractor()
        self.config_extractor = StructuredConfigExtractor()
        self.metrics_extractor = MetricsAndPerformanceExtractor()
        self.text_extractor = UnstructuredTextExtractor()
        self.source_code_extractor = SourceCodeExtractor()
        self.visual_extractor = VisualEvidenceExtractor()
        self.data_sanitizer = DataSanitizer()

        # ChunkingService for large documents (Phase 4)
        self.chunking_service = ChunkingService(
            llm_router=self.llm_provider,
            chunk_size_tokens=self.settings.preprocessing.chunk_size_tokens,
            overlap_tokens=self.settings.preprocessing.chunk_overlap_tokens,
            max_parallel_chunks=self.settings.preprocessing.map_reduce_max_parallel
        )

        self.preprocessing_service = PreprocessingService(
            classifier=self.data_classifier,
            sanitizer=self.data_sanitizer,
            logs_extractor=self.logs_extractor,
            config_extractor=self.config_extractor,
            metrics_extractor=self.metrics_extractor,
            text_extractor=self.text_extractor,
            source_code_extractor=self.source_code_extractor,
            visual_extractor=self.visual_extractor,
            chunking_service=self.chunking_service,
            chunk_trigger_tokens=self.settings.preprocessing.chunk_trigger_tokens
        )
        
        # Vector store for knowledge base
        from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
        try:
            if not self.settings.server.skip_service_checks:
                self.vector_store: IVectorStore = ChromaDBVectorStore()
                logger.debug("Vector store initialized")
            else:
                logger.info("Skipping vector store initialization (SKIP_SERVICE_CHECKS=True)")
                self.vector_store = None
        except Exception as e:
            logger.warning(f"Vector store initialization failed: {e}")
            self.vector_store = None

        # Case vector store for Session-Specific RAG (Working Memory)
        from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
        try:
            if not self.settings.server.skip_service_checks:
                # Lifecycle-based cleanup (deleted when case closes/archives)
                self.case_vector_store = CaseVectorStore()
                logger.debug("Case vector store initialized for Session-Specific RAG (lifecycle-based cleanup)")
            else:
                logger.info("Skipping case vector store initialization (SKIP_SERVICE_CHECKS=True)")
                self.case_vector_store = None
        except Exception as e:
            logger.warning(f"Case vector store initialization failed: {e}")
            self.case_vector_store = None

        # User KB vector store for persistent user knowledge bases
        from faultmaven.infrastructure.persistence.user_kb_vector_store import UserKBVectorStore
        try:
            if not self.settings.server.skip_service_checks:
                # Permanent storage (documents persist until explicitly deleted)
                self.user_kb_vector_store = UserKBVectorStore()
                logger.debug("User KB vector store initialized for persistent runbooks")
            else:
                logger.info("Skipping user KB vector store initialization (SKIP_SERVICE_CHECKS=True)")
                self.user_kb_vector_store = None
        except Exception as e:
            logger.warning(f"User KB vector store initialization failed: {e}")
            self.user_kb_vector_store = None

        # Redis client for persistence (sessions, cases, KB metadata)
        try:
            if not self.settings.server.skip_service_checks:
                from faultmaven.infrastructure.redis_client import create_redis_client, validate_redis_connection
                self.redis_client = create_redis_client()

                # Validate connection in async context (ensures event loop is properly bound)
                await validate_redis_connection(self.redis_client)
                logger.info("✅ Redis client initialized and validated for application persistence")

            else:
                logger.info("Skipping Redis client initialization (SKIP_SERVICE_CHECKS=True)")
                self.redis_client = None
        except Exception as e:
            logger.warning(f"Redis client initialization failed: {e}")
            self.redis_client = None
        
        # Session store for session management (fail-fast unless skipped)
        from faultmaven.infrastructure.persistence.redis_session_store import RedisSessionStore
        from faultmaven.infrastructure.persistence.redis_session_manager import RedisSessionManager
        try:
            if not self.settings.server.skip_service_checks:
                # Create RedisSessionStore for low-level ISessionStore interface (used by CaseService, etc.)
                self.session_store = RedisSessionStore()
                logger.debug("Session store initialized with RedisSessionStore")
            else:
                logger.info("Skipping session store initialization (SKIP_SERVICE_CHECKS=True)")
                self.session_store = None
        except Exception as e:
            # In production, Redis session store is a hard dependency - fail fast
            if self.settings.is_production() and not self.settings.server.skip_service_checks:
                logger.error(f"CRITICAL: Redis session store required in production but initialization failed: {e}")
                logger.error("Production deployment cannot continue without Redis session store")
                raise RuntimeError(f"Redis session store required in production: {e}") from e
            
            # Only fall back to minimal service in non-production environments
            logger.warning(f"Redis session store initialization failed in non-production environment; continuing with in-memory minimal service: {e}")
            self.session_store = None
        
        # Case store for case persistence (optional feature)
        try:
            from faultmaven.infrastructure.persistence.redis_case_store import RedisCaseStore
            if not self.settings.server.skip_service_checks:
                self.case_store: ICaseStore = RedisCaseStore(redis_client=self.redis_client)
                logger.debug("Case store initialized")
            else:
                logger.debug("Case store skipped (SKIP_SERVICE_CHECKS=True)")
                self.case_store = None
        except ImportError:
            logger.debug("Case store not available - case persistence disabled")
            self.case_store = None
        except Exception as e:
            logger.warning(f"Case store initialization failed: {e}")
            self.case_store = None

        # Report store for report persistence (requires vector_store and redis_client)
        try:
            from faultmaven.infrastructure.persistence.redis_report_store import RedisReportStore
            if not self.settings.server.skip_service_checks and self.redis_client and self.vector_store:
                self.report_store: IReportStore = RedisReportStore(
                    redis_client=self.redis_client,
                    vector_store=self.vector_store,
                    runbook_kb=None  # Will be injected later after RunbookKnowledgeBase is initialized
                )
                logger.debug("Report store initialized")
            else:
                logger.debug("Report store skipped (missing dependencies or SKIP_SERVICE_CHECKS=True)")
                self.report_store = None
        except ImportError:
            logger.debug("Report store not available - report persistence disabled")
            self.report_store = None
        except Exception as e:
            logger.warning(f"Report store initialization failed: {e}")
            self.report_store = None

        # Authentication services initialization
        try:
            from faultmaven.infrastructure.auth.token_manager import DevTokenManager
            from faultmaven.infrastructure.auth.user_store import DevUserStore

            if not self.settings.server.skip_service_checks and self.redis_client:
                self.token_manager = DevTokenManager(redis_client=self.redis_client)
                self.user_store = DevUserStore(redis_client=self.redis_client)
                logger.info("✅ Authentication services initialized (token manager + user store)")
            else:
                logger.debug("Authentication services skipped (no Redis client or SKIP_SERVICE_CHECKS=True)")
                self.token_manager = None
                self.user_store = None
        except ImportError as e:
            logger.debug(f"Authentication services not available: {e}")
            self.token_manager = None
            self.user_store = None
        except Exception as e:
            logger.warning(f"Authentication services initialization failed: {e}")
            self.token_manager = None
            self.user_store = None

        logger.debug("Infrastructure layer created with settings-based dependency injection")
    
    def _create_tools_layer(self):
        """Create tools using the registry pattern with settings injection"""
        logger = logging.getLogger(__name__)
        from faultmaven.tools.registry import tool_registry
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester

        # Import tools to trigger registration
        import faultmaven.tools.knowledge_base
        import faultmaven.tools.web_search

        # Create knowledge ingester for tools that need it
        try:
            if not self.settings.server.skip_service_checks:
                ingester = KnowledgeIngester(settings=self.settings)
            else:
                logger.debug("KnowledgeIngester skipped (SKIP_SERVICE_CHECKS=True)")
                ingester = None
        except Exception as e:
            logger.warning(f"KnowledgeIngester creation failed: {e}")
            ingester = None

        # Create all registered tools with settings
        self.tools: List[BaseTool] = tool_registry.create_all_tools(
            knowledge_ingester=ingester,
            settings=self.settings
        )

        # Create KB-neutral document Q&A tools (Strategy Pattern)
        # Three tool instances from ONE DocumentQATool class configured differently
        try:
            if not self.settings.server.skip_service_checks and hasattr(self, 'case_vector_store') and self.case_vector_store:
                from faultmaven.tools.case_evidence_qa import AnswerFromCaseEvidence
                from faultmaven.tools.user_kb_qa import AnswerFromUserKB
                from faultmaven.tools.global_kb_qa import AnswerFromGlobalKB

                # All three use same LLM router but configured differently via KBConfig strategy pattern
                # Tool 1 and 3 share case_vector_store, Tool 2 uses dedicated user_kb_vector_store

                # Tool 1: Case Evidence (case-scoped forensic analysis)
                self.case_evidence_qa_tool = AnswerFromCaseEvidence(
                    vector_store=self.case_vector_store,
                    llm_router=self.llm_provider
                )

                # Tool 2: User KB (user-scoped personal runbooks) - uses dedicated store
                if hasattr(self, 'user_kb_vector_store') and self.user_kb_vector_store:
                    self.user_kb_qa_tool = AnswerFromUserKB(
                        vector_store=self.user_kb_vector_store,  # Dedicated user KB store
                        llm_router=self.llm_provider
                    )
                else:
                    logger.warning("User KB QA tool skipped (user_kb_vector_store not available)")
                    self.user_kb_qa_tool = None

                # Tool 3: Global KB (system-wide best practices)
                self.global_kb_qa_tool = AnswerFromGlobalKB(
                    vector_store=self.case_vector_store,  # Shares case vector store
                    llm_router=self.llm_provider
                )

                # Add available tools to agent's tool list
                tools_to_add = [self.case_evidence_qa_tool, self.global_kb_qa_tool]
                if self.user_kb_qa_tool:
                    tools_to_add.insert(1, self.user_kb_qa_tool)

                self.tools.extend(tools_to_add)

                logger.info(
                    f"✅ Created 3 KB-neutral document Q&A tools from single DocumentQATool class "
                    f"(case evidence, user KB, global KB) - {len(self.tools)} total tools"
                )
            else:
                logger.debug("Document Q&A tools skipped (no case_vector_store or SKIP_SERVICE_CHECKS=True)")
                self.case_evidence_qa_tool = None
                self.user_kb_qa_tool = None
                self.global_kb_qa_tool = None
        except Exception as e:
            logger.warning(f"Document Q&A tools creation failed: {e}")
            self.case_evidence_qa_tool = None
            self.user_kb_qa_tool = None
            self.global_kb_qa_tool = None

        logger.debug(
            f"Tools layer created with {len(self.tools)} tools: "
            f"{tool_registry.list_tools() + (['case_evidence_qa', 'user_kb_qa', 'global_kb_qa'] if self.case_evidence_qa_tool else [])}"
        )
    
    def _create_service_layer(self):
        """Create service layer with interface dependencies"""
        import logging
        logger = logging.getLogger(__name__)
        
        from faultmaven.services.agentic.orchestration.agent_service import AgentService
        from faultmaven.services.domain.data_service import DataService
        from faultmaven.services.domain.knowledge_service import KnowledgeService
        from faultmaven.services.domain.session_service import SessionService
        
        # Case Service - Case persistence and management (optional)
        try:
            from faultmaven.services.domain.case_service import CaseService
            if hasattr(self, 'case_store') and self.case_store:
                self.case_service: ICaseService = CaseService(
                    case_store=self.case_store,
                    session_store=self.get_session_store(),
                    report_store=self.get_report_store(),
                    case_vector_store=self.case_vector_store,
                    settings=self.settings
                )
                logger.debug("Case service initialized")
            else:
                # Use cached minimal case service to maintain state across requests
                self.case_service = self._create_minimal_case_service()
                logger.debug("Case service using minimal in-memory implementation - case store not available")
        except ImportError:
            logger.debug("Case service not available, using cached minimal implementation")
            # Use cached minimal case service to maintain state across requests
            self.case_service = self._create_minimal_case_service()
        except Exception as e:
            logger.warning(f"Case service initialization failed: {e}, using cached minimal implementation")
            # Use cached minimal case service to maintain state across requests  
            self.case_service = self._create_minimal_case_service()

        # Session Service - Session management and validation
        try:
            # If no real session store is available, use minimal in-memory service
            if self.get_session_store() is None:
                logging.getLogger(__name__).info("Session store unavailable; using minimal in-memory session service")
                self.session_service = self._create_minimal_session_service()
            else:
                # Create session service directly with session store
                self.session_service = SessionService(
                    session_store=self.get_session_store(),
                    case_service=self.case_service,  # Inject case service for enhanced features
                    settings=self.settings
                )
        except Exception:
            # Create a minimal session service for testing
            self.session_service = self._create_minimal_session_service()
            
        # Agentic Framework Services - Must be created before AgentService
        self._create_agentic_framework_services()
            
        # Agent Service - Core troubleshooting orchestration with Agentic Framework
        # Only create if not already set or if it's a mock object (testing scenario)
        from unittest.mock import MagicMock
        if (not hasattr(self, 'agent_service') or
            self.agent_service is None or
            isinstance(self.agent_service, MagicMock)):
            self.agent_service = AgentService(
                llm_provider=self.get_llm_provider(),
                tools=self.get_tools(),
                tracer=self.get_tracer(),
                sanitizer=self.get_sanitizer(),
                session_service=self.session_service,
                case_service=self.case_service,
                settings=self.settings,
                # Agentic Framework Components (required - direct access during initialization)
                business_logic_workflow_engine=self.business_logic_workflow_engine,
                query_classification_engine=self.query_classification_engine,
                tool_skill_broker=self.tool_skill_broker,
                guardrails_policy_layer=self.guardrails_policy_layer,
                response_synthesizer=self.response_synthesizer,
                error_fallback_manager=self.error_fallback_manager,
                agent_state_manager=self.agent_state_manager
            )
        
        # Data Service - Data processing and analysis
        # Create simple storage backend for development
        from faultmaven.services.domain.data_service import SimpleStorageBackend
        storage_backend = SimpleStorageBackend(settings=self.settings)
        
        self.data_service = DataService(
            data_classifier=self.get_data_classifier(),
            log_processor=self.get_log_processor(),
            sanitizer=self.get_sanitizer(),
            tracer=self.get_tracer(),
            storage_backend=storage_backend,
            session_service=self.session_service,
            settings=self.settings
        )

        # Knowledge Service - Knowledge base operations
        # Create knowledge ingester and vector store placeholders
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester
        try:
            if not self.settings.server.skip_service_checks:
                knowledge_ingester = KnowledgeIngester(settings=self.settings)
            else:
                logger.debug("KnowledgeIngester skipped for KnowledgeService (SKIP_SERVICE_CHECKS=True)")
                knowledge_ingester = None
        except Exception as e:
            logger.warning(f"KnowledgeIngester creation failed: {e}")
            knowledge_ingester = None
        
        # Always create KnowledgeService; it can operate without an ingester for API upload path
        self.knowledge_service = KnowledgeService(
            knowledge_ingester=knowledge_ingester,
            sanitizer=self.get_sanitizer(),
            tracer=self.get_tracer(),
            vector_store=self.get_vector_store(),
            redis_client=getattr(self, 'redis_client', None),
            settings=self.settings
        )
        
        # Phase 2: Advanced Intelligence Services
        self._create_advanced_intelligence_services()
        
        # Phase 2: Performance Monitoring and Optimization Services
        self._create_performance_monitoring_services()
        
        # Phase 3: Enhanced Data Processing Services
        self._create_enhanced_data_processing_services()
        
        # Phase A: Microservice Foundation Services
        self._create_microservice_foundation_services()
            
        logger.debug("Service layer created with settings-based dependency injection")

        # Legacy Skills system completely removed - Pure Agentic Framework only
        logging.getLogger(__name__).info("✅ Legacy Skills system removed - Pure Agentic Framework")
    
    def _create_advanced_intelligence_services(self):
        """Legacy advanced intelligence services removed - replaced by Agentic Framework"""
        # Memory and Planning services replaced by AgentStateManager and BusinessLogicWorkflowEngine
        logging.getLogger(__name__).info("✅ Advanced intelligence services replaced by Agentic Framework")
        # All functionality now handled by the 7-component Agentic Framework
    
    def _create_performance_monitoring_services(self):
        """Create Phase 2 performance monitoring and optimization services"""
        try:
            # Metrics Collector - Advanced performance metrics collection
            from faultmaven.infrastructure.monitoring.metrics_collector import MetricsCollector
            self.metrics_collector = MetricsCollector(
                max_samples=10000,
                retention_hours=24
            )
            logging.getLogger(__name__).debug("Metrics collector created")
            
            # Intelligent Cache - Multi-tier caching with usage pattern analysis  
            from faultmaven.infrastructure.caching.intelligent_cache import IntelligentCache
            self.intelligent_cache = IntelligentCache(
                l1_max_size=1000,
                l1_ttl_seconds=300,
                l2_ttl_seconds=3600,
                l3_ttl_seconds=86400,
                metrics_collector=self.metrics_collector,
                redis_client=self.get_redis_client(),
                enable_analytics=True
            )
            logging.getLogger(__name__).debug("Intelligent cache created")
            
            # Analytics Dashboard Service - System performance insights
            from faultmaven.services.analytics.dashboard_service import AnalyticsDashboardService
            self.analytics_dashboard_service = AnalyticsDashboardService(
                metrics_collector=self.metrics_collector,
                intelligent_cache=self.intelligent_cache,
                tracer=self.get_tracer()
            )
            logging.getLogger(__name__).debug("Analytics dashboard service created")
            
            # Performance Optimization Service - Temporarily disabled (module missing)
            # TODO: Re-enable when faultmaven.services.performance_optimization module is created
            # from faultmaven.services.performance_optimization import PerformanceOptimizationService
            # self.performance_optimization_service = PerformanceOptimizationService(
            #     metrics_collector=self.metrics_collector,
            #     intelligent_cache=self.intelligent_cache,
            #     analytics_service=self.analytics_dashboard_service,
            #     tracer=self.get_tracer(),
            #     enable_auto_optimization=True,
            #     optimization_aggressiveness="moderate"
            # )
            self.performance_optimization_service = None
            logging.getLogger(__name__).debug("Performance optimization service temporarily disabled (module missing)")
            
            # SLA Monitor - Performance SLA monitoring and alerting
            from faultmaven.infrastructure.monitoring.sla_monitor import SLAMonitor
            self.sla_monitor = SLAMonitor(
                metrics_collector=self.metrics_collector,
                analytics_service=self.analytics_dashboard_service,
                tracer=self.get_tracer(),
                alert_rate_limit_per_hour=20
            )
            logging.getLogger(__name__).debug("SLA monitor created")
            
            # Performance Monitoring Integration - Initialize monitoring decorators
            from faultmaven.infrastructure.observability.performance_monitoring import initialize_performance_monitoring
            self.performance_monitor = initialize_performance_monitoring(
                metrics_collector=self.metrics_collector,
                tracer=self.get_tracer(),
                enable_user_patterns=True,
                enable_cache_monitoring=True
            )
            logging.getLogger(__name__).debug("Performance monitoring initialized")
            
        except Exception as e:
            logging.getLogger(__name__).warning(f"Performance monitoring services creation failed: {e}")
            # Set fallback services
            self.metrics_collector = None
            self.intelligent_cache = None
            self.analytics_dashboard_service = None
            self.performance_optimization_service = None
            self.sla_monitor = None
            self.performance_monitor = None
    
    def _create_enhanced_data_processing_services(self):
        """Create Phase 3 enhanced data processing services with memory integration"""
        try:
            # Pattern Learner - Core learning system for adaptive pattern recognition
            from faultmaven.core.processing.pattern_learner import PatternLearner
            self.pattern_learner = PatternLearner(
                memory_service=self.get_memory_service()
            )
            logging.getLogger(__name__).debug("Pattern learner created")
            
            # Enhanced Data Classifier - DISABLED (old classifier removed, using new preprocessing pipeline)
            # from faultmaven.core.processing.classifier import EnhancedDataClassifier
            # self.enhanced_data_classifier = EnhancedDataClassifier(
            #     memory_service=self.get_memory_service()
            # )
            self.enhanced_data_classifier = None  # Placeholder
            logging.getLogger(__name__).debug("Enhanced data classifier disabled (using new preprocessing pipeline)")
            
            # Enhanced Log Processor - Context-aware log processing with memory integration
            from faultmaven.core.processing.log_analyzer import EnhancedLogProcessor
            self.enhanced_log_processor = EnhancedLogProcessor(
                memory_service=self.get_memory_service()
            )
            logging.getLogger(__name__).debug("Enhanced log processor created")
            
            # Enhanced Security Assessment - Pattern-based security with learning
            from faultmaven.infrastructure.security.enhanced_security_assessment import EnhancedSecurityAssessment
            self.enhanced_security_assessment = EnhancedSecurityAssessment(
                memory_service=self.get_memory_service(),
                data_sanitizer=self.get_sanitizer(),
                pattern_learner=self.pattern_learner
            )
            logging.getLogger(__name__).debug("Enhanced security assessment created")
            
            # Enhanced Data Service - Comprehensive data processing with all enhancements
            from faultmaven.services.domain.data_service import DataService
            self.enhanced_data_service = DataService(
                data_classifier=self.enhanced_data_classifier,
                log_processor=self.enhanced_log_processor,
                sanitizer=self.get_sanitizer(),
                tracer=self.get_tracer(),
                storage_backend=None,  # Will use default storage
                session_service=self.get_session_service(),
                settings=self.settings,
                memory_service=self.get_memory_service(),  # Enhanced capability
                pattern_learner=self.pattern_learner  # Enhanced capability
            )
            logging.getLogger(__name__).debug("Enhanced data service created")
            
        except Exception as e:
            logging.getLogger(__name__).warning(f"Enhanced data processing services creation failed: {e}")
            # Set fallback services
            self.pattern_learner = None
            self.enhanced_data_classifier = None
            self.enhanced_log_processor = None
            self.enhanced_security_assessment = None
            self.enhanced_data_service = None

    def _create_microservice_foundation_services(self):
        """Create Phase A microservice foundation services"""
        try:
            # Microservice Session Service - Enhanced session/case management
            try:
                from faultmaven.services.microservice_session import MicroserviceSessionService
                self.microservice_session_service = MicroserviceSessionService(
                    base_session_service=self.get_session_service(),
                    tracer=self.get_tracer()
                )
                logging.getLogger(__name__).debug("Microservice session service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Microservice session service import failed: {e}")
                self.microservice_session_service = None
            
            # Global Confidence Service - Calibrated confidence scoring
            try:
                from faultmaven.services.analytics.confidence_service import GlobalConfidenceService
                self.confidence_service = GlobalConfidenceService(
                    calibration_method="platt",
                    model_version="conf-v1",
                    hysteresis_up_turns=1,
                    hysteresis_down_turns=2
                )
                logging.getLogger(__name__).debug("Global confidence service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Confidence service import failed: {e}")
                self.confidence_service = None
            
            # Policy/Safety Service - Action classification and safety
            try:
                from faultmaven.services.policy import PolicySafetyService
                self.policy_service = PolicySafetyService(
                    enforcement_mode="strict",
                    enable_compliance_checking=True,
                    auto_approve_low_risk=False
                )
                logging.getLogger(__name__).debug("Policy/safety service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Policy service import failed: {e}")
                self.policy_service = None
            
            # Unified Retrieval Service - Federated knowledge access
            try:
                from faultmaven.services.unified_retrieval import UnifiedRetrievalService
                self.unified_retrieval_service = UnifiedRetrievalService(
                    knowledge_service=self.get_knowledge_service(),
                    vector_store=self.get_vector_store(),
                    sanitizer=self.get_sanitizer(),
                    tracer=self.get_tracer(),
                    enable_caching=True,
                    cache_ttl_seconds=3600
                )
                logging.getLogger(__name__).debug("Unified retrieval service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Unified retrieval service import failed: {e}")
                self.unified_retrieval_service = None
            
            # Decision Records & Telemetry Service - Observability infrastructure
            from faultmaven.infrastructure.telemetry.decision_recorder import DecisionRecorder
            self.decision_recorder = DecisionRecorder(
                tracer=self.get_tracer(),
                retention_days=90,
                enable_structured_logging=True,
                enable_performance_tracking=True
            )
            logging.getLogger(__name__).debug("Decision recorder created")
            
            # Phase B: Core Orchestration and Coordination Services
            self._create_phase_b_orchestration_services()
            
        except Exception as e:
            logging.getLogger(__name__).warning(f"Microservice foundation services creation failed: {e}")
            # Set fallback services
            self.microservice_session_service = None
            self.confidence_service = None
            self.policy_service = None
            self.unified_retrieval_service = None
            self.decision_recorder = None
            # Phase B fallbacks
            self.gateway_service = None
            self.loop_guard_service = None
            self.orchestrator_service = None

    def _create_phase_b_orchestration_services(self):
        """Create Phase B orchestration and coordination services"""
        try:
            # Gateway Service - Request processing and routing
            try:
                from faultmaven.services.gateway import GatewayProcessingService
                self.gateway_service = GatewayProcessingService(
                    llm_provider=self.get_llm_provider(),
                    sanitizer=self.get_sanitizer(),
                    tracer=self.get_tracer(),
                    settings=self.settings
                )
                logging.getLogger(__name__).debug("Gateway processing service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Gateway service import failed: {e}")
                self.gateway_service = None
            
            # Loop Guard Service - Prevents infinite processing loops
            try:
                from faultmaven.core.loop_guard.loop_guard import LoopGuard
                self.loop_guard_service = LoopGuard()
                logging.getLogger(__name__).debug("Loop guard service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Loop guard service import failed: {e}")
                self.loop_guard_service = None
            
            # Orchestrator Service - Multi-service coordination
            try:
                from faultmaven.core.orchestration.troubleshooting_orchestrator import TroubleshootingOrchestrator
                self.orchestrator_service = TroubleshootingOrchestrator(
                    memory_service=getattr(self, 'memory_service', None),
                    planning_service=getattr(self, 'planning_service', None),
                    reasoning_service=None,  # Legacy parameter - service removed
                    enhanced_knowledge_service=self.get_knowledge_service(),
                    llm_provider=self.get_llm_provider(),
                    tracer=self.get_tracer()
                )
                logging.getLogger(__name__).debug("Troubleshooting orchestrator service created")
            except ImportError as e:
                logging.getLogger(__name__).debug(f"Orchestrator service import failed: {e}")
                self.orchestrator_service = None
            
        except Exception as e:
            logging.getLogger(__name__).warning(f"Phase B orchestration services creation failed: {e}")
            # Set fallback services
            self.gateway_service = None
            self.loop_guard_service = None
            self.orchestrator_service = None

    def _create_agentic_framework_services(self):
        """Create Agentic Framework Services - Next-generation agent architecture"""
        logger = logging.getLogger(__name__)
        
        if not AGENTIC_AVAILABLE:
            logger.warning("Agentic Framework not available - creating placeholder services")
            # Create placeholder services for backward compatibility
            self.business_logic_workflow_engine = None
            self.agent_state_manager = None
            self.query_classification_engine = None
            self.tool_skill_broker = None
            self.guardrails_policy_layer = None
            self.response_synthesizer = None
            self.error_fallback_manager = None
            return
        
        try:
            # 1. Agent State Manager - Persistent memory and execution state management
            try:
                self.agent_state_manager: IAgentStateManager = AgentStateManager(
                    session_store=self.get_session_store(),
                    tracer=self.get_tracer()
                )
                logger.debug("✅ Agent State Manager initialized")
            except Exception as e:
                logger.warning(f"Agent State Manager initialization failed: {e}")
                self.agent_state_manager = None
            
            # 3. Query Classification Engine - REMOVED (superseded by doctor/patient architecture)
            # Archived to: archive/superseded_by_doctor_patient_v1.0/
            self.query_classification_engine = None
            
            # 4. Tool & Skill Broker - Dynamic orchestration of tools and skills
            try:
                self.tool_skill_broker: IToolSkillBroker = ToolSkillBroker(
                    tracer=self.get_tracer()
                )
                logger.debug("✅ Tool Skill Broker initialized")
            except Exception as e:
                logger.warning(f"Tool Skill Broker initialization failed: {e}")
                self.tool_skill_broker = None
            
            # 5. Guardrails & Policy Layer - Safety, security, and compliance enforcement
            try:
                self.guardrails_policy_layer: IGuardrailsPolicyLayer = GuardrailsPolicyLayer()
                logger.debug("✅ Guardrails Policy Layer initialized")
            except Exception as e:
                logger.warning(f"Guardrails Policy Layer initialization failed: {e}")
                self.guardrails_policy_layer = None
            
            # 6. Response Synthesizer - Intelligent response generation and formatting
            try:
                self.response_synthesizer: IResponseSynthesizer = ResponseSynthesizer()
                logger.debug("✅ Response Synthesizer initialized")
            except Exception as e:
                logger.warning(f"Response Synthesizer initialization failed: {e}")
                self.response_synthesizer = None
            
            # 7. Error Handling & Fallback Manager - Robust error recovery and graceful degradation
            try:
                self.error_fallback_manager: IErrorFallbackManager = ErrorFallbackManager()
                logger.debug("✅ Error Fallback Manager initialized")
            except Exception as e:
                logger.warning(f"Error Fallback Manager initialization failed: {e}")
                self.error_fallback_manager = None
            
            # 7. Business Logic & Workflow Engine - Plan-execute-observe-adapt workflow orchestration
            # Initialize last since it depends on all other agentic components
            try:
                self.business_logic_workflow_engine: IBusinessLogicWorkflowEngine = BusinessLogicWorkflowEngine(
                    state_manager=getattr(self, 'agent_state_manager', None),
                    classification_engine=getattr(self, 'query_classification_engine', None),
                    tool_broker=getattr(self, 'tool_skill_broker', None),
                    guardrails_layer=getattr(self, 'guardrails_policy_layer', None),
                    response_synthesizer=getattr(self, 'response_synthesizer', None),
                    error_manager=getattr(self, 'error_fallback_manager', None)
                )
                logger.debug("✅ Business Logic Workflow Engine initialized")
            except Exception as e:
                logger.warning(f"Business Logic Workflow Engine initialization failed: {e}")
                self.business_logic_workflow_engine = None
            
            logger.info("🚀 Agentic Framework Services initialized successfully")
            
        except Exception as e:
            logger.error(f"Critical error during Agentic Framework initialization: {e}")
            import traceback
            logger.debug(f"Agentic Framework error details: {traceback.format_exc()}")
            # Set all services to None on critical failure
            self.business_logic_workflow_engine = None
            self.agent_state_manager = None
            self.query_classification_engine = None
            self.tool_skill_broker = None
            self.guardrails_policy_layer = None
            self.response_synthesizer = None
            self.error_fallback_manager = None

    def get_settings(self) -> FaultMavenSettings:
        """Get the unified settings instance"""
        if not hasattr(self, 'settings') or self.settings is None:
            self.settings = get_settings()
        return self.settings
    
    def get_agent_service(self):
        """Get the agent service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Agent service requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'agent_service', None)
    
    def get_data_service(self):
        """Get the data service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Data service requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'data_service', None)

    def get_preprocessing_service(self):
        """Get the preprocessing service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Preprocessing service requested but container not initialized")
                self.initialize()
        return getattr(self, 'preprocessing_service', None)

    def get_knowledge_service(self):
        """Get the knowledge service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Knowledge service requested but container not initialized - this should not happen after startup")
                self.initialize()
        knowledge_service = getattr(self, 'knowledge_service', None)
        if knowledge_service is None:
            return self._create_minimal_knowledge_service()
        return knowledge_service
    
    def get_metrics_collector(self):
        """Get the metrics collector service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Metrics collector requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'metrics_collector', None)
    
    def get_intelligent_cache(self):
        """Get the intelligent cache service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Intelligent cache requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'intelligent_cache', None)
    
    def get_analytics_dashboard_service(self):
        """Get the analytics dashboard service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Analytics dashboard service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'analytics_dashboard_service', None)
    
    def get_sla_monitor(self):
        """Get the SLA monitor service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("SLA monitor requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'sla_monitor', None)
    
    def get_performance_monitor(self):
        """Get the performance monitor"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Performance monitor requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'performance_monitor', None)
    
    # Phase 2: Advanced Intelligence Services Getters
    
    def get_memory_service(self):
        """Get the memory service - now provided by AgentStateManager"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Memory service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        # Memory service functionality is now provided by AgentStateManager
        return getattr(self, 'agent_state_manager', None)
    
    def get_planning_service(self):
        """Get the planning service - now provided by BusinessLogicWorkflowEngine"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Planning service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        # Planning service functionality is now provided by BusinessLogicWorkflowEngine
        return getattr(self, 'business_logic_workflow_engine', None)
    
    def get_enhanced_agent_service(self):
        """Get the enhanced agent service with memory and planning capabilities"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced agent service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_service = getattr(self, 'enhanced_agent_service', None)
        if enhanced_service is None:
            # Fallback to standard agent service
            return self.get_agent_service()
        return enhanced_service
    
    def get_orchestration_service(self):
        """Get the orchestration service for multi-step workflows"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Orchestration service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'orchestration_service', None)
    
    
    def _create_minimal_knowledge_service(self):
        """Create a minimal knowledge service for testing environments"""
        import uuid
        from datetime import datetime, timezone
        from faultmaven.utils.serialization import to_json_compatible

        class MinimalKnowledgeService:
            def __init__(self):
                self.documents = {}  # Simple in-memory storage for testing
            
            async def upload_document(self, content, title, document_type, category=None, tags=None, source_url=None, description=None):
                doc_id = f"doc_{uuid.uuid4().hex[:8]}"
                job_id = f"job_{doc_id}"
                
                # Store document for later retrieval in tests
                self.documents[doc_id] = {
                    "document_id": doc_id,
                    "title": title,
                    "content": content,
                    "document_type": document_type,
                    "category": category or document_type,
                    "tags": tags or [],
                    "source_url": source_url,
                    "description": description,
                    "created_at": to_json_compatible(datetime.now(timezone.utc)),
                    "updated_at": to_json_compatible(datetime.now(timezone.utc))
                }
                
                return {
                    "document_id": doc_id,
                    "job_id": job_id,
                    "status": "processing",
                    "metadata": {
                        "title": title,
                        "document_type": document_type,
                        "category": category or document_type,
                        "tags": tags or [],
                        "created_at": to_json_compatible(datetime.now(timezone.utc))
                    }
                }
            
            async def get_document(self, document_id):
                # Return document if it exists, or create a mock one for testing
                if document_id in self.documents:
                    return self.documents[document_id]
                elif document_id and (document_id.startswith("doc_") or len(document_id) >= 8):
                    # Return mock document for testing
                    return {
                        "document_id": document_id,
                        "title": f"Document {document_id}",
                        "content": "This is sample document content for testing purposes.",
                        "document_type": "troubleshooting",
                        "category": "troubleshooting",
                        "status": "processed",
                        "tags": ["test", "sample"],
                        "source_url": None,
                        "created_at": to_json_compatible(datetime.now(timezone.utc)),
                        "updated_at": to_json_compatible(datetime.now(timezone.utc)),
                        "metadata": {
                            "author": "test-system",
                            "version": "1.0"
                        }
                    }
                return None
            
            async def list_documents(self, document_type=None, tags=None, limit=50, offset=0):
                docs = list(self.documents.values())
                
                # Apply filters
                if document_type:
                    docs = [d for d in docs if d.get("document_type") == document_type]
                if tags:
                    docs = [d for d in docs if any(tag in d.get("tags", []) for tag in tags)]
                
                # Apply pagination
                total = len(docs)
                docs = docs[offset:offset + limit]
                
                return {
                    "documents": docs,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "filters": {
                        "document_type": document_type,
                        "tags": tags
                    }
                }
            
            async def delete_document(self, document_id):
                if document_id in self.documents:
                    del self.documents[document_id]
                    return {"success": True, "document_id": document_id}
                else:
                    return {"success": False, "document_id": document_id}
            
            async def search_documents(self, query, document_type=None, tags=None, limit=10):
                # Simple text search in titles and content
                results = []
                for doc_id, doc in self.documents.items():
                    if query.lower() in doc.get("title", "").lower() or query.lower() in doc.get("content", "").lower():
                        # Apply filters
                        if document_type and doc.get("document_type") != document_type:
                            continue
                        if tags and not any(tag in doc.get("tags", []) for tag in tags):
                            continue
                            
                        results.append({
                            "document_id": doc_id,
                            "content": doc.get("content", "")[:200] + "...",
                            "metadata": {
                                "title": doc.get("title"),
                                "document_type": doc.get("document_type"),
                                "tags": doc.get("tags", [])
                            },
                            "similarity_score": 0.8  # Mock score
                        })
                
                return {
                    "query": query,
                    "total_results": len(results),
                    "results": results[:limit]
                }
            
            async def get_job_status(self, job_id):
                # Extract document ID from job ID
                if job_id.startswith("job_doc_"):
                    document_id = job_id[4:]  # Remove "job_" prefix
                    return {
                        "job_id": job_id,
                        "document_id": document_id,
                        "status": "completed",
                        "progress": 100,
                        "created_at": to_json_compatible(datetime.now(timezone.utc)),
                        "completed_at": to_json_compatible(datetime.now(timezone.utc)),
                        "processing_results": {
                            "chunks_created": 1,
                            "embeddings_generated": 1,
                            "indexing_complete": True,
                            "error_count": 0
                        }
                    }
                return None
            
            async def update_document(self, document_id, title=None, content=None, tags=None):
                # Create or update document
                if document_id not in self.documents:
                    # Create mock document if it doesn't exist
                    self.documents[document_id] = {
                        "document_id": document_id,
                        "title": f"Document {document_id}",
                        "content": "Sample content",
                        "document_type": "troubleshooting",
                        "category": "troubleshooting",
                        "tags": [],
                        "created_at": to_json_compatible(datetime.now(timezone.utc)),
                        "updated_at": to_json_compatible(datetime.now(timezone.utc))
                    }
                
                doc = self.documents[document_id]
                if title:
                    doc["title"] = title
                if content:
                    doc["content"] = content
                if tags is not None:
                    doc["tags"] = tags
                doc["updated_at"] = to_json_compatible(datetime.now(timezone.utc))
                
                # Return as KnowledgeBaseDocument-like structure
                return {
                    "document_id": document_id,
                    "title": doc["title"],
                    "content": doc["content"],
                    "document_type": doc["document_type"],
                    "category": doc.get("category", doc["document_type"]),
                    "tags": doc["tags"],
                    "created_at": doc["created_at"],
                    "updated_at": doc["updated_at"]
                }

            async def update_document_metadata(self, document_id, **kwargs):
                if document_id in self.documents:
                    doc = self.documents[document_id]
                    doc.update(kwargs)
                    doc["updated_at"] = to_json_compatible(datetime.now(timezone.utc))
                    return doc
                return None
            
            async def bulk_update_documents(self, document_ids, updates):
                updated_count = 0
                for doc_id in document_ids:
                    if doc_id in self.documents:
                        self.documents[doc_id].update(updates)
                        self.documents[doc_id]["updated_at"] = to_json_compatible(datetime.now(timezone.utc))
                        updated_count += 1
                
                return {
                    "success": True,
                    "updated_count": updated_count,
                    "total_requested": len(document_ids)
                }
            
            async def bulk_delete_documents(self, document_ids):
                deleted_count = 0
                for doc_id in document_ids:
                    if doc_id in self.documents:
                        del self.documents[doc_id]
                        deleted_count += 1
                
                return {
                    "success": True,
                    "deleted_count": deleted_count,
                    "total_requested": len(document_ids)
                }
            
            async def get_knowledge_stats(self):
                doc_types = {}
                categories = {}
                
                for doc in self.documents.values():
                    doc_type = doc.get("document_type", "unknown")
                    doc_types[doc_type] = doc_types.get(doc_type, 0) + 1
                    
                    # Use document_type as category for simplicity
                    categories[doc_type] = categories.get(doc_type, 0) + 1
                
                return {
                    "total_documents": len(self.documents),
                    "document_types": doc_types,
                    "categories": categories,
                    "total_chunks": len(self.documents),  # Simplified
                    "avg_chunk_size": 500,  # Mock value
                    "storage_used": f"{len(self.documents) * 0.5} MB",
                    "last_updated": to_json_compatible(datetime.now(timezone.utc))
                }
            
            async def get_search_analytics(self):
                return {
                    "popular_queries": ["database error", "connection timeout", "network issue"],
                    "search_volume": 150,
                    "avg_response_time": 0.2,
                    "hit_rate": 0.85,
                    "category_distribution": {
                        "database": 40,
                        "network": 30,
                        "application": 30
                    }
                }
        
        return MinimalKnowledgeService()
    
    def get_llm_provider(self):
        """Get the LLM provider interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("LLM provider requested but container not initialized - this should not happen after startup")
                self.initialize()
        
        # Ensure we always return a valid implementation, even if initialization failed
        llm_provider = getattr(self, 'llm_provider', None)
        if llm_provider is None:
            # Create proper fallback implementation instead of MagicMock
            from faultmaven.models.interfaces import ILLMProvider
            logger = logging.getLogger(__name__)
            logger.error("LLM provider not initialized - creating minimal fallback implementation")

            class MinimalLLMProvider(ILLMProvider):
                async def generate(self, prompt: str, **kwargs) -> str:
                    return "I apologize, but the AI service is temporarily unavailable. Please try again in a few moments."

            self.llm_provider = MinimalLLMProvider()
            return self.llm_provider
        return llm_provider
    
    def get_sanitizer(self):
        """Get the data sanitizer interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Data sanitizer requested but container not initialized - this should not happen after startup")
                self.initialize()
        
        # Ensure we always return a valid implementation, even if initialization failed
        sanitizer = getattr(self, 'sanitizer', None)
        if sanitizer is None:
            # Create minimal fallback implementation
            from unittest.mock import MagicMock
            logger = logging.getLogger(__name__)
            logger.warning("Creating fallback sanitizer due to initialization failure")
            self.sanitizer = MagicMock()
            return self.sanitizer
        return sanitizer
    
    def get_tracer(self):
        """Get the tracer interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Tracer requested but container not initialized - this should not happen after startup")
                self.initialize()
        
        # Ensure we always return a valid implementation, even if initialization failed
        tracer = getattr(self, 'tracer', None)
        if tracer is None:
            # Create minimal fallback implementation
            from unittest.mock import MagicMock
            logger = logging.getLogger(__name__)
            logger.warning("Creating fallback tracer due to initialization failure")
            self.tracer = MagicMock()
            return self.tracer
        return tracer
    
    def get_tools(self):
        """Get list of available tools"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Tools requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'tools', [])
    
    def get_data_classifier(self):
        """Get the data classifier interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Data classifier requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'data_classifier', None)
    
    def get_log_processor(self):
        """Get the log processor interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Log processor requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'log_processor', None)

    def get_preprocessing_service(self):
        """Get the preprocessing service (new Phase 1 pipeline)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            if not getattr(self, '_initializing', False):
                logger.warning("Preprocessing service requested but container not initialized")
                self.initialize()
        return getattr(self, 'preprocessing_service', None)
    
    def get_vector_store(self):
        """Get the vector store interface implementation"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'vector_store', None)
    
    def get_knowledge_ingester(self):
        """Get the knowledge ingester interface implementation"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'knowledge_ingester', None)
    
    def get_session_store(self):
        """Get the session store interface implementation"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'session_store', None)
    
    def get_session_service(self):
        """Get the session service implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, '_initializing', False):
                logger.warning("Session service requested but container not initialized - this should not happen after startup")
                self.initialize()
        return getattr(self, 'session_service', None)
    
    def get_case_service(self) -> Optional[ICaseService]:
        """Get the case service implementation (optional feature)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Case service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'case_service', None)
    
    def get_case_store(self) -> Optional[ICaseStore]:
        """Get the case store implementation (optional feature)"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'case_store', None)

    def get_report_store(self) -> Optional[IReportStore]:
        """Get the report store implementation (optional feature)"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'report_store', None)
    
    def get_config(self):
        """Get the configuration manager instance"""
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'config', None)
    
    def _create_minimal_session_service(self):
        """Create a minimal session service for testing environments"""
        from datetime import datetime
        import uuid
        
        class MockSessionContext:
            def __init__(self, session_id, user_id=None, metadata=None):
                self.session_id = session_id
                self.user_id = user_id
                self.metadata = metadata or {}
                self.created_at = datetime.now(timezone.utc)
                self.last_activity = datetime.now(timezone.utc)
                self.data_uploads = []
                self.case_history = []
        
        class MockSessionManager:
            """Mock session manager that tracks operations"""
            def __init__(self):
                self.sessions = {}
                
            async def add_case_history(self, session_id, record):
                if session_id in self.sessions:
                    self.sessions[session_id].case_history.append(record)
                    return True
                return False
                

        class MinimalSessionService:
            def __init__(self):
                self.sessions = {}  # Store sessions in memory for testing
                self.session_manager = MockSessionManager()  # Add mock session manager
                self.session_manager.sessions = self.sessions  # Share session storage
                
            async def create_session(self, user_id=None, session_id=None, metadata=None, client_id=None, initial_context=None):
                if not session_id:
                    session_id = str(uuid.uuid4())
                session = MockSessionContext(session_id, user_id, metadata)
                self.sessions[session_id] = session
                return session
            
            async def get_session(self, session_id, validate=True):
                return self.sessions.get(session_id)
            
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
            
            async def get_session_stats(self):
                return {"total_sessions": len(self.sessions), "active_sessions": len(self.sessions)}
                
            async def cleanup_session_data(self, session_id):
                return {
                    "session_id": session_id,
                    "success": True,
                    "cleaned_items": {
                        "data_uploads": 0,
                        "case_history": 0,
                        "temp_files": 0
                    }
                }
            
            async def get_or_create_current_case_id(self, session_id, force_new_case=False):
                """Get or create a case ID for the session"""
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not hasattr(session, 'current_case_id') or force_new_case:
                        session.current_case_id = str(uuid.uuid4())
                    return session.current_case_id
                else:
                    # Create session if it doesn't exist
                    await self.create_session()
                    return str(uuid.uuid4())
            
            async def format_conversation_context(self, session_id, case_id, limit=5):
                """Format conversation context for a case"""
                if session_id in self.sessions:
                    # Return empty context for mock implementation
                    return ""
                return ""
            
            async def record_query_operation(self, session_id, query, case_id, context=None, confidence_score=1.0):
                """Record a query operation in the session"""
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not hasattr(session, 'operations'):
                        session.operations = []
                    session.operations.append({
                        "query": query,
                        "case_id": case_id,
                        "context": context,
                        "confidence_score": confidence_score,
                        "timestamp": datetime.now(timezone.utc)
                    })
                    return True
                return False
            
            async def record_case_message(
                self,
                session_id: str,
                message_content: str,
                message_type=None,  # Use Any to avoid import issues in container
                author_id=None,
                metadata=None
            ) -> bool:
                """
                Record a message in the current case for this session
                
                Args:
                    session_id: Session identifier
                    message_content: Message content
                    message_type: Type of message (ignored in minimal impl)
                    author_id: Optional message author (ignored in minimal impl)
                    metadata: Optional message metadata (ignored in minimal impl)
                    
                Returns:
                    True if message was recorded successfully
                """
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not hasattr(session, 'case_messages'):
                        session.case_messages = []
                    session.case_messages.append({
                        "content": message_content,
                        "message_type": str(message_type) if message_type else "user_query",
                        "author_id": author_id,
                        "metadata": metadata or {},
                        "timestamp": datetime.now(timezone.utc)
                    })
                    return True
                return False
        
        return MinimalSessionService()
    
    def _create_minimal_case_service(self):
        """Create a minimal case service for testing environments"""
        from datetime import datetime
        import uuid
        from faultmaven.models.case import Case, CaseStatus, CasePriority
        
        class MinimalCaseService:
            def __init__(self):
                self.cases = {}  # Store cases in memory for testing
                self.case_messages = {}  # Store messages per case: {case_id: [messages]}
                
            async def create_case(self, title=None, description=None, owner_id=None, session_id=None, initial_message=None, initial_query=None, priority=None, user_id=None, metadata=None):
                case_id = str(uuid.uuid4())
                
                # Create case with proper Case model structure
                # Default to "anonymous" if no owner specified (API contract compliance)
                final_owner_id = user_id or owner_id or "anonymous"
                
                # Phase 2: Handle initial_message transactionally
                current_time = datetime.now(timezone.utc)
                message_count = 0
                
                # Phase 2: If initial_message provided, set message_count=1 and update timestamp
                if initial_message and initial_message.strip():
                    message_count = 1
                    current_time = datetime.now(timezone.utc)  # Refresh timestamp for message creation
                
                # Phase 3: Handle auto-title generation - set title_manually_set flag
                provided_title = title or "New Chat"
                title_manually_set = bool(title and title.strip() and title.strip() != "New Chat")  # True if user explicitly provided a non-default title
                
                # Phase 3: Auto-title generation after first committed message
                # Debug: Check if conditions are met
                should_auto_title = (initial_message and initial_message.strip() and 
                    provided_title == "New Chat" and not title_manually_set)
                
                if should_auto_title:
                    # Generate auto-title: chat-<UTC ISO 8601 Z>
                    provided_title = f"chat-{current_time.isoformat()}Z"
                
                case = Case(
                    case_id=case_id,
                    title=provided_title,
                    description=description or "",
                    status=CaseStatus.ACTIVE,  # Use enum value
                    priority=CasePriority(priority) if priority else CasePriority.MEDIUM,  # Use enum value
                    owner_id=final_owner_id,
                    message_count=message_count,
                    created_at=current_time,
                    updated_at=current_time,
                    title_manually_set=title_manually_set
                )
                
                # Store session relationship in metadata for tracking
                if session_id:
                    case.metadata["session_id"] = session_id
                
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
                        "user_id": final_owner_id
                    }
                    self.case_messages[case_id].append(initial_msg)
                
                return case
            
            async def get_case(self, case_id, user_id=None):
                return self.cases.get(case_id)
            
            async def list_cases_for_session(self, session_id, limit=20, offset=0):
                # Filter cases by checking if session_id matches case.current_session_id
                session_cases = [case for case in self.cases.values() if case.current_session_id == session_id]
                total = len(session_cases)
                paginated = session_cases[offset:offset + limit]
                return paginated, total
                
            async def list_cases_by_session(self, session_id, limit=50, offset=0, filters=None):
                """List cases by session_id - Phase 1: Apply default filtering like list_user_cases"""
                session_cases = [case for case in self.cases.values() if case.current_session_id == session_id]
                
                # Phase 1: Apply same core filtering as list_user_cases
                if filters:
                    # Phase 1: Default filtering behavior (exclude non-active cases)
                    if not getattr(filters, 'include_deleted', False):
                        # Exclude cases marked as deleted
                        session_cases = [case for case in session_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_archived', False):
                        # Exclude archived cases
                        session_cases = [case for case in session_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_empty', False):
                        # Exclude empty cases (message_count == 0)
                        session_cases = [case for case in session_cases if getattr(case, 'message_count', 1) > 0]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_user_cases)
                    # Exclude archived/deleted cases by default
                    session_cases = [case for case in session_cases if case.status == CaseStatus.ACTIVE]
                    # Exclude empty cases by default
                    session_cases = [case for case in session_cases if getattr(case, 'message_count', 1) > 0]
                
                return session_cases[offset:offset + limit]
                
            async def count_cases_by_session(self, session_id, filters=None):
                """Count cases by session_id - Phase 1: Apply default filtering like list_cases_by_session"""
                session_cases = [case for case in self.cases.values() if case.current_session_id == session_id]
                
                # Phase 1: Apply same core filtering as list_cases_by_session
                if filters:
                    # Phase 1: Default filtering behavior (exclude non-active cases)
                    if not getattr(filters, 'include_deleted', False):
                        # Exclude cases marked as deleted
                        session_cases = [case for case in session_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_archived', False):
                        # Exclude archived cases
                        session_cases = [case for case in session_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_empty', False):
                        # Exclude empty cases (message_count == 0)
                        session_cases = [case for case in session_cases if getattr(case, 'message_count', 1) > 0]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_cases_by_session)
                    # Exclude archived/deleted cases by default
                    session_cases = [case for case in session_cases if case.status == CaseStatus.ACTIVE]
                    # Exclude empty cases by default
                    session_cases = [case for case in session_cases if getattr(case, 'message_count', 1) > 0]
                
                return len(session_cases)
            
            async def update_case_status(self, case_id, status):
                if case_id in self.cases:
                    self.cases[case_id].status = status
                    self.cases[case_id].updated_at = datetime.now(timezone.utc)
                    return True
                return False
                
            async def add_case_query(self, case_id, query, priority=None):
                # Phase 2 & 3: Update message_count, updated_at, and handle auto-title generation
                if case_id in self.cases:
                    case = self.cases[case_id]
                    current_time = datetime.now(timezone.utc)
                    
                    # Phase 2: Update message count and timestamp
                    case.message_count = getattr(case, 'message_count', 0) + 1
                    case.updated_at = current_time
                    
                    # Phase 3: Auto-title generation after first message
                    # Only generate auto-title if title is "New Chat" AND not manually set
                    if (case.title == "New Chat" and 
                        not getattr(case, 'title_manually_set', False) and
                        case.message_count == 1):  # This is the first query
                        
                        # Generate auto-title: chat-<UTC_ISO8601_Z_timestamp>
                        auto_title = f"chat-{current_time.isoformat()}Z"
                        case.title = auto_title
                        # Keep title_manually_set as False since this is auto-generated
                
                # Mock implementation - return a simple query response
                return {
                    "query_id": str(uuid.uuid4()),
                    "case_id": case_id,
                    "query": query,
                    "priority": priority or "medium",
                    "created_at": datetime.now(timezone.utc)
                }
                
            async def check_idempotency_key(self, idempotency_key: str):
                # Minimal implementation - no actual idempotency checking for testing
                return None
                
            async def store_idempotency_result(self, idempotency_key: str, status_code: int, content: dict, headers: dict):
                # Minimal implementation - no actual storage for testing
                return True
                
            async def list_user_cases(self, user_id=None, filters=None, limit=20, offset=0):
                """List cases for a user with pagination - Phase 1: Core filtering implementation"""
                # Filter cases by user_id if provided
                if user_id:
                    user_cases = [case for case in self.cases.values() if case.owner_id == user_id]
                else:
                    # Return all cases if no user filter
                    user_cases = list(self.cases.values())
                
                # Phase 1: Apply core filtering - exclude deleted/archived/empty by default
                if filters:
                    # Phase 1: Default filtering behavior (exclude non-active cases)
                    if not getattr(filters, 'include_deleted', False):
                        # Exclude cases marked as deleted (we'll use ARCHIVED status as "deleted" marker for now)
                        # In full implementation, this would check a deleted flag or soft delete status
                        user_cases = [case for case in user_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_archived', False):
                        # Exclude archived cases (already handled above for delete, but explicit for clarity)
                        user_cases = [case for case in user_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_empty', False):
                        # Exclude empty cases (message_count == 0)
                        # For MinimalCaseService, we'll consider all cases as having at least 1 message unless explicitly marked
                        user_cases = [case for case in user_cases if getattr(case, 'message_count', 1) > 0]
                    
                    # Apply other existing filters
                    if hasattr(filters, 'status') and filters.status:
                        user_cases = [case for case in user_cases if case.status == filters.status]
                    if hasattr(filters, 'priority') and filters.priority:
                        user_cases = [case for case in user_cases if case.priority == filters.priority]
                    if hasattr(filters, 'owner_id') and filters.owner_id:
                        user_cases = [case for case in user_cases if case.owner_id == filters.owner_id]
                else:
                    # Phase 1: No filters provided - apply default exclusions
                    # Exclude archived/deleted cases by default
                    user_cases = [case for case in user_cases if case.status == CaseStatus.ACTIVE]
                    # Exclude empty cases by default
                    user_cases = [case for case in user_cases if getattr(case, 'message_count', 1) > 0]
                
                # Extract pagination parameters from filters if available
                if filters and hasattr(filters, 'limit'):
                    limit = filters.limit
                if filters and hasattr(filters, 'offset'):
                    offset = filters.offset
                
                # Pagination
                paginated_cases = user_cases[offset:offset + limit]
                
                return paginated_cases
                
            async def count_user_cases(self, user_id=None, filters=None):
                """Count cases for a user with filters - Phase 1: Mirror filtering from list_user_cases"""
                # Filter cases by user_id if provided
                if user_id:
                    user_cases = [case for case in self.cases.values() if case.owner_id == user_id]
                else:
                    # Return all cases if no user filter
                    user_cases = list(self.cases.values())
                
                # Phase 1: Apply same core filtering as list_user_cases
                if filters:
                    # Phase 1: Default filtering behavior (exclude non-active cases)
                    if not getattr(filters, 'include_deleted', False):
                        # Exclude cases marked as deleted (we'll use ARCHIVED status as "deleted" marker for now)
                        user_cases = [case for case in user_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_archived', False):
                        # Exclude archived cases (already handled above for delete, but explicit for clarity)
                        user_cases = [case for case in user_cases if case.status != CaseStatus.ARCHIVED]
                    
                    if not getattr(filters, 'include_empty', False):
                        # Exclude empty cases (message_count == 0)
                        user_cases = [case for case in user_cases if getattr(case, 'message_count', 1) > 0]
                    
                    # Apply other existing filters
                    if hasattr(filters, 'status') and filters.status:
                        user_cases = [case for case in user_cases if case.status == filters.status]
                    if hasattr(filters, 'priority') and filters.priority:
                        user_cases = [case for case in user_cases if case.priority == filters.priority]
                    if hasattr(filters, 'owner_id') and filters.owner_id:
                        user_cases = [case for case in user_cases if case.owner_id == filters.owner_id]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_user_cases)
                    # Exclude archived/deleted cases by default
                    user_cases = [case for case in user_cases if case.status == CaseStatus.ACTIVE]
                    # Exclude empty cases by default
                    user_cases = [case for case in user_cases if getattr(case, 'message_count', 1) > 0]
                
                return len(user_cases)
                
            async def archive_case(self, case_id: str, reason: str = None, user_id: str = None) -> bool:
                """Archive a case by updating its status to ARCHIVED"""
                if case_id not in self.cases:
                    return False
                
                # Update case status to archived
                self.cases[case_id].status = CaseStatus.ARCHIVED
                self.cases[case_id].updated_at = datetime.now(timezone.utc)
                
                # Store archive reason in metadata if provided
                if reason:
                    if not hasattr(self.cases[case_id], 'metadata') or self.cases[case_id].metadata is None:
                        self.cases[case_id].metadata = {}
                    self.cases[case_id].metadata['archive_reason'] = reason
                    if user_id:
                        self.cases[case_id].metadata['archived_by'] = user_id
                
                return True
                
            async def hard_delete_case(self, case_id: str, user_id: str = None) -> bool:
                """Permanently delete a case and all associated data (idempotent)"""
                # For MinimalCaseService, just remove from memory
                # Always return True for idempotent behavior
                if case_id in self.cases:
                    del self.cases[case_id]
                return True
                
            async def get_case_messages(self, case_id: str, limit: int = 50, offset: int = 0):
                """Get messages for a case"""
                if case_id not in self.case_messages:
                    return []

                messages = self.case_messages[case_id]
                # Apply pagination
                start = offset
                end = start + limit
                return messages[start:end]

            async def get_case_messages_enhanced(self, case_id: str, limit: int = 50, offset: int = 0, include_debug: bool = False):
                """Enhanced message retrieval with debugging support."""
                import time
                from faultmaven.models.api import CaseMessagesResponse, MessageRetrievalDebugInfo, Message

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
                                msg_type = msg.get('message_type')
                                message_id = msg.get('message_id')
                                content = msg.get('content', '')
                                timestamp = msg.get('timestamp')
                            else:
                                msg_type = getattr(msg, 'message_type', None)
                                message_id = getattr(msg, 'message_id', None)
                                content = getattr(msg, 'content', '')
                                timestamp = getattr(msg, 'timestamp', None)

                            # Map message_type to role
                            role = None
                            if hasattr(msg_type, 'value'):
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
                                    if hasattr(timestamp, 'isoformat'):
                                        created_at = to_json_compatible(timestamp)
                                    else:
                                        created_at = str(timestamp)
                                except Exception:
                                    created_at = str(timestamp)

                            messages.append(Message(
                                message_id=message_id or f"msg_{len(messages)}",
                                role=role,
                                content=content,
                                created_at=created_at or to_json_compatible(datetime.now(timezone.utc))
                            ))
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
                        message_parsing_errors=message_parsing_errors
                    )

                return CaseMessagesResponse(
                    messages=messages,
                    total_count=total_count,
                    retrieved_count=retrieved_count,
                    has_more=has_more,
                    next_offset=next_offset,
                    debug_info=debug_info
                )

            async def add_case_query(self, case_id: str, query: str, user_id: str = None):
                """Add a query message to a case"""
                if case_id not in self.cases:
                    return False
                    
                if case_id not in self.case_messages:
                    self.case_messages[case_id] = []
                
                # Add user query message
                query_msg = {
                    "message_id": f"query_{len(self.case_messages[case_id])}_{case_id}",
                    "case_id": case_id,
                    "message_type": "user_query", 
                    "content": query.strip(),
                    "timestamp": datetime.now(timezone.utc),
                    "user_id": user_id or "anonymous"
                }
                self.case_messages[case_id].append(query_msg)
                
                # Update case metadata
                case = self.cases[case_id]
                case.message_count = len(self.case_messages[case_id])
                case.updated_at = datetime.now(timezone.utc)
                
                return True
            
            async def add_assistant_response(self, case_id: str, response_content: str, response_type: str = "ANSWER", user_id: str = None):
                """Add an assistant response message to a case"""
                if case_id not in self.cases:
                    return False
                    
                if case_id not in self.case_messages:
                    self.case_messages[case_id] = []
                
                # Add assistant response message
                response_msg = {
                    "message_id": f"response_{len(self.case_messages[case_id])}_{case_id}",
                    "case_id": case_id,
                    "message_type": "agent_response", 
                    "content": response_content.strip() if response_content else "",
                    "response_type": response_type,
                    "timestamp": datetime.now(timezone.utc),
                    "user_id": user_id or "anonymous"
                }
                self.case_messages[case_id].append(response_msg)
                
                # Update case metadata
                case = self.cases[case_id]
                case.message_count = len(self.case_messages[case_id])
                case.updated_at = datetime.now(timezone.utc)
                
                return True
            
            async def get_case_conversation_context(self, case_id: str, limit: int = 10) -> str:
                """Get formatted conversation context for LLM injection"""
                if case_id not in self.cases:
                    return ""
                
                # For minimal implementation, return a simple context format
                # In full implementation, this would retrieve actual messages from storage
                case = self.cases[case_id]
                
                context_lines = []
                context_lines.append(f"Previous conversation for case: {case.title}")
                context_lines.append(f"Case status: {case.status.value}")
                context_lines.append(f"Created: {case.created_at}")
                context_lines.append(f"Last updated: {case.updated_at}")
                context_lines.append(f"Message count: {getattr(case, 'message_count', 0)}")
                
                if case.description:
                    context_lines.append(f"Description: {case.description}")
                
                # Add placeholder for actual messages
                if getattr(case, 'message_count', 0) > 0:
                    context_lines.append("\n--- Recent conversation history would appear here ---")
                    context_lines.append("(In full implementation, this would show actual messages)")
                else:
                    context_lines.append("\n--- No conversation history yet ---")
                
                return "\n".join(context_lines)
            
            async def update_case(self, case_id: str, updates: dict, user_id: str = None) -> bool:
                """Update a case with new data - Phase 3: Handle manual title flag changes"""
                if case_id not in self.cases:
                    return False
                
                case = self.cases[case_id]
                current_time = datetime.now(timezone.utc)
                
                # Phase 3: Handle manual title updates
                if 'title' in updates:
                    new_title = updates['title']
                    if new_title and new_title.strip():
                        case.title = new_title
                        # Phase 3: Mark title as manually set to prevent auto-title override
                        case.title_manually_set = True
                    elif new_title == '':
                        # Allow clearing title (reset to "New Chat")
                        case.title = "New Chat"
                        # Reset manual flag when clearing title
                        case.title_manually_set = False
                
                # Update other fields
                if 'description' in updates:
                    case.description = updates.get('description', '')
                if 'status' in updates:
                    case.status = CaseStatus(updates['status']) if updates['status'] else case.status
                if 'priority' in updates:
                    case.priority = CasePriority(updates['priority']) if updates['priority'] else case.priority
                
                # Always update timestamp when any field is modified
                case.updated_at = current_time
                
                return True
        
        # Cache the instance to maintain state across requests
        if not hasattr(self, '_cached_minimal_case_service'):
            self._cached_minimal_case_service = MinimalCaseService()
        return self._cached_minimal_case_service
    
    # Phase 3: Enhanced Data Processing Services Getters
    
    def get_pattern_learner(self):
        """Get the pattern learner service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Pattern learner requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'pattern_learner', None)
    
    def get_enhanced_data_classifier(self):
        """Get the enhanced data classifier service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced data classifier requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_classifier = getattr(self, 'enhanced_data_classifier', None)
        if enhanced_classifier is None:
            # Fallback to standard classifier
            return self.get_data_classifier()
        return enhanced_classifier
    
    def get_enhanced_log_processor(self):
        """Get the enhanced log processor service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced log processor requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_processor = getattr(self, 'enhanced_log_processor', None)
        if enhanced_processor is None:
            # Fallback to standard processor
            return self.get_log_processor()
        return enhanced_processor
    
    def get_enhanced_security_assessment(self):
        """Get the enhanced security assessment service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced security assessment requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'enhanced_security_assessment', None)
    
    def get_enhanced_data_service(self):
        """Get the enhanced data service with memory integration and pattern learning"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced data service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_service = getattr(self, 'enhanced_data_service', None)
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
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'confidence_service', None)
    
    def get_decision_recorder(self):
        """Get the decision records & telemetry service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Decision recorder requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'decision_recorder', None)
    
    def get_microservice_session_service(self):
        """Get the microservice session service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Microservice session service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_service = getattr(self, 'microservice_session_service', None)
        if enhanced_service is None:
            # Fallback to standard session service
            return self.get_session_service()
        return enhanced_service
    
    def get_policy_service(self):
        """Get the policy/safety service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Policy service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'policy_service', None)
    
    def get_unified_retrieval_service(self):
        """Get the unified retrieval service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Unified retrieval service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'unified_retrieval_service', None)
    
    # Phase B: Orchestration and Coordination Services Getters
    
    def get_gateway_service(self):
        """Get the gateway processing service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Gateway service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'gateway_service', None)
    
    def get_loop_guard_service(self):
        """Get the loop guard service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Loop guard service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'loop_guard_service', None)
    
    def get_orchestrator_service(self):
        """Get the orchestrator service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Orchestrator service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'orchestrator_service', None)
    
    def get_redis_client(self):
        """Get the Redis client for job persistence and caching"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            if not getattr(self, '_initializing', False):
                logger.warning("Redis client requested but container not initialized")
                self.initialize()
        return getattr(self, 'redis_client', None)
    
    def get_job_service(self):
        """Get the job service for async operation management"""
        logger = logging.getLogger(__name__)
        if not self._initialized:
            if not getattr(self, '_initializing', False):
                logger.warning("Job service requested but container not initialized")
                self.initialize()
        
        # Create job service if not already created
        if not hasattr(self, '_job_service'):
            try:
                from faultmaven.infrastructure.jobs.job_service import JobService
                redis_client = self.get_redis_client()
                self._job_service = JobService(
                    redis_client=redis_client,
                    settings=self.settings
                )
                logger.info("✅ Job service initialized")
            except Exception as e:
                logger.warning(f"Job service initialization failed: {e}")
                self._job_service = None
        
        return self._job_service
    
    # Agentic Framework Services Getters
    
    def get_business_logic_workflow_engine(self) -> Optional[IBusinessLogicWorkflowEngine]:
        """Get the business logic workflow engine for plan-execute-observe-adapt orchestration"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Business Logic Workflow Engine requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'business_logic_workflow_engine', None)
    
    def get_agent_state_manager(self) -> Optional[IAgentStateManager]:
        """Get the agent state manager for persistent memory and execution state management"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Agent State Manager requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'agent_state_manager', None)
    
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Query Classification Engine requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'query_classification_engine', None)
    
    def get_tool_skill_broker(self) -> Optional[IToolSkillBroker]:
        """Get the tool skill broker for dynamic orchestration of tools and skills"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Tool Skill Broker requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'tool_skill_broker', None)
    
    def get_guardrails_policy_layer(self) -> Optional[IGuardrailsPolicyLayer]:
        """Get the guardrails policy layer for safety, security, and compliance enforcement"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Guardrails Policy Layer requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'guardrails_policy_layer', None)
    
    def get_response_synthesizer(self) -> Optional[IResponseSynthesizer]:
        """Get the response synthesizer for intelligent response generation and formatting"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Response Synthesizer requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'response_synthesizer', None)
    
    def get_error_fallback_manager(self) -> Optional[IErrorFallbackManager]:
        """Get the error fallback manager for robust error recovery and graceful degradation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Error Fallback Manager requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'error_fallback_manager', None)

    # Authentication Services

    def get_token_manager(self):
        """Get the token manager for authentication token operations"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Token manager requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'token_manager', None)

    def get_user_store(self):
        """Get the user store for user account management"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("User store requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'user_store', None)

    def health_check(self) -> dict:
        """Check health of all container dependencies"""
        if not self._initialized:
            return {"status": "not_initialized", "components": {}}
        
        components = {
            "llm_provider": self.llm_provider is not None,
            "sanitizer": self.sanitizer is not None,
            "tracer": self.tracer is not None,
            "vector_store": self.vector_store is not None,
            "session_store": self.session_store is not None,
            # Authentication Services
            "token_manager": getattr(self, 'token_manager', None) is not None,
            "user_store": getattr(self, 'user_store', None) is not None,
            "tools_count": len(self.tools) if self.tools else 0,
            "agent_service": self.agent_service is not None,
            "data_service": self.data_service is not None,
            "knowledge_service": self.knowledge_service is not None,
            "session_service": self.session_service is not None,
            "data_classifier": self.data_classifier is not None,
            "log_processor": self.log_processor is not None,
            # Phase 2 Advanced Intelligence Services
            "memory_service": getattr(self, 'memory_service', None) is not None,
            "planning_service": getattr(self, 'planning_service', None) is not None,
            "enhanced_agent_service": getattr(self, 'enhanced_agent_service', None) is not None,
            "orchestration_service": getattr(self, 'orchestration_service', None) is not None,
            # Performance Monitoring Services
            "metrics_collector": getattr(self, 'metrics_collector', None) is not None,
            "intelligent_cache": getattr(self, 'intelligent_cache', None) is not None,
            "analytics_dashboard_service": getattr(self, 'analytics_dashboard_service', None) is not None,
            "sla_monitor": getattr(self, 'sla_monitor', None) is not None,
            "performance_monitor": getattr(self, 'performance_monitor', None) is not None,
            # Phase 3 Enhanced Data Processing Services
            "pattern_learner": getattr(self, 'pattern_learner', None) is not None,
            "enhanced_data_classifier": getattr(self, 'enhanced_data_classifier', None) is not None,
            "enhanced_log_processor": getattr(self, 'enhanced_log_processor', None) is not None,
            "enhanced_security_assessment": getattr(self, 'enhanced_security_assessment', None) is not None,
            "enhanced_data_service": getattr(self, 'enhanced_data_service', None) is not None,
            # Phase A Microservice Foundation Services
            "microservice_session_service": getattr(self, 'microservice_session_service', None) is not None,
            "confidence_service": getattr(self, 'confidence_service', None) is not None,
            "policy_service": getattr(self, 'policy_service', None) is not None,
            "unified_retrieval_service": getattr(self, 'unified_retrieval_service', None) is not None,
            "decision_recorder": getattr(self, 'decision_recorder', None) is not None,
            # Phase B Orchestration and Coordination Services
            "gateway_service": getattr(self, 'gateway_service', None) is not None,
            "loop_guard_service": getattr(self, 'loop_guard_service', None) is not None,
            "orchestrator_service": getattr(self, 'orchestrator_service', None) is not None,
            # Agentic Framework Services - Next-generation agent architecture
            "business_logic_workflow_engine": getattr(self, 'business_logic_workflow_engine', None) is not None,
            "agent_state_manager": getattr(self, 'agent_state_manager', None) is not None,
            "query_classification_engine": getattr(self, 'query_classification_engine', None) is not None,
            "tool_skill_broker": getattr(self, 'tool_skill_broker', None) is not None,
            "guardrails_policy_layer": getattr(self, 'guardrails_policy_layer', None) is not None,
            "response_synthesizer": getattr(self, 'response_synthesizer', None) is not None,
            "error_fallback_manager": getattr(self, 'error_fallback_manager', None) is not None,
        }
        
        all_healthy = all(
            comp if isinstance(comp, bool) else comp > 0
            for comp in components.values()
        )
        
        return {
            "status": "healthy" if all_healthy else "degraded",
            "components": components
        }
    
    def reset(self):
        """Reset container state (useful for testing)"""
        self._initialized = False
        self._initializing = False
        
        # Clear all cached infrastructure and service components
        infrastructure_attrs = [
            'llm_provider', 'sanitizer', 'tracer', 'vector_store', 'session_store', 'token_manager', 'user_store',
            'data_classifier', 'log_processor', 'session_service', 'agent_service', 'data_service', 'knowledge_service'
        ]
        for attr in infrastructure_attrs:
            if hasattr(self, attr):
                delattr(self, attr)
        
        # Clear tools layer
        if hasattr(self, 'tools'):
            delattr(self, 'tools')
        
        # Clear cached services
        service_attrs = ['agent_service', 'data_service', 'knowledge_service']
        for attr in service_attrs:
            if hasattr(self, attr):
                delattr(self, attr)


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
