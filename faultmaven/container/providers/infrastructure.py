"""Infrastructure service providers.

This module contains factory functions for core infrastructure services:
- Security (sanitizer)
- Observability (tracer)
- LLM (provider)
- Storage (vector store, session store, repositories)
- Preprocessing pipeline
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultmaven.config.settings import FaultMavenSettings
    from faultmaven.container.base import BaseDIContainer

logger = logging.getLogger(__name__)


def create_sanitizer(settings: FaultMavenSettings) -> Any:
    """Create data sanitizer for PII protection."""
    from faultmaven.infrastructure.security.redaction import DataSanitizer

    logger.debug(f"Protection config: enabled={settings.protection.protection_enabled}")
    return DataSanitizer(settings=settings)


def create_tracer(settings: FaultMavenSettings) -> Any:
    """Create distributed tracer."""
    from faultmaven.infrastructure.observability.tracing import OpikTracer

    logger.debug(
        f"Observability config: enabled={settings.observability.tracing_enabled}"
    )
    return OpikTracer(settings=settings)


def create_llm_provider() -> Any:
    """Create LLM provider/router."""
    from faultmaven.infrastructure.llm.router import LLMRouter

    return LLMRouter()


def create_da_provider() -> tuple[Any | None, str | None]:
    """Select a dedicated provider for DA (directed analysis) turns.

    Only activates when DA_PROVIDER is explicitly set in .env.
    When not set, returns (None, None) — the DA tool loop uses the default
    LLM router, which inherits the user's configured model (e.g. GEMINI_MODEL).

    Returns:
        Tuple of (provider_instance, model_name) or (None, None).
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.llm.providers import get_registry

    settings = get_settings()

    # Only activate when DA_PROVIDER is explicitly set
    if settings.llm.da_provider is None:
        logger.info("DA_PROVIDER not set; DA turns will use default LLM router")
        return None, None

    provider_enum = settings.llm.get_da_provider()
    model = settings.llm.get_da_model()
    provider_name = provider_enum.value

    registry = get_registry()
    provider = registry.get_provider(provider_name)

    if provider and provider.is_available():
        logger.info(
            f"DA provider for directed analysis turns: {provider_name} (model: {model})"
        )
        return provider, model

    logger.warning(
        f"DA provider '{provider_name}' not available; "
        "DA turns will use default router"
    )
    return None, None


def create_log_processor() -> Any:
    """Create legacy log processor."""
    from faultmaven.core.processing.log_analyzer import LogProcessor

    return LogProcessor()


def create_data_classifier() -> Any:
    """Create data classifier for preprocessing."""
    from faultmaven.services.preprocessing.classifier import DataClassifier

    return DataClassifier()


def create_extractors() -> dict[str, Any]:
    """Create all data extractors.

    Returns:
        Dict mapping extractor names to instances
    """
    from faultmaven.services.preprocessing.extractors import (
        CommandOutputExtractor,
        DocumentationExtractor,
        ErrorReportExtractor,
        LogsAndErrorsExtractor,
        MetricsAndPerformanceExtractor,
        ProfilingDataExtractor,
        SourceCodeExtractor,
        StructuredConfigExtractor,
        TraceDataExtractor,
        UnstructuredTextExtractor,
        VisualEvidenceExtractor,
    )

    return {
        "logs_extractor": LogsAndErrorsExtractor(),
        "config_extractor": StructuredConfigExtractor(),
        "metrics_extractor": MetricsAndPerformanceExtractor(),
        "text_extractor": UnstructuredTextExtractor(),
        "source_code_extractor": SourceCodeExtractor(),
        "visual_extractor": VisualEvidenceExtractor(),
        "trace_extractor": TraceDataExtractor(),
        "profiling_extractor": ProfilingDataExtractor(),
        "error_report_extractor": ErrorReportExtractor(),
        "documentation_extractor": DocumentationExtractor(),
        "command_output_extractor": CommandOutputExtractor(),
    }


