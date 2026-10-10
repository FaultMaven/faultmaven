"""Application lifespan: startup fail-fast gates, background tasks, shutdown.

Moved out of ``faultmaven/main.py`` (fm#1707 wave 2), alongside the
composition root it calls (``bootstrap/composition.py``) and the middleware
setup ``main.py`` still runs before ``uvicorn`` ever reaches this context
manager (``bootstrap/middleware.py``). ``faultmaven.main`` imports
``lifespan`` from here to build its ``FastAPI(lifespan=lifespan, ...)``.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from faultmaven.bootstrap.composition import _is_test_environment, compose_application
from faultmaven.infrastructure.observability.tracing import init_opik_tracing

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    logger.info("Starting FaultMaven API server...")

    # Initialize and validate configuration first
    logger.info("Validating configuration...")
    try:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        # Fail fast if the running config contradicts DEPLOYMENT_MODE (ADR-004):
        # a cloud deployment must present cloud identity, never silently run as standalone.
        from faultmaven.config.deployment_coherence import validate_deployment_coherence

        validate_deployment_coherence(settings)

        # Fail fast if no persistent database is configured (fm#1647). An empty
        # or in-memory DATABASE_URL used to boot as far as the enterprise seed
        # and die there as "Critical bootstrap failure". Placed BEFORE
        # resolve_pseudonym_key, which creates the data directory and writes
        # the key file, so a refused boot leaves nothing on disk; and outside
        # the test-environment skip below, because no test boots the app
        # without a database. The jobs runner calls the same gate.
        from faultmaven.config.persistent_database import require_persistent_database

        require_persistent_database(settings)

        # Fail fast if redaction has no pseudonym key it may use. Resolving is
        # what creates the standalone key file, so doing it here also means the
        # first redaction of the process is never the thing that writes it.
        # In cloud an unset key raises: generating one per pod would give each
        # replica a different placeholder for the same value, and discovering
        # that mid-request — the regex pass runs on every sanitize call,
        # whatever PROTECTION_SANITIZE_PII says — would surface as scattered
        # 500s rather than a refusal to start (#971).
        from faultmaven.infrastructure.security.pseudonym_key import (
            resolve_pseudonym_key,
        )

        resolve_pseudonym_key(settings)

        # Fail fast if no LLM provider was explicitly chosen, or the chosen
        # provider's credential is missing. There is no default provider — a
        # silent default would only fail later, mid-turn, with an opaque error.
        # Skipped in test environments (pytest / SKIP_SERVICE_CHECKS), which
        # boot the app without real credentials.
        if not _is_test_environment(settings):
            from faultmaven.config.llm_validation import (
                validate_llm_provider_credentials,
            )

            validate_llm_provider_credentials(settings)

            # Fail fast if the resolved investigation model (DA → CHAT) can't do
            # tool calling: the engine needs it to gather evidence
            # (search_file, deep_analysis), and concluding without reaching the
            # evidence is the premature-conclusion failure we guarantee against.
            # Explicit opt-out: ALLOW_TOOLLESS_INVESTIGATION (degraded/offline).
            from faultmaven.config.investigation_capability import (
                validate_investigation_tooling,
                validate_structured_output_capacity,
                warn_best_effort_enforcement,
            )
            from faultmaven.infrastructure.llm.providers.registry import get_registry

            validate_investigation_tooling(settings, get_registry())

            # Second capability axis: can the resolved structured-output model
            # SERVE the engine's response schemas? A constrained-decoding backend
            # can reject the larger stage schemas outright, and it does so only
            # once a case reaches that stage — so without this gate an
            # incompatible model runs several turns of a live investigation and
            # then fails every remaining turn. Fails open when capacity is
            # unmeasured; no opt-out flag, because there is no degraded mode that
            # still records investigation state.
            validate_structured_output_capacity(settings, get_registry())

            # Third, ADVISORY axis: schema-enforcement class per resolved role.
            # Warns (never blocks) when investigation/chat resolve to a model
            # whose response schemas are only requested in-prompt
            # (BEST_EFFORT) — the degraded-state failure is otherwise silent
            # and discovered from broken investigations, not from boot.
            # Classifier/synthesis roles are exempt by design.
            warn_best_effort_enforcement(settings, get_registry())

        # Validate workers configuration for in-memory storage
        workers = settings.server.workers
        storage_type = (settings.database.session_storage_type or "inmemory").lower()

        if workers > 1 and storage_type == "inmemory":
            logger.error(
                f"❌ Invalid configuration: WORKERS={workers} with in-memory storage"
            )
            logger.error(
                "   In-memory storage only works with WORKERS=1 (each worker has separate memory)."
            )
            logger.error("   Solutions:")
            logger.error(
                "   1. Set WORKERS=1 in your .env file (recommended for local deployment)"
            )
            logger.error(
                "   2. Use database storage: Set CASE_STORAGE_TYPE=database in your .env file"
            )
            raise ValueError(
                f"WORKERS={workers} is incompatible with in-memory storage. "
                "Set WORKERS=1 or use CASE_STORAGE_TYPE=database."
            )
        elif workers > 1:
            logger.info(
                f"✅ Multi-worker configuration (WORKERS={workers}) with {storage_type} storage"
            )
        else:
            logger.debug(f"Using single worker (WORKERS={workers})")
        logger.info("Configuration validated successfully")

        # Make configuration available to app
        app.extra["settings"] = settings
    except Exception as e:
        logger.error(f"Configuration initialization failed: {e}")
        raise

    # Container initialization and the composition root. Failures are refused
    # or tolerated there, per deployment mode — not here.
    logger.info("Initializing DI container...")
    await compose_application(app, settings)

    # Fail fast if the composition root withheld a route from idempotent
    # replay but left it collapsible by deduplication. That combination
    # cannot be built by ``declare_route_policy`` (it applies the
    # implication itself), so reaching here means the declaration was
    # assigned onto ``app.state`` by hand — and the symptom is a 409 raised
    # by the middleware *further out* than the one the declaration named,
    # on an operation whose whole purpose is to be repeatable. Close to
    # undiagnosable in production; trivial to state at boot (#1303).
    from faultmaven.api.middleware.route_policy import assert_policy_coherent

    _policy_problem = assert_policy_coherent(app)
    if _policy_problem:
        raise RuntimeError(_policy_problem)

    # Initialize core services with K8s support
    # SessionManager replaced by services.session.SessionService via DI container
    # Access via: container.get_session_service()

    # ML Model Loading Strategy (configurable lazy vs eager loading)
    # Default: lazy loading for faster startup, models load on first use
    try:
        lazy_load = settings.embedding.lazy_load_ml_models
        preload_models = settings.embedding.preload_models or []

        if lazy_load and not preload_models:
            logger.info(
                "🚀 Lazy ML model loading enabled - models will load on first use"
            )
            logger.info("   (Set LAZY_LOAD_ML_MODELS=false for eager loading)")
        else:
            # Eager loading or specific models requested
            logger.info("Pre-loading ML models during startup...")
            from faultmaven.infrastructure.model_cache import model_cache

            # Determine which models to load
            models_to_load = []
            if not lazy_load:
                # Eager mode: load all default models
                models_to_load = ["BAAI/bge-m3"]
            elif preload_models:
                # Lazy mode with specific preload list
                models_to_load = preload_models

            for model_name in models_to_load:
                try:
                    triggered_by = "startup" if not lazy_load else "preload"
                    if model_name == "BAAI/bge-m3":
                        bge_model = model_cache.get_bge_m3_model(
                            triggered_by=triggered_by
                        )
                        if bge_model:
                            load_info = model_cache.get_model_load_info(model_name)
                            load_time = (
                                load_info.load_time_seconds if load_info else "?"
                            )
                            logger.info(f"✅ {model_name} pre-loaded in {load_time}s")
                        else:
                            logger.warning(f"⚠️ {model_name} not available")
                    else:
                        logger.warning(f"Unknown model for preloading: {model_name}")
                except Exception as e:
                    logger.warning(f"Failed to pre-load {model_name}: {e}")
    except Exception as e:
        logger.warning(f"ML model loading configuration error: {e}")

    # Setup tracing
    init_opik_tracing()

    # Check and start local LLM services if needed
    try:
        from faultmaven.infrastructure.llm.local_llm_manager import (
            check_and_start_local_llm_service,
        )

        # Check if we're configured to use local LLM providers
        # Use settings (deployment-agnostic) instead of os.getenv()
        llm_settings = settings.llm
        chat_provider = (
            llm_settings.provider.value.lower() if llm_settings.provider else ""
        )
        classifier_provider = (
            llm_settings.classifier_provider.value.lower()
            if llm_settings.classifier_provider
            else ""
        )

        # Note: LOCAL_LLM_MODEL and LOCAL_LLM_URL would need to be added to LLMSettings
        # For now, using defaults (these are rarely used in local deployment)
        local_llm_model = "llama2-7b"  # Default fallback
        local_llm_base_url = "http://localhost:8080"  # Default fallback

        if chat_provider == "local":
            logger.info("Chat provider set to 'local', checking local LLM service...")
            success = await check_and_start_local_llm_service(
                "local", local_llm_base_url, local_llm_model
            )
            if success:
                logger.info("✅ Local LLM service ready for chat provider")
            else:
                logger.warning("⚠️ Failed to start local LLM service for chat provider")

        if classifier_provider == "local":
            logger.info(
                "Classifier provider set to 'local', checking local LLM service..."
            )
            success = await check_and_start_local_llm_service(
                "local", local_llm_base_url, local_llm_model
            )
            if success:
                logger.info("✅ Local LLM service ready for classifier provider")
            else:
                logger.warning(
                    "⚠️ Failed to start local LLM service for classifier provider"
                )

        if chat_provider != "local" and classifier_provider != "local":
            logger.info(
                "No local LLM providers configured, skipping local service check"
            )

    except Exception as e:
        logger.warning(f"Local LLM service check failed (non-critical): {e}")

    # Initialize Phase 2 monitoring components
    try:
        from faultmaven.infrastructure.observability.alerting import (
            setup_default_alert_rules,
        )
        from faultmaven.infrastructure.observability.apm_integration import (
            apm_integration,
        )

        # Start APM integration background export
        apm_integration.start_background_export()
        logger.info("✅ APM integration started")

        # Set up default alert rules
        setup_default_alert_rules()
        logger.info("✅ Default alert rules configured")

        logger.info("✅ Phase 2 monitoring components initialized")

    except Exception as e:
        logger.warning(f"Phase 2 monitoring initialization failed (non-critical): {e}")

    # In-process scheduler (opt-in via RUN_SCHEDULER=true)
    # Default: disabled for operational neutrality - use CLI jobs or external schedulers instead
    # See: python -m faultmaven.jobs.run --list
    case_cleanup_scheduler = None
    llm_usage_retention_task = None
    if settings.server.run_scheduler:
        # LLM usage ledger retention (#640, Q6). Beside case cleanup and
        # refused under multi for the same reason: the horizon is
        # deployment-wide and RLS would show this process one enterprise.
        try:
            from faultmaven.infrastructure.tasks.llm_usage_retention import (
                start_llm_usage_retention_scheduler,
            )
            from faultmaven.providers.tenancy.factory import (
                BUILTIN_MULTI,
                requested_tenant_provider,
            )

            llm_usage_retention_task = start_llm_usage_retention_scheduler(
                interval_hours=24,
                is_multi_tenant=(requested_tenant_provider() == BUILTIN_MULTI),
            )
        except Exception as e:
            logger.warning(
                f"LLM usage retention scheduler not started (non-critical): {e}"
            )
        try:
            # Self-contained import: the container is composed in
            # compose_application, not bound in this scope.
            from faultmaven.container import container
            from faultmaven.infrastructure.tasks import start_case_cleanup_scheduler

            # Only start if both case_vector_store and case_repository are available
            case_vector_store = getattr(container, "case_vector_store", None)
            case_repository = getattr(container, "case_repository", None)
            if case_vector_store and case_repository:
                # The cleanup task is cross-tenant scoped; the scheduler refuses
                # to start under the multi-tenant provider (ADR-010 P3, #629).
                # Self-contained import: the earlier factory import sits inside
                # the bootstrap try-block, which a degraded (non-production)
                # startup can skip past.
                from faultmaven.providers.tenancy.factory import (
                    BUILTIN_MULTI,
                    requested_tenant_provider,
                )

                case_cleanup_scheduler = start_case_cleanup_scheduler(
                    case_vector_store=case_vector_store,
                    case_repository=case_repository,
                    interval_hours=6,  # Run cleanup every 6 hours
                    is_multi_tenant=(requested_tenant_provider() == BUILTIN_MULTI),
                )
                if case_cleanup_scheduler:
                    logger.info(
                        "✅ Case cleanup scheduler started (RUN_SCHEDULER=true, single-process mode)"
                    )
                    app.extra["case_cleanup_scheduler"] = case_cleanup_scheduler
            else:
                logger.debug(
                    "Case cleanup scheduler skipped (missing case_vector_store or case_repository)"
                )
        except Exception as e:
            logger.warning(
                f"Case cleanup scheduler initialization failed (non-critical): {e}"
            )
    else:
        logger.info(
            "ℹ️ In-process scheduler disabled (RUN_SCHEDULER=false). Use 'python -m faultmaven.jobs.run' for jobs."
        )

    # Middleware must be added before the app starts. It is configured at import time.
    logger.info("✅ Middleware already configured")

    # KB Bootstrap: atomic, idempotent ingestion of shipped runbooks.
    # Pre-deployed `.md` files under data/knowledge/{scope}/ are ingested
    # directly into knowledge_items + ChromaDB without passing through the
    # conversion_drafts table. Idempotent: unchanged files are skipped on
    # subsequent runs. See faultmaven/bootstrap/kb_init.py.
    #
    # Single-tenant only: the pack writes the org-free global platform tier,
    # which under TENANT_PROVIDER=multi is seeded exclusively via the audited
    # maintenance path (`python -m faultmaven.jobs.run kb_seed
    # --cross-tenant-maintenance`), not by web workers on the RLS-enforced app
    # role (#770).
    try:
        from faultmaven.providers.tenancy.factory import BUILTIN_MULTI as _BUILTIN_MULTI
        from faultmaven.providers.tenancy.factory import (
            requested_tenant_provider as _requested_tenant_provider,
        )

        if _requested_tenant_provider() == _BUILTIN_MULTI:
            logger.info(
                "KB bootstrap skipped under multi-tenancy: seed the platform "
                "KB pack via the kb_seed maintenance job (#770)."
            )
        elif getattr(app.state, "knowledge_service", None):
            from faultmaven.bootstrap.kb_init import bootstrap_kb
            from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
            from faultmaven.infrastructure.persistence.database import get_db_session

            kb_result = await bootstrap_kb(
                knowledge_service=app.state.knowledge_service,
                db_session_factory=get_db_session,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
            )
            logger.info(f"✅ KB bootstrap: {kb_result!r}")
            if kb_result.failed:
                # Don't block startup, but make failures loud.
                for path, reason in kb_result.failed:
                    logger.warning(f"  KB bootstrap failed for {path}: {reason}")
        else:
            logger.warning("KB bootstrap skipped: knowledge_service not available")
    except Exception as e:
        logger.error(f"KB bootstrap raised (non-fatal): {e}", exc_info=True)

    # Funnel metrics: refresh the case-funnel gauges from the DB on an interval
    # (a projection of durable state, not transition counters -- see ADR 005).
    # Only when the Prometheus exporter is mounted; the gauges are no-ops
    # otherwise. Runs as a background task; cancelled on shutdown.
    try:
        from faultmaven.config.settings import MetricsExporter, get_settings

        if get_settings().providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
            from faultmaven.infrastructure.observability.funnel_metrics import (
                collector as funnel_collector,
            )

            app.state.funnel_metrics_task = asyncio.create_task(
                funnel_collector.run_periodic()
            )
            logger.info("✅ Funnel metrics collector started (case-state projection)")
    except Exception as e:
        logger.warning(f"Funnel metrics collector not started (non-fatal): {e}")

    logger.info(
        "🚀 FaultMaven API server startup COMPLETE - ready to serve fast requests!"
    )

    yield

    # Shutdown
    logger.info("Shutting down FaultMaven API server...")

    # Finish the LLM usage ledger's in-flight own-row writes (#640) while the
    # database is still open, then uninstall it — whatever the drain did: calls
    # metered after this point are counted as not persisted rather than
    # scheduled against a closing engine. A write still pending at the timeout
    # is counted.
    from faultmaven.infrastructure.llm.usage_ledger import (
        drain_pending_usage_writes,
        install_usage_ledger,
    )

    try:
        await drain_pending_usage_writes(timeout_s=5)
    except Exception as e:
        logger.warning(f"LLM usage ledger drain failed (non-critical): {e}")
    finally:
        install_usage_ledger(None)

    # Stop funnel metrics collector
    _funnel_task = getattr(app.state, "funnel_metrics_task", None)
    if _funnel_task is not None:
        _funnel_task.cancel()
        try:
            await _funnel_task
        except (asyncio.CancelledError, Exception):
            pass

    # Stop LLM config watcher (cloud multi-replica propagation)
    _config_watch_task = getattr(app.state, "llm_config_watch_task", None)
    if _config_watch_task is not None:
        _config_watch_task.cancel()
        try:
            await _config_watch_task
        except (asyncio.CancelledError, Exception):
            pass

    # Stop case cleanup scheduler
    if case_cleanup_scheduler:
        try:
            from faultmaven.infrastructure.tasks import stop_case_cleanup_scheduler

            stop_case_cleanup_scheduler(case_cleanup_scheduler)
        except Exception as e:
            logger.warning(f"Error stopping case cleanup scheduler: {e}")

    # Stop LLM usage retention
    if llm_usage_retention_task is not None:
        try:
            from faultmaven.infrastructure.tasks.llm_usage_retention import (
                stop_llm_usage_retention_scheduler,
            )

            await stop_llm_usage_retention_scheduler(llm_usage_retention_task)
        except Exception as e:
            logger.warning(f"Error stopping LLM usage retention scheduler: {e}")

    # Cleanup resources
    if "session_manager" in app.extra:
        # Cleanup any active sessions
        session_manager = app.extra["session_manager"]
        try:
            cleaned_count = await session_manager.cleanup_inactive_sessions()
            logger.info(f"Cleaned up {cleaned_count} expired sessions during shutdown")

            # Close session manager (stops scheduler and connections)
            await session_manager.close()
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")

    # Cleanup Phase 2 monitoring components
    try:
        from faultmaven.infrastructure.observability.apm_integration import (
            apm_integration,
        )

        # Stop APM background export
        apm_integration.stop_background_export()

        # Flush any remaining metrics
        await apm_integration.flush_metrics()

        logger.info("✅ Phase 2 monitoring components cleaned up")

    except Exception as e:
        logger.warning(f"Phase 2 monitoring cleanup failed (non-critical): {e}")

    # Dispose the canonical async engine. The user store no longer owns a
    # session (sessionless repository per #703), so there is nothing to
    # release here beyond the engine below.
    try:
        from faultmaven.infrastructure.persistence.database import close_database

        await close_database()
    except Exception as e:
        logger.warning(f"Database close failed (non-critical): {e}")

    logger.info("FaultMaven API server shutdown complete")
