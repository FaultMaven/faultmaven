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
    _is_test_environment,
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

        # WORKERS>1 is checked after composition (_start_serving), against
        # the stores the container actually built.
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


def refuse_per_process_stores_across_workers(app: FastAPI, workers: int) -> None:
    """Refuse ``WORKERS>1`` when a store the workers must share lives in one process.

    Every uvicorn worker runs this lifespan and composes its own container.
    That is sound for a store each worker reaches over the network, and wrong
    for one held inside the process. Two stores can be:

    * Redis, when the container's client is the in-process FakeRedis: each
      worker would keep its own sessions, token revocations, rate-limit
      counters and idempotency claims;
    * ChromaDB, when a client is a local ``PersistentClient``: every worker
      would open the same directory, each with its own in-memory index.

    Asked of the clients the composition root built (published on
    ``app.state``), never of the configuration. Standalone falls back to
    FakeRedis when the Redis that ``REDIS_URL``/``REDIS_HOST`` names cannot be
    reached (the default host is a Kubernetes service name), and to a local
    ``PersistentClient`` when ``CHROMADB_URL``'s server cannot, so a setting
    says what was asked for, not what was built. No storage-type setting
    decides either one (#2269).

    Raises:
        ValueError: ``workers > 1`` and either store is process-local. The
            message names the settings that clear it.
    """
    if workers <= 1:
        return

    reasons: list[str] = []
    remedies: list[str] = []

    redis_client = getattr(app.state, "redis_client", None)
    if redis_client is not None:
        from faultmaven.infrastructure.redis_client import is_fakeredis

        if is_fakeredis(redis_client):
            reasons.append(
                "Redis is the in-process FakeRedis, so each worker would keep "
                "its own sessions, token revocations, rate limits and "
                "idempotency claims"
            )
            remedies.append(
                "REDIS_URL (or REDIS_HOST) naming a Redis server every worker reaches"
            )

    local_chroma = []
    for label, attr in (
        ("KB", "kb_chromadb_client"),
        ("evidence", "evidence_chromadb_client"),
    ):
        client = getattr(app.state, attr, None)
        if client is None:
            continue
        from faultmaven.infrastructure.chroma_client import is_server_backed

        if not is_server_backed(client):
            local_chroma.append(label)
    if local_chroma:
        stores = (
            "store is a local PersistentClient"
            if len(local_chroma) == 1
            else "stores are local PersistentClients"
        )
        reasons.append(
            f"the {' and '.join(local_chroma)} ChromaDB {stores}, which every "
            "worker would open on the same directory"
        )
        remedies.append(
            "CHROMADB_URL naming a ChromaDB server (with VECTOR_STORAGE_TYPE=chromadb)"
        )

    if reasons:
        raise ValueError(
            f"WORKERS={workers} runs {workers} worker processes, and "
            f"{'; and '.join(reasons)}. Set WORKERS=1, or set "
            f"{' and '.join(remedies)}."
        )
    logger.info(
        "Multi-worker configuration (WORKERS=%d): Redis and ChromaDB are "
        "servers the workers share",
        workers,
    )


async def _start_serving(app: FastAPI, settings: "FaultMavenSettings") -> None:
    """The startup that follows composition: gates, preloads, background work.

    Anything it starts that the shutdown stops is published on ``app.state``,
    so a startup refused part-way still leaves :func:`_stop_serving` a handle
    on what it got as far as starting.
    """
    # Fail fast if several workers would each hold a store the deployment
    # must share. First, so a refused worker preloads and starts nothing.
    refuse_per_process_stores_across_workers(app, settings.server.workers)

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
    """What :func:`_stop_serving` stops, as teardown steps, in order."""
    from faultmaven.infrastructure.llm.usage_ledger import (
        drain_pending_usage_writes,
        install_usage_ledger,
    )

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
        ("LLM usage ledger drain", lambda: drain_pending_usage_writes(timeout_s=5)),
        ("LLM usage ledger uninstall", lambda: install_usage_ledger(None)),
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
