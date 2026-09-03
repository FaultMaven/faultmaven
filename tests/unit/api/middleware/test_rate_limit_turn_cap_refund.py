"""A turn the cap refused does not spend the quota that protects LLM compute.

``per_session`` and ``per_session_hourly`` exist to bound what one session can
make the product *spend on models* (fm#994 — that is why cheap reads were moved
out of them). A turn the per-tenant cap refuses reaches no model at all, so
leaving its entry in those windows meters one event against two independent
quotas: a capped tenant could exhaust its own hourly write allowance by
retrying, and then be unable to submit a turn for an hour after an operator
raised its cap.

So the cap marks the request and ``RateLimitMiddleware`` releases this request's
own entry from the per-session **write** pair on the way out. Two properties are
asserted separately here, because they fail for different reasons:

* the release happens, and gives back exactly one entry;
* ``global`` is NOT released. That window is keyed on the client address and
  bounds request *volume*, which a refused caller still generates — refunding it
  would let a capped client hammer the deployment for free.

Driven end to end over the real ASGI stack: the middleware and the guard each
name the attribute with their own constant, and a rename on one side is exactly
the failure this module has to catch. ``test_both_sides_name_the_same_attribute``
states that dependency directly, so the failure reads as a rename rather than as
a window count nobody expected.

Through ``httpx.ASGITransport`` rather than ``TestClient``, and that is not a
preference. ``TestClient`` runs the app in a **new event loop**, while these
assertions read the Redis windows from the test's own loop; FakeRedis's response
queue binds permanently to the first loop that uses it, so every window read
would raise "bound to a different event loop" — the same trap
``test_rate_limit_wire_refusal.py`` documents from the other side. One loop for
the request and the assertion is what makes the counts readable at all.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware import rate_limiting
from faultmaven.api.middleware.rate_limiting import (
    REFUND_MARKER_ATTR,
    RateLimitMiddleware,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    Reservation,
    TenantTurnCapExceeded,
)
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitSpec
from faultmaven.modules.case.api import turn_cap
from faultmaven.modules.case.api.turn_cap import enforce_tenant_turn_cap

pytestmark = [pytest.mark.unit, pytest.mark.security]

ORG = "11111111-1111-1111-1111-111111111111"
SESSION = "sess-cap"
_IP_COUNTER = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _settings():
    """Roomy buckets: this module measures window CONTENTS, never a refusal."""
    settings = get_development_protection_settings()
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=1000, window=60),
        "per_session": RateLimitConfig(enabled=True, requests=1000, window=60),
        "per_session_hourly": RateLimitConfig(enabled=True, requests=1000, window=3600),
    }
    return settings


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(turn_cap, "get_current_tenant_id", lambda: ORG)

    application = FastAPI()

    @application.post(
        "/api/v1/cases/c1/turns", dependencies=[Depends(enforce_tenant_turn_cap)]
    )
    async def _turn():
        return {"ok": True}

    application.dependency_overrides[require_authentication] = lambda: object()
    application.add_middleware(RateLimitMiddleware, settings=_settings())
    application.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
    return application


def _client(app, address=None):
    """An ASGI client on THIS loop, with a per-test address for ``global``."""
    return AsyncClient(
        transport=ASGITransport(
            app=app, client=(address or f"10.9.0.{next(_IP_COUNTER)}", 1234)
        ),
        base_url="http://testserver",
    )


async def _post_turn(client):
    return await client.post(
        "/api/v1/cases/c1/turns", headers={"X-Session-ID": SESSION}
    )


def _live_middleware(app) -> RateLimitMiddleware:
    node = app.middleware_stack
    for _ in range(32):
        if node is None:
            break
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("no RateLimitMiddleware in the built stack")


async def _window_size(limiter, limit_type, key) -> int:
    """How many entries the named window holds, through the limiter's own key rule."""
    return int(
        await limiter._redis.zcard(
            limiter.window_key(RateLimitSpec(key=key, limit_type=limit_type))
        )
    )


def _refuse(**_kwargs):
    async def _raise(organization_id, **_):
        raise TenantTurnCapExceeded(
            organization_id=organization_id,
            limit=30,
            used=30,
            reset_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )

    return _raise


