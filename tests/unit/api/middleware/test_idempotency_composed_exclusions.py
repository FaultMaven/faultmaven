"""Idempotency exclusions for routes this repository does not serve (fm#1299).

Root cause guarded here: ``IdempotencyMiddleware`` states the rule "replaying a
token mint is not idempotency … excluded structurally rather than relying on the
cache key being scoped correctly", but expressed that rule as two module-level
constants. A constant in this package can only name a route this package serves,
and the deployed route table is larger than this package: ``faultmaven-cloud``
mounts its routers onto the same ``app`` singleton, and one of them —
``POST /api/v1/admin/integrations/slack/workspaces`` — returns a service-account
refresh token (ADR-012 D10). It matched neither constant, so the middleware
wrote a live credential into Redis under a one-hour TTL and replayed it on the
re-bind that is the documented recovery path for a lost credential.

The fixture route below is that Cloud route in shape: a 2xx body carrying a
freshly minted credential. The first test asserts it *is* cached when nothing is
declared — the positive control, without which every "nothing was cached"
assertion here would hold whether or not the mechanism exists.

Conventions follow ``test_idempotency_caller_scoping.py``: real middleware, real
``fakeredis``, one event loop (``TestClient`` creates a loop per request and
async FakeRedis then raises "bound to a different event loop", which this
middleware's own ``except`` would swallow into a pass).
"""

import logging

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.routing import Host, Mount

from faultmaven.api.middleware.idempotency import (
    APP_STATE_EXCLUSIONS_ATTR,
    IdempotencyMiddleware,
    exclude_from_idempotency,
)

# The real Cloud path, so this file names the route it exists for.
BIND = "/api/v1/admin/integrations/slack/workspaces"
PAGERDUTY = "/api/v1/admin/integrations/pagerduty/services"
ADMIN = "Bearer org-admin-token-aaaaaaaaaaaaaaaa"
KEY = "d1f0c0de-0000-4000-8000-000000000000"
OTHER_KEY = "99999999-8888-7777-6666-555555555555"
BODY = {"slack_team_id": "T0123ABCD", "team_name": "Acme Platform"}
_MODULE_LOGGER = "faultmaven.api.middleware.idempotency"


def _clear_declaration_reports():
    """The once-only report latch is per process, so it must be reset
    between tests or the second test to install a given mistake sees the
    log it is asserting on already spent."""
    from faultmaven.api.middleware.idempotency import _REPORTED_BAD_DECLARATIONS

    _REPORTED_BAD_DECLARATIONS.clear()


def _build_app():
    """An app shaped like the composed deployment: OSS middleware, Cloud route."""
    app = FastAPI()
    mints = {"n": 0}

    async def _bind():
        mints["n"] += 1
        # The shape that matters: a 2xx body carrying a live credential. Each
        # call mints a *different* one, so a replay is visible in the body and
        # not only in the absence of a Redis key.
        return {
            "slack_team_id": "T0123ABCD",
            "service_account_username": "slack-agent-t0123abcd",
            "refresh_token": f"live-refresh-token-{mints['n']}",
            "account_created": mints["n"] == 1,
        }

    @app.post(BIND)
    async def bind():
        return await _bind()

    @app.post(BIND + "/")
    async def bind_trailing_slash():
        """Registered so a trailing-slash request reaches a cacheable 2xx.

        Without it FastAPI answers 307, which is never cached, and a test
        asserting "nothing was cached" would pass whether or not the exclusion
        normalises the slash. ``TrailingSlashMiddleware`` plays this role in the
        real app, and it sits *inside* this middleware, so idempotency still
        sees the raw path.
        """
        return await _bind()

    @app.post(PAGERDUTY)
    async def bind_pagerduty():
        """A second composed unit, so accumulation is shown between the two
        kinds of path this mechanism is for rather than by reaching into a core
        route — an exclusion aimed at a core route is a misuse, not an example."""
        return {"integration_id": "pd-1"}

    @app.post("/api/v1/cases")
    async def create_case():
        """An ordinary idempotent route, to show a declaration does not widen."""
        return {"case_id": "c-1"}

    @app.post("/api/v1/sessions")
    async def mint_session():
        """The core's own excluded mint, so a malformed composed declaration can
        be shown not to have disturbed the tier this file does not own."""
        return {"session_id": "newly-minted-credential"}

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    app.state.mints = mints
    return app, fake


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _seed_one_cache_entry(client, fake):
    """Positive control for the ``idempotency:*`` glob.

    ``keys("idempotency:*") == []`` proves nothing on its own: rename
    ``key_prefix`` and the glob matches nothing in every case. Seed one entry
    from a route known to be cacheable so a later "unchanged" is evidence.
    """
    await client.post(
        "/api/v1/cases",
        headers={"Authorization": ADMIN, "Idempotency-Key": OTHER_KEY},
        json={},
    )
    seeded = await fake.keys("idempotency:*")
    assert seeded, "positive control: an identified request must write a cache key"
    return sorted(seeded)


