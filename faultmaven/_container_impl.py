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
import os
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

    async def initialize(self):
        """Initialize all dependencies with proper error handling (async for proper event loop handling)"""
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

                # Fail-fast in production for critical infrastructure
                # Allow graceful degradation only in development/test environments
                is_production = os.getenv("ENVIRONMENT", "").lower() in (
                    "production",
                    "prod",
                )
                skip_service_checks = (
                    os.getenv("SKIP_SERVICE_CHECKS", "").lower() == "true"
                )
                is_test = "pytest" in sys.modules

                if is_production and not skip_service_checks and not is_test:
                    # Fail-fast: raise exception to prevent half-initialized state
                    logger.critical(
                        "FAIL-FAST: Critical infrastructure initialization failed in production. Aborting startup."
                    )
                    raise RuntimeError(
                        f"DI Container initialization failed in production: {e}. "
                        "Critical infrastructure (database, LLM registry) must be available. "
                        "Check logs for details."
                    ) from e

                self._initialized = False

    def _ensure_initialized_for_getter(self) -> None:
        """Best-effort lazy initialization for sync getter methods.

        Tests and some legacy call sites expect getters to trigger initialization.

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
        self.knowledge_service = self._create_minimal_knowledge_service()
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
        knowledge_service = getattr(self, "knowledge_service", None)
        if knowledge_service is None:
            return self._create_minimal_knowledge_service()
        return knowledge_service

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

    # Phase 2: Advanced Intelligence Services Getters

    def get_memory_service(self):
        """Get the memory service - now provided by AgentStateManager"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Memory service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        # Memory service functionality is now provided by AgentStateManager
        return getattr(self, "agent_state_manager", None)

    def get_planning_service(self):
        """Get the planning service - now provided by BusinessLogicWorkflowEngine"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Planning service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        # Planning service functionality is now provided by BusinessLogicWorkflowEngine
        return getattr(self, "business_logic_workflow_engine", None)

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

    def _create_minimal_knowledge_service(self):
        """Create a minimal knowledge service for testing environments"""
        import uuid
        from datetime import datetime, timezone

        from faultmaven.utils.serialization import to_json_compatible

        class MinimalKnowledgeService:
            def __init__(self):
                self.documents = {}  # Simple in-memory storage for testing

            async def upload_document(
                self,
                content,
                title,
                document_type,
                category=None,
                tags=None,
                source_url=None,
                description=None,
            ):
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
                    "updated_at": to_json_compatible(datetime.now(timezone.utc)),
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
                        "created_at": to_json_compatible(datetime.now(timezone.utc)),
                    },
                }

            async def get_document(self, document_id):
                # Return document if it exists, or create a mock one for testing
                if document_id in self.documents:
                    return self.documents[document_id]
                elif document_id and (
                    document_id.startswith("doc_") or len(document_id) >= 8
                ):
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
                        "metadata": {"author": "test-system", "version": "1.0"},
                    }
                return None

            async def list_documents(
                self, document_type=None, tags=None, limit=50, offset=0
            ):
                docs = list(self.documents.values())

                # Apply filters
                if document_type:
                    docs = [d for d in docs if d.get("document_type") == document_type]
                if tags:
                    docs = [
                        d for d in docs if any(tag in d.get("tags", []) for tag in tags)
                    ]

                # Apply pagination
                total = len(docs)
                docs = docs[offset : offset + limit]

                return {
                    "documents": docs,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "filters": {"document_type": document_type, "tags": tags},
                }

            async def delete_document(self, document_id):
                if document_id in self.documents:
                    del self.documents[document_id]
                    return {"success": True, "document_id": document_id}
                else:
                    return {"success": False, "document_id": document_id}

            async def search_documents(
                self, query, document_type=None, tags=None, limit=10
            ):
                # Simple text search in titles and content
                results = []
                for doc_id, doc in self.documents.items():
                    if (
                        query.lower() in doc.get("title", "").lower()
                        or query.lower() in doc.get("content", "").lower()
                    ):
                        # Apply filters
                        if document_type and doc.get("document_type") != document_type:
                            continue
                        if tags and not any(tag in doc.get("tags", []) for tag in tags):
                            continue

                        results.append(
                            {
                                "document_id": doc_id,
                                "content": doc.get("content", "")[:200] + "...",
                                "metadata": {
                                    "title": doc.get("title"),
                                    "document_type": doc.get("document_type"),
                                    "tags": doc.get("tags", []),
                                },
                                "similarity_score": 0.8,  # Mock score
                            }
                        )

                return {
                    "query": query,
                    "total_results": len(results),
                    "results": results[:limit],
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
                            "error_count": 0,
                        },
                    }
                return None

            async def update_document(
                self, document_id, title=None, content=None, tags=None
            ):
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
                        "updated_at": to_json_compatible(datetime.now(timezone.utc)),
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
                    "updated_at": doc["updated_at"],
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
                        self.documents[doc_id]["updated_at"] = to_json_compatible(
                            datetime.now(timezone.utc)
                        )
                        updated_count += 1

                return {
                    "success": True,
                    "updated_count": updated_count,
                    "total_requested": len(document_ids),
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
                    "total_requested": len(document_ids),
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
                    "last_updated": to_json_compatible(datetime.now(timezone.utc)),
                }

            async def get_search_analytics(self):
                return {
                    "popular_queries": [
                        "database error",
                        "connection timeout",
                        "network issue",
                    ],
                    "search_volume": 150,
                    "avg_response_time": 0.2,
                    "hit_rate": 0.85,
                    "category_distribution": {
                        "database": 40,
                        "network": 30,
                        "application": 30,
                    },
                }

        return MinimalKnowledgeService()

    def get_llm_provider(self):
        """Get the LLM provider interface implementation"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            # Only warn if not currently initializing
            if not getattr(self, "_initializing", False):
                logger.warning(
                    "LLM provider requested but container not initialized - this should not happen after startup"
                )

        # Ensure we always return a valid implementation, even if initialization failed
        llm_provider = getattr(self, "llm_provider", None)
        if llm_provider is None:
            # Create proper fallback implementation instead of MagicMock
            from faultmaven.models.interfaces import ILLMProvider

            logger = logging.getLogger(__name__)
            logger.error(
                "LLM provider not initialized - creating minimal fallback implementation"
            )

            class MinimalLLMProvider(ILLMProvider):
                async def generate(self, prompt: str, **kwargs) -> str:
                    return "I apologize, but the AI service is temporarily unavailable. Please try again in a few moments."

            self.llm_provider = MinimalLLMProvider()
            return self.llm_provider
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

    def get_investigation_orchestrator(self):
        """Get the investigation orchestrator service (TASK-026)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Investigation orchestrator requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "investigation_orchestrator", None)

    def get_evidence_service(self):
        """Get the evidence service (PR #46b - Evidence management)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Evidence service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "evidence_service", None)

    def get_organization_service(self):
        """Get the organization service implementation (optional feature)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Organization service requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "organization_service", None)

    def get_team_service(self):
        """Get the team service implementation (optional feature)"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Team service requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "team_service", None)

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
        """Create a minimal session service for testing environments"""
        import uuid
        from datetime import datetime

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
            """Mock session manager for testing (spec-compliant v2.0)"""

            def __init__(self):
                self.sessions = {}

        class MinimalSessionService:
            def __init__(self):
                self.sessions = {}  # Store sessions in memory for testing
                self.session_manager = MockSessionManager()  # Add mock session manager
                self.session_manager.sessions = self.sessions  # Share session storage

            async def create_session(
                self,
                user_id=None,
                session_id=None,
                metadata=None,
                client_id=None,
                initial_context=None,
            ):
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
                return {
                    "total_sessions": len(self.sessions),
                    "active_sessions": len(self.sessions),
                }

            async def cleanup_session_data(self, session_id):
                return {
                    "session_id": session_id,
                    "success": True,
                    "cleaned_items": {
                        "data_uploads": 0,
                        "case_history": 0,
                        "temp_files": 0,
                    },
                }

            async def get_or_create_current_case_id(
                self, session_id, force_new_case=False
            ):
                """Get or create a case ID for the session"""
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not hasattr(session, "current_case_id") or force_new_case:
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

            async def record_query_operation(
                self, session_id, query, case_id, context=None, confidence_score=1.0
            ):
                """Record a query operation in the session"""
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not hasattr(session, "operations"):
                        session.operations = []
                    session.operations.append(
                        {
                            "query": query,
                            "case_id": case_id,
                            "context": context,
                            "confidence_score": confidence_score,
                            "timestamp": datetime.now(timezone.utc),
                        }
                    )
                    return True
                return False

            async def record_case_message(
                self,
                session_id: str,
                message_content: str,
                message_type=None,  # Use Any to avoid import issues in container
                author_id=None,
                metadata=None,
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
                    if not hasattr(session, "case_messages"):
                        session.case_messages = []
                    session.case_messages.append(
                        {
                            "content": message_content,
                            "message_type": (
                                str(message_type) if message_type else "user_query"
                            ),
                            "author_id": author_id,
                            "metadata": metadata or {},
                            "timestamp": datetime.now(timezone.utc),
                        }
                    )
                    return True
                return False

        return MinimalSessionService()

    def _create_minimal_case_service(self):
        """Create a minimal case service for testing environments"""
        import uuid
        from datetime import datetime

        from faultmaven.modules.case.domain.models import Case, CaseStatus

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
                )  # Use owner_id as org_id if not provided

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
                    status=CaseStatus.INQUIRY,
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

            async def list_cases_for_session(self, session_id, limit=20, offset=0):
                # Filter cases by checking if session_id matches case.current_session_id
                session_cases = [
                    case
                    for case in self.cases.values()
                    if case.current_session_id == session_id
                ]
                total = len(session_cases)
                paginated = session_cases[offset : offset + limit]
                return paginated, total

            async def list_cases_by_session(
                self, session_id, limit=50, offset=0, filters=None
            ):
                """List cases by session_id - Phase 1: Apply default filtering like list_user_cases"""
                session_cases = [
                    case
                    for case in self.cases.values()
                    if case.current_session_id == session_id
                ]

                # Phase 1: Apply same core filtering as list_user_cases
                if filters:
                    # Phase 1: Default filtering behavior (exclude terminal cases)
                    if not getattr(filters, "include_deleted", False):
                        # Exclude closed cases
                        session_cases = [
                            case
                            for case in session_cases
                            if case.status != CaseStatus.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        session_cases = [
                            case
                            for case in session_cases
                            if case.status
                            not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
                        ]

                    if not getattr(filters, "include_empty", False):
                        # Exclude empty cases (message_count == 0)
                        session_cases = [
                            case
                            for case in session_cases
                            if getattr(case, "message_count", 1) > 0
                        ]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_user_cases)
                    # Only show active (non-terminal) cases by default
                    session_cases = [
                        case
                        for case in session_cases
                        if case.status in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]
                    ]
                    # Exclude empty cases by default
                    session_cases = [
                        case
                        for case in session_cases
                        if getattr(case, "message_count", 1) > 0
                    ]

                return session_cases[offset : offset + limit]

            async def count_cases_by_session(self, session_id, filters=None):
                """Count cases by session_id - Phase 1: Apply default filtering like list_cases_by_session"""
                session_cases = [
                    case
                    for case in self.cases.values()
                    if case.current_session_id == session_id
                ]

                # Phase 1: Apply same core filtering as list_cases_by_session
                if filters:
                    # Phase 1: Default filtering behavior (exclude terminal cases)
                    if not getattr(filters, "include_deleted", False):
                        # Exclude closed cases
                        session_cases = [
                            case
                            for case in session_cases
                            if case.status != CaseStatus.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        session_cases = [
                            case
                            for case in session_cases
                            if case.status
                            not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
                        ]

                    if not getattr(filters, "include_empty", False):
                        # Exclude empty cases (message_count == 0)
                        session_cases = [
                            case
                            for case in session_cases
                            if getattr(case, "message_count", 1) > 0
                        ]
                else:
                    # Phase 1: No filters provided - apply default exclusions (same as list_cases_by_session)
                    # Only show active (non-terminal) cases by default
                    session_cases = [
                        case
                        for case in session_cases
                        if case.status in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]
                    ]
                    # Exclude empty cases by default
                    session_cases = [
                        case
                        for case in session_cases
                        if getattr(case, "message_count", 1) > 0
                    ]

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
                    case.message_count = getattr(case, "message_count", 0) + 1
                    case.updated_at = current_time

                    # Phase 3: Auto-title generation after first message
                    # Only generate auto-title if title is "New Chat" AND not manually set
                    if (
                        case.title == "New Chat"
                        and not getattr(case, "title_manually_set", False)
                        and case.message_count == 1
                    ):  # This is the first query

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
                    "created_at": datetime.now(timezone.utc),
                }

            async def check_idempotency_key(self, idempotency_key: str):
                # Minimal implementation - no actual idempotency checking for testing
                return None

            async def store_idempotency_result(
                self,
                idempotency_key: str,
                status_code: int,
                content: dict,
                headers: dict,
            ):
                # Minimal implementation - no actual storage for testing
                return True

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
                            if case.status != CaseStatus.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        user_cases = [
                            case
                            for case in user_cases
                            if case.status
                            not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
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
                    if hasattr(filters, "status") and filters.status:
                        user_cases = [
                            case for case in user_cases if case.status == filters.status
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
                        if case.status in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]
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

                # Pagination
                paginated_cases = user_cases[offset : offset + limit]

                return paginated_cases

            async def count_user_cases(self, user_id=None, filters=None):
                """Count cases for a user with filters - Phase 1: Mirror filtering from list_user_cases"""
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
                            if case.status != CaseStatus.CLOSED
                        ]

                    if not getattr(filters, "include_terminal", False):
                        # Exclude terminal cases (resolved and closed)
                        user_cases = [
                            case
                            for case in user_cases
                            if case.status
                            not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
                        ]

                    if not getattr(filters, "include_empty", False):
                        # Exclude empty cases (message_count == 0)
                        user_cases = [
                            case
                            for case in user_cases
                            if getattr(case, "message_count", 1) > 0
                        ]

                    # Apply other existing filters
                    if hasattr(filters, "status") and filters.status:
                        user_cases = [
                            case for case in user_cases if case.status == filters.status
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
                        if case.status in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]
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
                self, case_id: str, query: str, user_id: str = None
            ):
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
                    "user_id": user_id or "anonymous",
                }
                self.case_messages[case_id].append(query_msg)

                # Update case metadata
                case = self.cases[case_id]
                case.message_count = len(self.case_messages[case_id])
                case.updated_at = datetime.now(timezone.utc)

                return True

            async def add_assistant_response(
                self,
                case_id: str,
                response_content: str,
                response_type: str = "ANSWER",
                user_id: str = None,
            ):
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
                    "user_id": user_id or "anonymous",
                }
                self.case_messages[case_id].append(response_msg)

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
                context_lines.append(f"Case status: {case.status.value}")
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
                        case.status = CaseStatus(status_value)
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

    def get_enhanced_security_assessment(self):
        """Get the enhanced security assessment service"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Enhanced security assessment requested but container not initialized"
            )
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "enhanced_security_assessment", None)

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

    def get_loop_guard_service(self):
        """Get the loop guard service (legacy - always returns None)"""
        return None

    def get_orchestrator_service(self):
        """Get the orchestrator service (legacy - always returns None)"""
        return None

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

    def get_token_manager(self):
        """Get the token manager for authentication token operations"""
        if not self._initialized:
            logger = logging.getLogger(__name__)
            logger.warning("Token manager requested but container not initialized")
            if not getattr(self, "_initializing", False):
                pass  # Container must be initialized via await container.initialize() at startup
        return getattr(self, "token_manager", None)

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
        (not via ServiceContainer.get() anti-pattern).
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
