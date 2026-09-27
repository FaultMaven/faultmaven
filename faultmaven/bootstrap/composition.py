"""Composition root: DI container wiring and startup fail-fast gates.

Moved out of ``faultmaven/main.py`` (fm#1707 wave 2) with the ``lifespan``
that calls it (``bootstrap/lifespan.py``) and the middleware setup that
shares its test-environment predicate (``bootstrap/middleware.py``). Lives
in ``faultmaven/bootstrap/`` rather than ``faultmaven/api/`` because
``tests/unit/architecture/test_architecture_boundaries.py`` forbids the API
layer from importing core/infrastructure, and this module does exactly
that on every service it wires onto ``app.state``.

``_wire_composition_root`` does the wiring and raises on any failure;
``compose_application`` decides whether that failure is fatal for this
deployment (see their own docstrings). ``_check_llm_configuration`` is the
startup warning for an unconfigured LLM provider. ``_is_test_environment``
is the shared predicate all three — and ``bootstrap/middleware.py`` — use to
skip service-availability checks under pytest/SKIP_SERVICE_CHECKS.
"""

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)

# Module-level settings cache (set during lifespan startup) — the one
# canonical slot; ``faultmaven.main`` imports it (for ``_is_debug_enabled``)
# rather than declaring its own. ``bootstrap.lifespan.lifespan`` — the only
# writer — does NOT set this copy: a `global` statement always binds to the
# function's defining module, so its write lands on its own module's
# namespace instead (see the comment in bootstrap/lifespan.py). That split is
# not observable in practice: every production call into this module's
# functions passes ``settings`` explicitly, so this slot only ever feeds the
# cold-start fallback (a direct call with no ``settings`` argument), which
# resolves the identical singleton via ``get_settings()`` either way.
_app_settings = None


def _is_test_environment(settings=None) -> bool:
    """Detect if we're running in a test environment (pytest or skip_service_checks)."""
    # Check for pytest in command line arguments
    if "pytest" in " ".join(sys.argv) or any("test" in arg.lower() for arg in sys.argv):
        return True

    # Get settings (from parameter, module cache, or environment)
    if settings is None:
        settings = _app_settings
        if settings is None:
            # Lazy load from get_settings() — handles calls before lifespan
            # runs. If construction itself raises (e.g. an env-var validator
            # rejects an input), degrade to reading the two test-env hints
            # directly so pytest collection doesn't crash on broken envs.
            try:
                from faultmaven.config.settings import get_settings

                settings = get_settings()
            except Exception:
                if os.getenv("SKIP_SERVICE_CHECKS", "").lower() == "true":
                    return True
                if os.getenv("PYTEST_CURRENT_TEST"):
                    return True
                return False

    # Use settings (deployment-agnostic)
    if settings.server.skip_service_checks:
        return True

    if settings.server.pytest_current_test:
        return True

    # Check if we're being imported by pytest
    if "pytest" in sys.modules:
        return True

    return False


def _check_llm_configuration(llm_provider, settings=None) -> None:
    """Check if any LLM provider is configured and print warning if not."""
    # Get settings if not provided
    if settings is None:
        settings = _app_settings
        if settings is None:
            try:
                from faultmaven.config.settings import get_settings

                settings = get_settings()
            except Exception:
                # If settings unavailable, skip check
                return

    # Skip check in test environments
    if _is_test_environment(settings=settings):
        return

    # Check if we have any configured providers
    has_provider = False
    provider_name = None

    # Check common API key environment variables from settings (deployment-agnostic)
    llm_settings = settings.llm
    llm_keys = {
        "OpenAI": (
            llm_settings.openai_api_key.get_secret_value()
            if llm_settings.openai_api_key
            else ""
        ),
        "Anthropic": (
            llm_settings.anthropic_api_key.get_secret_value()
            if llm_settings.anthropic_api_key
            else ""
        ),
        "Fireworks": (
            llm_settings.fireworks_api_key.get_secret_value()
            if llm_settings.fireworks_api_key
            else ""
        ),
        "Groq": (
            llm_settings.groq_api_key.get_secret_value()
            if llm_settings.groq_api_key
            else ""
        ),
        "Gemini": (
            llm_settings.gemini_api_key.get_secret_value()
            if llm_settings.gemini_api_key
            else ""
        ),
    }

    for name, key in llm_keys.items():
        if key and not key.startswith("your_") and key != "":
            has_provider = True
            provider_name = name
            break

    # Check for local LLM configuration from settings
    chat_provider = llm_settings.provider.value.lower() if llm_settings.provider else ""
    if chat_provider == "local":
        # Note: LOCAL_LLM_URL would need to be added to LLMSettings if used
        # For now, checking provider setting is sufficient
        has_provider = True
        provider_name = "Local (Ollama)"

    if has_provider:
        logger.info(f"✅ LLM Provider configured: {provider_name}")

        # NOTE: the investigation model's tool-calling capability is enforced by
        # the fail-fast gate in config/investigation_capability.py
        # (validate_investigation_tooling), called earlier in the lifespan — it
        # refuses to boot on a tool-incapable investigation model unless
        # ALLOW_TOOLLESS_INVESTIGATION is set (then it warns + /health reports
        # degraded). Kept there, not here, so there is a single source of truth.
    else:
        # Print prominent warning banner
        banner = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                        ⚠️  NO LLM PROVIDER CONFIGURED                         ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  FaultMaven requires an LLM provider to function. Please configure one:       ║
