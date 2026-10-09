"""The turn route's two 504s, over HTTP, through the route's real except arms (#1905).

Both commit nothing; they differ in what a retry can achieve, and the header
says so:

* ``REQUEST_TIMEOUT`` — the turn used its whole ceiling on THIS input, so the
  same input is likely to exhaust it again. No ``Retry-After``: a client
  retries at most once, and a header inviting a timed re-run would say the
  opposite.
* ``LLM_TIMEOUT`` — the provider timed out, a transient condition:
  ``Retry-After`` stays.

Driven through ``POST /api/v1/cases/{case_id}/turns`` on a scratch app with the
production ``HTTPException`` handler, so the assertion is on the headers a
client actually receives, not on the exception object.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from faultmaven.api.exception_handlers import http_exception_handler
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.config.turn_ceiling import TurnCeiling
from faultmaven.exceptions import LLMException, ServiceException
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models.case import Case

pytestmark = pytest.mark.unit

CASE_ID = "case_19050000beef"


def _case() -> Case:
    return Case(
        case_id=CASE_ID,
        title="Checkout API 502s",
        description="",
        user_id="user_1905",
        enterprise_id="org_test",
        state=CaseState.INQUIRY,
    )


def _user() -> UserDTO:
    return UserDTO(
        user_id="user_1905",
        username="u1905",
        email="u1905@example.com",
        display_name="User 1905",
        is_active=True,
    )


async def _post_turn(prepare_turn, *, ceiling_seconds: float = 120.0) -> httpx.Response:
    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_case())
    investigation_service = MagicMock()
    investigation_service.prepare_turn = prepare_turn
    investigation_service.commit_turn = AsyncMock()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.state.llm_provider = None
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: (
        investigation_service
    )

    ceiling = TurnCeiling(
        provider="test",
        ceiling_seconds=ceiling_seconds,
        response_bound_seconds=ceiling_seconds,
    )
    with patch.object(
        conversation_module, "resolve_turn_ceiling", lambda _settings: ceiling
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{CASE_ID}/turns",
                data={"query": "The checkout API is throwing 502s."},
            )
    investigation_service.commit_turn.assert_not_awaited()
    return response


async def test_request_timeout_sends_no_retry_after():
    async def _hangs(**_):
        await asyncio.Event().wait()

    response = await _post_turn(AsyncMock(side_effect=_hangs), ceiling_seconds=0.2)

    assert response.status_code == 504
    assert response.headers["x-error-code"] == "REQUEST_TIMEOUT"
    assert "retry-after" not in response.headers


async def test_llm_timeout_keeps_retry_after():
    """A provider 504 on the ``ServiceException``'s cause chain: the route's
    ``except ServiceException`` arm maps it via
    ``llm_service_error_http_exception``."""

    async def _provider_timed_out(**_):
        try:
            raise LLMException("upstream timed out", status_code=504)
        except LLMException as cause:
            raise ServiceException("turn failed") from cause

    response = await _post_turn(AsyncMock(side_effect=_provider_timed_out))

    assert response.status_code == 504
    assert response.headers["x-error-code"] == "LLM_TIMEOUT"
    assert response.headers["retry-after"] == "30"


async def test_the_in_flight_claim_lives_for_the_response_bound_not_the_ceiling():
    """The keyed turn's claim must outlive the whole response (commit and
    auto-title included), so the route hands the claim the BOUND it resolved,
    not the ceiling."""
    captured = {}

    async def _open_keyed_turn(**kwargs):
        captured.update(kwargs)
        raise HTTPException(status_code=418, detail="stop here")

    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_case())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: MagicMock()

    ceiling = TurnCeiling(
        provider="test", ceiling_seconds=120.0, response_bound_seconds=138.5
    )
    with (
        patch.object(
            conversation_module, "resolve_turn_ceiling", lambda _settings: ceiling
        ),
        patch.object(conversation_module, "open_keyed_turn", _open_keyed_turn),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{CASE_ID}/turns",
                data={"query": "Still failing?"},
                headers={"Idempotency-Key": "msg_19050001"},
            )

    assert response.status_code == 418
    assert captured["response_bound_seconds"] == 138.5


def test_the_route_documents_both_504_codes_and_which_carries_retry_after():
    """The published 504: both codes in the enum, ``Retry-After`` declared for
    ``LLM_TIMEOUT`` only."""
    documented = conversation_module._TURN_RESPONSES[504]
    headers = documented["headers"]

    assert headers["x-error-code"]["schema"]["enum"] == [
        "REQUEST_TIMEOUT",
        "LLM_TIMEOUT",
    ]
    assert "LLM_TIMEOUT" in headers["Retry-After"]["description"]
    assert "REQUEST_TIMEOUT" not in headers["Retry-After"]["description"]
    assert "at most once" in documented["description"]