async def _bind_twice(app, fake, path=BIND):
    headers = {"Authorization": ADMIN, "Idempotency-Key": KEY}
    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        first = await client.post(path, headers=headers, json=BODY)
        second = await client.post(path, headers=headers, json=BODY)
        after = sorted(await fake.keys("idempotency:*"))
    return first, second, seeded, after


# ---------------------------------------------------------------------------
# The defect, kept as the positive control for everything below.
# ---------------------------------------------------------------------------


async def test_an_undeclared_composed_mint_route_is_cached_and_replayed():
    """fm#1299 as reported. This is the control, not an aspiration.

    If this ever stops holding, the mechanism under test has become
    unobservable and every "nothing was cached" assertion below is vacuous.
    """
    app, fake = _build_app()
    first, second, seeded, after = await _bind_twice(app, fake)

    assert first.status_code == 200, "must reach a cacheable 2xx"
    assert len(after) == len(seeded) + 1, "the mint must be the thing that got cached"
    cached = [k for k in after if k not in seeded]
    assert "live-refresh-token-1" in await fake.get(
        cached[0]
    ), "the control depends on a live credential being at rest in Redis"
    assert second.json()["refresh_token"] == "live-refresh-token-1", "replayed"
    assert app.state.mints["n"] == 1, "the re-bind never minted"


# ---------------------------------------------------------------------------
# The fix: the composition root declares what this repository cannot name.
# ---------------------------------------------------------------------------


async def test_a_declared_composed_route_is_neither_cached_nor_replayed():
    app, fake = _build_app()
    exclude_from_idempotency(app, BIND)

    first, second, seeded, after = await _bind_twice(app, fake)

    assert first.status_code == 200 and second.status_code == 200
    assert after == seeded, "a credential mint must never reach Redis"
    assert app.state.mints["n"] == 2, "the re-bind must mint, not replay"
    assert first.json()["refresh_token"] != second.json()["refresh_token"]


async def test_the_exclusion_holds_when_the_request_carries_a_trailing_slash():
    """This middleware sits outside ``TrailingSlashMiddleware`` and sees '/'."""
    app, fake = _build_app()
    exclude_from_idempotency(app, BIND)

    first, _, seeded, after = await _bind_twice(app, fake, path=BIND + "/")

    assert first.status_code == 200, "must reach a cacheable 2xx, not a 307"
    assert app.state.mints["n"] == 2, "both calls must have reached the route"
    assert after == seeded, "a trailing slash must not slip past the exclusion"


async def test_the_exclusion_holds_when_the_declaration_carries_a_trailing_slash():
    """A composer's slash is a spelling, not a different route.

    Declared and incoming paths go through one normalisation for this reason: a
    mismatch here is a silent no-op that looks exactly like a working exclusion.
    """
    app, fake = _build_app()
    exclude_from_idempotency(app, BIND + "/")

    _, _, seeded, after = await _bind_twice(app, fake)

    assert after == seeded
    assert app.state.mints["n"] == 2


