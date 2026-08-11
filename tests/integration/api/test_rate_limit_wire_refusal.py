"""The rate limiter refuses, at the wire, on the app the deployment serves (fm#990).

Every other rate-limit test builds its own ``RateLimitMiddleware`` over a scratch
Starlette app. That proves the class works; it cannot prove the class is
*installed*, reachable, and able to refuse on ``faultmaven.main.app`` — the only
stack a deployment ever runs. Before this module there was no assertion anywhere
in ``tests/integration/`` that the protection layer can produce a 429 at all: the
suite drove the real app thousands of times and would not have noticed the
limiter being removed, bypassed, or permanently failed open. It did not notice
exactly that — see ``test_the_app_under_test_carries_the_protection_stack``.

**Isolation.** The ``global`` bucket is keyed on the *client address* rather than
on a process-wide constant (``rate_limiting.py``, ``_check_global_rate_limit``),
so each test claims an address no other test uses and gets a private window. The
counters live in the process-wide FakeRedis for the whole session, which is why
sharing an address would make these tests order-dependent.

**Why the assertions are shaped the way they are.** A test that only asserted
"the 4th request was a 429" would also pass against a limiter that refuses
*everything* — including one wedged fail-closed by a broken Redis client. So each
test pins both edges: the requests under the limit are served, the ones over it
are refused. It then reads the middleware's own counters, because a 429 that
arrived alongside a swallowed exception is not the refusal under test.
``metrics["errors"]`` is incremented on precisely the swallow paths fm#990 was
filed about, and ``is_degraded`` reports the per-replica stand-in.
"""

import contextlib
import itertools

import pytest
from fastapi.testclient import TestClient

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.models.protection import RateLimitConfig

pytestmark = [pytest.mark.integration, pytest.mark.security]

# A route that exists, is not liveness-exempt, and needs no fixture scaffolding.
# It answers 401 unauthenticated, which is the point: rate limiting runs in the
# middleware stack, ahead of the route's auth dependency, so an unauthenticated
# request is metered exactly like an authenticated one.
PROBE_PATH = "/api/v1/cases"

# Distinct client addresses hand each test its own ``global`` window.
_ADDRESS = (f"198.51.100.{n}" for n in itertools.count(1))


def _live_rate_limit_middleware(app) -> RateLimitMiddleware:
    """Return the ``RateLimitMiddleware`` *instance* serving ``app``.

    ``app.user_middleware`` holds ``Middleware(cls, ...)`` records, not objects —
    Starlette instantiates them in ``build_middleware_stack()``. Reading the
    records would prove only that a class was registered, not that the object
    serving traffic is the one being inspected, so this walks the built chain.
    That chain does not exist until the app has been started, so every caller
    here looks it up from inside a running ``TestClient``.
    """
    node = app.middleware_stack
    for _ in range(64):
        if node is None:
            break
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError(
        "no RateLimitMiddleware in the built middleware stack of the real app"
    )


@pytest.fixture
def real_app():
    from faultmaven.main import app

    return app


@contextlib.contextmanager
def _serving_with_global_limit(app, requests: int, window: int = 60):
    """Run ``app`` with its ``global`` bucket tightened, then put the limits back.

    The limits are replaced on the middleware object the app is actually serving,
    so the request path under test is the production one end to end. That object
    outlives the test, hence the restore.

    Only the ``global`` entry is overridden and the rest of the configuration is
    carried through: ``check_rate_limit`` answers "allowed, limit 0" for a bucket
    it holds no config for, so replacing the whole mapping would silently stop
    metering the buckets this test does not mean to touch.
    """
    with TestClient(app, client=(next(_ADDRESS), 51000)) as client:
        middleware = _live_rate_limit_middleware(app)
        limiter = middleware.rate_limiter
        original = dict(limiter._configs)
        tightened = dict(original)
        tightened["global"] = RateLimitConfig(
            enabled=True, requests=requests, window=window
        )
        limiter.configure_limits(tightened)
        try:
            yield client, middleware
        finally:
            limiter.configure_limits(original)


def test_the_app_under_test_carries_the_protection_stack(real_app):
    """The app this suite drives must be the protected one.

    This is the guard for the gate fm#990 removed. ``setup_middleware`` used to
    install the protection stack only when ``skip_service_checks`` was false —
    and every test entrypoint there is sets that flag: ``tests/conftest.py``,
    ``scripts/tests.py``, and both CI pytest jobs. The result was that no test
    anywhere ran against the app ``main.py`` builds; the suite drove an app whose
    entire middleware stack was ``[CORS, Logging, GZip, TrailingSlash]`` and
    would not have noticed the limiter being deleted.

    Nothing failed while that was true, which is exactly why it needs an
    assertion rather than a comment: an unprotected app serves every request the
    suite makes, just without any of the layers under test here. The two tests
    below would fail if the gate came back, but they would fail as a missing
    middleware instance rather than as the configuration mistake it is, so this
    names the condition directly.
    """
    installed = [entry.cls.__name__ for entry in real_app.user_middleware]

    assert "RateLimitMiddleware" in installed, (
        "the app under test carries no rate limiting — the protection install "
        f"has been re-gated on an environment flag: {installed}"
    )


