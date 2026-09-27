"""main.py

Purpose: FastAPI entry point and central application setup

Requirements:
--------------------------------------------------------------------------------
• Initialize the core FastAPI application instance
• Configure CORS middleware for browser extension
• Include API routers from data_ingestion, query_processing, and kb_management
• Set up startup/shutdown event handlers
• Integrate Comet Opik tracing middleware

Key Components:
--------------------------------------------------------------------------------
  app = FastAPI(title='FaultMaven API')
  app.include_router(data_ingestion.router, prefix='/api/v1')
  @app.on_event('startup')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Uvicorn, Comet Opik

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

# Load environment variables FIRST - before any other imports
from dotenv import load_dotenv

load_dotenv()

# Now import everything else
import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from faultmaven.api.contract_version import API_CONTRACT_VERSION
from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.utils.serialization import to_json_compatible

# Configure enhanced logging system first
from .infrastructure.logging.config import get_logger

logger = get_logger(__name__)

# Module-level settings cache (set during lifespan startup), read by
# ``_is_debug_enabled`` below. The one canonical slot moved to
# ``bootstrap.composition`` with the other code that reads it (fm#1707 wave 2)
# — imported rather than redeclared, so there is still exactly one. See the
# comment beside it there.
from .api.route_enumeration import iter_served_routes

# Admin routes
from .api.routes.admin import router as admin_users_router
from .api.routes.admin_cases import router as admin_cases_router
from .api.routes.admin_config import router as admin_config_router
from .api.routes.admin_grants import router as admin_grants_router
from .api.routes.sessions import router as investigation_sessions_router
from .api.v1.auth_dependencies import require_authentication, require_platform_admin

# The application lifespan (startup fail-fast gates, background tasks,
# shutdown) moved to bootstrap/lifespan.py (fm#1707 wave 2), alongside the
# composition root it calls (bootstrap/composition.py). Imported here, at
# the point ``lifespan`` used to be defined, so ``FastAPI(lifespan=lifespan)``
# below binds the same name at the same point in this module's execution.
from .bootstrap.lifespan import lifespan

# SessionManager now handled via DI container - services.session.SessionService
# Middleware assembly, and the Opik SDK/middleware availability probe that
# decides whether it adds ``OpikMiddleware`` to the stack, both live in
# bootstrap/middleware.py (fm#1707 wave 2) — imported here, at the point the
# probe used to run inline, so its import-time side effect (attempting
# ``import opik``) still happens at the same place in this module's execution.
from .bootstrap.middleware import setup_middleware

# Import API routes from modules
# All routes now in modules following vertical slice architecture
from .modules.auth.api.auth import router as auth_router
from .modules.auth.api.invitations import router as invitations_router
from .modules.auth.api.oauth import router as oauth_router
from .modules.auth.api.session import router as session_router
from .modules.auth.api.teams import router as teams_router
from .modules.auth.domain.models.auth import DevUser
from .modules.case.api.routes.router import router as case_router
from .modules.knowledge.api.conversion_routes import router as conversion_router
from .modules.knowledge.api.routes import router as knowledge_router
from .modules.report.api.routes import router as report_router

# Create FastAPI application with disabled automatic redirects
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting copilot for Engineers, "
    "SREs, and DevOps professionals",
    # Becomes `info.version` in the published contract, so it is the CONTRACT
    # version rather than the product's — see api/contract_version.py. The two
    # were the same literal, which is part of why neither ever moved.
    version=API_CONTRACT_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    redirect_slashes=False,  # Disable automatic trailing slash redirects
    # Bind the request's organization to the RLS contextvar before any endpoint
    # opens a database transaction (ADR-010 P2b). Single-tenant forces the
    # Standalone org; multi-tenant sources it from the verified auth claim and
    # fails closed on a missing org. A global dependency (not BaseHTTPMiddleware)
    # so the contextvar reaches the endpoint's task.
    dependencies=[Depends(bind_request_enterprise_context)],
)

# Override Starlette's default multipart form-field size limit.
# Starlette defaults to 1MB per form field, but our upload limit is MAX_UPLOAD_SIZE_MB (default 10MB).
# All three data submission paths (file upload, page injection, pasted text) go through
# the same unified /turns endpoint as multipart form data and must respect the same limit.
# Starlette >= 1.1 enforces the limit via Request.form()'s max_part_size keyword default —
# a MultiPartParser class-attribute override is shadowed by it — so the override replaces
# that default for every form parse in the app. max_part_size bounds non-file fields only;
# file parts are unbounded at the parser, so each route accepting UploadFile enforces the
# same limit per file via UploadFile.size.
# See: docs/architecture/data-processing/data-preprocessing-design-specification.md Appendix A
_upload_max_bytes = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024
_original_request_form = StarletteRequest.form


def _form_with_configured_limits(
    self: StarletteRequest,
    *,
    max_files: int | float = 1000,
    max_fields: int | float = 1000,
    max_part_size: int = _upload_max_bytes,
):
    return _original_request_form(
        self,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )


StarletteRequest.form = _form_with_configured_limits


# Configure middleware at import time (must run before app startup).
setup_middleware(app)

# Include API routers (only those in locked spec)
# REMOVED: data.router - moved to modules/case (data ingestion)
# REMOVED: knowledge.router - moved to modules/knowledge/api/routes.py
# REMOVED: session.router - moved to modules/auth (session management)
# REMOVED: auth.router - moved to faultmaven/api/routes/auth.py


app.include_router(auth_router, prefix="/api/v1")
logger.info("✅ Auth endpoints added")

app.include_router(teams_router, prefix="/api/v1")
logger.info("✅ Team endpoints added")

# The invitee's half of team consent (ADR-017 D4). Mounted unconditionally, like
# the team router above: under TENANT_PROVIDER=single ``team_service`` is unwired
# and every route here refuses with a reason slug, so the published contract
# stays one document describing every deployment rather than a function of a
# deployment's tenancy mode.
app.include_router(invitations_router, prefix="/api/v1")
logger.info("✅ Team invitation endpoints added")

app.include_router(case_router, prefix="/api/v1")
logger.info("✅ Case endpoints added")

app.include_router(
    investigation_sessions_router
)  # No prefix - router already has /api/v1/cases/{case_id}/sessions
logger.info("✅ Investigation session endpoints added")

app.include_router(knowledge_router, prefix="/api/v1")
logger.info("✅ Knowledge endpoints added")

app.include_router(conversion_router, prefix="/api/v1")
logger.info("✅ Document conversion endpoints added")

app.include_router(report_router, prefix="/api/v1")
logger.info("✅ Report endpoints added")

app.include_router(session_router, prefix="/api/v1")
logger.info("✅ Session endpoints added")

# Admin routes (user management + configuration)
app.include_router(admin_users_router)  # prefix already set on router: /api/v1/admin
logger.info("✅ Admin user management endpoints added")

app.include_router(admin_config_router)  # prefix already set on router: /api/v1/admin
logger.info("✅ Admin configuration endpoints added")

app.include_router(admin_cases_router)  # prefix already set on router: /api/v1/admin
logger.info("✅ Admin cross-tenant case listing endpoint added")

# prefix already set on router: /api/v1/admin/grants
app.include_router(admin_grants_router)
logger.info("✅ Break-glass grant endpoints added")

# OAuth router (only if enabled)
try:
    from .config.settings import get_settings

    _oauth_settings = get_settings()
    if _oauth_settings.auth.oauth_enabled:
        app.include_router(oauth_router, prefix="/api/v1")
        logger.info("✅ OAuth endpoints added")
    else:
        logger.info("ℹ️ OAuth endpoints disabled (using dev-login mode)")
except Exception as e:
    logger.warning(f"OAuth router initialization failed (non-critical): {e}")

# SSO hosted-login router (ADR-015) — only when WorkOS is fully configured in
# oauth mode. Mirrors the OAuth router gate above; standalone never mounts it.
try:
    from .config.settings import get_settings

    _sso_settings = get_settings()
    if _sso_settings.auth.sso_configured:
        from .modules.auth.api.sso import router as sso_router

        app.include_router(sso_router, prefix="/api/v1")
        logger.info("✅ SSO endpoints added")
    else:
        logger.info("ℹ️ SSO endpoints disabled (WorkOS not configured)")
except Exception as e:
    logger.warning(f"SSO router initialization failed (non-critical): {e}")

# Prometheus metrics endpoint (PR #5 - observability neutrality)
# Only mounted when METRICS_EXPORTER=prometheus_http
try:
    from .config.settings import MetricsExporter, get_settings

    _metrics_settings = get_settings()
    if _metrics_settings.providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker
        from .infrastructure.observability.metrics_exporters import (
            create_prometheus_metrics_endpoint,
            register_scrape_hook,
        )

        app.include_router(create_prometheus_metrics_endpoint(), tags=["metrics"])
        # SLA gauges are recomputed at every scrape so /health/sla is alertable
        register_scrape_hook(sla_tracker.update_prometheus_gauges)
        # Component health likewise (#1547). /health is the liveness surface
        # and answers 200 by design, so the Kubernetes probes reading it cannot
        # act on a dependency outage — correctly, since restarting a pod does
        # not fix a shared primary. `component_health_status` is what puts that
        # verdict somewhere a human can be paged from. No alert rule consumes
        # it yet: one belongs in faultmaven-enterprise-infra, and the
        # expression it should use is in docs/operations/monitoring/README.md.
        # The classifications ride as labels so that rule can select the set
        # instead of naming components, keeping the fatal set data.
        register_scrape_hook(component_monitor.publish_health_gauges)
        logger.info(
            "✅ Prometheus /metrics endpoint mounted (METRICS_EXPORTER=prometheus_http)"
        )
    else:
        logger.info(
            "ℹ️ Prometheus /metrics not mounted (METRICS_EXPORTER=none). Set METRICS_EXPORTER=prometheus_http to enable."
        )
except Exception as e:
    logger.warning(
        f"Prometheus metrics endpoint initialization failed (non-critical): {e}"
    )


# Debug endpoints - mounted when ENVIRONMENT is `development`, or in ANY
# environment when the ENABLE_DEBUG_ENDPOINTS operator switch is set.
#
# `development` and nothing else, although the predicate below reads
# `env in ("development", "testing", "test")`. `Environment` admits exactly
# development / staging / production, so the other two strings are DEAD against
# settings: `ENVIRONMENT=testing` does not select them, it raises
# ValidationError ("Input should be 'development', 'staging' or 'production'").
# They can only ever match in the degraded `os.getenv` fallback further down —
# which runs when `get_settings()` already failed, i.e. exactly the state
# `ENVIRONMENT=testing` produces. Documenting them as mounting values sent an
# operator to set one and break their own boot, which is why every published
# statement of this flag now says `development`.
#
# `staging` does NOT mount by default either; "outside production" would be the
# same kind of wrong as the claim this comment replaced. The routes expose
# internal state, so every route on the router requires an authenticated caller
# and the four #1474 gated require the platform administrator role; the block at
# the mount below says why that is the layer the gate sits at.
def _is_debug_enabled(settings=None) -> bool:
    """Check if debug endpoints should be enabled based on environment."""
    # Get settings if not provided
    if settings is None:
        try:
            from .config.settings import get_settings

            settings = get_settings()
        except Exception:
            # Fallback to environment check if settings unavailable
            env = os.getenv("ENVIRONMENT", "development").lower()
            return (
                env in ("development", "testing", "test")
                or os.getenv("ENABLE_DEBUG_ENDPOINTS", "").lower() == "true"
            )

    # Use settings (deployment-agnostic)
    env = settings.server.environment.value.lower()
    return (
        env in ("development", "testing", "test")
        or settings.server.enable_debug_endpoints
    )


# Get settings for debug check (may not be available at module level)
try:
    from .config.settings import get_settings

    _debug_settings = get_settings()
except Exception:
    _debug_settings = None

# Mounting is one question; exposure is another, and #1474 is what happens when
# a codebase answers only the first. ``_is_debug_enabled()`` is a DISJUNCTION —
# ``env in (development, testing, test) OR enable_debug_endpoints`` — not an
# environment check, so the header comment's former claim that these "should
# never be enabled in production" was an intent the code did not implement:
# ``ENABLE_DEBUG_ENDPOINTS=true`` mounts this router in production, and four of
# its five routes took no auth dependency at all. Self hosted publishes 8090 on
# 0.0.0.0 with no proxy, so the audience of ``GET /debug/config`` — environment,
# preset, storage backend, tenant provider, llm provider, protection on/off —
# was whoever could reach the port.
#
# The flag is left alone. Debugging a production deployment is presumably why an
# operator switch exists, and taking that away is the owner's call, not a
# security fix's. What changes is that the flag now governs MOUNTING rather than
# EXPOSURE: the four routes below carry ``require_platform_admin``, so no route
# on this router is reachable without a credential.
#
# The router is NOT uniform, and saying "every route here is platform-admin"
# would be the next version of the claim this change exists to correct.
# ``/debug/cases/{case_id}/causal-graph`` carries ``require_authentication``
# only — any signed-in caller of the deployment may reach it, bounded by the
# owner ∪ shared-to-my-teams check it applies to the case. #1474 scoped itself
# to the four that took NO dependency at all and said that route "is not part
# of this". Whether the most data-revealing route on the router should also be
# operator-only, while ``/debug/health`` (a static ``{"status": "ok"}``) is,
# is a real question and a separate decision — it is recorded on the pull
# request rather than taken here.
#
# The gate is declared on the DECORATOR (``dependencies=[...]``) rather than as a
# handler parameter, which is the shape #1467 established for
# ``/admin/optimization/trigger-cleanup``. It is not a style preference: FastAPI
# inserts decorator-level dependencies at the FRONT of the route's dependant, so
# they resolve before any parameter the handler declares. A gate written as a
# parameter resolves in declaration order beside the others, and an anonymous
# caller can get a service's 500 instead of the 401 the gate promises. These four
# handlers declare no parameters today; the ordering is asserted anyway, in
# ``tests/integration/api/test_no_unauthenticated_operations.py``, because the
# next person to add one must not have to rediscover it.
#
# Deferred rather than done here: making ``_is_debug_enabled()`` a conjunction as
# well, so the flag cannot lift the router into production at all. That is
# defence in depth on top of this, not an alternative to it.
if _is_debug_enabled(settings=_debug_settings):
    # Says what is true of the whole router rather than of most of it: four
    # routes are platform-admin, the causal-graph route is authenticated. The
    # blanket "platform-admin required" this line first carried is the claim
    # the block above forbids in as many words.
    logger.info(
        "🔧 Debug endpoints mounted (ENVIRONMENT=%s); every route requires a "
        "credential, the four operator diagnostics require platform admin",
        _debug_settings.server.environment.value if _debug_settings else "unknown",
    )
    # Whether the operator flag lifted this router into a non-development
    # environment is a security-relevant deployment fact, and a startup line is
    # not an observable — it rolls out of `kubectl logs` long before anyone
    # asks. `GET /admin/config/status` reports it beside `kb_prefetch` and
    # `first_party_consent_skip`, which are there for the same reason.
    #
    # It reads the ROUTE TABLE rather than a flag written here. A flag only this
    # branch sets reports "no debug surface" for any app composed another way,
    # and for a security-audit observable that is the dangerous direction: it is
    # the answer that ends an investigation early about a pod that does serve
    # /debug. See `api/route_enumeration.serves_path_prefix`.

    @app.get(
        "/debug/routes",
        dependencies=[Depends(require_platform_admin)],
    )
    async def debug_routes():
        """List all registered routes (path + methods).

        Requires the platform administrator role (#1474): the table includes
        every route registered with ``include_in_schema=False``, so it is a
        strictly larger surface than the published contract.

        Flattened through ``iter_route_contexts``, not by walking ``app.routes``
        directly. FastAPI 0.139 stopped copying an included router's routes into
        ``app.routes`` and records one ``_IncludedRouter`` placeholder per
        ``include_router`` instead, so the flat walk this used to do reported
        **24 of 147 routes** on fastapi 0.141.1 — a strict SUBSET of the
        published contract rather than a superset of it, and an operator
        checking whether a route is registered was told "no" about a hundred
        routes that are. Same flattener, and same reason, as
        ``api/middleware/route_policy.py`` (fm#1305); it resolves the EFFECTIVE
        path, including any prefix passed to ``include_router``, which walking
        ``original_router`` by hand does not.
        """
        routes_info = []
        seen = set()
        for served in iter_served_routes(app):
            key = (served.path, tuple(sorted(served.methods)))
            if served.path and key not in seen:
                seen.add(key)
                routes_info.append(
                    {"path": served.path, "methods": sorted(served.methods)}
                )

        return {
            "routes": routes_info,
            "count": len(routes_info),
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }

    @app.get(
        "/debug/health",
        dependencies=[Depends(require_platform_admin)],
    )
    async def debug_health():
        """Minimal debug health endpoint.

        Requires the platform administrator role (#1474). Strictly less than
        the ``/health`` family, which is public in every environment and needs
        no flag — gated with its three siblings because they mount together
        and a router where only some routes are gated is the state that
        produced this issue.
        """
        return {
            "status": "ok",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }

    @app.get(
        "/debug/config",
        dependencies=[Depends(require_platform_admin)],
    )
    async def debug_config():
        """Get current configuration summary including active preset.

        Requires the platform administrator role (#1474): this is the most
        revealing of the four, and the reason the fix is auth on the routes
        rather than a note in the allowlist.

        Returns information about:
        - Active configuration preset (if any)
        - Environment settings
        - Storage backend types
        - LLM provider configuration
        - Protection settings

        Useful for debugging configuration issues and verifying preset application.
        """
        try:
            from .config.presets import list_available_presets
            from .config.settings import get_settings

            settings = get_settings()

            return {
                "timestamp": to_json_compatible(datetime.now(UTC)),
                "configuration": settings.get_configuration_summary(),
                "available_presets": list_available_presets(),
            }
        except Exception as e:
            logger.error(f"Failed to get configuration info: {e}")
            return {
                "error": "Failed to get configuration",
                "timestamp": to_json_compatible(datetime.now(UTC)),
            }

    @app.get(
        "/debug/llm-providers",
        dependencies=[Depends(require_platform_admin)],
    )
    async def debug_llm_providers():
        """Get current LLM provider status and fallback chain.

        Requires the platform administrator role (#1474): never a key, but
        deployment reconnaissance — which providers are configured, which are
        reachable, and the resolved context-window budget.
        """
        try:
            from .container import container

            # Get the LLM provider (router) from the container
            llm_provider = container.get_llm_provider()

            # Get provider status
            provider_status = llm_provider.get_provider_status()

            # Get fallback chain
            fallback_chain = llm_provider.registry.get_fallback_chain()

            # Get available providers
            available_providers = llm_provider.registry.get_available_providers()

            # Check if strict mode is enabled (from settings, deployment-agnostic)
            from .config.settings import get_settings

            settings_debug = get_settings()
            strict_mode = settings_debug.llm.strict_provider_mode

            # GAP-1: surface the resolved context-window budget for the active
            # provider/model so operators can see the true window, the derived
            # hard prompt ceiling, the soft fill target, and whether the
            # conservative default fired for an unrecognized model.
            prompt_budget = None
            try:
                from .utils.model_context import resolve_model_budget

                active_provider = getattr(llm_provider, "provider_name", None) or (
                    fallback_chain[0] if fallback_chain else None
                )
                active_model = (
                    getattr(llm_provider.config, "default_model", None)
                    if hasattr(llm_provider, "config")
                    else None
                )
                rb = resolve_model_budget(active_provider, active_model)
                prompt_budget = {
                    "provider": rb.provider,
                    "model": rb.model,
                    # The budget FaultMaven actually fills (PROMPT_TARGET_TOKENS,
                    # clamped to the window when known).
                    "prompt_target_tokens": rb.prompt_target,
                    # Hard ceiling + inputs: present only when the window is
                    # known; null means we trusted the configured target.
                    "window_known": rb.window_known,
                    "context_window": rb.context_window,
                    "response_reserve": rb.response_reserve,
                    "hard_prompt_budget": rb.prompt_budget,
                    "matched_registry_key": rb.matched_key,
                }
            except Exception as budget_exc:  # pragma: no cover - best effort
                logger.warning(f"Prompt budget unavailable: {budget_exc}")
                prompt_budget = {"error": "Prompt budget unavailable"}

            return {
                "timestamp": to_json_compatible(datetime.now(UTC)),
                "primary_provider": fallback_chain[0] if fallback_chain else "none",
                "strict_mode": strict_mode,
                "fallback_chain": fallback_chain,
                "available_providers": available_providers,
                "provider_details": provider_status,
                "prompt_budget": prompt_budget,
            }

        except Exception as e:
            logger.error(f"Failed to get LLM provider status: {e}")
            return {
                "error": "Failed to get LLM provider status",
                "timestamp": to_json_compatible(datetime.now(UTC)),
            }

    @app.get("/debug/cases/{case_id}/causal-graph")
    async def debug_causal_graph(
        case_id: str,
        request: Request,
        current_user: DevUser = Depends(require_authentication),
    ):
        """Dump a case's causal graph + hypothesis-chain wiring (debug router).

        Instrumentation hook for the 2D-hypothesis chain-emission validation
        (chain emission is always active). Returns the persisted causal
        DAG (nodes/edges), each hypothesis's chain link (``root_node_id`` /
        ``path``), the engine-derived ``cause_state``, and the root-cause
        conclusion — enough for the simulator probe to detect well-formed
        chains, bridge-stub divergence, rung-level evidence, and M6 demotion.

        Authenticated, and gated by the same owner ∪ shared-to-my-teams check
        every other single-case read carries. It previously took neither and
        loaded the row straight from the repository: under the deployed cloud
        posture PostgreSQL row-level security covered it, so the exposure was
        bounded by a layer this route did not ask for — and on any deployment
        without RLS (standalone on SQLite) it served any case to any caller,
        authenticated or not. Recorded as an observation by the two-tenant
        surface probe; closed here.

        A case the caller may not read answers the same ``case not found``
        envelope an absent one does, so the refusal is not an existence oracle.

        Best-effort: never raises on serialization; absent graph returns empty
        collections. Registered only where the debug router mounts — which is
        `ENVIRONMENT=development`, or ANY environment including production when
        ENABLE_DEBUG_ENDPOINTS is set. It is not production-free (#1493).
        """
        from .api.debug_introspection import build_causal_graph_debug_payload

        case_service = getattr(request.app.state, "case_service", None)
        if case_service is None:
            return {"error": "case service unavailable", "case_id": case_id}
        # Through the service, not the repository: `user_id` is what applies the
        # owner ∪ shared check, and the repository has no such notion.
        case = await case_service.get_case(case_id, user_id=current_user.user_id)
        if case is None:
            return {"error": "case not found", "case_id": case_id}

        return {
            **build_causal_graph_debug_payload(case),
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }

else:
    # Two corrections in one line. "disabled in production" was wrong for
    # `staging`, which also lands here and which this reported as production;
    # and "ENABLE_DEBUG_ENDPOINTS unset" was wrong for every falsey SET value —
    # `false`, `0`, `no` — including the `"false"` the API-reference generator
    # exports. The resolved value is interpolated instead of described.
    logger.info(
        "🔒 Debug endpoints not mounted (ENVIRONMENT=%s, " "ENABLE_DEBUG_ENDPOINTS=%s)",
        _debug_settings.server.environment.value if _debug_settings else "unknown",
        os.getenv("ENABLE_DEBUG_ENDPOINTS", "<unset>"),
    )

# Modular monolith pivot: keep only core endpoints; advanced routes disabled
# Protection monitoring is now handled by middleware and health endpoints


# Register domain exception handlers (TASK-027)
from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    request_validation_exception_handler,
)

for exc_type, handler in get_exception_handlers().items():
    app.add_exception_handler(exc_type, handler)
logger.info("✅ Domain exception handlers registered")


# Custom exception handlers
#
# RequestValidationError is registered explicitly rather than through
# get_exception_handlers(), which maps domain exceptions: this one fires before
# any module code runs, on a request FastAPI could not bind to the endpoint
# signature. The handler lives beside the others in api/exception_handlers.py —
# see fm#1048 for why its serialization has to be total.
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)


# HTTPException is registered explicitly too, and that is load-bearing rather
# than stylistic: FastAPI does `exception_handlers.setdefault(HTTPException,
# ...)` at construction, so losing this registration does NOT leave the
# exception unhandled — it falls back to FastAPI's default, which renders a
# dict `detail` into the body raw. The failure would be silent, which is why
# `tests/integration/api/test_exception_handlers_are_registered.py` asserts it.
app.add_exception_handler(HTTPException, http_exception_handler)


@app.exception_handler(500)
async def internal_server_error_handler(request: Request, exc):
    """Custom 500 handler for internal server errors with Request ID for correlation"""
    # Extract Request ID from middleware (stored in request.state by RequestIdMiddleware)
    request_id = getattr(request.state, "request_id", None)

    # Path only, never the full URL. request.url renders the query string, and
    # the SSO callback carries the IdP authorization code there -- so any
    # unhandled exception on that route wrote a live credential to an ERROR
    # record. The redaction filter cannot reach this one: it covers the foreign
    # stdlib loggers that print URLs, and this is a first-party structlog event.
    logger.error(
        f"Internal server error on {request.method} {request.url.path}: {exc}",
        extra={"request_id": request_id} if request_id else {},
    )

    error_response = {"detail": "Internal server error"}
    if request_id:
        error_response["request_id"] = request_id

    return JSONResponse(status_code=500, content=error_response)


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "message": "FaultMaven API",
        # The product version. Deliberately not API_CONTRACT_VERSION: what a
        # client negotiates against is the contract, which moves on its own
        # cadence (api/contract_version.py).
        "version": "1.0.0",
        "description": "AI-powered troubleshooting copilot",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/api/v1/meta/capabilities")
@app.get(
    "/v1/meta/capabilities",
    deprecated=True,
    description=(
        "Deprecated: use `GET /api/v1/meta/capabilities`, which serves the "
        "identical response. This path is kept for already-installed browser "
        "extensions and is unreachable for a same-origin client: the "
        "Kubernetes ingress routes `/api`, `/health` and `/metrics` to this "
        "service and everything else to the Dashboard SPA, so this path is "
        "answered with the SPA's HTML."
    ),
)
async def get_capabilities(request: Request):
    """
    Return backend capabilities for browser extension configuration.

    This endpoint is called by the FaultMaven Copilot browser extension
    and the Dashboard to detect the deployment mode and gate features
    (e.g. team sharing, the org/team management console) accordingly.

    Served at two paths for one handler, so both answer byte-identically.
    ``/api/v1/meta/capabilities`` is the canonical one: every other
    client-facing route lives under ``/api``, and that is the only prefix the
    Kubernetes ingress forwards here — a same-origin Dashboard
    (``VITE_API_URL=""``, the deployed default) asking for the bare ``/v1``
    path receives the SPA's own HTML and degrades its capabilities silently.
    The bare ``/v1`` path stays as a deprecated alias because extensions
    already installed are pinned to it.

    Returns:
        Backend capabilities including deployment mode, dashboard URL, and feature flags
    """
    from .config.settings import get_settings

    settings = get_settings()

    # Determine deployment mode based on dashboard URL
    # Cloud: https://app.faultmaven.ai (managed SaaS)
    # Self-hosted: localhost or custom domain (customer-managed)
    is_cloud = settings.is_cloud
    deployment_mode = "cloud" if is_cloud else "self-hosted"

    # Team collaboration is active only when a TeamService is wired
    # (multi-tenant provider, ADR-010 P2). This is the correct signal for
    # team-gated capabilities: keying on ``deployment_mode == "cloud"`` would
    # light them up in Cloud *before* multi-tenancy is ready (team_service is
    # None until then), which the dashboard/copilot would then act on. The
    # inventory/team routes read the same ``app.state.team_service`` signal.
    team_service = getattr(request.app.state, "team_service", None)
    team_management_active = team_service is not None

    return {
        "deploymentMode": deployment_mode,
        "kbManagement": "dashboard",
        "dashboardUrl": settings.auth.dashboard_url,
        "features": {
            "extensionKB": False,  # Always false - extension KB removed
            "adminKB": deployment_mode == "cloud",
            # Team-based KB/case sharing (ADR-013: Team = the sharing unit).
            # Gated on the live TeamService, not deployment mode, so it stays
            # off in Cloud until multi-tenancy is ready (ADR-010 P2).
            "teamSharing": team_management_active,
            # Org/Team management console (the composed Cloud admin module,
            # ADR-010 D7). Advertised from the same TeamService signal so the
            # dashboard hides the console until team management is live.
            "managementConsole": team_management_active,
            "caseHistory": deployment_mode == "cloud",
            "sso": deployment_mode == "cloud",
        },
        "limits": {
            "maxFileBytes": 10485760,  # 10MB
            "allowedExtensions": [
                ".md",
                ".txt",
                ".log",
                ".json",
                ".csv",
                ".yaml",
                ".yml",
            ],
        },
        "branding": {
            "name": "FaultMaven",
            "supportUrl": "https://github.com/FaultMaven/faultmaven/issues",
        },
    }


@app.get("/health")
async def health_check():
    """Component health and SLA detail. Always answers 200; read `status`.

    This is the **liveness** surface: production points its liveness *and*
    startup probes here, and a liveness probe that fails on a dependency
    restarts a pod that restarting cannot fix — during a database outage that
    replaces a degraded service with a crash-looping one whose recovery is
    then delayed by kubelet backoff. So a failing dependency is reported in
    the body and never in the status code. The verdict that gates traffic
    lives on `/readiness`, which is the question a status code can answer
    without that side effect.
    """
    from .infrastructure.health.component_monitor import component_monitor
    from .infrastructure.health.sla_tracker import sla_tracker

    # Get component health status
    try:
        component_health_results = await component_monitor.check_all_components()
        overall_status, overall_summary = component_monitor.get_overall_health_status()
        sla_summary = sla_tracker.get_sla_summary()

        # Enhanced health status with component details
        health_status = {
            "status": overall_status.value,
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "overall_sla": sla_summary["overall_sla"],
            "components": {},
            "services": {"session_manager": "active", "api": "running"},
            "summary": overall_summary,
            "sla_status": {
                "active_breaches": sla_summary["active_breaches"],
                "total_breaches_24h": sla_summary["total_breaches_24h"],
                "worst_performing": sla_summary["worst_performing_component"],
                "best_performing": sla_summary["best_performing_component"],
            },
        }

        # Add detailed component information
        for component_name, component_health in component_health_results.items():
            health_status["components"][component_name] = {
                "status": component_health.status.value,
                "fatal": component_health.fatal,
                "fails_per_replica": component_health.fails_per_replica,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "probe_availability_24h": component_health.probe_availability_24h,
                "probe_failures_24h": component_health.probe_failures_24h,
                "probe_successes_24h": component_health.probe_successes_24h,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata,
            }

    except Exception as e:
        logger.error(f"Enhanced health check failed: {e}")
        # This arm is the one place `/health` and `component_health_status`
        # can disagree, and nothing in the `try` above can reach it today.
        # Measured rather than argued (#1568 item 2). The `try` holds FIVE
        # statements, not the three the issue named — counted, because an
        # enumeration that claims to be exhaustive and is not is worse than
        # none:
        #
        #  1. `check_all_components()` — a probe raising `Exception` is
        #     handled by `check_component_health`; a probe raising a bare
        #     `BaseException` is handled by the sweep's own
        #     `task.exception()` arm; both were driven and neither escapes.
        #     `_record_abandoned_probe` can raise `KeyError`, but only for a
        #     component deleted mid-sweep, and nothing deletes from
        #     `component_health`. `asyncio.wait` raises `ValueError` on an
        #     empty task set, but the registry is filled in
        #     `ComponentHealthMonitor.__init__` and never cleared.
        #  2. `get_overall_health_status()` — the gauge publish is wholly
        #     inside its own `try`; driven with an exploding metrics
        #     registry, it still returns.
        #  3. `sla_tracker.get_sla_summary()` — a different subsystem, and
        #     the first statement here whose totality is not a property of
        #     this module.
        #  4. the `health_status = {...}` literal — which dereferences FIVE
        #     `sla_summary[...]` keys. A `get_sla_summary()` that returns
        #     successfully having renamed one raises `KeyError` here, so
        #     statement 3 returning is not enough; its SHAPE is load-bearing
        #     too.
        #  5. the `for … in component_health_results.items()` loop, reading
        #     `.status.value` and the two declarations off each record.
        #
        # If the arm does become reachable, WHICH half is wrong depends on
        # where it raised. At 1 or 2, the gauge still carries the PREVIOUS
        # sweep's verdict and looks fresh — the alert is misled and `/health`
        # is blind. At 3, 4 or 5 the publish has already run, so the gauge is
        # current and correct and only `/health` is blind. Pinned by
        # `tests/unit/infrastructure/health/test_component_health_gauge.py::
        # test_no_component_failure_shape_reaches_the_health_fallback_body`.
        #
        # Fallback to basic health status
        health_status = {
            "status": "degraded",
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "error": "Enhanced health monitoring unavailable",
            "services": {"session_manager": "unknown", "api": "running"},
        }

    # Add session manager health and metrics
    try:
        if "session_manager" in app.extra:
            session_manager = app.extra["session_manager"]
            session_metrics = session_manager.get_session_metrics()

            # Determine session manager health status
            session_status = "healthy"
            if session_metrics["active_sessions"] > 1000:
                session_status = "degraded"
            elif session_metrics["memory_usage_mb"] > session_manager.max_memory_mb:
                session_status = "degraded"

            health_status["services"]["session_manager"] = {
                "status": session_status,
                "metrics": session_metrics,
            }
    except Exception as e:
        logger.warning(f"Failed to get session manager health: {e}")
        health_status["services"]["session_manager"] = "unknown"

    # Add DI container health if available
    try:
        if "di_container" in app.extra:
            container_instance = app.extra["di_container"]
            if hasattr(container_instance, "health_check"):
                container_health = container_instance.health_check()
                health_status["services"]["di_container"] = container_health["status"]
                health_status["container_components"] = container_health.get(
                    "components", {}
                )

                # Add container initialization status for debugging
                health_status["container_initialized"] = getattr(
                    container_instance, "_initialized", False
                )
                health_status["container_initializing"] = getattr(
                    container_instance, "_initializing", False
                )
    except Exception as e:
        logger.warning(f"Failed to get DI container health: {e}")
        health_status["services"]["di_container"] = "unknown"

    # Investigation tool-calling capability. Normally the startup gate
    # (validate_investigation_tooling) prevents boot on a tool-incapable model,
    # so this is only ever degraded in the explicit ALLOW_TOOLLESS_INVESTIGATION
    # opt-in — surface it so the degraded state stays visible, not just in logs.
    try:
        from .config.investigation_capability import resolve_investigation_capability
        from .config.settings import get_settings
        from .infrastructure.llm.providers.registry import get_registry

        cap = resolve_investigation_capability(get_settings(), get_registry())
        health_status["investigation"] = {
            "tools_available": cap.tool_capable,
            "provider": cap.provider,
            "model": cap.model,
        }
        if not cap.tool_capable:
            health_status["investigation"]["reason"] = cap.reason
            # Only downgrade from a healthy state; never upgrade a worse one.
            if health_status.get("status") == "healthy":
                health_status["status"] = "degraded"
    except Exception as e:
        # Health must never crash on a best-effort capability probe.
        logger.debug(f"Could not resolve investigation capability for health: {e}")

    return health_status


@app.get("/health/dependencies")
async def health_check_dependencies():
    """Enhanced detailed health check for all dependencies with SLA metrics"""
    try:
        from .container import container
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker

        health = container.health_check()

        # Add detailed timing information
        import time

        start_time = time.time()

        # Test each service getter for performance
        service_tests = {}
        services = [
            "agent",
            "data",
            "knowledge",
            "session",
            "llm_provider",
            "sanitizer",
            "tracer",
        ]

        for service_name in services:
            service_start = time.time()
            try:
                service_method = getattr(
                    container,
                    (
                        f"get_{service_name}_service"
                        if service_name in ["agent", "data", "knowledge", "session"]
                        else f"get_{service_name}"
                    ),
                )
                service_instance = service_method()
                service_tests[service_name] = {
                    "available": service_instance is not None,
                    "response_time_ms": round((time.time() - service_start) * 1000, 2),
                }
            except Exception as e:
                logger.warning(f"Service probe failed for {service_name}: {e}")
                service_tests[service_name] = {
                    "available": False,
                    "error": "Service probe failed",
                    "response_time_ms": round((time.time() - service_start) * 1000, 2),
                }

        total_time_ms = round((time.time() - start_time) * 1000, 2)

        # Get enhanced component health data
        component_health_results = await component_monitor.check_all_components()
        dependency_map = component_monitor.get_dependency_map()
        critical_dependencies = component_monitor.get_critical_path_dependencies()

        # Get SLA details for each component
        sla_details = {}
        for component_name in component_health_results.keys():
            try:
                sla_details[component_name] = sla_tracker.get_component_sla_details(
                    component_name
                )
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                sla_details[component_name] = {"error": "SLA details unavailable"}

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "container_health": health,
            "service_tests": service_tests,
            "component_health": {
                component_name: {
                    "status": health.status.value,
                    "response_time_ms": health.response_time_ms,
                    "probe_availability_24h": health.probe_availability_24h,
                    "last_error": health.last_error,
                    "dependencies": health.dependencies,
                    "metadata": health.metadata,
                }
                for component_name, health in component_health_results.items()
            },
            "dependency_mapping": {
                "all_dependencies": dependency_map,
                "critical_dependencies": critical_dependencies,
            },
            "sla_metrics": sla_details,
            "performance": {
                "total_response_time_ms": total_time_ms,
                "container_initialized": getattr(container, "_initialized", False),
                "container_initializing": getattr(container, "_initializing", False),
                "health_check_overhead_ms": round((time.time() - start_time) * 1000, 2),
            },
        }
    except Exception as e:
        logger.error(f"Enhanced dependency health check failed: {e}")
        return {
            "error": "Enhanced dependency health check failed",
            "container_available": False,
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get(
    "/readiness",
    responses={503: {"description": "Not ready to serve traffic"}},
)
async def readiness(response: Response):
    """Readiness probe: 503 when a *readiness-fatal* component is unhealthy.

    This is the endpoint whose status code carries a verdict, and the only
    one — a Kubernetes readiness failure removes the pod from its Service
    without restarting it, which is exactly the action a per-pod fault
    warrants. `/health` deliberately stays 200; see its docstring.

    Only the readiness-fatal set is probed: a component fatal to serving that
    can also fail on **one replica while the others keep serving**. That set
    is **empty today**, so this endpoint currently agrees with `/health` on
    every input — including a database outage, which is fatal but shared, so
    gating on it would empty the Service rather than shed traffic to a
    healthy sibling (#1524). The membership test and the argument for
    `database`'s exclusion live beside the set, in
    `infrastructure/health/component_monitor.py`.

    Every additional dependency in this gate is another way to stop serving
    requests that could have been served, so a component that merely degrades
    the answer — the vector store, the knowledge base, the LLM router — is
    reported at `/health` and does not appear here. Prior to #1515 this
    endpoint pulled the pod when ChromaDB was absent, which pulls a pod that
    can still read cases, accept evidence and authenticate.

    A component we could not determine (UNKNOWN — typically the container has
    not finished wiring) is not treated as unhealthy: "we cannot tell" must
    never be the reason a pod leaves the Service.
    """
    from .infrastructure.health.component_monitor import component_monitor

    try:
        ready, detail = await component_monitor.check_serving_readiness()
    except Exception as e:
        # A probe that cannot run says so, and fails OPEN. A bug in the check
        # must not be able to empty the Service.
        logger.warning(f"Readiness probe failed: {e}")
        return {"status": "ready", "reason": "readiness_check_unavailable"}

    if ready:
        # `checked` is on the ready body too, so an empty readiness-fatal set
        # is self-describing: "nothing was probed, by design" is the correct
        # behaviour today and is otherwise only inferable from an absence.
        return {
            "status": "ready",
            "checked": detail["checked"],
            "components": detail["components"],
        }

    response.status_code = 503
    return {
        "status": "unready",
        "reason": "fatal_component_unhealthy",
        "blocking": detail["blocking"],
        "components": detail["components"],
    }


@app.get("/health/logging")
async def logging_health_check():
    """Get logging system health status."""
    try:
        from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

        coordinator = LoggingCoordinator()
        health_status = coordinator.get_health_status()

        # Add timestamp and additional metadata
        health_status["timestamp"] = to_json_compatible(datetime.now(UTC))
        health_status["service"] = "logging"

        return health_status
    except Exception as e:
        logger.error(f"Logging health check failed: {e}")
        return {
            "status": "error",
            "error": "Logging health check failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "service": "logging",
        }


@app.get("/health/sla")
async def health_check_sla():
    """Get SLA status and metrics for all components."""
    try:
        from .infrastructure.health.sla_tracker import sla_tracker

        sla_summary = sla_tracker.get_sla_summary()

        # Get detailed SLA information for each component
        detailed_sla = {}
        for component_name in sla_tracker.component_thresholds.keys():
            try:
                detailed_sla[component_name] = sla_tracker.get_component_sla_details(
                    component_name
                )
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                detailed_sla[component_name] = {"error": "SLA details unavailable"}

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "summary": sla_summary,
            "components": detailed_sla,
        }

    except Exception as e:
        logger.error(f"SLA health check failed: {e}")
        return {
            "error": "SLA health check failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/health/components/{component_name}")
async def health_check_component(component_name: str):
    """Get detailed health information for a specific component."""
    try:
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker

        # Get component health
        component_health = await component_monitor.check_component_health(
            component_name
        )

        # Get component metrics
        component_metrics = component_monitor.get_component_metrics(component_name)

        # Get SLA details
        try:
            sla_details = sla_tracker.get_component_sla_details(component_name)
        except Exception as e:
            logger.warning(f"Failed to get SLA details for {component_name}: {e}")
            sla_details = {"error": "SLA details unavailable"}

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "component_name": component_name,
            "health": {
                "status": component_health.status.value,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "fatal": component_health.fatal,
                # `fails_per_replica` is NOT repeated here: `metrics` below is
                # `get_component_metrics`, which carries the pair beside each
                # other. Serialising one declaration twice in one body is how
                # the two copies come to disagree.
                "probe_availability_24h": component_health.probe_availability_24h,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata,
            },
            "metrics": component_metrics,
            "sla": sla_details,
        }

    except Exception as e:
        logger.error(f"Component health check failed for {component_name}: {e}")
        return {
            "error": "Component health check failed",
            "component_name": component_name,
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/health/patterns")
async def health_check_error_patterns():
    """Get error patterns and recovery information from enhanced error context."""
    try:
        from .infrastructure.logging.coordinator import LoggingCoordinator

        coordinator = LoggingCoordinator()
        context = coordinator.get_context()

        if context and context.error_context:
            error_context = context.error_context

            return {
                "timestamp": to_json_compatible(datetime.now(UTC)),
                "escalation_level": error_context.escalation_level.value,
                "detected_patterns": error_context.get_pattern_summary(),
                "recovery_summary": error_context.get_recovery_summary(),
                "layer_errors": {
                    layer: {
                        "error_count": info.get("error_count", 0),
                        "severity_score": info.get("severity_score", 0.0),
                        "last_error_time": info.get("last_error_time"),
                    }
                    for layer, info in error_context.layer_errors.items()
                },
            }
        else:
            return {
                "timestamp": to_json_compatible(datetime.now(UTC)),
                "message": "No active error context",
                "patterns": [],
                "recovery_attempts": [],
            }

    except Exception as e:
        logger.error(f"Error patterns health check failed: {e}")
        return {
            "error": "Error patterns health check failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/metrics/performance")
async def get_performance_metrics():
    """Get comprehensive performance metrics."""
    try:
        from .api.middleware.performance import PerformanceMetricsEndpoint
        from .infrastructure.observability.alerting import alert_manager
        from .infrastructure.observability.apm_integration import apm_integration
        from .infrastructure.observability.apm_metrics import metrics_collector

        # Find the performance middleware instance
        performance_middleware = None
        for middleware in app.user_middleware:
            if (
                hasattr(middleware, "cls")
                and middleware.cls.__name__ == "PerformanceTrackingMiddleware"
            ):
                performance_middleware = middleware
                break

        if performance_middleware:
            metrics_endpoint = PerformanceMetricsEndpoint(performance_middleware)
            return await metrics_endpoint.get_performance_metrics()
        else:
            # Return basic metrics if middleware not found
            return {
                "timestamp": to_json_compatible(datetime.now(UTC)),
                "error": "Performance middleware not found",
                "metrics_collector": metrics_collector.get_metrics_summary(),
                "apm_integration": apm_integration.get_export_statistics(),
                "alerting": alert_manager.get_alert_statistics(),
            }

    except Exception as e:
        logger.error(f"Performance metrics endpoint failed: {e}")
        return {
            "error": "Performance metrics failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/metrics/realtime")
async def get_realtime_metrics(time_window_minutes: int = 5):
    """Get real-time performance metrics."""
    try:
        from .infrastructure.observability.alerting import alert_manager
        from .infrastructure.observability.apm_metrics import metrics_collector

        # Validate time window
        if time_window_minutes < 1 or time_window_minutes > 60:
            time_window_minutes = 5

        dashboard_data = metrics_collector.get_dashboard_data(time_window_minutes)
        active_alerts = alert_manager.get_active_alerts()

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "time_window_minutes": time_window_minutes,
            "dashboard": dashboard_data,
            "active_alerts": [
                {
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": to_json_compatible(alert.triggered_at),
                    "message": alert.message,
                }
                for alert in active_alerts[:10]  # Last 10 alerts
            ],
        }

    except Exception as e:
        logger.error(f"Real-time metrics endpoint failed: {e}")
        return {
            "error": "Real-time metrics failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/metrics/alerts")
async def get_alert_status():
    """Get current alert status and statistics."""
    try:
        from .infrastructure.observability.alerting import alert_manager

        active_alerts = alert_manager.get_active_alerts()
        alert_stats = alert_manager.get_alert_statistics()

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "statistics": alert_stats,
            "active_alerts": [
                {
                    "alert_id": alert.alert_id,
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "status": alert.status.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": to_json_compatible(alert.triggered_at),
                    "resolved_at": (
                        to_json_compatible(alert.resolved_at)
                        if alert.resolved_at
                        else None
                    ),
                    "message": alert.message,
                    "notification_count": alert.notification_count,
                }
                for alert in active_alerts
            ],
        }

    except Exception as e:
        logger.error(f"Alert status endpoint failed: {e}")
        return {
            "error": "Alert status failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


@app.get("/metrics/optimization")
async def get_system_optimization_metrics():
    """Get comprehensive system optimization metrics."""
    try:
        # Get resource optimization metrics if available
        resource_metrics = {}
        try:
            from .container import container

            if hasattr(container, "_resource_optimization_service"):
                resource_service = container._resource_optimization_service
                if resource_service and hasattr(
                    resource_service, "get_resource_usage_stats"
                ):
                    resource_metrics = await resource_service.get_resource_usage_stats()
        except Exception as e:
            logger.warning(f"Failed to get resource optimization metrics: {e}")

        # Get LLM router optimization metrics if available
        llm_optimization_metrics = {}
        try:
            from .container import container

            llm_provider = container.get_llm_provider()
            if hasattr(llm_provider, "get_optimization_metrics"):
                llm_optimization_metrics = llm_provider.get_optimization_metrics()
        except Exception as e:
            logger.warning(f"Failed to get LLM optimization metrics: {e}")

        return {
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "resource_optimization": resource_metrics,
            "llm_optimization": llm_optimization_metrics,
            "optimization_summary": {
                "total_optimizations_applied": sum(
                    [
                        resource_metrics.get("optimization_metrics", {}).get(
                            "memory_pools_created", 0
                        ),
                        llm_optimization_metrics.get("requests_batched", 0),
                    ]
                ),
                # `response_compression` and `cache_hit_rate` were reported here
                # from SystemOptimizationMiddleware. They were always 0.0: the
                # middleware's compression and caching sat behind a
                # `hasattr(response, "body")` guard that never holds, because
                # BaseHTTPMiddleware hands `call_next` a `_StreamingResponse`.
                # Reporting a measured-looking zero for a feature that cannot
                # run is worse than not reporting it.
                "performance_improvements": {
                    "memory_pool_efficiency": resource_metrics.get(
                        "memory_pools", {}
                    ).get("efficiency", 0.0),
                    "llm_batching_efficiency": llm_optimization_metrics.get(
                        "optimization_status", {}
                    ).get("batching_enabled", False),
                },
            },
        }

    except Exception as e:
        logger.error(f"System optimization metrics endpoint failed: {e}")
        return {
            "error": "System optimization metrics failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
        }


# POST, and platform-admin only, since #1447. It was an unauthenticated GET.
#
# The METHOD is part of the fix rather than tidiness. This is not a safe
# operation: it drives an aggressive resource cleanup and a full
# ``gc.collect()`` on the running API. A GET says the opposite — it is
# prefetchable by a browser or a link scanner, followable by a proxy, and
# replayable from history — and the one thing an operator lever like this must
# not be is something a caller can trip without meaning to. The bump is MAJOR
# either way and no client sends it (below), so the honest shape costs nothing
# here and would cost a deprecation cycle later.
#
# What it is deliberately NOT: a row in ``operator_access_audit``. That table's
# vocabulary is a closed four — ``LIST`` and ``CONTENT_OPEN`` (operator reads of
# TENANT DATA, the D8/D9 boundary) and ``ROLE_GRANTED``/``ROLE_REVOKED``
# (changes to who is an operator) — and its own docstring reads it as "operator
# events, of which data access is two". This route reads no tenant data and
# grants no role, so recording it would mean inventing a fifth member and
# widening the table from that to "every platform-admin action". Its neighbours
# agree: the four ``require_platform_admin`` routes in
# ``api/routes/admin_config.py`` write no audit row either. Worth revisiting as
# a deliberate decision about what the trail is FOR; not worth deciding as a
# side effect of adding a gate.
#
# The auth gate itself is a router-level dependency rather than a handler
# parameter, matching the OAuth and SSO rate limiters: what this route needs
# from ``require_platform_admin`` is the refusal, not the principal, and an
# unused parameter says otherwise. It emits the same ``security`` either way.
#
# The original finding: it took no auth dependency at all — an
# anonymous caller on a self-hosted deployment, which publishes 8090 on
# 0.0.0.0 with no proxy, could drive an aggressive resource cleanup and a full
# gc.collect() on the running API. Not a disclosure — a write effect, and a
# denial-of-service lever, reachable with no credential.
#
# It is declared on `app` directly rather than on one of the admin routers,
# which is exactly how it escaped the place where require_platform_admin is the
# house rule: "every route is individually responsible for its own auth, and
# nothing tells you when one forgets" is #1447's root, and this route is not a
# session route. The guard installed with it,
# tests/integration/api/test_no_unauthenticated_operations.py, is what named it.
#
# No client calls it: outside this file the only occurrences of the path are the
# generated api.generated.ts declarations in faultmaven-copilot and
# faultmaven-dashboard, and a declaration is what a generator emits, not what a
# client sends.
@app.post(
    "/admin/optimization/trigger-cleanup",
    dependencies=[Depends(require_platform_admin)],
)
async def trigger_system_cleanup():
    """Trigger comprehensive system cleanup and optimization.

    Requires the platform administrator role.
    """
    try:
        cleanup_results = {}

        # Trigger resource optimization cleanup if available
        try:
            from .container import container

            if hasattr(container, "_resource_optimization_service"):
                resource_service = container._resource_optimization_service
                if resource_service:
                    cleanup_results["resource_cleanup"] = (
                        await resource_service.trigger_resource_cleanup(aggressive=True)
                    )
        except Exception as e:
            logger.warning(f"Resource cleanup failed: {e}")
            cleanup_results["resource_cleanup"] = {"error": "Resource cleanup failed"}

        # Trigger manual garbage collection
        import gc

        collected_objects = gc.collect()
        cleanup_results["garbage_collection"] = {
            "objects_collected": collected_objects,
            "memory_freed": True,
        }

        cleanup_results["timestamp"] = to_json_compatible(datetime.now(UTC))
        cleanup_results["cleanup_triggered"] = True

        return cleanup_results

    except Exception as e:
        logger.error(f"System cleanup trigger failed: {e}")
        return {
            "error": "System cleanup failed",
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "cleanup_triggered": False,
        }


if __name__ == "__main__":
    import uvicorn

    # Configuration from unified settings
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    host = settings.server.host
    port = settings.server.port
    reload = settings.server.reload
    workers = settings.server.workers

    # Start server
    # Note: workers parameter is only used if > 1 (uvicorn defaults to 1 worker if not specified)
    # Validation happens in lifespan startup, which will catch invalid configurations
    # access_log=False: uvicorn's plaintext access lines duplicate the
    # structured (JSON) request start/completion logs emitted by
    # LoggingMiddleware — one access log, structured, with correlation IDs.
    if workers > 1:
        uvicorn.run(
            "faultmaven.main:app",
            host=host,
            port=port,
            reload=reload,
            workers=workers,
            log_level="info",
            access_log=False,
            # log_config=None so uvicorn installs no handlers of its own.
            # Its default config gives the `uvicorn` logger a handler and
            # propagate=False, so `uvicorn.error` records -- including the
            # "Exception in ASGI application" traceback, which embeds an HTTP
            # client's exception message and therefore its request URL -- stop
            # at uvicorn's handler and never reach the root logger where the
            # redacting renderer lives. With no config of its own, uvicorn's
            # loggers propagate to root and are rendered like everything else.
            log_config=None,
        )
    else:
        uvicorn.run(
            "faultmaven.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
            access_log=False,
            # log_config=None so uvicorn installs no handlers of its own.
            # Its default config gives the `uvicorn` logger a handler and
            # propagate=False, so `uvicorn.error` records -- including the
            # "Exception in ASGI application" traceback, which embeds an HTTP
            # client's exception message and therefore its request URL -- stop
            # at uvicorn's handler and never reach the root logger where the
            # redacting renderer lives. With no config of its own, uvicorn's
            # loggers propagate to root and are rendered like everything else.
            log_config=None,
        )
