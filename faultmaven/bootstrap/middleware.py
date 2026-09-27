"""Middleware stack assembly, and the Opik SDK/middleware availability probe.

Moved out of ``faultmaven/main.py`` (fm#1707 wave 2). ``setup_middleware``
took no parameter and read the module-global ``app`` in ``main.py``; here it
takes ``app`` as a parameter — the one behaviour-preserving signature change
this move requires, since ``bootstrap/`` modules must not import
``faultmaven.main`` (that would cycle against ``main.py`` importing this
module). ``faultmaven.main`` still calls ``setup_middleware(app)`` at
import time, at the same point it always has.

The Opik availability probe lives here, rather than in ``main.py`` or
``bootstrap/composition.py``, because ``setup_middleware`` is its only
reader: it decides whether ``OpikMiddleware`` is added to the stack.
"""

import logging

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.bootstrap.composition import _is_test_environment
from faultmaven.utils.optional_dependency import module_is_usable

logger = logging.getLogger(__name__)


# Optional Opik middleware import.
#
# ``import opik`` succeeding is not proof the SDK is installed — see
# faultmaven/utils/optional_dependency.py. Here that decides only what gets
# logged: without it this reports "Opik SDK available but middleware not found"
# instead of "Opik not available". (The OpikMiddleware import below is a
# from-import and already fails correctly, so the middleware was never at risk.)
try:
    import opik

    OPIK_AVAILABLE = module_is_usable(opik)
    # See tracing.py for why no `attr` is passed.
    _opik_reason = None if OPIK_AVAILABLE else "shadowed by a namespace package"
except ImportError:
    OPIK_AVAILABLE = False
    _opik_reason = "not installed"

OPIK_MIDDLEWARE_AVAILABLE = False
if OPIK_AVAILABLE:
    try:
        from opik.integrations.fastapi import OpikMiddleware

        OPIK_MIDDLEWARE_AVAILABLE = True
    except ImportError:
        logger.debug(
            "Opik middleware class not available, tracing will work without middleware"
        )
else:
    # Name WHICH cause: an operator staring at an empty site-packages/opik/
    # tree and one who never installed the extra need different fixes, and
    # this is the only line here that tells them apart.
    logger.info("Opik not available (%s), running without tracing", _opik_reason)


def _assert_cors_outermost(target_app) -> None:
    """Refuse to run with anything stacked outside CORS.

    Starlette wraps in reverse registration order, so ``user_middleware[0]`` is
    the last-registered and therefore outermost layer. CORS has to be exactly
    that layer: a middleware registered after it sits *outside* it, and any
    response that layer short-circuits (a 429, a 503, a rejected upload) leaves
    without CORS headers — the browser then reports a network error and the
    caller never sees the status code it was supposed to act on. Two CORS layers
    are the same defect from the other side: the outer one answers, so the
    inner's configuration is silently dead.

    A plain ``raise``, never ``assert``: assertions vanish under ``-O`` and a
    guard that disappears in the configuration most likely to be a production
    one is not a guard. Unconditional too — no environment gate. The reduced
    test-env stack skips several registrations, so an ordering test written
    against it cannot see production's stack; this runs inside
    ``setup_middleware`` itself, on whatever stack that environment actually
    built, and fails the import that produced a bad one.
    """
    cors_layers = [m for m in target_app.user_middleware if m.cls is CORSMiddleware]
    if len(cors_layers) != 1 or target_app.user_middleware[0].cls is not CORSMiddleware:
        raise RuntimeError(
            "CORS must be the single outermost middleware; stack: "
            f"{[m.cls.__name__ for m in target_app.user_middleware]}"
        )