def create_chunking_service(
    llm_provider: Any,
    settings: FaultMavenSettings,
) -> Any:
    """Create chunking service for large documents."""
    from faultmaven.services.preprocessing.chunking_service import ChunkingService

    return ChunkingService(
        llm_router=llm_provider,
        chunk_size_tokens=settings.preprocessing.chunk_size_tokens,
        overlap_tokens=settings.preprocessing.chunk_overlap_tokens,
        max_parallel_chunks=settings.preprocessing.map_reduce_max_parallel,
    )


def create_preprocessing_service(
    data_classifier: Any,
    data_sanitizer: Any,
    extractors: dict[str, Any],
    chunking_service: Any,
    settings: FaultMavenSettings,
) -> Any:
    """Create preprocessing service with all extractors."""
    from faultmaven.services.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    return PreprocessingService(
        classifier=data_classifier,
        sanitizer=data_sanitizer,
        logs_extractor=extractors["logs_extractor"],
        config_extractor=extractors["config_extractor"],
        metrics_extractor=extractors["metrics_extractor"],
        text_extractor=extractors["text_extractor"],
        source_code_extractor=extractors["source_code_extractor"],
        visual_extractor=extractors["visual_extractor"],
        trace_extractor=extractors["trace_extractor"],
        profiling_extractor=extractors["profiling_extractor"],
        error_report_extractor=extractors["error_report_extractor"],
        documentation_extractor=extractors["documentation_extractor"],
        command_output_extractor=extractors["command_output_extractor"],
        chunking_service=chunking_service,
        chunk_trigger_tokens=settings.preprocessing.chunk_trigger_tokens,
    )


