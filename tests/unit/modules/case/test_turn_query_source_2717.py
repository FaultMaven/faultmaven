"""#2717: ``POST /cases/{case_id}/turns`` takes ``query_source``.

The form field says who wrote ``query``: ``typed``, ``chosen`` or
``generated``. Driven through the MOUNTED case router with FastAPI's own form
parsing; the investigation service is a double that records the payload the
route built and stops the turn there.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from tests.unit.modules.case.test_turn_receipt_1888 import (
    OWNER,
    TURNS,
    _investigating_case,
    _user,
)

pytestmark = pytest.mark.unit

#: The double's stop, after the payload is recorded.
_STOPPED = 418


@pytest.fixture
async def route():
    seen: list = []

    async def _prepare_turn(*, case_id, user_id, payload):
        seen.append(payload)
        raise HTTPException(status_code=_STOPPED, detail="recorded")

    service = MagicMock()
    service.prepare_turn = AsyncMock(side_effect=_prepare_turn)
    case_service = MagicMock()
    case_service.require_case = AsyncMock(return_value=_investigating_case())

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.llm_provider = None
    app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.seen = seen
        yield client


class TestTheQuerySourceField:
    @pytest.mark.parametrize("source", ["typed", "chosen", "generated"])
    async def test_each_value_reaches_the_payload(self, route, source):
        response = await route.post(
            TURNS, data={"query": "It's stable now.", "query_source": source}
        )

        assert response.status_code == _STOPPED, response.text
        (payload,) = route.seen
        assert payload.query_source == source

    @pytest.mark.parametrize(
        "sent", [{}, {"query_source": ""}], ids=["absent", "empty"]
    )
    async def test_absent_or_empty_it_is_typed(self, route, sent):
        response = await route.post(TURNS, data={"query": "It's stable now.", **sent})

        assert response.status_code == _STOPPED, response.text
        (payload,) = route.seen
        assert payload.query_source == "typed"

    async def test_an_unknown_value_is_a_422(self, route):
        response = await route.post(
            TURNS, data={"query": "It's stable now.", "query_source": "pasted"}
        )

        assert response.status_code == 422, response.text
        assert route.seen == []