async def test_a_declaration_does_not_widen_past_its_own_path():
    """An exclusion silently disables idempotency, so it must not spread."""
    app, fake = _build_app()
    exclude_from_idempotency(app, BIND)
    headers = {"Authorization": ADMIN, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post("/api/v1/cases", headers=headers, json={})
        retry = await client.post("/api/v1/cases", headers=headers, json={})

    assert first.headers.get("X-Idempotency-Replayed") != "true"
    assert (
        retry.headers.get("X-Idempotency-Replayed") == "true"
    ), "ordinary routes must keep idempotency"


# ---------------------------------------------------------------------------
# The declaration is read defensively: it is consulted with ``in``.
# ---------------------------------------------------------------------------


async def test_a_bare_string_declaration_is_read_as_one_path():
    """``app.state`` is assignable by hand, and ``"a" in "abc"`` is substring.

    A composer who writes the attribute directly and passes a lone string must
    not silently turn the exact comparison into containment. Read as the one
    path it obviously means.
    """
    app, fake = _build_app()
    setattr(app.state, APP_STATE_EXCLUSIONS_ATTR, BIND)

    _, _, seeded, after = await _bind_twice(app, fake)

    assert after == seeded, "a bare-string declaration must still exclude"
    assert app.state.mints["n"] == 2


async def test_a_bare_string_declaration_does_not_become_a_prefix_match():
    """The other half: containment would exclude every prefix of that string."""
    app, fake = _build_app()
    setattr(app.state, APP_STATE_EXCLUSIONS_ATTR, BIND)
    headers = {"Authorization": ADMIN, "Idempotency-Key": KEY}

    async with _client(app) as client:
        await client.post("/api/v1/cases", headers=headers, json={})
        retry = await client.post("/api/v1/cases", headers=headers, json={})

    assert retry.headers.get("X-Idempotency-Replayed") == "true"


async def test_an_unusable_declaration_degrades_rather_than_500s():
    """A malformed declaration must not take every POST down with it.

    Erring the other way here would be wrong — this is the fail-open half, and
    it is why ``exclude_from_idempotency`` refuses a bad path up front instead
    of leaving the middleware to notice at request time.
    """
    app, fake = _build_app()
    setattr(app.state, APP_STATE_EXCLUSIONS_ATTR, 12345)
    headers = {"Authorization": ADMIN, "Idempotency-Key": KEY}

    async with _client(app) as client:
        bound = await client.post(BIND, headers=headers, json=BODY)
        # The core tier must be untouched: a malformed *composed* declaration
        # cannot be allowed to switch off the exclusions this file owns.
        before = sorted(await fake.keys("idempotency:*"))
        await client.post("/api/v1/sessions", headers=headers, json={})
        after_mint = sorted(await fake.keys("idempotency:*"))
        # ...and ordinary idempotency must still work, or "200" would hold on an
        # implementation where caching had simply stopped.
        first = await client.post("/api/v1/cases", headers=headers, json={})
        retry = await client.post("/api/v1/cases", headers=headers, json={})

    assert bound.status_code == 200, "the request must be served, not 500"
    assert after_mint == before, "the core session-mint exclusion must still hold"
    assert first.headers.get("X-Idempotency-Replayed") != "true"
    assert (
        retry.headers.get("X-Idempotency-Replayed") == "true"
    ), "idempotency itself must still work"


# ---------------------------------------------------------------------------
# ``exclude_from_idempotency`` verifies the declaration against the route table.
# ---------------------------------------------------------------------------


def test_a_path_naming_no_post_route_is_refused():
    """The failure this closes is silent: a typo leaves the route cached, and
    from outside that is indistinguishable from a working exclusion."""
    app, _ = _build_app()

    with pytest.raises(ValueError, match="names no POST route"):
        exclude_from_idempotency(app, "/api/v1/admin/integrations/slack/workspace")

    assert not getattr(app.state, APP_STATE_EXCLUSIONS_ATTR, None)


def test_a_templated_path_is_refused():
    """``/orgs/{org_id}/tokens`` can never equal a concrete request path."""
    app, _ = _build_app()

    @app.post("/api/v1/orgs/{org_id}/tokens")
    async def mint(org_id: str):
        return {"token": "t"}

    with pytest.raises(ValueError, match="templated"):
        exclude_from_idempotency(app, "/api/v1/orgs/{org_id}/tokens")


def test_a_relative_path_is_refused():
    app, _ = _build_app()

    with pytest.raises(ValueError, match="absolute path"):
        exclude_from_idempotency(app, "api/v1/cases")


def test_a_get_only_route_is_refused():
    """This middleware only acts on POST, so excluding a GET declares nothing."""
    app, _ = _build_app()

    @app.get("/api/v1/readonly")
    async def readonly():
        return {}

    with pytest.raises(ValueError, match="names no POST route"):
        exclude_from_idempotency(app, "/api/v1/readonly")


def test_declarations_accumulate_across_composed_units():
    """Composed units declare independently and must not clobber each other."""
    app, _ = _build_app()

    exclude_from_idempotency(app, BIND)
    final = exclude_from_idempotency(app, PAGERDUTY)

    assert final == frozenset({BIND, PAGERDUTY})
    assert getattr(app.state, APP_STATE_EXCLUSIONS_ATTR) == final


def test_a_route_served_under_a_mount_can_be_declared():
    """Composition may mount a sub-application rather than include a router.

    Walking mounts keeps the check from refusing a legitimate declaration — a
    refusal a composer learns to route around is worse than no check.
    """
    app, _ = _build_app()
    sub = FastAPI()

    @sub.post("/tokens")
    async def mint():
        return {"token": "t"}

    app.mount("/ext", sub)

    assert exclude_from_idempotency(app, "/ext/tokens") == frozenset({"/ext/tokens"})


# ---------------------------------------------------------------------------
# This repository's own deployment is unchanged.
# ---------------------------------------------------------------------------


def test_the_oss_composition_root_declares_nothing():
    """The extension point is inert here; it exists for the composed edition.

    If a path is ever declared in *this* repository's composition root, that is
    a decision to make deliberately — ``EXCLUDED_EXACT_PATHS`` is where a route
    this repository serves belongs, next to the reasoning for why it is there.

    Asserted against the source rather than against ``faultmaven.main.app``'s
    live state: that app is a process-wide singleton, ``exclude_from_idempotency``
    mutates it in place, and there is no removal API — so a state-based
    assertion would pass or fail on test collection order, and under ``-n 8`` on
    which worker happened to run what.
    """
    import inspect

    import faultmaven.main

    assert "exclude_from_idempotency" not in inspect.getsource(faultmaven.main)


def test_a_scope_carrying_no_app_degrades_rather_than_raising():
    """The declaration is read *outside* ``dispatch``'s ``try``.

    ``request.app`` raises ``KeyError`` on a scope without the key, which would
    turn every POST into a 500 rather than into an ordinary uncached request.
    """
    middleware = IdempotencyMiddleware(app=lambda scope, receive, send: None)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/cases", "headers": []}
    )

    assert middleware._declared_exclusions(request) == frozenset()


