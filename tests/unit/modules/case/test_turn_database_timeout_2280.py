"""A database that stops answering while a turn is prepared is a 503, not a timeout of the turn (#2280).

The turn route answers 504 for two timeouts of the TURN, and both say
something about a retry: ``REQUEST_TIMEOUT`` (the turn used its whole ceiling
on this input, retry at most once) and ``LLM_TIMEOUT`` (the provider timed out,
retry after ``Retry-After``). A database that does not answer is neither. Since
#2280 bounds every database command, its timeout reaches the turn, and it is
answered the way the revocation read answers the same cause: 503, its own
``x-error-code`` (``DATABASE_UNAVAILABLE``), a ``Retry-After``, nothing
committed.

The distinction is made at the source: ``get_db_session``, the one session
every repository read goes through, turns the driver's ``TimeoutError`` into
``DatabaseTimeout``. The turn's preparation lets it through whatever wrapped it
on the way, and the route maps it. A ``TimeoutError`` from anywhere else keeps
its 504.

Driven over HTTP through ``POST /api/v1/cases/{case_id}/turns`` on a scratch app
with the production ``HTTPException`` handler, as the #1905 tests are.
"""

from __future__ import annotations

import asyncio
import importlib.util
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from faultmaven.api.exception_handlers import http_exception_handler
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.config.turn_ceiling import TurnCeiling
from faultmaven.exceptions import DatabaseTimeout, LLMException, ServiceException
from faultmaven.infrastructure.persistence import database
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.domain.models.case import Case
from tests.case_builders import inquiry_case
from tests.silent_postgres import SilentPostgres
from tests.utils import reset_settings_singleton

pytestmark = pytest.mark.unit

CASE_ID = "case_22800000beef"
USER_ID = "user_2280"


def _case() -> Case:
    return inquiry_case(
        case_id=CASE_ID,
        title="Checkout API 502s",
        description="",
        user_id=USER_ID,
        enterprise_id="00000000-0000-0000-0000-000000000002",
    )


def _user() -> UserDTO:
    return UserDTO(
        user_id=USER_ID,
        username="u2280",
        email="u2280@example.com",
        display_name="User 2280",
        is_active=True,
    )


async def _post_turn(
    investigation_service, *, ceiling_seconds: float = 120.0
) -> httpx.Response:
    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_case())

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
            return await client.post(
                f"/api/v1/cases/{CASE_ID}/turns",
                data={"query": "The checkout API is throwing 502s."},
            )


def _stubbed_service(prepare_turn) -> MagicMock:
    service = MagicMock()
    service.prepare_turn = prepare_turn
    service.commit_turn = AsyncMock()
    return service


def _assert_database_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"
    assert "not saved" in response.json()["detail"]


# ============================================================
# The three answers, side by side
# ============================================================


class TestEachTimeoutKeepsItsOwnAnswer:
    async def test_a_database_timeout_in_the_preparation_is_a_503(self):
        async def _database_did_not_answer(**_):
            raise DatabaseTimeout("the database did not answer")

        service = _stubbed_service(AsyncMock(side_effect=_database_did_not_answer))
        response = await _post_turn(service)

        _assert_database_unavailable(response)
        service.commit_turn.assert_not_awaited()

    async def test_an_llm_timeout_is_still_a_504_llm_timeout(self):
        async def _provider_timed_out(**_):
            try:
                raise LLMException("upstream timed out", status_code=504)
            except LLMException as cause:
                raise ServiceException("turn failed") from cause

        response = await _post_turn(
            _stubbed_service(AsyncMock(side_effect=_provider_timed_out))
        )

        assert response.status_code == 504
        assert response.headers["x-error-code"] == "LLM_TIMEOUT"
        assert response.headers["retry-after"] == "30"

    async def test_the_ceiling_is_still_a_504_request_timeout(self):
        async def _hangs(**_):
            await asyncio.Event().wait()

        response = await _post_turn(
            _stubbed_service(AsyncMock(side_effect=_hangs)), ceiling_seconds=0.2
        )

        assert response.status_code == 504
        assert response.headers["x-error-code"] == "REQUEST_TIMEOUT"
        assert "retry-after" not in response.headers

    async def test_a_plain_timeout_error_from_elsewhere_keeps_the_504(self):
        """Only the database's timeout is relabelled: a bare ``TimeoutError``
        that did not come out of a database session is still the turn's."""

        async def _times_out(**_):
            raise TimeoutError()

        response = await _post_turn(_stubbed_service(AsyncMock(side_effect=_times_out)))

        assert response.status_code == 504
        assert response.headers["x-error-code"] == "REQUEST_TIMEOUT"