║                                                                               ║
║  Option 1: Cloud Provider (in your .env file)                                 ║
║    OPENAI_API_KEY=sk-...                                                      ║
║    ANTHROPIC_API_KEY=sk-ant-...                                               ║
║                                                                               ║
║  Option 2: Local LLM (Ollama)                                                 ║
║    CHAT_PROVIDER=local                                                        ║
║    LOCAL_LLM_URL=http://localhost:11434                                       ║
║    LOCAL_LLM_MODEL=llama3.1                                                   ║
║                                                                               ║
║  See: https://github.com/FaultMaven/faultmaven#quick-start                    ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
        logger.warning(banner)


async def _wire_composition_root(app: FastAPI, settings: "FaultMavenSettings") -> None:
    """Initialize the DI container and wire every service onto ``app.state``.

    Raises on any failure — deliberately. Whether a failure here is fatal is
    one decision, and it belongs to :func:`compose_application`, not to the
    wiring steps: a step that decides for itself is how a cloud pod ends up
    serving an API with whole service layers missing.
    """
    from faultmaven.container import container

    await container.initialize()

    # Verify critical services are available IMMEDIATELY after initialization
    user_store = container.get_user_store()
    # Every revoke path depends on this store (#767/#769), so a missing
    # registration is fatal rather than something to discover on the first
    # logout. Presence only — this does NOT probe connectivity. The database
    # arm is probed a few lines below by `validate_revocation_storage`; the
    # Redis arm is not, so a dead Redis still boots and surfaces per-request,
    # where every path now fails CLOSED (#1478): the request path refuses with
    # 503 REVOCATION_STATE_UNKNOWN, generator validation refuses too.
    token_revocation_store = container.get_service("token_revocation_store")

    logger.info(
        "Container initialization complete. Checking authentication services..."
    )
    logger.info(
        f"   - user_store: {type(user_store).__name__ if user_store else 'None'}"
    )
    logger.info(
        "   - token_revocation_store: "
        f"{type(token_revocation_store).__name__ if token_revocation_store else 'None'}"
    )

    if user_store is None or token_revocation_store is None:
        logger.error(
            "❌ Critical authentication services missing after container initialization:"
        )
        logger.error(f"   - user_store: {user_store}")
        logger.error(f"   - token_revocation_store: {token_revocation_store}")
        logger.error(f"   - Container initialized: {container.is_initialized}")
        logger.error(
            f"   - Container has user_store attr: {hasattr(container, 'user_store')}"
        )
        if hasattr(container, "user_store"):
            logger.error(f"   - Container.user_store value: {container.user_store}")
        raise RuntimeError(
            "Container initialization incomplete: authentication services not available. "
            "Check container initialization logs for errors during register_infrastructure()."
        )

    logger.info("✅ DI container initialized successfully with authentication services")

    # Make container available to app for access by other components
    app.extra["di_container"] = container

    # ============================================================
    # Bootstrap Application (deployment-agnostic architecture)
    # ============================================================
    # Ensures default organization exists for single-tenant mode
    # Must run after container initialization (requires tenant_provider)
    # Must run after container initialization (requires tenant_provider)
    try:
        from faultmaven.bootstrap.startup import bootstrap_application

        await bootstrap_application(container)
        logger.debug("✅ Application bootstrap complete")

        # Apply config overrides from database (cloud mode only).
        # Standalone uses .env as the sole source of truth.
        is_cloud = settings.is_cloud
        if is_cloud:
            try:
                from faultmaven.config.llm_config_overrides import (
                    apply_overrides_to_settings,
                    watch_config_version,
                )

                await apply_overrides_to_settings(settings)
                logger.debug("✅ Config overrides applied (cloud mode)")

                # Multi-replica propagation: a UI config write hot-reloads
                # only the serving replica; this watcher reloads the others
                # when the shared config version changes. Cancelled on
                # shutdown. Cloud-only — standalone has no DB overrides.
                app.state.llm_config_watch_task = asyncio.create_task(
                    watch_config_version()
                )
            except Exception as e:
                logger.debug(f"Config overrides skipped: {e}")
        else:
            logger.debug(
                "Config overrides skipped (local mode — .env is source of truth)"
            )
    except Exception as e:
        logger.critical(
            f"🔥 BLOCKING STARTUP FAILURE: Application bootstrap failed: {e}"
        )
        # FAIL FAST: Re-raise to stop startup.
        # A broken bootstrap means DB or critical directories are missing.
        raise RuntimeError(f"Critical bootstrap failure: {e}") from e

    # Multi-tenant hard gate: refuse to serve if the app's PostgreSQL role is
    # exempt from RLS. Superusers and table owners bypass row-level security,
    # so a misprovisioned role would silently defeat tenant isolation (the
    # policies from migrations 018/023/030 become no-ops). Runs after
    # bootstrap so the DB + RLS policies are guaranteed present; no-op in
    # single-tenant mode and on SQLite.
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_app_db_role_enforces_rls,
    )
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    await assert_app_db_role_enforces_rls(
        is_multi_tenant=(requested_tenant_provider() == BUILTIN_MULTI)
    )

    # Can the resolved revocation store reach its storage? Presence was checked
    # at composition; this asks whether the table is there (#828). Since #1478
    # the request-path check fails CLOSED, so a missing `token_revocations` is
    # no longer a silently-disabled control — it is every authenticated request
    # answering 503. Refusing the boot is still the right answer, and for a
    # better reason: the condition is permanent (one migration, already
    # stamped; `alembic upgrade head` is a no-op), so a pod that came up would
    # serve nothing but 503s while reporting itself healthy.
    #
    # AFTER bootstrap, for the same reason the RLS guard above is: bootstrap is
    # what runs the migrations (and creates `data/` in the first place), so a
    # probe before it fails on every FIRST-EVER install — a brand-new Quick
    # Start would refuse to boot, advised to re-provision a deployment that had
    # never run (#828 delta review). Ordering is the whole of this gate's
    # correctness, which is why it has a startup-sequence test and not only a
    # direct-call one.
    if not _is_test_environment(settings):
        from faultmaven.config.revocation_storage import validate_revocation_storage

        await validate_revocation_storage(token_revocation_store)

    # ============================================================
    # Composition Root: Attach all services to app.state
    # ============================================================
    # This follows the Composition Root principle (P5):
    # - Services are wired here at startup
    # - FastAPI dependencies access via request.app.state
    # - Services do NOT call container.get_*() themselves
    # ============================================================

    # CRITICAL: Set authentication services FIRST - they're required for the API to work
    # These were already verified above, so they must be available
    # Deployment-wide token revocation store (#767): the single store all
    # revoke paths write to and the request-path check reads from.
    app.state.token_revocation_store = token_revocation_store
    app.state.user_store = user_store
    app.state.user_service = container.get_user_service()
    app.state.auth_service = container.get_auth_service()
    app.state.oauth_service = container.get_oauth_service()  # OAuth service (optional)
    # SSO hosted-login orchestration (ADR-015). None unless WorkOS is fully
    # configured in oauth mode; the SSO router only mounts in that case.
    app.state.sso_login_service = container.get_service("sso_login_service")
    # RS256 token generator for oauth-mode /auth/refresh (ADR-015 D6).
    # None in local mode, where refresh builds its HS256 generator per
    # request instead.
    app.state.jwt_token_generator = container.get_service("jwt_token_generator")

    # Durable, append-only operator access trail (ADR-012 D8/D9). Wired
    # beside the auth services rather than in the "may fail gracefully"
    # block below: the operator routes fail closed without it, which is the
    # intended behaviour — an unrecorded cross-tenant read is the failure
    # this table exists to prevent.
    from faultmaven.infrastructure.persistence.sessionless_operator_audit_repository import (  # noqa: E501
        SessionlessOperatorAuditRepository,
    )

    app.state.operator_audit_repository = SessionlessOperatorAuditRepository()

    # Break-glass grants over Cloud tenant case content (ADR-012 D9, #815).
    # Same posture as the audit trail above: without it the content path
    # fails closed rather than degrading to standing access.
    from faultmaven.infrastructure.persistence.sessionless_operator_grant_repository import (  # noqa: E501
        SessionlessOperatorGrantRepository,
    )

    app.state.operator_grant_repository = SessionlessOperatorGrantRepository()

    # Shared Redis client (real Redis in cloud, FakeRedis in standalone).
    # Single source of truth for Redis-dependent middleware (deduplication,
    # idempotency), which resolve it lazily from app.state on first request —
    # after this composition root has run. The container guarantees a working
    # client (never None), so this is always populated.
    app.state.redis_client = container.get_redis_client()

    # Refuse to serve if another process in this deployment redacts under a
    # different key. Resolution alone cannot establish that — whether a
    # generated key is shared is a property of the topology, which the app
    # cannot see, and the predicate it used to infer from (DEPLOYMENT_MODE)
    # is one an operator can simply not set. On-prem does not, so a
    # multi-replica Deployment took the standalone path and minted a key per
    # pod, silently. Redis is the one store every replica genuinely shares, so
    # the invariant is checked there instead of guessed at.
    from faultmaven.infrastructure.security.pseudonym_key import (
        resolve_pseudonym_key,
        verify_pseudonym_key_agreement,
    )

    await verify_pseudonym_key_agreement(
        resolve_pseudonym_key(settings), app.state.redis_client
    )

    # The rest of the service layer. Genuinely optional services (the
    # conversion service, the query classification engine) name themselves
    # optional at their own line and fall back to None; everything else
    # raises out of this function, because a service missing from app.state
    # is not a degraded feature — it is a route that 500s on first use, or a
    # gate that never runs. Which failures are survivable is the caller's
    # decision (compose_application), not a blanket except here.
    app.state.session_service = container.get_session_service()
    app.state.case_service = container.get_case_service()
    app.state.investigation_service = container.get_investigation_service()

    # knowledge_service is the one exception to the paragraph above, and it
    # names itself here. Since #899 the container returns None rather than
    # substituting a stub that fabricated documents, so this slot CAN be empty
    # — the KB routes then answer 503 instead of 500ing per request. Log it at
    # the assignment: the only other line that names the condition sits in the
    # KB-bootstrap branch, which is skipped entirely under TENANT_PROVIDER=
    # multi, so a cloud pod would otherwise start clean and stay green with no
    # knowledge base at all. Not raised, because must_not_degrade already
    # refused composition for every deployment that must not degrade; what is
    # left here is the self-hosted instance whose operator reads the log.
    app.state.knowledge_service = container.get_knowledge_service()
    if app.state.knowledge_service is None:
        logger.error(
            "No knowledge service was composed — the knowledge base is "
            "unavailable for this process. Every /knowledge route will answer "
            "503, KB retrieval is absent from investigations, and KB "
            "bootstrap/seeding cannot run."
        )

    # Knowledge suggestion service — the case → KB write side (#1214).
    #
    # This slot was READ by two routes and WRITTEN by none, so both silently
    # built a fresh, collaborator-less SuggestionService per request: the
    # suggestion an extract created lived in that instance's private store and
    # was gone by the time approve looked for it (404). Wired here, next to the
    # knowledge service it depends on.
    #
    # Follows knowledge_service's precedent for the empty case: logged, not
    # raised, and the routes answer 503. Composing one without a knowledge
    # service is impossible by construction — the factory takes it — so this is
    # empty only when the knowledge service itself is.
    app.state.suggestion_service = container.get_suggestion_service()
    if app.state.suggestion_service is None:
        logger.error(
            "No knowledge suggestion service was composed — extracting "
            "knowledge from a case and approving a suggestion will both answer "
            "503 for this process."
        )

    # Document-to-runbook conversion service. Composed in the container,
    # before the engine that calls it (#1722); the lifespan only ensures its
    # tables exist (a no-op when the baseline migration created them). A
    # failure here disables the conversion routes, as it always has.
    app.state.conversion_service = getattr(container, "conversion_service", None)
    if app.state.conversion_service is not None:
        try:
            from faultmaven.infrastructure.persistence.database import get_engine
            from faultmaven.infrastructure.persistence.models import (
                ConversionDraftModel,
                ConversionJobModel,
            )

            _conv_engine = get_engine()
            async with _conv_engine.begin() as _conn:
                await _conn.run_sync(
                    ConversionJobModel.__table__.create,
                    checkfirst=True,
                )
                await _conn.run_sync(
                    ConversionDraftModel.__table__.create,
                    checkfirst=True,
                )
            logger.info("✅ Document conversion service initialized")
        except Exception as conv_err:
            logger.warning(
                f"Document conversion service not available: {conv_err}",
                exc_info=True,
            )
            app.state.conversion_service = None

    # The composed web-search tool, or None when the registry did not register
    # one (disabled by ENABLE_WEB_SEARCH, no provider key, or construction
    # raised). Published so /admin/config/status can report the tool THIS
    # PROCESS actually holds rather than re-deriving from settings whether one
    # would compose — the same reason `suggestion_service` is reachable here
    # (#1227, #1234). A settings-derived answer reports a capability the model
    # does not have whenever startup composition failed.
    app.state.web_search_tool = getattr(container, "web_search_tool", None)
    app.state.preprocessing_service = container.get_preprocessing_service()
    app.state.enhanced_agent_service = container.get_enhanced_agent_service()
    app.state.orchestration_service = container.get_orchestration_service()
    app.state.data_service = container.get_data_service()
    app.state.tenant_provider = container.get_tenant_provider()
    # Organization rows (the tenant substrate, ADR-010 D4). Management is the
    # hosted admin composed module; the core exposes the repository so read
    # paths like the /auth/me tenant label resolve it through DI.
    app.state.organization_repository = container.get_organization_repository()
    # KB team-scope resolver (None in standalone — team collaboration is
    # a Cloud feature; the KB inventory route reads this off app.state).
    app.state.team_service = container.get_team_service()
    # Resource-share source of truth (ADR-013 §D4). Present in both modes;
    # the agent retrieval path resolves the shared-id allowlist through it.
    app.state.share_repository = getattr(container, "share_repository", None)
    app.state.report_generation_service = container.get_report_generation_service()
    app.state.job_service = container.get_job_service()
    # Query classification engine (optional - may not be available)
    try:
        app.state.query_classification_engine = (
            container.get_query_classification_engine()
        )
    except AttributeError:
        logger.warning("Query classification engine not available - skipping")
        app.state.query_classification_engine = None
    app.state.tracer = container.get_tracer()
    app.state.llm_provider = container.get_llm_provider()
    logger.info("✅ Services attached to app.state (Composition Root)")

    # Check LLM provider configuration and warn if none configured
    _check_llm_configuration(app.state.llm_provider, settings=settings)


