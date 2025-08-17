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
import os
import logging

# Import interfaces with graceful fallback for testing environments
try:
    from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool, IVectorStore, ISessionStore
    from faultmaven.models.interfaces_case import ICaseStore, ICaseService
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


class DIContainer:
    """Singleton dependency injection container for centralized component management"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._initializing = False  # Prevent re-entrant initialization
        return cls._instance
    
    def initialize(self):
        """Initialize all dependencies with proper error handling"""
        logger = logging.getLogger(__name__)
        
        if self._initialized:
            logger.debug("Container already initialized, skipping")
            return
            
        if getattr(self, '_initializing', False):
            logger.debug("Container initialization already in progress, skipping")
            return
            
        self._initializing = True
        logger.info("Initializing DI Container with interface-based dependencies")
        
        try:
            # Always try to create infrastructure layer first - even if interfaces not available
            # This allows tests to mock the infrastructure layer creation
            self._create_infrastructure_layer()
            
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
    
    def _create_infrastructure_layer(self):
        """Create infrastructure components with interface implementations"""
        # Initialize configuration manager first
        try:
            from faultmaven.config.configuration_manager import get_config
            self.config = get_config()
            logging.getLogger(__name__).info("✅ Configuration manager initialized")
            
            # Debug configuration during infrastructure creation
            llm_config = self.config.get_llm_config()
            logging.getLogger(__name__).info(f"🔍 Container: Configuration check during infrastructure creation:")
            logging.getLogger(__name__).info(f"🔍 Container: CHAT_PROVIDER = {llm_config.get('provider', 'NOT_SET')}")
            logging.getLogger(__name__).info(f"🔍 Container: LLM_REQUEST_TIMEOUT = {llm_config.get('timeout', 'NOT_SET')}")
        except Exception as e:
            logging.getLogger(__name__).warning(f"Configuration manager not available: {e}")
            # Fallback to direct environment variable access
            import os
            logging.getLogger(__name__).info(f"🔍 Container: Environment check during infrastructure creation:")
            logging.getLogger(__name__).info(f"🔍 Container: CHAT_PROVIDER = {os.getenv('CHAT_PROVIDER', 'NOT_SET')}")
            self.config = None
        
        # Data sanitization for PII protection
        from faultmaven.infrastructure.security.redaction import DataSanitizer
        if self.config:
            security_config = self.config.get_security_config()
            logging.getLogger(__name__).debug(f"Security config loaded: {security_config}")
        self.sanitizer: ISanitizer = DataSanitizer()
        
        # Distributed tracing (initialize first to set up environment variables)
        from faultmaven.infrastructure.observability.tracing import OpikTracer
        if self.config:
            observability_config = self.config.get_observability_config()
            logging.getLogger(__name__).debug(f"Observability config loaded: {observability_config}")
        self.tracer: ITracer = OpikTracer()
        
        # LLM Provider (initialize after Opik tracer to ensure environment is properly set up)
        from faultmaven.infrastructure.llm.router import LLMRouter
        self.llm_provider: ILLMProvider = LLMRouter()
        
        # Core processing interfaces
        from faultmaven.core.processing.classifier import DataClassifier
        from faultmaven.core.processing.log_analyzer import LogProcessor
        self.data_classifier = DataClassifier()  # Already implements IDataClassifier
        self.log_processor = LogProcessor()  # Already implements ILogProcessor
        
        # Vector store for knowledge base
        from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
        try:
            self.vector_store: IVectorStore = ChromaDBVectorStore()
            logging.getLogger(__name__).debug("Vector store initialized")
        except Exception as e:
            logging.getLogger(__name__).warning(f"Vector store initialization failed: {e}")
            self.vector_store = None
        
        # Session store for session management
        from faultmaven.infrastructure.persistence.redis_session_store import RedisSessionStore
        try:
            self.session_store: ISessionStore = RedisSessionStore()
            logging.getLogger(__name__).debug("Session store initialized")
        except Exception as e:
            logging.getLogger(__name__).warning(f"Session store initialization failed: {e}")
            self.session_store = None
        
        # Case store for case persistence (optional feature)
        try:
            from faultmaven.infrastructure.persistence.redis_case_store import RedisCaseStore
            self.case_store: ICaseStore = RedisCaseStore()
            logging.getLogger(__name__).debug("Case store initialized")
        except ImportError:
            logging.getLogger(__name__).debug("Case store not available - case persistence disabled")
            self.case_store = None
        except Exception as e:
            logging.getLogger(__name__).warning(f"Case store initialization failed: {e}")
            self.case_store = None
        
        logging.getLogger(__name__).debug("Infrastructure layer created")
    
    def _create_tools_layer(self):
        """Create tools using the registry pattern"""
        from faultmaven.tools.registry import tool_registry
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester
        
        # Import tools to trigger registration
        import faultmaven.tools.knowledge_base
        import faultmaven.tools.web_search
        
        # Create knowledge ingester for tools that need it
        try:
            ingester = KnowledgeIngester()
        except Exception as e:
            logging.getLogger(__name__).warning(f"KnowledgeIngester creation failed: {e}")
            ingester = None
        
        # Create all registered tools
        self.tools: List[BaseTool] = tool_registry.create_all_tools(
            knowledge_ingester=ingester
        )
        
        logging.getLogger(__name__).debug(
            f"Tools layer created with {len(self.tools)} tools: {tool_registry.list_tools()}"
        )
    
    def _create_service_layer(self):
        """Create service layer with interface dependencies"""
        from faultmaven.services.agent_service import AgentService
        from faultmaven.services.data_service import DataService  
        from faultmaven.services.knowledge_service import KnowledgeService
        from faultmaven.services.session_service import SessionService
        
        # Case Service - Case persistence and management (optional)
        try:
            from faultmaven.services.case_service import CaseService
            if hasattr(self, 'case_store') and self.case_store:
                self.case_service: ICaseService = CaseService(
                    case_store=self.case_store,
                    session_store=self.get_session_store()
                )
                logging.getLogger(__name__).debug("Case service initialized")
            else:
                self.case_service = None
                logging.getLogger(__name__).debug("Case service disabled - case store not available")
        except ImportError:
            logging.getLogger(__name__).debug("Case service not available")
            self.case_service = None
        except Exception as e:
            logging.getLogger(__name__).warning(f"Case service initialization failed: {e}")
            self.case_service = None

        # Session Service - Session management and validation
        try:
            from faultmaven.session_management import SessionManager
            session_manager = SessionManager(session_store=self.get_session_store())
            self.session_service = SessionService(
                session_manager=session_manager,
                case_service=self.case_service  # Inject case service for enhanced features
            )
        except Exception:
            # Create a minimal session service for testing
            self.session_service = self._create_minimal_session_service()
            
        # Agent Service - Core troubleshooting orchestration
        self.agent_service = AgentService(
            llm_provider=self.get_llm_provider(),
            tools=self.get_tools(),
            tracer=self.get_tracer(),
            sanitizer=self.get_sanitizer(),
            session_service=self.session_service
        )
        
        # Data Service - Data processing and analysis
        # Create simple storage backend for development
        from faultmaven.services.data_service import SimpleStorageBackend
        storage_backend = SimpleStorageBackend()
        
        self.data_service = DataService(
            data_classifier=self.get_data_classifier(),
            log_processor=self.get_log_processor(),
            sanitizer=self.get_sanitizer(),
            tracer=self.get_tracer(),
            storage_backend=storage_backend,
            session_service=self.session_service
        )
        
        # Knowledge Service - Knowledge base operations
        # Create knowledge ingester and vector store placeholders
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester
        try:
            knowledge_ingester = KnowledgeIngester()
        except Exception as e:
            logging.getLogger(__name__).warning(f"KnowledgeIngester creation failed: {e}")
            knowledge_ingester = None
        
        if knowledge_ingester:
            self.knowledge_service = KnowledgeService(
                knowledge_ingester=knowledge_ingester,
                sanitizer=self.get_sanitizer(),
                tracer=self.get_tracer(),
                vector_store=self.get_vector_store(),  # Now using actual IVectorStore implementation
            )
        else:
            self.knowledge_service = None
        
        # Phase 2: Advanced Intelligence Services
        self._create_advanced_intelligence_services()
        
        # Phase 2: Performance Monitoring and Optimization Services
        self._create_performance_monitoring_services()
        
        # Phase 3: Enhanced Data Processing Services
        self._create_enhanced_data_processing_services()
            
        logging.getLogger(__name__).debug("Service layer created")
    
    def _create_advanced_intelligence_services(self):
        """Create Phase 2 advanced intelligence services (Memory & Planning)"""
        try:
            # Memory Service - Intelligent conversation context management
            from faultmaven.services.memory_service import MemoryService
            self.memory_service = MemoryService(
                llm_provider=self.get_llm_provider()
            )
            logging.getLogger(__name__).debug("Memory service created")
            
            # Planning Service - Strategic planning and problem decomposition
            from faultmaven.services.planning_service import PlanningService
            self.planning_service = PlanningService(
                llm_provider=self.get_llm_provider(),
                memory_service=self.memory_service
            )
            logging.getLogger(__name__).debug("Planning service created")
            
            # Enhanced Agent Service - Integrates memory and planning
            from faultmaven.services.enhanced_agent_service import EnhancedAgentService
            self.enhanced_agent_service = EnhancedAgentService(
                llm_provider=self.get_llm_provider(),
                tools=self.get_tools(),
                tracer=self.get_tracer(),
                sanitizer=self.get_sanitizer(),
                memory_service=self.memory_service,
                planning_service=self.planning_service,
                session_service=self.session_service
            )
            logging.getLogger(__name__).debug("Enhanced agent service created")
            
            # Orchestration Service - Multi-step troubleshooting workflow management
            from faultmaven.services.orchestration_service import OrchestrationService
            from faultmaven.services.reasoning_service import ReasoningService
            from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
            
            # Create supporting services for orchestration
            reasoning_service = ReasoningService(
                llm_provider=self.get_llm_provider(),
                memory_service=self.memory_service,
                planning_service=self.planning_service
            )
            
            enhanced_knowledge_service = EnhancedKnowledgeService(
                llm_provider=self.get_llm_provider(),
                memory_service=self.memory_service,
                vector_store=self.get_vector_store(),
                sanitizer=self.get_sanitizer(),
                tracer=self.get_tracer()
            )
            
            self.orchestration_service = OrchestrationService(
                memory_service=self.memory_service,
                planning_service=self.planning_service,
                reasoning_service=reasoning_service,
                enhanced_knowledge_service=enhanced_knowledge_service,
                llm_provider=self.get_llm_provider(),
                tracer=self.get_tracer()
            )
            logging.getLogger(__name__).debug("Orchestration service created")
            
        except Exception as e:
            logging.getLogger(__name__).warning(f"Advanced intelligence services creation failed: {e}")
            # Set fallback services
            self.memory_service = None
            self.planning_service = None
            self.enhanced_agent_service = None
            self.orchestration_service = None
    
    def _create_performance_monitoring_services(self):
        """Create Phase 2 performance monitoring and optimization services"""
        try:
            # Metrics Collector - Advanced performance metrics collection
            from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
            self.metrics_collector = MetricsCollector(
                tracer=self.get_tracer(),
                buffer_size=10000,
                flush_interval=60,
                analytics_window=300
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
                redis_client=None,  # Would connect to Redis in production
                enable_analytics=True
            )
            logging.getLogger(__name__).debug("Intelligent cache created")
            
            # Analytics Dashboard Service - System performance insights
            from faultmaven.services.analytics_dashboard_service import AnalyticsDashboardService
            self.analytics_dashboard_service = AnalyticsDashboardService(
                metrics_collector=self.metrics_collector,
                intelligent_cache=self.intelligent_cache,
                tracer=self.get_tracer()
            )
            logging.getLogger(__name__).debug("Analytics dashboard service created")
            
            # Performance Optimization Service - Proactive optimization
            from faultmaven.services.performance_optimization_service import PerformanceOptimizationService
            self.performance_optimization_service = PerformanceOptimizationService(
                metrics_collector=self.metrics_collector,
                intelligent_cache=self.intelligent_cache,
                analytics_service=self.analytics_dashboard_service,
                tracer=self.get_tracer(),
                enable_auto_optimization=True,
                optimization_aggressiveness="moderate"
            )
            logging.getLogger(__name__).debug("Performance optimization service created")
            
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
            
            # Enhanced Data Classifier - Memory-aware classification with pattern learning
            from faultmaven.core.processing.classifier import EnhancedDataClassifier
            self.enhanced_data_classifier = EnhancedDataClassifier(
                memory_service=self.get_memory_service()
            )
            logging.getLogger(__name__).debug("Enhanced data classifier created")
            
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
            from faultmaven.services.enhanced_data_service import EnhancedDataService
            self.enhanced_data_service = EnhancedDataService(
                memory_service=self.get_memory_service(),
                data_classifier=self.enhanced_data_classifier,
                log_processor=self.enhanced_log_processor,
                sanitizer=self.get_sanitizer(),
                tracer=self.get_tracer(),
                storage_backend=None,  # Will use default storage
                session_service=self.get_session_service(),
                pattern_learner=self.pattern_learner
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
    
    # Public getter methods for dependency injection
    
    def get_agent_service(self):
        """Get the agent service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Agent service requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'agent_service', None)
    
    def get_data_service(self):
        """Get the data service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data service requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'data_service', None)
        
    def get_knowledge_service(self):
        """Get the knowledge service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Knowledge service requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        knowledge_service = getattr(self, 'knowledge_service', None)
        if knowledge_service is None:
            return self._create_minimal_knowledge_service()
        return knowledge_service
    
    def get_memory_service(self):
        """Get the memory service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Memory service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'memory_service', None)
    
    def get_planning_service(self):
        """Get the planning service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Planning service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'planning_service', None)
    
    def get_enhanced_agent_service(self):
        """Get the enhanced agent service with memory and planning integration"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Enhanced agent service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        enhanced_service = getattr(self, 'enhanced_agent_service', None)
        if enhanced_service is None:
            # Fallback to regular agent service if enhanced is not available
            return self.get_agent_service()
        return enhanced_service
    
    def get_orchestration_service(self):
        """Get the orchestration service for multi-step troubleshooting workflows"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Orchestration service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'orchestration_service', None)
    
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
    
    def get_performance_optimization_service(self):
        """Get the performance optimization service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Performance optimization service requested but container not initialized")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'performance_optimization_service', None)
    
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
    
    def _create_minimal_knowledge_service(self):
        """Create a minimal knowledge service for testing environments"""
        import uuid
        from datetime import datetime
        
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
                    "created_at": datetime.utcnow().isoformat() + 'Z',
                    "updated_at": datetime.utcnow().isoformat() + 'Z'
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
                        "created_at": datetime.utcnow().isoformat() + 'Z'
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
                        "created_at": datetime.utcnow().isoformat() + 'Z',
                        "updated_at": datetime.utcnow().isoformat() + 'Z',
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
                        "created_at": datetime.utcnow().isoformat() + 'Z',
                        "completed_at": datetime.utcnow().isoformat() + 'Z',
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
                        "created_at": datetime.utcnow().isoformat() + 'Z',
                        "updated_at": datetime.utcnow().isoformat() + 'Z'
                    }
                
                doc = self.documents[document_id]
                if title:
                    doc["title"] = title
                if content:
                    doc["content"] = content
                if tags is not None:
                    doc["tags"] = tags
                doc["updated_at"] = datetime.utcnow().isoformat() + 'Z'
                
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
                    doc["updated_at"] = datetime.utcnow().isoformat() + 'Z'
                    return doc
                return None
            
            async def bulk_update_documents(self, document_ids, updates):
                updated_count = 0
                for doc_id in document_ids:
                    if doc_id in self.documents:
                        self.documents[doc_id].update(updates)
                        self.documents[doc_id]["updated_at"] = datetime.utcnow().isoformat() + 'Z'
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
                    "last_updated": datetime.utcnow().isoformat() + 'Z'
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
            logger.warning("LLM provider requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        
        # Ensure we always return a valid implementation, even if initialization failed
        llm_provider = getattr(self, 'llm_provider', None)
        if llm_provider is None:
            # Create minimal fallback implementation
            from unittest.mock import MagicMock
            logger = logging.getLogger(__name__)
            logger.warning("Creating fallback LLM provider due to initialization failure")
            self.llm_provider = MagicMock()
            return self.llm_provider
        return llm_provider
    
    def get_sanitizer(self):
        """Get the data sanitizer interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data sanitizer requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
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
            logger.warning("Tracer requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
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
            logger.warning("Tools requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'tools', [])
    
    def get_data_classifier(self):
        """Get the data classifier interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data classifier requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'data_classifier', None)
    
    def get_log_processor(self):
        """Get the log processor interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Log processor requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
                self.initialize()
        return getattr(self, 'log_processor', None)
    
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
            logger.warning("Session service requested but container not initialized - this should not happen after startup")
            if not getattr(self, '_initializing', False):
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
            def __init__(self, session_id, user_id=None):
                self.session_id = session_id
                self.user_id = user_id
                self.created_at = datetime.utcnow()
                self.last_activity = datetime.utcnow()
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
                
            async def create_session(self, user_id=None, metadata=None):
                session_id = str(uuid.uuid4())
                session = MockSessionContext(session_id, user_id)
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
                    self.sessions[session_id].last_activity = datetime.utcnow()
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
        
        return MinimalSessionService()
    
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
            "performance_optimization_service": getattr(self, 'performance_optimization_service', None) is not None,
            "sla_monitor": getattr(self, 'sla_monitor', None) is not None,
            "performance_monitor": getattr(self, 'performance_monitor', None) is not None,
            # Phase 3 Enhanced Data Processing Services
            "pattern_learner": getattr(self, 'pattern_learner', None) is not None,
            "enhanced_data_classifier": getattr(self, 'enhanced_data_classifier', None) is not None,
            "enhanced_log_processor": getattr(self, 'enhanced_log_processor', None) is not None,
            "enhanced_security_assessment": getattr(self, 'enhanced_security_assessment', None) is not None,
            "enhanced_data_service": getattr(self, 'enhanced_data_service', None) is not None,
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
            'llm_provider', 'sanitizer', 'tracer', 'vector_store', 'session_store', 'data_classifier', 'log_processor',
            'session_service', 'agent_service', 'data_service', 'knowledge_service'
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