def setup_middleware(app):
    """Setup middleware - only log when not in test mode"""
    import sys

    from faultmaven.config.settings import get_settings

    settings = get_settings()

    # Is this a DEPLOYED box? Asked ONCE, here, and used by both decisions in
    # this function that turn on it — the protection carve-out below (does a
    # setup failure refuse the boot, or boot unprotected?) and the CORS branch
    # near the end. They are ~200 lines apart, they had drifted apart, and the
    # drift was fm#985 item 17.
    #
    # ``is_deployed_environment`` rather than ``settings.is_development()``
    # alone: ``DEPLOYMENT_MODE=cloud`` with ``ENVIRONMENT=development`` is a
    # reachable configuration — nothing relates the two — and it is the worst
    # shape to hand a development policy to. One import, so the composition
    # root, ``api.protection`` and their tests cannot answer it differently.
    from faultmaven.config.protection import is_deployed_environment

    deployed = is_deployed_environment(
        settings.server.environment, is_cloud_deployment=settings.is_cloud
    )

    # Skip verbose logging during test collection
    if settings.server.pytest_current_test or "pytest" in sys.modules:
        logging_enabled = False
    else:
        logging_enabled = True

    if logging_enabled:
        logger.info("Starting middleware registration...")
        logger.info(
            f"Initial middleware stack: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 1. Trailing slash middleware (prevents 307 redirects)
    try:
        from faultmaven.api.middleware.trailing_slash import TrailingSlashMiddleware

        app.add_middleware(TrailingSlashMiddleware)
        if logging_enabled:
            logger.info("✅ Trailing slash middleware added")
    except Exception as e:
        logger.warning(f"Failed to add trailing slash middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After trailing slash middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 2. Idempotency middleware (before protection) — skip when SKIP_SERVICE_CHECKS
    try:
        if not settings.server.skip_service_checks:
            from faultmaven.api.middleware.idempotency import IdempotencyMiddleware

            # No client injected here: this runs at import time, before the
            # lifespan creates Redis. The middleware resolves the client lazily
            # from app.state (wired by the composition root) on the first request.
            app.add_middleware(IdempotencyMiddleware)
            if logging_enabled:
                logger.info("✅ Idempotency middleware added")
        else:
            if logging_enabled:
                logger.info(
                    "Skipping Idempotency middleware (SKIP_SERVICE_CHECKS=True)"
                )
    except Exception as e:
        logger.warning(f"Failed to add idempotency middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After Idempotency middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 3. Request ID middleware - skip in test environments
    #
    # Correlation only. No rate-limit headers are registered here: the
    # enforcement layers (RateLimitMiddleware, the OAuth limiter dependencies)
    # are the single authority for those, because they are the only components
    # that know what was actually enforced.
    try:
        if not settings.server.skip_service_checks and not _is_test_environment():
            from faultmaven.api.middleware.request_id import RequestIdMiddleware

            # Add Request ID middleware
            app.add_middleware(RequestIdMiddleware)

            if logging_enabled:
                logger.info("✅ Request ID middleware added")
        else:
            if logging_enabled:
                logger.info(
                    "Skipping Request ID middleware (test environment or SKIP_SERVICE_CHECKS=True)"
                )

    except Exception as e:
        logger.warning(f"Failed to add request ID middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After Request ID middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 4. Protection middleware (early in stack for security)
    try:
        from faultmaven.api.protection import setup_protection_middleware

        # Deliberately *not* gated on ``skip_service_checks`` (fm#990).
        #
        # That flag means "do not require external services": it tells the DI
        # container not to create stores it would otherwise health-check, and
        # Redis degrades to the in-process FakeRedis instead. Protection needs
        # no external service — the limiter and the deduplicator resolve their
        # client from ``app.state``, which is populated either way — so the
        # flag's contract never covered them.
        #
        # The measured consequence was a CI blind spot: every pytest job sets
        # the flag in its workflow ``env`` (as does ``scripts/tests.py``), so
        # the app under test carried ``[CORS, Logging, GZip, TrailingSlash]``
        # and nothing more, and no job would have noticed the limiter being
        # deleted. No deployment sets the flag today — it appears in CI and test
        # tooling only — so the "boots unprotected" reading of this gate was
        # latent rather than live. It was still the shape fm#1023 closed for
        # ``staging``, reachable through one more door, which is why the gate
        # is removed rather than narrowed.
        protection_info = setup_protection_middleware(
            app,
            environment=settings.server.environment,
            # ADR-004's single source of truth for "am I standalone or cloud?",
            # and since fm#1566 the axis the Redis degrade policy keys on: a
            # cloud fleet pins fail-closed, a self-hosted deployment fails open
            # and keeps its per-replica FakeRedis stand-in rung. Passed from
            # the resolved settings rather than re-read from the environment,
            # so protection cannot disagree with auth, storage and tenancy
            # about which deployment this is.
            is_cloud_deployment=settings.is_cloud,
        )
        if logging_enabled:
            if protection_info.get("protection_enabled"):
                middleware_names = protection_info.get("middleware_added", [])
                logger.info(f"✅ Protection middleware enabled: {middleware_names}")
            else:
                logger.info("ℹ️ Protection middleware disabled")
        app.extra["protection_info"] = protection_info
    except Exception as e:
        # Never gated on ``logging_enabled``: a swallowed setup failure must not
        # be a zero-output event. Under the carve-out below this line is the only
        # trace that the app is running unprotected.
        logger.warning(f"Failed to setup protection middleware: {e}")
        # The carve-out, named explicitly: **a development checkout only** —
        # which is also what an unset ``ENVIRONMENT`` reads as — deliberately
        # boots unprotected-with-a-warning when protection setup fails, so a
        # broken local config does not block iteration. Every deployed box
        # (``staging``, ``production``, any unrecognised value, and any cloud
        # deployment however it names its environment) refuses to boot,
        # re-muting the raise ``api/protection.py`` makes for exactly one
        # audience rather than for all of them.
        #
        # ``deployed`` rather than ``not settings.is_development()``: a cloud
        # fleet naming ``ENVIRONMENT=development`` would otherwise have been
        # carved out of the refusal as well, which is the one deployment where
        # serving unprotected is least acceptable.
        if deployed:
            raise

    if logging_enabled:
        logger.info(
            f"After Protection middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 5. GZip middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if logging_enabled:
        logger.info(
            f"After GZip middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 6. New unified logging middleware (integrates with Phase 1 & 2 infrastructure)
    if logging_enabled:
        logger.info("Adding LoggingMiddleware to FastAPI app")
    app.add_middleware(LoggingMiddleware)
    if logging_enabled:
        logger.info(
            f"After LoggingMiddleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 7. Performance tracking middleware (Phase 2 enhancement)
    from faultmaven.api.middleware.performance import PerformanceTrackingMiddleware

    if not settings.server.skip_service_checks:
        if logging_enabled:
            logger.info("Adding PerformanceTrackingMiddleware to FastAPI app")
        # Same trusted-proxy list the limiter keys on, from the same single
        # reader, so the address a request is *labelled* with and the address
        # it is *limited* by cannot disagree. Building a whole
        # ProtectionSettings here just to read one field would give the trust
        # policy a second source; ``get_trusted_proxies`` is the one the presets
        # call too.
        from faultmaven.config.protection import get_trusted_proxies

        app.add_middleware(
            PerformanceTrackingMiddleware,
            service_name="faultmaven_api",
            trusted_proxies=get_trusted_proxies(),
        )
        if logging_enabled:
            logger.info(
                f"After PerformanceTrackingMiddleware: {[type(m).__name__ for m in app.user_middleware]}"
            )
    else:
        if logging_enabled:
            logger.info(
                "Skipping PerformanceTrackingMiddleware (SKIP_SERVICE_CHECKS=True)"
            )

    # 9. Opik tracing middleware (if available) - skip in test environments
    if (
        OPIK_AVAILABLE
        and OPIK_MIDDLEWARE_AVAILABLE
        and not settings.server.skip_service_checks
        and not _is_test_environment()
    ):
        if logging_enabled:
            if settings.observability.opik_use_local:
                logger.info("Adding OpikMiddleware for local Opik instance")
            else:
                logger.info("Adding OpikMiddleware for cloud instance")
        app.add_middleware(OpikMiddleware)
        if logging_enabled:
            logger.info(
                f"After Opik middleware: {[type(m).__name__ for m in app.user_middleware]}"
            )
    elif OPIK_AVAILABLE and logging_enabled:
        if settings.server.skip_service_checks or _is_test_environment():
            logger.info(
                "Skipping OpikMiddleware (test environment or SKIP_SERVICE_CHECKS=True)"
            )
        else:
            logger.info(
                "Opik SDK available but middleware not found - tracing will work at function level"
            )

    # 10. Contract Probe middleware (for API compliance monitoring)
    if not settings.server.skip_service_checks and not _is_test_environment():
        try:
            from faultmaven.api.middleware.contract_probe import ContractProbeMiddleware

            app.add_middleware(
                ContractProbeMiddleware,
                probe_enabled=True,
                log_all_requests=False,  # Only log violations, not all requests
                failure_sample_rate=1.0,
            )
            if logging_enabled:
                logger.info(
                    "✅ Contract Probe middleware added for API compliance monitoring"
                )
        except Exception as e:
            logger.warning(f"Failed to add contract probe middleware: {e}")

    # 10b. Request body size limit — registered immediately before CORS, so it
    # sits just INSIDE the outermost layer.
    #
    # Position is load-bearing in both directions. Inside CORS, so a refused
    # request still carries the CORS headers and the Dashboard sees a real 413
    # rather than an opaque network error. Outside `DeduplicationMiddleware` and
    # `IdempotencyMiddleware`, both of which `await request.body()` — registered
    # inside them, this would refuse the body only after they had buffered it.
    #
    # Unconditional: not behind `_is_test_environment()` or `SKIP_SERVICE_CHECKS`
    # the way several neighbours are, because a guard the test application does
    # not mount is a guard with no test.
    from faultmaven.api.middleware.body_size import RequestBodySizeLimitMiddleware

    app.add_middleware(RequestBodySizeLimitMiddleware)
    if logging_enabled:
        logger.info(
            "✅ Request body size limit: %sMB",
            settings.upload.max_upload_size_mb,
        )

    # 11. CORS middleware — registered LAST, which makes it the OUTERMOST layer.
    #
    # Starlette wraps in reverse registration order, so the last middleware
    # added is the first to see a request and the last to touch a response.
    # That placement is load-bearing rather than cosmetic, and it buys two
    # things:
    #
    # - Every short-circuit response from every inner layer carries CORS
    #   headers, from this one CORS authority. Registered first (innermost),
    #   CORS only ever saw responses the route itself produced: the rate
    #   limiter's 429, its fail-closed 503 and its dispatch catch-all 503 were
    #   all synthesized above it and travelled straight past, reaching a
    #   cross-origin caller with no ``Access-Control-Allow-Origin`` — so the
    #   browser refused the response and the Copilot/Dashboard saw an opaque
    #   network error instead of "you are being rate limited".
    # - Preflight OPTIONS is answered here, before rate limiting or logging see
    #   it at all. Innermost, a client whose limit was already tripped had its
    #   *preflight* refused with a 429, so the real request was never sent and
    #   the limit could not even report itself.
    #
    # The corollary: no inner middleware needs (or should grow) its own OPTIONS
    # special-case or its own copy of the CORS configuration. Two CORS
    # authorities can disagree; one cannot.
    #
    # ⚠️ ACCEPTED, NOT OVERLOOKED: preflight OPTIONS are therefore UNMETERED and
    # invisible in-process — no rate limiting, no request log line, no metric
    # (fm#985 item 10). The disposition is deliberate: a preflight touches no
    # application code, the ingress in front of a deployed box already sees and
    # can limit it, and a second meter for a request class the application
    # never executes is cost with no consumer. A deployment that needs preflight
    # visibility gets it from the ingress. Stated for operators in
    # docs/operations/security/client-protection.md § "Preflight OPTIONS are
    # not metered in-process".
    #
    # Use configurable origins from settings - deployed environments should
    # specify concrete origins (e.g. chrome-extension://abc123) rather than
    # wildcards.
    cors_origins = list(settings.security.cors_allow_origins)

    # Which CORS policy a box runs is decided by ONE question — is this a
    # deployed box? — and `deployed`, resolved once at the top of this
    # function, is the whole of it. The three branches below used to ask
    # `== Environment.PRODUCTION` instead, the "only production is special"
    # pattern fm#1023 removed from protection routing. `ENVIRONMENT=staging`
    # therefore ran production's strict rate limits AND development's CORS at
    # the same time: a wildcard origin accepted, localhost appended, and the
    # RFC1918 `allow_origin_regex` installed with `allow_credentials` on — so
    # any host on any private network could make credentialed calls to a
    # deployed box (fm#985 item 17). Staging is classified as deployed, the
    # same as production, and so is a cloud deployment whatever it names its
    # environment.

    # SECURITY: Fail-fast validation - no wildcards allowed on a deployed box
    if deployed:
        wildcard_origins = [o for o in cors_origins if "://*" in o]
        if wildcard_origins:
            # Unwrapped: `server.environment` holds the Enum member, and a bare
            # str() would put "Environment.STAGING" in front of an operator
            # (#827) — in the one message that tells them what to fix.
            named_environment = getattr(
                settings.server.environment, "value", settings.server.environment
            )
            raise RuntimeError(
                f"SECURITY ERROR: Wildcard CORS origins are not allowed on a deployed "
                f"environment (ENVIRONMENT={named_environment}): {wildcard_origins}. "
                "Configure CORS_ALLOW_ORIGINS with specific extension IDs "
                "(e.g., chrome-extension://abc123def456)."
            )

    # Add production domain if not already present
    if "https://faultmaven.ai" not in cors_origins:
        cors_origins.append("https://faultmaven.ai")

    # Development only: dynamic CORS for local network access
    if not deployed:
        # Add common development origins if not already present
        for dev_origin in [
            "http://localhost:3333",
            "http://localhost:8090",
            "http://localhost:5173",
        ]:
            if dev_origin not in cors_origins:
                cors_origins.append(dev_origin)

        # Add regex pattern for local network IPs (RFC 1918 private networks)
        # This allows dashboard access from phones/tablets on local network
        local_network_regex = (
            r"^https?://"
            r"("
            r"localhost|127\.0\.0\.1|"  # Localhost
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # Class A: 10.0.0.0/8
            r"172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}|"  # Class B: 172.16.0.0/12
            r"192\.168\.\d{1,3}\.\d{1,3}"  # Class C: 192.168.0.0/16
            r")"
            r"(:\d+)?$"
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_origin_regex=local_network_regex,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(settings.security.cors_expose_headers),
        )

        if logging_enabled:
            logger.info("✅ CORS configured for development with local network support")
            logger.info(f"   Allowed origins: {cors_origins}")
            logger.info(f"   Local network pattern: {local_network_regex}")
    else:
        # Deployed (staging and production): strict origin checking only, no
        # regex patterns and no appended localhost origins.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(settings.security.cors_expose_headers),
        )
    if logging_enabled:
        logger.info(
            f"After CORS middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    if logging_enabled:
        logger.info(
            f"Final middleware stack: {[type(m).__name__ for m in app.user_middleware]}"
        )

    _assert_cors_outermost(app)