def test_the_predicate_defaults_to_the_core_exclusions_alone():
    """Called with no declaration, the predicate is exactly what it was."""
    middleware = IdempotencyMiddleware(app=lambda scope, receive, send: None)

    assert middleware._is_excluded_path("/api/v1/sessions")
    assert not middleware._is_excluded_path(BIND)
    assert middleware._is_excluded_path(BIND, frozenset({BIND}))
    assert not middleware._is_excluded_path("/api/v1/cases", frozenset({BIND}))


# ---------------------------------------------------------------------------
# Route-table walking: a refusal a composer routes around defeats the check.
# ---------------------------------------------------------------------------


def test_a_mount_carrying_middleware_is_still_walked():
    """``Mount(..., middleware=[...])`` hides the app behind a wrapper.

    Measured on starlette 1.3.1: the wrapper on ``.app`` reports **no** routes
    while ``.routes`` reports all of them. Walking ``.app`` therefore refuses a
    real credential-minting route the moment its sub-app gains CORS, tracing or
    tenant binding — and the composer's way out is to assign ``app.state`` by
    hand, which is exactly the verification this function exists to provide.
    """
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    app, _ = _build_app()
    sub = FastAPI()

    @sub.post("/tokens")
    async def mint():
        return {"token": "t"}

    app.router.routes.append(
        Mount(
            "/ext",
            app=sub,
            middleware=[Middleware(CORSMiddleware, allow_origins=["*"])],
        )
    )

    assert exclude_from_idempotency(app, "/ext/tokens") == frozenset({"/ext/tokens"})