# ============================================================
# The source: a real driver timeout, through the real preparation
# ============================================================


@pytest.fixture
async def silent_database(monkeypatch):
    """The app's engine pointed at a PostgreSQL that never answers, with a
    0.3 s command timeout. Yields the stand-in, which counts CancelRequests."""
    for key, value in {
        "DATABASE_COMMAND_TIMEOUT_SECONDS": "0.3",
        "DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS": "0.1",
    }.items():
        monkeypatch.setenv(key, value)
    reset_settings_singleton()
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_engine_bounds", None)
    monkeypatch.setattr(database, "_session_factory", None)
    async with SilentPostgres() as server:
        database.get_engine(f"postgresql+asyncpg://fm@{server.host}:{server.port}/fm")
        yield server
        await database._engine.dispose()
    monkeypatch.undo()
    reset_settings_singleton()


@pytest.mark.skipif(
    importlib.util.find_spec("asyncpg") is None, reason="asyncpg is not installed"
)
class TestARealSilentDatabase:
    async def test_the_session_names_the_drivers_timeout(self, silent_database):
        with pytest.raises(DatabaseTimeout) as raised:
            async with database.get_db_session() as session:
                await session.execute(text("SELECT 1"))

        assert isinstance(raised.value, TimeoutError), "existing catchers still match"
        assert isinstance(raised.value.__cause__, TimeoutError)
        assert silent_database.cancel_requests == 0

    async def test_a_turn_whose_case_read_meets_it_answers_503(self, silent_database):
        """The real ``InvestigationService.prepare_turn`` on the real sessionless
        repository: its first read, the case load, meets the silent database."""
        from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
            SessionlessCaseRepository,
        )
        from tests.unit.modules.agent.test_one_commit_per_turn_1882 import (
            _engine,
            _service,
        )

        repository = SessionlessCaseRepository()
        service = _service(repository, _engine(repository))
        service.commit_turn = AsyncMock()

        response = await _post_turn(service)

        _assert_database_unavailable(response)
        service.commit_turn.assert_not_awaited()


# ============================================================
# Wrapped on the way, and after the commit
# ============================================================


class TestThePreparationLetsItThrough:
    async def test_a_database_timeout_the_engine_wrapped_is_still_named(self):
        """The engine wraps every failure as ``MilestoneEngineError`` and the
        preparation wraps that as ``ServiceException``; the database's timeout
        on that chain must still come out as itself."""
        from faultmaven.core.investigation.milestone_engine.errors import (
            MilestoneEngineError,
        )
        from tests.unit.modules.agent.test_one_commit_per_turn_1882 import (
            _engine,
            _service,
        )

        repository = MagicMock()
        repository.get = AsyncMock(return_value=_case())
        service = _service(repository, _engine(repository))

        async def _engine_failed(*_args, **_kwargs):
            try:
                raise DatabaseTimeout("the database did not answer")
            except DatabaseTimeout as cause:
                raise MilestoneEngineError("Turn processing failed") from cause

        with patch.object(
            service, "_verify_access_and_reserve", side_effect=_engine_failed
        ):
            with pytest.raises(DatabaseTimeout):
                from faultmaven.core.investigation.schemas import TurnPayload

                await service.prepare_turn(
                    case_id=CASE_ID,
                    user_id=USER_ID,
                    payload=TurnPayload(query="what does the pool log show?"),
                )


class TestAfterTheCommit:
    async def test_a_database_timeout_after_the_commit_is_not_answered_as_unsaved(
        self,
    ):
        """The turn committed; a database timeout while the response is gated
        for the viewer must not say "nothing was saved" (neither the 503 nor the
        504 does): it is the 500 any later failure gets."""
        service = _stubbed_service(AsyncMock(return_value=MagicMock()))
        service.commit_turn = AsyncMock(return_value=MagicMock())

        async def _gate_times_out(*_args, **_kwargs):
            raise DatabaseTimeout("the database did not answer")

        with (
            patch.object(
                conversation_module,
                "_auto_title_case_if_default",
                AsyncMock(return_value=None),
            ),
            patch.object(
                conversation_module,
                "_turn_gated_for_viewer",
                side_effect=_gate_times_out,
            ),
        ):
            response = await _post_turn(service)

        service.commit_turn.assert_awaited_once()
        assert response.status_code == 500
        assert "saved" not in response.json()["detail"]


def test_the_route_documents_the_503():
    documented = conversation_module._TURN_RESPONSES[503]

    assert "DATABASE_UNAVAILABLE" in documented["description"]
    assert "nothing of the turn committed" in documented["description"]
    assert "Retry-After" in documented["headers"]
