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
from typing import TYPE_CHECKING, Any, Callable, Mapping

from fastapi import FastAPI

from faultmaven.bootstrap.composition import (
    compose_application,
    decompose_application,
    stop_config_watcher,
)
from faultmaven.infrastructure.concurrency.teardown import cancel_and_wait, run_teardown
from faultmaven.infrastructure.observability.tracing import init_opik_tracing

if TYPE_CHECKING:
    from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)


async def _bootstrap_kb_step(app: FastAPI) -> None:
    """The lifespan's KB step: ingest the shipped pack, or log why it does not.

    Never raises. A failure is logged and startup continues.
    """
    # KB Bootstrap: atomic, idempotent ingestion of shipped runbooks.
    # The KB pack (`KB_PACK_DIR`, default `resources/knowledge/pack`) is
    # ingested directly into knowledge_items + ChromaDB without passing through
    # the conversion_drafts table. Idempotent: runbooks whose content hash is
    # unchanged are skipped on subsequent runs. See
    # faultmaven/bootstrap/kb_init.py.
    #
    # Single-tenant only: the pack writes the org-free global platform tier,
    # which under TENANT_PROVIDER=multi is seeded exclusively via the audited
    # maintenance path (`python -m faultmaven.jobs.run kb_seed
    # --cross-tenant-maintenance`), not by web workers on the RLS-enforced app
    # role (#770).
    try:
        # Self-contained import: the container is composed in
        # compose_application, not bound in this scope.
        from faultmaven.container import container
        from faultmaven.providers.tenancy.factory import BUILTIN_MULTI as _BUILTIN_MULTI
        from faultmaven.providers.tenancy.factory import (
            requested_tenant_provider as _requested_tenant_provider,
        )

        if _requested_tenant_provider() == _BUILTIN_MULTI:
            logger.info(
                "KB bootstrap skipped under multi-tenancy: seed the platform "
                "KB pack via the kb_seed maintenance job (#770)."
            )
        elif getattr(container, "knowledge_vector_store", None) is None:
            # No KB vector store was composed. That happens only under
            # SKIP_SERVICE_CHECKS=true on standalone: cloud refuses a
            # store-less boot, and a ChromaDB failure aborts composition. The
            # service would index every runbook into nothing, so each one
            # would fail with "0 chunks", and a changed pack would delete rows
            # it cannot re-create. Write nothing to knowledge_items (#2171).
            logger.warning(
                "KB bootstrap skipped: SKIP_SERVICE_CHECKS=true composes no KB "
                "vector store, so the pack cannot be indexed"
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
            # Its refused repairs are reported on /admin/config/status (#2055).
            app.state.kb_bootstrap_result = kb_result
            if kb_result.failed:
                # Don't block startup, but make failures loud.
                for path, reason in kb_result.failed:
                    logger.warning(f"  KB bootstrap failed for {path}: {reason}")
        else:
            logger.warning("KB bootstrap skipped: knowledge_service not available")
    except Exception as e:
        logger.error(f"KB bootstrap raised (non-fatal): {e}", exc_info=True)


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

        # Fail fast on everything the configuration alone decides: coherence
        # with DEPLOYMENT_MODE, a persistent database, the stores several
        # workers need, no wildcard CORS origin on a deployed box, a pseudonym
        # key, and (outside test environments) the LLM provider's credential and
        # capabilities. run_server runs the same
        # gates before uvicorn forks several workers. What only the built
        # clients can tell is refused inside composition.
        from faultmaven.bootstrap.configuration_gates import validate_configuration

        validate_configuration(settings)

        logger.info("Configuration validated successfully")

        # Make configuration available to app
        app.extra["settings"] = settings
    except Exception as e:
        logger.error(f"Configuration initialization failed: {e}")
        raise

    # Container initialization and the composition root. Failures are refused
    # or tolerated there, per deployment mode — not here. A refused composition
    # takes itself back before it raises.
    logger.info("Initializing DI container...")
    published = await compose_application(app, settings)

    # From here composition is installed, so every exit runs the one shutdown:
    # a startup refused after composition (a gate in _start_serving, or a
    # cancellation at any of its awaits) is undone exactly as a normal shutdown
    # is, and a later lifespan composes afresh rather than finding the
    # container still initialized.
    try:
        await _start_serving(app, settings)
        yield
    finally:
        await _stop_serving(app, published)


async def _start_serving(app: FastAPI, settings: "FaultMavenSettings") -> None:
    """The startup that follows composition: gates, preloads, background work.

    Anything it starts that the shutdown stops is published on ``app.state``,
    so a startup refused part-way still leaves :func:`_stop_serving` a handle
    on what it got as far as starting.
    """
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
            if llm_usage_retention_task is not None:
                app.state.llm_usage_retention_task = llm_usage_retention_task
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
                    app.state.case_cleanup_scheduler = case_cleanup_scheduler
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

    # KB Bootstrap: ingest the shipped runbook pack (non-fatal).
    await _bootstrap_kb_step(app)

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

    # Revocation watermark keyspace gauge (#1479): how many live per-user
    # watermarks the revocation store holds. Same exporter gate and the same
    # background-task shape as the funnel collector above.
    try:
        from faultmaven.config.settings import MetricsExporter, get_settings

        if get_settings().providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
            from faultmaven.modules.auth.infrastructure.metrics.revocation_metrics import (
                collector as watermark_collector,
            )

            app.state.revocation_watermark_task = asyncio.create_task(
                watermark_collector.run_periodic(app.state.token_revocation_store)
            )
            logger.info("✅ Revocation watermark gauge started")
    except Exception as e:
        logger.warning(f"Revocation watermark gauge not started (non-fatal): {e}")

    # Event-loop lag (#2056): how late the loop wakes from each tick. Same
    # exporter gate and background-task shape as the collectors above.
    try:
        from faultmaven.config.settings import MetricsExporter, get_settings

        if get_settings().providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
            from faultmaven.infrastructure.observability import loop_lag

            app.state.loop_lag_task = asyncio.create_task(
                loop_lag.monitor.run_periodic()
            )
            logger.info("✅ Event-loop lag monitor started")
    except Exception as e:
        logger.warning(f"Event-loop lag monitor not started (non-fatal): {e}")

    logger.info(
        "🚀 FaultMaven API server startup COMPLETE - ready to serve fast requests!"
    )


def _unpublish_then_stop(
    app: FastAPI, handle: str, stop: Callable[[Any], Any]
) -> Callable[[], Any]:
    """A shutdown step: take ``handle`` off ``app.state``, then ``stop`` what it names.

    A no-op when startup never got as far as publishing it. The handle comes off
    before the stop, so a stop that fails or is interrupted leaves none behind
    for a later shutdown to be handed (#2257). The step returns what ``stop``
    returns, which the step runner awaits when it is awaitable.
    """

    def _step() -> Any:
        running = getattr(app.state, handle, None)
        if running is None:
            return None
        delattr(app.state, handle)
        return stop(running)

    return _step


def _stop_case_cleanup_scheduler(scheduler: Any) -> None:
    """Stop the case cleanup scheduler, importing its stop inside the step.

    Imported here rather than when the steps are built, so that a failure to
    import is this one step's failure, logged, and not the whole shutdown's.
    """
    from faultmaven.infrastructure.tasks import stop_case_cleanup_scheduler

    stop_case_cleanup_scheduler(scheduler)


def _serving_stops(app: FastAPI) -> tuple[tuple[str, Callable[[], Any]], ...]:
    """What :func:`_stop_serving` stops, as teardown steps, in order.

    Each step imports what it needs, so a failed import is that step's failure
    alone (#2267).
    """

    async def _shutdown_protection() -> None:
        from faultmaven.api.protection import shutdown_protection

        await shutdown_protection(app)

    async def _clean_up_monitoring() -> None:
        from faultmaven.infrastructure.observability.apm_integration import (
            apm_integration,
        )

        apm_integration.stop_background_export()
        await apm_integration.flush_metrics()
        logger.info("✅ Phase 2 monitoring components cleaned up")

    async def _drain_usage_ledger() -> None:
        from faultmaven.infrastructure.llm.usage_ledger import (
            drain_pending_usage_writes,
        )

        await drain_pending_usage_writes(timeout_s=5)

    def _uninstall_usage_ledger() -> None:
        from faultmaven.infrastructure.llm.usage_ledger import install_usage_ledger

        install_usage_ledger(None)

    return (
        # Owned clients first, then the shared one, then the engine. The
        # protection middleware closes only a Redis client it built for itself;
        # the shared client is the container's, closed by the decomposition.
        ("Protection middleware shutdown", _shutdown_protection),
        # Finish the LLM usage ledger's in-flight own-row writes (#640) while the
        # database is still open, then uninstall it, whatever the drain did:
        # calls metered after this point are counted as not persisted rather
        # than scheduled against a closing engine. A write still pending at the
        # timeout is counted. This is the uninstall on the serving path;
        # decompose_application's own is then a no-op.
        ("LLM usage ledger drain", _drain_usage_ledger),
        ("LLM usage ledger uninstall", _uninstall_usage_ledger),
        (
            "Stopping the funnel metrics collector",
            _unpublish_then_stop(app, "funnel_metrics_task", cancel_and_wait),
        ),
        (
            "Stopping the revocation watermark gauge",
            _unpublish_then_stop(app, "revocation_watermark_task", cancel_and_wait),
        ),
        (
            "Stopping the event-loop lag monitor",
            _unpublish_then_stop(app, "loop_lag_task", cancel_and_wait),
        ),
        # The LLM config watcher (cloud multi-replica propagation), before the
        # schedulers, so that no config reload lands mid-shutdown.
        # decompose_application stops it again, a no-op by then.
        ("Stopping the LLM config watcher", lambda: stop_config_watcher(app)),
        (
            "Stopping the case cleanup scheduler",
            _unpublish_then_stop(
                app, "case_cleanup_scheduler", _stop_case_cleanup_scheduler
            ),
        ),
        (
            "Stopping the LLM usage retention scheduler",
            _unpublish_then_stop(app, "llm_usage_retention_task", cancel_and_wait),
        ),
        ("Phase 2 monitoring cleanup", _clean_up_monitoring),
    )


async def _stop_serving(app: FastAPI, published: Mapping[str, Any]) -> None:
    """The shutdown: after serving, and after a startup refused post-composition.

    Stops only what :func:`_start_serving` got as far as starting, taking each
    handle off ``app.state`` as it stops it, then takes composition back
    through :func:`decompose_application`, whose last step disposes the
    database engine. Every stop runs through :func:`run_teardown`, whatever the
    ones before it did: an interruption during one (a cancellation included) is
    re-raised once all have run (#2257).
    """
    try:
        logger.info("Shutting down FaultMaven API server...")
        await run_teardown(_serving_stops(app))
    finally:
        # Take composition back: the container (whose shared Redis client's pool
        # is bound to this event loop), the usage ledger, the services published
        # on app.state, which hold what that closed, and, last, the database
        # engine. In a finally, so that what is raised outside the stops (in
        # building them) still takes all of it back.
        await decompose_application(app, published)

    logger.info("FaultMaven API server shutdown complete")