async def compose_application(app: FastAPI, settings: "FaultMavenSettings") -> None:
    """Compose the application, refusing to start where a partial API is unsafe.

    Deployments that must not degrade (``settings.must_not_degrade`` — cloud,
    or any deployment declaring ``ENVIRONMENT=production``) abort startup, so
    uvicorn exits, the pod CrashLoops and the rollout rolls back. Anywhere
    else a partial application is a development affordance and startup
    continues with a warning.
    """
    try:
        await _wire_composition_root(app, settings)
    except RuntimeError:
        # The container's established fail-fast channel — the cloud refusal in
        # ``DIContainer.initialize``, the bootstrap failure, the RLS role gate.
        # Those callees have already decided the boot cannot continue, in any
        # deployment mode, so this handler must not reinterpret them.
        raise
    except Exception as e:
        if settings.must_not_degrade:
            # Unwrapped: `server.environment` holds the Enum member, and a bare
            # str() would log "Environment.STAGING" at an operator (#827).
            env = getattr(
                settings.server.environment, "value", settings.server.environment
            )
            logger.critical(
                "FAIL-FAST: composition root failed under "
                f"DEPLOYMENT_MODE={'cloud' if settings.is_cloud else 'standalone'}"
                f"/ENVIRONMENT={env}. Refusing to serve a partial API."
            )
            raise RuntimeError(
                f"Composition root failed: {e}. A partially wired application "
                "would serve an API missing whole service layers."
            ) from e
        logger.error(f"Composition root failed: {e}", exc_info=True)
        logger.warning(
            "Continuing with fallback service implementations — this deployment "
            "permits a partial API (see settings.must_not_degrade)"
        )
