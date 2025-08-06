"""Dependency Injection Container

Purpose: Centralized dependency injection configuration

This module defines the main DI container that manages all service
dependencies and their lifecycles throughout the application.

Key Features:
- Singleton service instances
- Lazy initialization
- Configuration management
- Service factory methods
"""

import logging
import os
from typing import Optional

from .core.agent.agent import FaultMavenAgent
from .tools.knowledge_base import KnowledgeBaseTool
from .tools.web_search import WebSearchTool
from .core.processing.classifier import DataClassifier
from .core.processing.log_analyzer import LogProcessor
from .core.knowledge.ingestion import KnowledgeIngester
from .infrastructure.llm.router import LLMRouter
from .infrastructure.security.redaction import DataSanitizer
from .services import AgentService, DataService, KnowledgeService, SessionService
from .session_management import SessionManager


class Container:
    """Main dependency injection container for FaultMaven"""

    def __init__(self):
        """Initialize the container with configuration"""
        self.logger = logging.getLogger(__name__)
        
        # Configuration with K8s Redis support
        self.config = {
            "redis_url": os.getenv("REDIS_URL"),
            "redis_host": os.getenv("REDIS_HOST"),
            "redis_port": int(os.getenv("REDIS_PORT", "30379")) if os.getenv("REDIS_PORT") else None,
            "redis_password": os.getenv("REDIS_PASSWORD"),
            "session_timeout_hours": int(os.getenv("SESSION_TIMEOUT_HOURS", "24")),
            "max_sessions_per_user": int(os.getenv("MAX_SESSIONS_PER_USER", "10")),
            "openai_api_key": os.getenv("OPENAI_API_KEY"),
            "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY"),
        }
        
        # Service instances (lazy-loaded)
        self._session_manager: Optional[SessionManager] = None
        self._data_sanitizer: Optional[DataSanitizer] = None
        self._llm_router: Optional[LLMRouter] = None
        self._knowledge_ingester: Optional[KnowledgeIngester] = None
        self._knowledge_base_tool: Optional[KnowledgeBaseTool] = None
        self._web_search_tool: Optional[WebSearchTool] = None
        self._core_agent: Optional[FaultMavenAgent] = None
        self._data_classifier: Optional[DataClassifier] = None
        self._log_processor: Optional[LogProcessor] = None
        
        # Service layer instances
        self._session_service: Optional[SessionService] = None
        self._agent_service: Optional[AgentService] = None
        self._data_service: Optional[DataService] = None
        self._knowledge_service: Optional[KnowledgeService] = None

    # Core Infrastructure Services

    @property
    def session_manager(self) -> SessionManager:
        """Get or create SessionManager instance"""
        if self._session_manager is None:
            self._session_manager = SessionManager(
                redis_url=self.config["redis_url"],
                redis_host=self.config["redis_host"],
                redis_port=self.config["redis_port"],
                redis_password=self.config["redis_password"],
                session_timeout_hours=self.config["session_timeout_hours"],
            )
            self.logger.info("Initialized SessionManager with enhanced Redis client")
        return self._session_manager

    @property
    def data_sanitizer(self) -> DataSanitizer:
        """Get or create DataSanitizer instance"""
        if self._data_sanitizer is None:
            self._data_sanitizer = DataSanitizer()
            self.logger.info("Initialized DataSanitizer")
        return self._data_sanitizer

    # LLM and AI Services

    @property
    def llm_router(self) -> LLMRouter:
        """Get or create LLMRouter instance"""
        if self._llm_router is None:
            self._llm_router = LLMRouter()
            self.logger.info("Initialized LLMRouter")
        return self._llm_router

    # Knowledge Base Services

    @property
    def knowledge_ingester(self) -> KnowledgeIngester:
        """Get or create KnowledgeIngester instance"""
        if self._knowledge_ingester is None:
            self._knowledge_ingester = KnowledgeIngester()
            self.logger.info("Initialized KnowledgeIngester")
        return self._knowledge_ingester

    @property
    def knowledge_base_tool(self) -> KnowledgeBaseTool:
        """Get or create KnowledgeBaseTool instance"""
        if self._knowledge_base_tool is None:
            self._knowledge_base_tool = KnowledgeBaseTool(
                knowledge_ingester=self.knowledge_ingester
            )
            self.logger.info("Initialized KnowledgeBaseTool")
        return self._knowledge_base_tool

    @property
    def web_search_tool(self) -> WebSearchTool:
        """Get or create WebSearchTool instance"""
        if self._web_search_tool is None:
            self._web_search_tool = WebSearchTool()
            self.logger.info("Initialized WebSearchTool")
        return self._web_search_tool

    # Agent Services

    @property
    def core_agent(self) -> FaultMavenAgent:
        """Get or create FaultMavenAgent instance"""
        if self._core_agent is None:
            try:
                self._core_agent = FaultMavenAgent(
                    llm_router=self.llm_router,
                    knowledge_base_tool=self.knowledge_base_tool,
                    web_search_tool=self.web_search_tool,
                )
                self.logger.info("Initialized FaultMavenAgent")
            except Exception as e:
                self.logger.error(f"Failed to initialize FaultMavenAgent: {e}")
                raise
        return self._core_agent

    # Data Processing Services

    @property
    def data_classifier(self) -> DataClassifier:
        """Get or create DataClassifier instance"""
        if self._data_classifier is None:
            self._data_classifier = DataClassifier()
            self.logger.info("Initialized DataClassifier")
        return self._data_classifier

    @property
    def log_processor(self) -> LogProcessor:
        """Get or create LogProcessor instance"""
        if self._log_processor is None:
            self._log_processor = LogProcessor()
            self.logger.info("Initialized LogProcessor")
        return self._log_processor

    # Service Layer

    @property
    def session_service(self) -> SessionService:
        """Get or create SessionService instance"""
        if self._session_service is None:
            self._session_service = SessionService(
                session_manager=self.session_manager,
                max_sessions_per_user=self.config["max_sessions_per_user"],
            )
            self.logger.info("Initialized SessionService")
        return self._session_service

    @property
    def agent_service(self) -> AgentService:
        """Get or create AgentService instance"""
        if self._agent_service is None:
            self._agent_service = AgentService(
                core_agent=self.core_agent,
                data_sanitizer=self.data_sanitizer,
            )
            self.logger.info("Initialized AgentService")
        return self._agent_service

    @property
    def data_service(self) -> DataService:
        """Get or create DataService instance"""
        if self._data_service is None:
            self._data_service = DataService(
                data_classifier=self.data_classifier,
                log_processor=self.log_processor,
                data_sanitizer=self.data_sanitizer,
            )
            self.logger.info("Initialized DataService")
        return self._data_service

    @property
    def knowledge_service(self) -> KnowledgeService:
        """Get or create KnowledgeService instance"""
        if self._knowledge_service is None:
            self._knowledge_service = KnowledgeService(
                knowledge_ingester=self.knowledge_ingester,
                data_sanitizer=self.data_sanitizer,
            )
            self.logger.info("Initialized KnowledgeService")
        return self._knowledge_service

    # Lifecycle Management

    async def initialize(self):
        """Initialize all services that require async setup"""
        self.logger.info("Initializing container services...")
        
        # Initialize services that need async setup
        # For now, most are initialized on-demand
        
        self.logger.info("Container initialization complete")

    async def shutdown(self):
        """Cleanup and shutdown all services"""
        self.logger.info("Shutting down container services...")
        
        # Close Redis connection
        if self._session_manager:
            await self._session_manager.close()
            
        self.logger.info("Container shutdown complete")

    # Factory Methods

    def create_session_context(self, user_id: Optional[str] = None):
        """Factory method to create a new session context"""
        return self.session_service.create_session(user_id)

    def get_troubleshooting_pipeline(self):
        """Get the complete troubleshooting pipeline"""
        return {
            "agent": self.agent_service,
            "data": self.data_service,
            "knowledge": self.knowledge_service,
            "session": self.session_service,
        }


# Global container instance
container = Container()