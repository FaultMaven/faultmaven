"""The templated tier of ``route_policy``, and the turn route's place in it (#1888).

Turn replay is owned by the turn receipt committed with the turn, so the turn
route is declared ``never_replayed`` by its TEMPLATE,
``/api/v1/cases/{case_id}/turns``. Both repeat-suppressing middlewares must read
that tier through ``policy_for``: a template matched by nothing is a
declaration that looks like it holds and does not.

Real middleware, real ``fakeredis``, one event loop (``httpx.ASGITransport``),
as ``test_idempotency_composed_exclusions.py`` does.
"""

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.route_policy import (
    APP_STATE_POLICY_ATTR,
    RoutePolicy,
    declare_route_policy,
)
from faultmaven.config.protection import get_development_protection_settings

pytestmark = pytest.mark.unit

TEMPLATE = "/api/v1/cases/{case_id}/turns"
TURN = "/api/v1/cases/case-1/turns"
OTHER = "/api/v1/cases/{case_id}/notes"
AUTH = {"Authorization": "Bearer some-token-aaaaaaaaaaaaaaaa"}


def _app(*, idempotency: bool = False, dedup: bool = False):
    app = FastAPI()
    runs = {"n": 0}

    @app.post(TEMPLATE)
    async def turn(case_id: str):
        runs["n"] += 1
        return {"run": runs["n"]}

    @app.post(OTHER)
    async def note(case_id: str):
        runs["n"] += 1
        return {"run": runs["n"]}

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    app.state.redis_client = fake
    if idempotency:
        app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    if dedup:
        settings = get_development_protection_settings()
        settings.deduplication_enabled = True
        app.add_middleware(DeduplicationMiddleware, settings=settings)
    app.state.runs = runs
    return app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


class TestIdempotencyReadsTheTemplatedTier:
    async def test_a_declared_template_is_never_replayed(self):
        app = _app(idempotency=True)
        declare_route_policy(app, TEMPLATE, never_replayed=True)
        headers = {**AUTH, "Idempotency-Key": "opt_msg_1_aaaaaaaa"}

        async with _client(app) as client:
            first = await client.post(TURN, headers=headers, json={"q": 1})
            second = await client.post(TURN, headers=headers, json={"q": 1})
            slash = await client.post(TURN + "/", headers=headers, json={"q": 1})

        assert first.status_code == 200
        assert "X-Idempotency-Replayed" not in second.headers
        assert second.json() == {"run": 2}
        # ``/turns/`` is normalised before the template is matched; FastAPI
        # redirects it (307), which is never cached either way, so the claim is
        # only that the request reached the route rather than the cache.
        assert "X-Idempotency-Replayed" not in slash.headers

    async def test_control_an_undeclared_template_is_replayed(self):
        """Without this, the test above passes on a middleware that caches
        nothing at all."""
        app = _app(idempotency=True)
        declare_route_policy(app, TEMPLATE, never_replayed=True)
        headers = {**AUTH, "Idempotency-Key": "opt_msg_1_aaaaaaaa"}
        other = OTHER.format(case_id="case-1")

        async with _client(app) as client:
            await client.post(other, headers=headers, json={"q": 1})
            second = await client.post(other, headers=headers, json={"q": 1})

        assert second.headers.get("X-Idempotency-Replayed") == "true"
        assert second.json() == {"run": 1}


class TestDeduplicationReadsTheTemplatedTier:
    async def test_a_declared_template_is_never_collapsed(self):
        app = _app(dedup=True)
        declare_route_policy(app, TEMPLATE, never_replayed=True)
        headers = {"X-Session-ID": "sess-1"}

        async with _client(app) as client:
            await client.post(TURN, headers=headers, json={"q": 1})
            second = await client.post(TURN, headers=headers, json={"q": 1})

        assert second.status_code == 200, second.text
        assert second.json() == {"run": 2}

    async def test_control_an_undeclared_template_is_collapsed(self):
        app = _app(dedup=True)
        declare_route_policy(app, TEMPLATE, never_replayed=True)
        headers = {"X-Session-ID": "sess-1"}
        other = OTHER.format(case_id="case-1")

        async with _client(app) as client:
            await client.post(other, headers=headers, json={"q": 1})
            second = await client.post(other, headers=headers, json={"q": 1})

        assert second.status_code == 409


class TestDeclaringATemplate:
    def test_a_template_naming_no_route_is_refused(self):
        app = _app()
        with pytest.raises(ValueError, match="names no POST route"):
            declare_route_policy(
                app, "/api/v1/cases/{case_id}/nope", never_replayed=True
            )

    def test_a_template_spelling_the_parameter_differently_is_refused(self):
        """Checked against the route's own template, as written."""
        app = _app()
        with pytest.raises(ValueError, match="names no POST route"):
            declare_route_policy(app, "/api/v1/cases/{id}/turns", never_replayed=True)

    def test_the_template_matches_by_starlettes_regex_and_nothing_wider(self):
        app = _app()
        declare_route_policy(app, TEMPLATE, never_replayed=True)
        policy = getattr(app.state, APP_STATE_POLICY_ATTR)

        assert policy.templated("/api/v1/cases/c-9/turns").never_replayed
        assert policy.templated("/api/v1/cases/c-9/turns/").never_replayed
        assert policy.templated("/api/v1/cases/c-9/turns/x") == RoutePolicy()
        assert policy.templated("/api/v1/cases/a/b/turns") == RoutePolicy()


def test_the_deployed_app_declares_the_turn_route():
    """The composition root's declaration is in effect on the real app, by the
    template the case router serves."""
    from faultmaven.main import app

    policy = getattr(app.state, APP_STATE_POLICY_ATTR)

    assert policy[TEMPLATE] == RoutePolicy(never_replayed=True, never_collapsed=True)
    assert policy.templated(TURN) == RoutePolicy(
        never_replayed=True, never_collapsed=True
    )
