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


def create_llm_provider(settings: FaultMavenSettings | None = None) -> Any:
    """Create LLM provider/router.

    Defaults to the core LLMRouter. Override via LLM_ROUTER_CLASS env var
    pointing to a dotted path for deployments that need a custom router
    (e.g. tenant-aware routing — a core concern when built, ADR-010 D4).

    The replacement class must satisfy ``ILLMProvider`` and accept zero
    constructor arguments (or have all-optional kwargs).
    """
    if settings is None:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

    router_class_path = settings.llm.router_class
    if router_class_path:
        import importlib

        module_path, class_name = router_class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        router_class = getattr(module, class_name)
        logger.info(f"Using custom LLM router: {router_class_path}")
        return router_class()

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
    from faultmaven.modules.preprocessing.classifier import DataClassifier

    return DataClassifier()


def create_extractors() -> dict[str, Any]:
    """Create all data extractors.

    Returns:
        Dict mapping extractor names to instances
    """
    from faultmaven.modules.preprocessing.extractors import (
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


def create_preprocessing_service(
    data_classifier: Any,
    extractors: dict[str, Any],
    settings: FaultMavenSettings,
) -> Any:
    """Create preprocessing service with all extractors."""
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    return PreprocessingService(
        classifier=data_classifier,
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
    )


def _create_chromadb_client(settings: FaultMavenSettings, persist_dir: str, label: str):
    """Create a ChromaDB client for a specific persist directory.

    Cloud deployment: HttpClient to external ChromaDB server.
    Local deployment: PersistentClient at the given persist_dir.

    For cloud (HttpClient), both KB and evidence clients connect to the same
    server — collection names provide isolation. For local (PersistentClient),
    separate directories provide physical isolation for different lifecycles.

    Args:
        settings: Application settings
        persist_dir: Local persist directory (used for PersistentClient fallback)
        label: Human-readable label for logging (e.g., "KB", "evidence")
    """
    from faultmaven.infrastructure.chroma_client import (
        chroma_token_auth_kwargs,
        is_external_chroma_configured,
        local_chroma_or_fail,
    )

    # The skip branch goes THROUGH the gate rather than around it, mirroring
    # create_redis_client: under cloud, one env var must not buy a pod its way
    # out of the vector-store guarantee — a cloud process with no vector store
    # is the degradation the gate exists to refuse. Standalone keeps the skip
    # (returns None; downstream stores register as disabled). Checked BEFORE
    # the chromadb import below so skip-mode boots (CI) keep not paying it.
    if settings.server.skip_service_checks:
        local_chroma_or_fail(
            "SKIP_SERVICE_CHECKS=true skips ChromaDB entirely", settings
        )
        logger.info(f"Skipping ChromaDB {label} client (SKIP_SERVICE_CHECKS=True)")
        return None

    import chromadb
    from chromadb.config import Settings as ChromaSettings

    # Dispatch: canonical value is "chromadb" (default). If the caller
    # configures CHROMADB_URL, we probe it via HttpClient; on failure,
    # standalone falls back to a local PersistentClient and cloud refuses
    # (ChromaUnavailableError — see local_chroma_or_fail for why the fallback
    # is corruption, not degradation, under cloud). If CHROMADB_URL is unset
    # (default empty) we skip the probe entirely: straight to PersistentClient
    # on standalone, refusal on cloud. Any legacy value (including "inmemory")
    # is accepted as a synonym for "local PersistentClient" on standalone.
    # `InMemoryVectorStore` no longer exists — chromadb is a base dependency
    # and PersistentClient is always available, same principle as FakeRedis.
    fallback_reason = (
        "no external ChromaDB is configured (CHROMADB_URL unset or "
        "VECTOR_STORAGE_TYPE is not chromadb)"
    )
    if is_external_chroma_configured(settings):
        # Cloud: external ChromaDB server via HTTP
        from urllib.parse import urlparse

        parsed = urlparse(settings.database.chromadb_url.strip())
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Token auth via the shared helper — no probe, no swallow. The old
        # inline block imported the pre-0.5 module path inside a bare
        # ``except Exception: pass``, so at every supported chromadb version
        # it silently built an unauthenticated client (#1173).
        settings_kwargs = chroma_token_auth_kwargs(settings)

        try:
            client = chromadb.HttpClient(
                host=host,
                port=port,
                settings=ChromaSettings(**settings_kwargs),
            )
            logger.info(f"✅ ChromaDB {label} client: HttpClient @ {host}:{port}")
            return client
        except Exception as e:
            # Don't log "falling back" here — under cloud the gate below
            # refuses instead, and a fallback announcement followed by a
            # refusal reads as a contradiction in the Job logs.
            fallback_reason = f"{type(e).__name__}: {e}"
            logger.warning(f"ChromaDB server unavailable ({fallback_reason})")

    # Local: in-process persistent ChromaDB. Standalone only — under cloud
    # this raises ChromaUnavailableError instead of silently forking the
    # vector store into this container's filesystem (#901).
    local_chroma_or_fail(fallback_reason, settings)
    from faultmaven.infrastructure.persistence.chromadb_store import (
        create_persistent_client,
    )

    client = create_persistent_client(persist_dir)
    logger.info(f"✅ ChromaDB {label} client: PersistentClient @ {persist_dir}")
    return client


def create_kb_chromadb_client(settings: FaultMavenSettings):
    """Create ChromaDB client for the permanent KB collection.

    Stores: faultmaven_kb — one collection, holding KB documents and runbooks
    alike. Runbooks have no collection of their own: ``RunbookKnowledgeBase`` is
    injected this store and its ``COLLECTION_NAME = "faultmaven_runbooks"`` is
    decorative, so ``report_type == "runbook"`` is the only discriminator (#912).
    Lifecycle: permanent — backed up, never wiped.
    """
    persist_dir = getattr(
        settings.database, "chromadb_kb_persist_dir", "./data/chroma-kb"
    )
    return _create_chromadb_client(settings, persist_dir, "KB")


def create_evidence_chromadb_client(settings: FaultMavenSettings):
    """Create ChromaDB client for ephemeral case evidence collections.

    Stores: case_{case_id} collections (dynamic, one per active case).
    Lifecycle: ephemeral — deleted on case closure, excluded from backups.
    """
    persist_dir = getattr(
        settings.database, "chromadb_evidence_persist_dir", "./data/chroma-evidence"
    )
    return _create_chromadb_client(settings, persist_dir, "evidence")


def create_vector_store(
    settings: FaultMavenSettings, chromadb_client=None
) -> tuple[Any, bool]:
    """Create global KB vector store.

    Args:
        settings: Application settings
        chromadb_client: KB ChromaDB client (from create_kb_chromadb_client)

    Returns:
        Tuple of (vector_store, is_disabled)
    """
    if settings.server.skip_service_checks:
        logger.info("Skipping vector store (SKIP_SERVICE_CHECKS=True)")
        return None, True

    from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore

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
        chromadb_client: Evidence ChromaDB client (from create_evidence_chromadb_client)
    """
    if settings.server.skip_service_checks:
        logger.info("Skipping case vector store (SKIP_SERVICE_CHECKS=True)")
        return None

    from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore

    store = CaseVectorStore(client=chromadb_client)
    logger.info("✅ Case vector store: ChromaDB (dynamic per-case collections)")
    return store


def create_knowledge_vector_store(
    settings: FaultMavenSettings, chromadb_client=None
) -> Any | None:
    """Create knowledge vector store for permanent KB collections.

    Uses collection names as-is from KBConfig (no prefix manipulation).
    This ensures ingestion and retrieval use the same collection names.

    Args:
        settings: Application settings
        chromadb_client: KB ChromaDB client (from create_kb_chromadb_client)
    """
    if settings.server.skip_service_checks:
        logger.info("Skipping knowledge vector store (SKIP_SERVICE_CHECKS=True)")
        return None

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )

    store = KnowledgeVectorStore(client=chromadb_client)
    logger.info("✅ Knowledge vector store: ChromaDB (permanent KB collections)")
    return store


async def create_redis_client(settings: FaultMavenSettings) -> Any:
    """Create Redis client for session storage.

    Connection parameters are resolved from settings in ONE place — the factory —
    so nothing is relayed through here: a second path carrying the same values is
    a second place one of them can be dropped. Cloud deployment: real Redis, or
    a refusal to boot. Local deployment: FakeRedis (in-process).
    """
    if settings.server.skip_service_checks:
        # Skipping the service checks still needs a working client, so all
        # subsystems keep functioning — but it goes through the same gate rather
        # than around it. Under cloud, substituting an in-process FakeRedis is
        # the exact per-replica degradation the gate exists to refuse, and one
        # env var must not buy a cloud pod its way out of it.
        from faultmaven.infrastructure.redis_client import fakeredis_or_fail

        client = fakeredis_or_fail("SKIP_SERVICE_CHECKS=true skips Redis entirely")
        logger.info("✅ Redis client: FakeRedis (SKIP_SERVICE_CHECKS=True)")
        return client

    from faultmaven.infrastructure.redis_client import get_async_redis_client

    return await get_async_redis_client()


def create_session_store(redis_client: Any, settings: FaultMavenSettings) -> Any:
    """Create session store backed by Redis (real or FakeRedis)."""
    from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
        RedisSessionStore,
    )

    store = RedisSessionStore(redis_client)
    logger.info("✅ Session store: Redis")
    return store


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
        from faultmaven.config.settings import persistent_database_configured

        database_url = settings.database.database_url or ""

        # Persistence decided by the shared predicate (fm#1128) — this was the
        # third inline copy of the DATABASE_URL rule.
        if not persistent_database_configured(database_url):
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
        url_lower = database_url.lower()
        if "sqlite" in url_lower:
            db_type, deployment_type = "SQLite", "Local"
        elif "postgresql" in url_lower:
            db_type, deployment_type = "PostgreSQL", "Cloud"
        else:
            # Unsupported dialects still count as configured (see the
            # predicate's docstring). Label with the DSN's own scheme instead
            # of guessing "Cloud: PostgreSQL" — this log is what an operator
            # reads while diagnosing the fail-loudly-at-first-use path, and it
            # must not lie about the backend (same three-way labeling as
            # create_user_store below).
            db_type = database_url.split(":", 1)[0] or "unknown"
            deployment_type = "Unrecognized dialect"
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
    from faultmaven.config.settings import persistent_database_configured

    # Persistence is decided by the ONE shared predicate (fm#1128), not a
    # local reading of DATABASE_URL. This factory used to require a
    # sqlite/postgresql substring while create_user_service accepted any
    # non-empty URL — under a DSN only one of them recognized, login wrote
    # accounts to this store while UserService (the /auth/me read path)
    # queried an always-empty other.
    if persistent_database_configured(settings.database.database_url):
        database_url = settings.database.database_url or ""
        try:
            from faultmaven.infrastructure.auth.database_user_store import (
                DatabaseUserStore,
            )
            from faultmaven.infrastructure.persistence.user_repository import (
                SessionlessUserRepository,
            )

            # Sessionless repository (Principle 5): opens a fresh session per
            # operation via get_db_session(), which commits/rolls-back/closes
            # each time. This is the #703 fix — the previous wiring handed a
            # single process-lifetime AsyncSession to this singleton store,
            # and read methods never committed, so PostgreSQL left the backing
            # connection idle-in-transaction indefinitely (held ACCESS SHARE on
            # users, blocked migration 025's DDL, froze auth). Works with both
            # SQLite (local) and PostgreSQL (cloud).
            user_repository = SessionlessUserRepository()

            # Create DatabaseUserStore wrapper. The store holds no session —
            # each repository call is self-contained, so there is nothing to
            # release at shutdown.
            store = DatabaseUserStore(user_repository)
            if "sqlite" in database_url.lower():
                db_type = "SQLite"
            elif "postgresql" in database_url.lower():
                db_type = "PostgreSQL"
            else:
                # Unsupported dialects still count as configured (see the
                # predicate's docstring): both this store and UserService then
                # point at the same database and fail loudly together, rather
                # than this side quietly splitting off to Redis.
                db_type = database_url.split(":", 1)[0] or "unknown"
            logger.info(
                f"✅ User store: Database ({db_type}) - persistent across restarts"
            )
            return store
        except Exception as e:
            # ERROR, not a shrug: UserService selects its repository with the
            # same predicate and does NOT fall back, so from here on logins
            # write to Redis while /auth/me reads database rows — a split that
            # is invisible at request time (#1128).
            logger.error(
                "Failed to create database user store (%s); falling back to "
                "Redis while UserService still reads the database — user rows "
                "written from now on will not be visible to /auth/me",
                e,
                exc_info=True,
            )
            # Fall through to Redis

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
    llm_provider = create_llm_provider(settings)
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

    # Preprocessing service
    preprocessing_service = create_preprocessing_service(
        data_classifier, extractors, settings
    )
    container._register_service(
        "preprocessing_service",
        preprocessing_service,
        dependencies=["data_classifier"],
    )

    # File storage service — must be registered BEFORE the Tier 2 deep
    # analysis service below, because create_tier2_service reads it via
    # getattr(container, "file_storage_service", None). The canonical
    # registration name is "file_storage_service"; downstream consumers in
    # infrastructure.py (here), tools.py (via get_service), and services.py
    # (via container.file_storage_service) must all agree on this name.
    # Moved here from services.py in 2026-04 to repair an init-order bug
    # where register_services ran after register_infrastructure, so Tier 2
    # was always built with storage_service=None. See also
    # tests/unit/container/test_file_storage_service_wiring.py.
    try:
        from faultmaven.modules.evidence.domain.services.file_storage_service import (
            FileStorageService,
        )

        # No storage root here: the backend (filesystem or S3, per
        # STORAGE_BACKEND) is resolved by get_storage_backend().
        file_storage_service = FileStorageService(
            max_file_size_bytes=settings.max_evidence_file_size,
        )
        container._register_service("file_storage_service", file_storage_service)
        logger.info(
            f"✅ File storage service registered: {type(file_storage_service).__name__}"
        )
    except Exception as e:
        # Fail-soft with loud logging so operators see misconfiguration at
        # startup. Non-storage code paths (auth, KB-only queries) still work;
        # deep_analysis and search_file will surface "unavailable" to the
        # agent rather than crashing mid-turn.
        logger.error(f"❌ Failed to create file storage service: {e}", exc_info=True)
        container._register_failed("file_storage_service", str(e))

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
        max_tokens=settings.deep_analysis.max_tokens,
    )
    if deep_analysis_service:
        container._register_service("deep_analysis_service", deep_analysis_service)
    else:
        container._register_disabled(
            "deep_analysis_service", "DEEP_ANALYSIS_BACKEND=disabled"
        )

    # ChromaDB clients — split by lifecycle:
    #   KB client: one permanent collection (faultmaven_kb — documents AND runbooks)
    #   Evidence client: ephemeral per-case collections (case_{case_id})
    # The factories own the SKIP_SERVICE_CHECKS branch (returning None on
    # standalone, refusing under cloud) so the skip path cannot bypass the
    # deployment gate — see _create_chromadb_client.
    kb_chromadb_client = create_kb_chromadb_client(settings)
    evidence_chromadb_client = create_evidence_chromadb_client(settings)
    container.kb_chromadb_client = kb_chromadb_client
    container.evidence_chromadb_client = evidence_chromadb_client
    if kb_chromadb_client is not None:
        container._register_service("kb_chromadb_client", kb_chromadb_client)
    if evidence_chromadb_client is not None:
        container._register_service(
            "evidence_chromadb_client", evidence_chromadb_client
        )

    # Vector store (global KB) — uses KB client
    try:
        vector_store, is_disabled = create_vector_store(settings, kb_chromadb_client)
        if is_disabled:
            container._register_disabled("vector_store", "SKIP_SERVICE_CHECKS=True")
        else:
            container._register_service("vector_store", vector_store)
    except Exception as e:
        logger.warning(f"Vector store initialization failed: {e}")
        container._register_failed("vector_store", str(e))

    # Knowledge vector store — scope-enforcing wrapper around faultmaven_kb collection
    knowledge_vector_store = create_knowledge_vector_store(settings, kb_chromadb_client)
    if knowledge_vector_store:
        container._register_service("knowledge_vector_store", knowledge_vector_store)
        container.knowledge_vector_store = knowledge_vector_store
    else:
        container.knowledge_vector_store = None

    # Case vector store (dynamic per-case collections) — uses evidence client
    case_vector_store = create_case_vector_store(settings, evidence_chromadb_client)
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
