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
    from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool
    INTERFACES_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Interfaces not available: {e}")
    # Create placeholder types for testing environments
    ILLMProvider = Any
    ITracer = Any 
    ISanitizer = Any
    BaseTool = Any
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
                self._initializing = False
    
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
        # LLM Provider
        from faultmaven.infrastructure.llm.router import LLMRouter
        self.llm_provider: ILLMProvider = LLMRouter()
        
        # Data sanitization for PII protection
        from faultmaven.infrastructure.security.redaction import DataSanitizer
        self.sanitizer: ISanitizer = DataSanitizer()
        
        # Distributed tracing
        from faultmaven.infrastructure.observability.tracing import OpikTracer
        self.tracer: ITracer = OpikTracer()
        
        # Core processing interfaces
        from faultmaven.core.processing.classifier import DataClassifier
        from faultmaven.core.processing.log_analyzer import LogProcessor
        self.data_classifier = DataClassifier()  # Already implements IDataClassifier
        self.log_processor = LogProcessor()  # Already implements ILogProcessor
        
        logging.getLogger(__name__).debug("Infrastructure layer created")
    
    def _create_tools_layer(self):
        """Create tools with proper interface implementations"""
        from faultmaven.tools.knowledge_base import KnowledgeBaseTool
        from faultmaven.tools.web_search import WebSearchTool
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester
        
        # Knowledge base tool with ingester dependency
        try:
            ingester = KnowledgeIngester()
            knowledge_base_tool = KnowledgeBaseTool(knowledge_ingester=ingester)
        except Exception as e:
            logging.getLogger(__name__).warning(f"KnowledgeBaseTool initialization failed: {e}")
            knowledge_base_tool = None
        
        # Web search tool
        try:
            web_search_tool = WebSearchTool()
        except Exception as e:
            logging.getLogger(__name__).warning(f"WebSearchTool initialization failed: {e}")
            web_search_tool = None
        
        # Create tools list, filtering out None values
        self.tools: List[BaseTool] = [
            tool for tool in [knowledge_base_tool, web_search_tool] 
            if tool is not None
        ]
        
        logging.getLogger(__name__).debug(f"Tools layer created with {len(self.tools)} tools")
    
    def _create_service_layer(self):
        """Create service layer with interface dependencies"""
        from faultmaven.services.agent_service import AgentService
        from faultmaven.services.data_service import DataService  
        from faultmaven.services.knowledge_service import KnowledgeService
        
        # Session Service - Session management and validation
        try:
            from faultmaven.session_management import SessionManager
            self.session_service = SessionManager()
        except Exception:
            # Create a minimal session service for testing
            self.session_service = self._create_minimal_session_service()
            
        # Agent Service - Core troubleshooting orchestration
        self.agent_service = AgentService(
            llm_provider=self.llm_provider,
            tools=self.tools,
            tracer=self.tracer,
            sanitizer=self.sanitizer,
            session_service=self.session_service
        )
        
        # Data Service - Data processing and analysis
        # Create simple storage backend for development
        from faultmaven.services.data_service import SimpleStorageBackend
        storage_backend = SimpleStorageBackend()
        
        self.data_service = DataService(
            data_classifier=self.data_classifier,
            log_processor=self.log_processor,
            sanitizer=self.sanitizer,
            tracer=self.tracer,
            storage_backend=storage_backend
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
                sanitizer=self.sanitizer,
                tracer=self.tracer,
                vector_store=None,  # Will be implemented in future phases
            )
        else:
            self.knowledge_service = None
            
        logging.getLogger(__name__).debug("Service layer created")
    
    # Public getter methods for dependency injection
    
    def get_agent_service(self):
        """Get the agent service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Agent service requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.agent_service
    
    def get_data_service(self):
        """Get the data service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data service requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.data_service
        
    def get_knowledge_service(self):
        """Get the knowledge service with all dependencies injected"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Knowledge service requested but container not initialized - this should not happen after startup")
            self.initialize()
        if self.knowledge_service is None:
            return self._create_minimal_knowledge_service()
        return self.knowledge_service
    
    def _create_minimal_knowledge_service(self):
        """Create a minimal knowledge service for testing environments"""
        class MinimalKnowledgeService:
            async def upload_document(self, content, title, document_type, tags=None, source_url=None):
                return {"document_id": "test_doc", "job_id": "test_job", "status": "queued", "message": "Document uploaded"}
            
            async def get_document(self, document_id):
                return None  # Simulate not found
            
            async def list_documents(self, document_type=None, tags=None, limit=50, offset=0):
                return {"documents": [], "total": 0, "limit": limit, "offset": offset}
            
            async def delete_document(self, document_id):
                return {"success": False}
            
            async def search_documents(self, query, document_type=None, tags=None, limit=10):
                return {"query": query, "results": [], "total": 0}
            
            async def get_job_status(self, job_id):
                return None  # Simulate not found
        
        return MinimalKnowledgeService()
    
    def get_llm_provider(self):
        """Get the LLM provider interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("LLM provider requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.llm_provider
    
    def get_sanitizer(self):
        """Get the data sanitizer interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data sanitizer requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.sanitizer
    
    def get_tracer(self):
        """Get the tracer interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Tracer requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.tracer
    
    def get_tools(self):
        """Get list of available tools"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Tools requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.tools
    
    def get_data_classifier(self):
        """Get the data classifier interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Data classifier requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.data_classifier
    
    def get_log_processor(self):
        """Get the log processor interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Log processor requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.log_processor
    
    def get_session_service(self):
        """Get the session service implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Session service requested but container not initialized - this should not happen after startup")
            self.initialize()
        return self.session_service
    
    def _create_minimal_session_service(self):
        """Create a minimal session service for testing environments"""
        from datetime import datetime
        
        class MockSessionContext:
            def __init__(self, session_id, user_id=None):
                self.session_id = session_id
                self.user_id = user_id
                self.created_at = datetime.utcnow()
                self.last_activity = datetime.utcnow()
                self.data_uploads = []
                self.investigation_history = []
        
        class MinimalSessionService:
            async def create_session(self, user_id=None):
                return MockSessionContext("test_session", user_id)
            
            async def get_session(self, session_id):
                if session_id == "test_session":
                    return MockSessionContext(session_id, None)
                return None
            
            async def list_sessions(self, user_id=None, pattern="session:*"):
                return []
            
            async def delete_session(self, session_id):
                return True
            
            async def update_last_activity(self, session_id):
                return True
            
            async def get_session_stats(self):
                return {"total_sessions": 0, "active_sessions": 0}
        
        return MinimalSessionService()
    
    def health_check(self) -> dict:
        """Check health of all container dependencies"""
        if not self._initialized:
            return {"status": "not_initialized", "components": {}}
        
        components = {
            "llm_provider": self.llm_provider is not None,
            "sanitizer": self.sanitizer is not None,
            "tracer": self.tracer is not None,
            "tools_count": len(self.tools) if self.tools else 0,
            "agent_service": self.agent_service is not None,
            "data_service": self.data_service is not None,
            "knowledge_service": self.knowledge_service is not None,
            "session_service": self.session_service is not None,
            "data_classifier": self.data_classifier is not None,
            "log_processor": self.log_processor is not None,
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
            'llm_provider', 'sanitizer', 'tracer', 'data_classifier', 'log_processor',
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