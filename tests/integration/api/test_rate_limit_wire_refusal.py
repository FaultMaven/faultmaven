"""The rate limiter refuses, at the wire, on the app the deployment serves (fm#990).

Every other rate-limit test builds its own ``RateLimitMiddleware`` over a scratch
Starlette app. That proves the class works; it cannot prove the class is
*installed*, reachable, and able to refuse on ``faultmaven.main.app`` — the only
stack a deployment ever runs. Before this module there was no assertion anywhere
in ``tests/integration/`` that the protection layer can produce a 429 at all: the
suite drove the real app thousands of times and would not have noticed the
limiter being removed, bypassed, or permanently failed open. It did not notice
exactly that — see ``test_the_published_app_carries_the_protection_stack``.

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


def test_the_published_app_carries_the_protection_stack(real_app):
    """``faultmaven.main.app`` must be the protected app, whoever imports it.

    This is the guard for the defect fm#990 actually names. ``faultmaven.main``
    holds the app as a module-level singleton, and a test module that rebuilds it
    under ``SKIP_SERVICE_CHECKS`` — as the OAuth integration modules must, to get
    an OAuth-enabled app — used to leave the rebuilt, *unprotected* app published
    in ``sys.modules``. Because pytest imports every test module during
    collection, that silently replaced the app for every module collected
    afterwards, and the suite ran split-brain: four modules held a protected app
    and the rest, including ``tests/integration/test_main_app.py``, held one with
    no rate limiting, deduplication, idempotency or request-id middleware.

    Nothing failed when that happened, which is the problem: an unprotected app
    serves every request the suite makes, just without any of the layers under
    test here. Asserting on the published singleton is therefore the point — a
    test that built its own app would keep passing through the same regression.
    """
    installed = [entry.cls.__name__ for entry in real_app.user_middleware]

    assert "RateLimitMiddleware" in installed, (
        "the published app carries no rate limiting — something rebuilt "
        f"faultmaven.main under SKIP_SERVICE_CHECKS and published it: {installed}"
    )


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
        assert limiter.is_degraded is False, (
            "the limiter is running on the per-replica stand-in, so limits are "
            "per-process rather than per-deployment"
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