def test_protection_is_installed_even_under_skip_service_checks(monkeypatch):
    """Builds the app in the failure state, rather than reporting the ambient one.

    The assertion above can only fail where ``SKIP_SERVICE_CHECKS`` is set when
    the app is imported. That is true of CI and of ``scripts/tests.py``, but not
    of a bare ``pytest`` run — ``tests/conftest.py`` sets the flag inside a
    function-scoped fixture, long after collection has imported the app. So on a
    developer's machine the guard above would sit silent through exactly the
    regression it exists to catch.

    This one puts the process into that state deliberately and rebuilds the app
    under it, so it discriminates everywhere.
    """
    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "true")
    reset_settings()
    try:
        rebuilt = rebuild_app()
        installed = [entry.cls.__name__ for entry in rebuilt.user_middleware]
        assert "RateLimitMiddleware" in installed, (
            "SKIP_SERVICE_CHECKS stripped the protection stack. That flag means "
            "'do not require external services'; the limiter needs none, so it "
            f"must not be gated on it: {installed}"
        )
    finally:
        # ``monkeypatch`` restores the variable; the singleton built from it
        # while it was set has to be dropped explicitly or it outlives the test.
        reset_settings()


def test_the_real_app_refuses_over_the_global_limit_with_429_and_headers(real_app):
    """Under the limit the app serves; over it, it refuses with a usable 429."""
    limit = 3

    with _serving_with_global_limit(real_app, limit) as (client, middleware):
        errors_before = middleware.metrics["errors"]
        blocked_before = middleware.metrics["requests_blocked"]

        responses = [client.get(PROBE_PATH) for _ in range(limit + 2)]

        served, refused = responses[:limit], responses[limit:]
        codes = [r.status_code for r in responses]

        assert all(
            r.status_code != 429 for r in served
        ), f"a request inside the limit was refused: {codes}"
        assert all(r.status_code == 429 for r in refused), codes

        # The refusal has to be actionable, not merely a status code.
        blocked = refused[0]
        assert blocked.headers["X-RateLimit-Limit"] == str(limit)
        assert blocked.headers["X-RateLimit-Remaining"] == "0"
        assert int(blocked.headers["Retry-After"]) > 0
        assert int(blocked.headers["X-RateLimit-Reset"]) > 0

        # The 429 came from the limiter deciding, not from an error it swallowed.
        assert middleware.rate_limiter.is_degraded is False
        assert middleware.metrics["errors"] == errors_before, (
            "the limiter swallowed an exception during the run, so the refusal "
            "above cannot be attributed to the limit"
        )
        assert middleware.metrics["requests_blocked"] == blocked_before + len(
            refused
        ), "the refusals were not counted by the limiter that supposedly made them"


def test_the_limiter_answers_from_redis_rather_than_degrading(real_app):
    """The guard for a limiter that is installed but silently not working.

    A limiter whose Redis client is unusable either degrades to the per-replica
    stand-in or, under ``fail_open_on_redis_error``, swallows the error and
    allows everything. Both are invisible to an ordinary request/response
    assertion — the app keeps answering — so the condition is named here
    directly.
    """
    with _serving_with_global_limit(real_app, 2) as (client, middleware):
        errors_before = middleware.metrics["errors"]

        first = client.get(PROBE_PATH)
        second = client.get(PROBE_PATH)
        third = client.get(PROBE_PATH)

        limiter = middleware.rate_limiter

        assert limiter._redis is not None, "the limiter never obtained a Redis client"
        # Says only what it can: the limiter reached its *configured* client
        # rather than falling back internally. Whether that client is shared
        # across replicas is a deployment question — here it is the in-process
        # FakeRedis by configuration, so this is not a claim about that.
        assert limiter.is_degraded is False, (
            "the limiter fell back to the per-replica stand-in instead of the "
            "client it was configured with"
        )
        assert (
            middleware.metrics["errors"] == errors_before
        ), "the limiter swallowed an exception per request"
        # Positive evidence that the counting is real: the window actually filled.
        assert first.status_code != 429 and second.status_code != 429
        assert third.status_code == 429, (
            "the limiter allowed a request past its configured limit — it is "
            "installed but not counting"
        )
