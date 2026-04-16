"""Service Factory Registration for Dependency Injection Container.

This module registers all service factories with the DI container to enable
dependency injection and resolve service independence violations.

The factories registered here will be used by services when they need dependencies
injected lazily (e.g., EmbeddingService, VectorStoreService, FileStorageService).

Usage:
    # On application startup
    from faultmaven.bootstrap.service_factories import register_services
    register_services()

    # Services will now use DI container
    from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
    service = KnowledgeSearchService(knowledge_repo)  # Dependencies injected automatically

Design Reference: Phase 3, Week 14-15 - DI Container Implementation
"""

import logging
from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.core.container import ServiceContainer

logger = logging.getLogger(__name__)


def register_services(redis_client=None) -> None:
    """Register all service factories with the DI container.

    This function registers factories for services that need to be injected
    as dependencies in other services. It follows the pattern:
    - Lower-level services (storage, auth, embedding, vector store)
    - Mid-level services (session, evidence, knowledge)
    - Higher-level services (orchestration)

    Args:
        redis_client: Optional Redis client for auth/session services

    Example:
        # At application startup
        register_services()

        # Later, when services need dependencies
        embedding_service = ServiceContainer.get(EmbeddingService)
    """
    logger.info("Registering service factories with DI container")

    settings = get_settings()

    # ============================================================
    # Lower-Level Services (Foundation Layer)
    # ============================================================

    # Import service classes
    from faultmaven.modules.knowledge.domain.services.embedding_service import (
        EmbeddingService,
    )
    from faultmaven.modules.knowledge.domain.services.vector_store_service import (
        VectorStoreService,
    )
    from faultmaven.modules.auth.domain.services.auth_service import AuthService
    from faultmaven.services.file_storage_service import FileStorageService

    # AuthService - Used by UserService
    # Note: AuthService gets JWT settings internally from get_settings(),
    # so we only pass redis_client and optional keys here.
    def create_auth_service():
        logger.debug("Creating AuthService via DI container")
        # Load keys from settings if needed
        private_key = None
        public_key = None
        if settings.security.jwt_private_key:
            private_key = settings.security.jwt_private_key.get_secret_value()
        if settings.security.jwt_public_key:
            public_key = settings.security.jwt_public_key
        return AuthService(
            redis_client=redis_client,
            private_key=private_key,
            public_key=public_key,
        )

    ServiceContainer.register_factory(AuthService, create_auth_service)

    # EmbeddingService - Used by KnowledgeSearchService
    def create_embedding_service():
        logger.debug("Creating EmbeddingService via DI container")
        # Use settings to determine which provider/model
        return EmbeddingService(
            provider=getattr(settings, "embedding_provider", "openai"),
            model=getattr(settings, "embedding_model", "bge-m3"),
            api_key=getattr(settings, "openai_api_key", None),
        )

    ServiceContainer.register_factory(EmbeddingService, create_embedding_service)

    # VectorStoreService - Used by KnowledgeSearchService
    # Receives the shared ChromaDB client from the DI container
    def create_vector_store_service():
        logger.debug("Creating VectorStoreService via DI container")
        from faultmaven.container import container

        kb_chromadb_client = getattr(container, "kb_chromadb_client", None)
        if kb_chromadb_client is None:
            kb_chromadb_client = container.get_service("kb_chromadb_client")

        return VectorStoreService(
            client=kb_chromadb_client,
            collection_name=getattr(
                settings, "vector_store_collection", "faultmaven_kb"
            ),
        )

    ServiceContainer.register_factory(VectorStoreService, create_vector_store_service)

    # FileStorageService - Used by EvidenceArtifactService
    def create_file_storage_service():
        logger.debug("Creating FileStorageService via DI container")
        return FileStorageService(
            storage_root=getattr(settings, "evidence_storage_root", "./data/evidence"),
            max_file_size_bytes=getattr(
                settings, "max_evidence_file_size", 100 * 1024 * 1024
            ),
            allowed_mime_types=getattr(settings, "allowed_evidence_mime_types", []),
        )

    ServiceContainer.register_factory(FileStorageService, create_file_storage_service)

    # ============================================================
    # Mid-Level Services (Domain Layer) - Request-Scoped
    # ============================================================

    # Import mid-level services
    from faultmaven.infrastructure.persistence.repository_factory import (
        STORAGE_TYPE_INMEMORY,
        get_agent_execution_repository,
        get_case_repository,
        get_evidence_artifact_repository,
        get_investigation_session_repository,
    )
    from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
    from faultmaven.modules.case.domain.services.investigation_session_service import (
        APIInvestigationSessionService,
    )

    # APIInvestigationSessionService - Used by AgentOrchestrationService
    # Note: These are request-scoped and require DB session, so we register
    # a factory that expects repositories to be injected by ServiceFactory
    def create_investigation_session_service():
        logger.debug(
            "Creating APIInvestigationSessionService via DI container (in-memory)"
        )
        # For DI container, use in-memory repositories as fallback
        # In production, ServiceFactory should be used with proper DB session
        session_repo = get_investigation_session_repository(
            storage_type=STORAGE_TYPE_INMEMORY
        )
        execution_repo = get_agent_execution_repository(
            storage_type=STORAGE_TYPE_INMEMORY
        )
        case_repo = get_case_repository(storage_type=STORAGE_TYPE_INMEMORY)

        return APIInvestigationSessionService(
            session_repo=session_repo,
            execution_repo=execution_repo,
            case_repo=case_repo,
        )

    ServiceContainer.register_factory(
        APIInvestigationSessionService, create_investigation_session_service
    )

    # APIEvidenceArtifactService - Used by AgentOrchestrationService
    def create_evidence_artifact_service():
        logger.debug("Creating APIEvidenceArtifactService via DI container (in-memory)")
        evidence_repo = get_evidence_artifact_repository(
            storage_type=STORAGE_TYPE_INMEMORY
        )
        case_repo = get_case_repository(storage_type=STORAGE_TYPE_INMEMORY)

        # FileStorageService will be injected via DI
        return APIEvidenceArtifactService(
            evidence_repo=evidence_repo,
            case_repo=case_repo,
            file_storage=None,  # Will be injected via DI
        )

    ServiceContainer.register_factory(
        APIEvidenceArtifactService, create_evidence_artifact_service
    )

    logger.info(f"Registered {len(ServiceContainer._factories)} service factories")
    logger.debug(
        f"Registered services: {list(ServiceContainer.get_registered_services().keys())}"
    )


def clear_service_instances() -> None:
    """Clear all service instances (useful for testing).

    This clears cached instances but keeps factory registrations,
    forcing fresh instances to be created on next get().
    """
    ServiceContainer.clear()
    logger.debug("Cleared all service instances from DI container")


def clear_all_services() -> None:
    """Complete reset of DI container (instances + factories).

    Use this when completely reconfiguring the application or
    in test setup/teardown.
    """
    ServiceContainer.clear_all()
    logger.debug("Cleared all services and factories from DI container")