def _admit():
    async def _reserve(organization_id, **_):
        return Reservation(organization_id, 1, 30, "default_personal")

    return _reserve


def test_both_sides_name_the_same_attribute():
    """The one-line failure that would otherwise read as an unexplained count."""
    assert turn_cap.RATE_LIMIT_REFUND_ATTR == REFUND_MARKER_ATTR


def test_the_refundable_set_is_the_llm_compute_pair_and_nothing_else():
    """``global`` bounds request volume; refunding it would be a free-hammer hole."""
    assert rate_limiting.REFUNDABLE_LIMIT_TYPES == frozenset(
        {LimitType.PER_SESSION, LimitType.PER_SESSION_HOURLY}
    )


async def test_a_capped_turn_leaves_the_llm_compute_windows_empty(app, monkeypatch):
    monkeypatch.setattr(turn_cap, "reserve_turn", _refuse())

    async with _client(app) as client:
        assert (await _post_turn(client)).status_code == 429

    limiter = _live_middleware(app).rate_limiter
    assert await _window_size(limiter, LimitType.PER_SESSION, SESSION) == 0
    assert await _window_size(limiter, LimitType.PER_SESSION_HOURLY, SESSION) == 0


async def test_the_global_window_keeps_the_refused_request(app, monkeypatch):
    """A capped caller is still a caller; the address-keyed bound stays honest."""
    monkeypatch.setattr(turn_cap, "reserve_turn", _refuse())

    address = f"10.9.0.{next(_IP_COUNTER)}"
    async with _client(app, address) as client:
        assert (await _post_turn(client)).status_code == 429

    limiter = _live_middleware(app).rate_limiter
    assert await _window_size(limiter, LimitType.GLOBAL, address) == 1


async def test_an_admitted_turn_keeps_its_entry_in_both_write_windows(app, monkeypatch):
    """The mirror: a served turn DID spend what those windows meter.

    Without this the module would pass against a middleware that released every
    request — which is a rate limiter that limits nothing.
    """
    monkeypatch.setattr(turn_cap, "reserve_turn", _admit())

    async with _client(app) as client:
        assert (await _post_turn(client)).status_code == 200

    limiter = _live_middleware(app).rate_limiter
    assert await _window_size(limiter, LimitType.PER_SESSION, SESSION) == 1
    assert await _window_size(limiter, LimitType.PER_SESSION_HOURLY, SESSION) == 1


async def test_a_refusal_gives_back_exactly_its_own_entry(app, monkeypatch):
    """It releases one request's member, not the window.

    A release implemented as "clear the key" would pass every assertion above
    and silently reset the whole session's quota — which is a bigger hole than
    the one this feature closes.
    """
    monkeypatch.setattr(turn_cap, "reserve_turn", _admit())

    async with _client(app) as client:
        for _ in range(3):
            assert (await _post_turn(client)).status_code == 200

        limiter = _live_middleware(app).rate_limiter
        assert await _window_size(limiter, LimitType.PER_SESSION_HOURLY, SESSION) == 3

        monkeypatch.setattr(turn_cap, "reserve_turn", _refuse())
        assert (await _post_turn(client)).status_code == 429

        # The three admitted turns are still counted; only the refused one is not.
        assert await _window_size(limiter, LimitType.PER_SESSION_HOURLY, SESSION) == 3


async def test_a_release_of_an_unknown_member_removes_nothing(app, monkeypatch):
    """The limiter's release can only widen a window by entries it owns."""
    monkeypatch.setattr(turn_cap, "reserve_turn", _admit())

    async with _client(app) as client:
        assert (await _post_turn(client)).status_code == 200

        limiter = _live_middleware(app).rate_limiter
        removed = await limiter.release(
            [RateLimitSpec(key=SESSION, limit_type=LimitType.PER_SESSION_HOURLY)],
            "a-member-that-was-never-written",
        )
        assert removed == 0
        assert await _window_size(limiter, LimitType.PER_SESSION_HOURLY, SESSION) == 1