def create_chromadb_client(settings: FaultMavenSettings):
    """Create a shared ChromaDB client for all vector stores.

    Cloud deployment: HttpClient to external ChromaDB server.
    Local deployment: PersistentClient at data/chroma/ (in-process, file-based).

    Never returns None — PersistentClient is always available (chromadb is a base dependency).
    Same pattern as Redis: one client, deployment-time selection, injected everywhere.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    vector_storage_type = (settings.database.vector_storage_type or "").lower()
    is_external_chroma = vector_storage_type in {
        "chromadb",
        "chroma",
        "chroma_db",
        "chroma-db",
    }

    if is_external_chroma:
        # Cloud: external ChromaDB server via HTTP
        from urllib.parse import urlparse

        chromadb_url = settings.database.chromadb_url
        chromadb_token = (
            settings.database.chromadb_api_key.get_secret_value()
            if settings.database.chromadb_api_key
            else None
        )

        parsed = urlparse(chromadb_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        settings_kwargs = {}
        if chromadb_token:
            try:
                from importlib import import_module

                import_module("chromadb.auth.token")
                settings_kwargs.update(
                    {
                        "chroma_client_auth_provider": "chromadb.auth.token.TokenAuthClientProvider",
                        "chroma_client_auth_credentials": chromadb_token,
                    }
                )
            except Exception:
                pass

        try:
            client = chromadb.HttpClient(
                host=host,
                port=port,
                settings=(
                    ChromaSettings(**settings_kwargs)
                    if settings_kwargs
                    else ChromaSettings()
                ),
            )
            logger.info(f"✅ ChromaDB client: HttpClient @ {host}:{port}")
            return client
        except Exception as e:
            logger.warning(
                f"ChromaDB server unavailable ({type(e).__name__}: {e}), "
                f"falling back to persistent local ChromaDB"
            )

    # Local: in-process persistent ChromaDB (always available)
    import os

    persist_dir = getattr(settings.database, "chromadb_persist_dir", "./data/chroma")
    os.makedirs(persist_dir, exist_ok=True)
    client = chromadb.PersistentClient(path=persist_dir)
    logger.info(f"✅ ChromaDB client: PersistentClient @ {persist_dir}")
    return client


def create_vector_store(
    settings: FaultMavenSettings, chromadb_client=None
) -> tuple[Any, bool]:
    """Create global KB vector store.

    Args:
        settings: Application settings
        chromadb_client: Shared ChromaDB client (from create_chromadb_client)

    Returns:
        Tuple of (vector_store, is_disabled)
    """
    if settings.server.skip_service_checks:
        logger.info("Skipping vector store (SKIP_SERVICE_CHECKS=True)")
        return None, True

    from faultmaven.infrastructure.persistence.chromadb_store import (
        ChromaDBVectorStore,
    )

    collection_name = getattr(settings.database, "chromadb_collection", "faultmaven_kb")
    store = ChromaDBVectorStore(client=chromadb_client, collection_name=collection_name)
    logger.info(f"✅ Vector store: ChromaDB (collection: {collection_name})")
    return store, False


def create_case_vector_store(
    settings: FaultMavenSettings, chromadb_client=None
) -> Any | None:
    """Create case vector store for session-specific RAG.

    Args:
        settings: Application settings
        chromadb_client: Shared ChromaDB client (from create_chromadb_client)
    """
    if settings.server.skip_service_checks:
        logger.info("Skipping case vector store (SKIP_SERVICE_CHECKS=True)")
        return None

    from faultmaven.infrastructure.persistence.case_vector_store import (
        CaseVectorStore,
    )

    store = CaseVectorStore(client=chromadb_client)
    logger.info("✅ Case vector store: ChromaDB (dynamic per-case collections)")
    return store


async def create_redis_client(settings: FaultMavenSettings) -> Any:
    """Create Redis client for session storage.

    Always returns a working async Redis-compatible client.
    Cloud deployment: real Redis. Local deployment: FakeRedis (in-process).
    """
    from faultmaven.infrastructure.redis_client import get_async_redis_client

    if settings.server.skip_service_checks:
        # Even with skip_service_checks, return FakeRedis so all subsystems work
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        logger.info("✅ Redis client: FakeRedis (SKIP_SERVICE_CHECKS=True)")
        return get_fakeredis_client()

    return await get_async_redis_client(
        redis_url=settings.database.redis_url,
        host=settings.database.redis_host,
        port=settings.database.redis_port,
    )


def create_session_store(redis_client: Any, settings: FaultMavenSettings) -> Any:
    """Create session store backed by Redis (real or FakeRedis)."""
    from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
        RedisSessionStore,
    )

    store = RedisSessionStore(redis_client)
    logger.info("✅ Session store: Redis")
    return store


def create_token_manager(
    redis_client: Any,
    settings: FaultMavenSettings,
    user_store: Any | None = None,
) -> Any:
    """Create token manager backed by Redis (real or FakeRedis).

    Args:
        redis_client: Async Redis-compatible client (always provided)
        settings: Application settings
        user_store: Unused (kept for backward compatibility)

    Returns:
        RedisTokenManager instance
    """
    from faultmaven.infrastructure.auth.token_manager import RedisTokenManager

    manager = RedisTokenManager(redis_client=redis_client)
    logger.info("✅ Token manager: Redis")
    return manager


def create_case_repository(settings: FaultMavenSettings) -> Any | None:
    """Create case repository based on deployment configuration.

    Follows Architectural Design Principles:
    - Principle 1 (Deployment Agnostic): Infrastructure choice is deployment-time decision
    - Principle 5 (Composition Root): Repository is stateless, uses get_db_session()

    Provider Selection (Deployment-Agnostic):
    - Local Deployment (Self-Host): SessionlessCaseRepository → SQLite
    - Cloud Deployment (Enterprise): SessionlessCaseRepository → PostgreSQL
    - Test/Ephemeral: InMemoryCaseRepository (no persistence)

    Configuration:
    - DATABASE_URL set → SessionlessCaseRepository (persistent database)
    - DATABASE_URL=:memory: or unset → InMemoryCaseRepository (ephemeral)

    Returns None if initialization fails.
    """
    try:
        database_url = settings.database.database_url or ""

        # Check if database is configured
        if not database_url or database_url == ":memory:":
            # Ephemeral storage (testing, no database available)
            from faultmaven.modules.case.infrastructure.case_repository import (
                InMemoryCaseRepository,
            )

            repository = InMemoryCaseRepository()
            logger.info(
                "✅ Case repository initialized (InMemory - ephemeral, no persistence)"
            )
            return repository

        # Persistent database storage (Local: SQLite, Cloud: PostgreSQL)
        from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
            SessionlessCaseRepository,
        )

        # This repository is stateless and creates sessions per operation
        # Following Principle 5: No shared session state
        repository = SessionlessCaseRepository()
        db_type = "SQLite" if "sqlite" in database_url.lower() else "PostgreSQL"
        deployment_type = "Local" if "sqlite" in database_url.lower() else "Cloud"
        logger.info(
            f"✅ Case repository initialized ({deployment_type}: {db_type}, sessionless)"
        )
        return repository

    except Exception as e:
        logger.error(f"❌ Case repository initialization failed: {e}", exc_info=True)
        return None


def create_user_store(redis_client: Any, settings: FaultMavenSettings) -> Any:
    """Create user store (Database or Redis) based on configuration.

    Provider selection:
    1. Database (SQLite/PostgreSQL) - if database is available (persistent)
    2. Redis (real or FakeRedis) - fallback when no database configured

    Args:
        redis_client: Async Redis-compatible client (always provided)
        settings: Application settings

    Returns:
        User store instance (DatabaseUserStore or RedisUserStore)
    """
    # Priority 1: Check if database is available (SQLite for local, PostgreSQL for cloud)
    database_url = settings.database.database_url or ""
    if database_url and (
        "sqlite" in database_url.lower() or "postgresql" in database_url.lower()
    ):
        try:
            from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
            from sqlalchemy.orm import sessionmaker

            from faultmaven.infrastructure.auth.database_user_store import (
                DatabaseUserStore,
            )
            from faultmaven.infrastructure.persistence.user_repository import (
                PostgreSQLUserRepository,
            )

            # Create database engine and session
            # Use NullPool for SQLite to avoid connection issues
            pool_class = None
            if "sqlite" in database_url.lower():
                from sqlalchemy.pool import NullPool

                pool_class = NullPool

            engine = create_async_engine(
                database_url,
                echo=False,
                poolclass=pool_class,
            )
            session_factory = sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            db_session = session_factory()

            # Create UserRepository (works with both SQLite and PostgreSQL)
            user_repository = PostgreSQLUserRepository(db_session)

            # Create DatabaseUserStore wrapper
            store = DatabaseUserStore(user_repository)
            db_type = "SQLite" if "sqlite" in database_url.lower() else "PostgreSQL"
            logger.info(
                f"✅ User store: Database ({db_type}) - persistent across restarts"
            )
            return store
        except Exception as e:
            logger.warning(
                f"Failed to create database user store: {e}, falling back to in-memory"
            )
            # Fall through to in-memory

    # Priority 2: Fall back to Redis-backed user store
    # redis_client is always available (real or FakeRedis)
    from faultmaven.infrastructure.auth.user_store import RedisUserStore

    store = RedisUserStore(redis_client=redis_client)
    logger.info("✅ User store: Redis")
    return store


async def register_infrastructure(container: BaseDIContainer) -> None:
    """Register all infrastructure services with the container.

    Args:
        container: The DI container to register services with
    """
    settings = container.settings

    # Log configuration
    logger.info("🔍 Infrastructure: Registering services...")
    logger.info(f"🔍 CHAT_PROVIDER = {settings.llm.provider}")
    logger.info(f"🔍 SKIP_SERVICE_CHECKS = {settings.server.skip_service_checks}")

    # Core security
    sanitizer = create_sanitizer(settings)
    container._register_service("sanitizer", sanitizer)

    # Observability
    tracer = create_tracer(settings)
    container._register_service("tracer", tracer)

    # LLM
    llm_provider = create_llm_provider()
    container._register_service("llm_provider", llm_provider)

    # Processing
    log_processor = create_log_processor()
    container._register_service("log_processor", log_processor)

    # Data classification
    data_classifier = create_data_classifier()
    container._register_service("data_classifier", data_classifier)

    # Create a separate sanitizer for preprocessing (stateless)
    from faultmaven.infrastructure.security.redaction import DataSanitizer

    data_sanitizer = DataSanitizer()
    container._register_service("data_sanitizer", data_sanitizer)

    # Extractors
    extractors = create_extractors()
    for name, extractor in extractors.items():
        setattr(container, name, extractor)

    # Chunking service
    chunking_service = create_chunking_service(llm_provider, settings)
    container._register_service("chunking_service", chunking_service)

    # Preprocessing service
    preprocessing_service = create_preprocessing_service(
        data_classifier, data_sanitizer, extractors, chunking_service, settings
    )
    container._register_service(
        "preprocessing_service",
        preprocessing_service,
        dependencies=["data_classifier", "chunking_service"],
    )

    # Deep analysis service (Tier 3 deep LLM analysis)
    from faultmaven.core.preprocessing.tier2.factory import create_tier2_service

    storage_service = getattr(container, "file_storage_service", None)
    deep_analysis_service = create_tier2_service(
        backend=settings.deep_analysis.backend,
        base_url=settings.deep_analysis.url or None,
        api_key=settings.deep_analysis.api_key or None,
        llm_client=llm_provider if settings.deep_analysis.backend == "local" else None,
        storage_service=storage_service,
        timeout_seconds=settings.deep_analysis.timeout_seconds,
    )
    if deep_analysis_service:
        container._register_service("deep_analysis_service", deep_analysis_service)
    else:
        container._register_disabled(
            "deep_analysis_service", "DEEP_ANALYSIS_BACKEND=disabled"
        )

    # ChromaDB client (shared by all vector stores)
    if not settings.server.skip_service_checks:
        chromadb_client = create_chromadb_client(settings)
        container._register_service("chromadb_client", chromadb_client)
        container.chromadb_client = chromadb_client
    else:
        chromadb_client = None
        container.chromadb_client = None

    # Vector store (global KB)
    try:
        vector_store, is_disabled = create_vector_store(settings, chromadb_client)
        if is_disabled:
            container._register_disabled("vector_store", "SKIP_SERVICE_CHECKS=True")
        else:
            container._register_service("vector_store", vector_store)
    except Exception as e:
        logger.warning(f"Vector store initialization failed: {e}")
        container._register_failed("vector_store", str(e))

    # Case vector store (dynamic per-case collections)
    case_vector_store = create_case_vector_store(settings, chromadb_client)
    if case_vector_store:
        container._register_service("case_vector_store", case_vector_store)
        container.case_vector_store = case_vector_store
    else:
        container.case_vector_store = None

    # Redis client (always available: real Redis or FakeRedis)
    redis_client = await create_redis_client(settings)
    container._register_service("redis_client", redis_client)
    container.redis_client = redis_client

    # Session store
    session_store = create_session_store(redis_client, settings)
    container._register_service("session_store", session_store)

    # User store (provider pattern: Database → Redis)
    # Create user_store first so it can be injected into token_manager
    try:
        user_store = create_user_store(redis_client, settings)
        if user_store is None:
            logger.error(
                "❌ Failed to create user store: create_user_store returned None"
            )
            raise ValueError("User store creation returned None")
        container.user_store = user_store
        container._register_service("user_store", user_store)
        logger.info(f"✅ User store registered: {type(user_store).__name__}")
        # Verify it's accessible
        if not hasattr(container, "user_store") or container.user_store is None:
            logger.error(
                "❌ User store not properly set on container after registration"
            )
            raise ValueError("User store registration failed - attribute not set")
    except Exception as e:
        logger.error(f"❌ Failed to create user store: {e}", exc_info=True)
        raise

    # Token manager (Redis-backed, real or FakeRedis)
    try:
        token_manager = create_token_manager(
            redis_client, settings, user_store=user_store
        )
        if token_manager is None:
            logger.error(
                "❌ Failed to create token manager: create_token_manager returned None"
            )
            raise ValueError("Token manager creation returned None")
        container.token_manager = token_manager
        container._register_service("token_manager", token_manager)
        logger.debug(f"✅ Token manager registered: {type(token_manager).__name__}")
    except Exception as e:
        logger.error(f"❌ Failed to create token manager: {e}", exc_info=True)
        raise

    # Case repository (database persistence for cases)
    try:
        case_repository = create_case_repository(settings)
        if case_repository:
            container.case_repository = case_repository
            container._register_service("case_repository", case_repository)
            logger.info(
                f"✅ Case repository registered: {type(case_repository).__name__}"
            )
        else:
            container.case_repository = None
            logger.warning("⚠️ Case repository not available (database not configured)")
    except Exception as e:
        logger.error(f"❌ Failed to create case repository: {e}", exc_info=True)
        container.case_repository = None
        # Don't fail startup - investigation service will be unavailable

    logger.info("✅ Infrastructure layer registered")
