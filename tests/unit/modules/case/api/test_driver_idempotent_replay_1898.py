"""A former driver's retry of their own committed POST is replayed (ADR-020 D2).

``IdempotencyMiddleware`` answers a keyed non-turn POST from its cache BEFORE
the route's gate runs, keyed on the verified caller. So a driver who committed
a write under a key, and then lost the wheel, gets that same response back on a
retry under the same key. That is accepted and pinned here, because it is
harmless: the replay is the response the caller was already sent, nothing runs
again, and nothing is written. What a former driver cannot do is a NEW write —
a new key reaches the gate, and the gate refuses.

The route here is a stand-in with the real gate in it (the real
``CaseService.get_case(..., driver_only=True)`` over the in-memory repository),
because every real driver-gated POST needs collaborators this property has
nothing to do with; the middleware, the token verification and the resolver
are the real ones.
"""

from __future__ import annotations

from unittest.mock import patch

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.unit.api.middleware.test_idempotency_survives_token_refresh import (
    ENTERPRISE_ID,
    _mock_settings,
)
from tests.unit.modules.case.test_case_driver_1898 import (
    CASE_ID,
    CREATOR,
    DRIVER,
    TEAMMATE,
    _Accounts,
    _case,
    _Shares,
    _Teams,
    _tenant,  # noqa: F401 - autouse fixture: binds the enterprise
)
from tests.utils import forge_access_token

pytestmark = [pytest.mark.unit, pytest.mark.security]

KEY = "driver-retry-0001"


@pytest.fixture
def auth_service():
    store = RedisTokenRevocationStore(
        fakeredis_aio.FakeRedis(decode_responses=True), key_prefix="revoked:token:"
    )
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_mock_settings(),
    ):
        yield AuthService(revocation_store=store)


def _token(auth_service, user_id):
    return forge_access_token(
        auth_service,
        user_id=user_id,
        enterprise_id=ENTERPRISE_ID,
        email=f"{user_id}@example.com",
        roles=["member"],
    )


@pytest.fixture
async def stack(auth_service):
    repository = InMemoryCaseRepository()
    service = CaseService(
        repository,
        team_service=_Teams({"t1": {CREATOR, DRIVER, TEAMMATE}}),
        share_repository=_Shares({CASE_ID: {"t1"}}, []),
        account_reader=_Accounts(),
    )
    await _case(repository, driver_id=DRIVER)
    calls = []

    app = FastAPI()

    @app.post("/api/v1/cases/{case_id}/driver-write")
    async def driver_write(case_id: str, request: Request):
        token = request.headers["Authorization"][7:]
        claims = await auth_service.verify_token_with_revocation_check(
            token, token_type="access"
        )
        if await service.get_case(case_id, claims["sub"], driver_only=True) is None:
            raise HTTPException(status_code=404, detail="Case not found")
        calls.append(claims["sub"])
        return {"written_by": claims["sub"], "n": len(calls)}

    app.add_middleware(
        IdempotencyMiddleware,
        redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
    )
    app.state.auth_service = auth_service
    return app, service, calls


async def _post(app, token, key):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(
            f"/api/v1/cases/{CASE_ID}/driver-write",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
            json={"change": "x"},
        )


async def test_a_former_drivers_retry_replays_and_a_new_write_is_refused(
    stack, auth_service
):
    app, service, calls = stack
    driver = _token(auth_service, DRIVER)

    first = await _post(app, driver, KEY)
    assert first.status_code == 200 and calls == [DRIVER]

    await service.reassign_driver(CASE_ID, CREATOR, CREATOR)

    retry = await _post(app, _token(auth_service, DRIVER), KEY)
    assert retry.status_code == 200
    assert retry.headers.get("X-Idempotency-Replayed") == "true"
    assert retry.json() == first.json()
    assert calls == [DRIVER], "a replay must not run the write again"

    fresh = await _post(app, driver, "driver-retry-0002")
    assert fresh.status_code == 404, "a NEW write by a former driver reached the write"
    assert calls == [DRIVER]


async def test_the_key_is_not_a_door_for_another_principal(stack, auth_service):
    """Keyed on the caller: the creator reusing the former driver's key gets
    their own bucket, so the gate decides — and admits them, now that they
    drive again."""
    app, service, calls = stack

    await _post(app, _token(auth_service, DRIVER), KEY)
    await service.reassign_driver(CASE_ID, DRIVER, CREATOR)

    creator = await _post(app, _token(auth_service, CREATOR), KEY)

    assert creator.headers.get("X-Idempotency-Replayed") != "true"
    assert creator.json()["written_by"] == CREATOR
    assert calls == [DRIVER, CREATOR]