def test_a_host_routed_table_is_walked_without_a_prefix():
    """``Host`` carries ``routes`` but no ``path``; host routing adds no prefix."""
    app, _ = _build_app()
    admin = FastAPI()

    @admin.post("/api/v1/tokens")
    async def mint():
        return {"token": "t"}

    app.router.routes.append(Host("admin.example.com", app=admin))

    assert exclude_from_idempotency(app, "/api/v1/tokens") == frozenset(
        {"/api/v1/tokens"}
    )


# ---------------------------------------------------------------------------
# A malformed declaration must be loud. Silence here reproduces fm#1299 with an
# exclusion sitting in the source.
# ---------------------------------------------------------------------------


def test_a_one_shot_iterator_is_refused_rather_than_consumed():
    """A generator would exclude the first POST of the process and no other.

    That is worse than never declaring: the first request demonstrates the
    exclusion working, and every later one silently caches the credential.
    """
    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    generator = (path for path in [BIND])

    assert _normalize_declared_exclusions(generator) == frozenset()
    assert list(generator) == [BIND], "the declaration must not have been consumed"


def test_non_string_entries_are_reported_rather_than_silently_dropped(caplog):
    """``PurePosixPath`` and ``bytes`` are what you get from *building* a path.

    Neither can match a request path, so both collapse to an empty exclusion
    set — indistinguishable from 'nothing declared' unless it is said out loud.
    """
    from pathlib import PurePosixPath

    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    _clear_declaration_reports()
    with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
        result = _normalize_declared_exclusions([PurePosixPath(BIND), b"/x"])

    assert result == frozenset()
    assert "NOT in effect" in caplog.text
    assert "PurePosixPath" in caplog.text or BIND in caplog.text


def test_usable_entries_survive_alongside_an_unusable_one(caplog):
    """Reporting the bad entry must not discard the good ones."""
    from pathlib import PurePosixPath

    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    _clear_declaration_reports()
    with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
        result = _normalize_declared_exclusions([BIND, PurePosixPath(PAGERDUTY)])

    assert result == frozenset({BIND})


def test_a_declaration_that_raises_on_read_does_not_break_the_request(caplog):
    """Only ``TypeError`` used to be caught, and the truthiness test sat outside
    the ``try`` — so a ``__bool__`` raising anything else 500'd every POST."""
    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    class Hostile:
        def __bool__(self):
            raise ValueError("boom")

        def __len__(self):
            raise ValueError("boom")

        def __iter__(self):
            raise ValueError("boom")

        def __contains__(self, item):
            raise ValueError("boom")

    _clear_declaration_reports()
    with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
        assert _normalize_declared_exclusions(Hostile()) == frozenset()


def test_a_bad_declaration_is_reported_once_not_once_per_request(caplog):
    """This is read on every POST — including the majority carrying no key.

    An unconditional log would emit a line per request for the life of the
    process, flooding the error-pattern detection behind ``/health/patterns``
    with a static condition already known at composition time.
    """
    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    _clear_declaration_reports()
    with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
        for _ in range(50):
            _normalize_declared_exclusions(12345)

    assert len(caplog.records) == 1, f"{len(caplog.records)} log lines for one mistake"


def test_a_declaration_this_module_normalised_is_not_rebuilt_per_request():
    """The stored set is already normalised; the request path should not
    re-derive it. ``exclude_from_idempotency`` marks its own output so the
    per-POST read is an identity check, while a hand-assigned value still takes
    the defensive path."""
    from faultmaven.api.middleware.idempotency import _normalize_declared_exclusions

    app, _ = _build_app()
    stored = exclude_from_idempotency(app, BIND)

    assert _normalize_declared_exclusions(stored) is stored
    assert _normalize_declared_exclusions(frozenset({BIND})) is not stored
