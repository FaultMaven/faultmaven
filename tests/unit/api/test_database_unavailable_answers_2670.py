"""A database failure that reaches the request boundary is a 503, by method (#2670).

A database timeout or an unreachable database (``is_database_unavailable``)
that no route answered is 503 ``DATABASE_UNAVAILABLE``. A GET/HEAD saved
nothing: the "not saved" detail and ``Retry-After``. Any other method may have
(an earlier session of the request may have committed): a neutral detail
telling the client to reload first, and no ``Retry-After``. A failure with
``CommitOutcomeUnknown`` on its chain keeps the answer it had.

Driven over HTTP on a scratch app with the production handlers, as
``test_turn_database_timeout_2280.py`` is; authentication is overridden, since
a revocation read meeting the same database answers its own 503.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    internal_server_error_handler,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.exceptions import (
    CommitOutcomeUnknown,
    DatabaseTimeout,
    DatabaseUnavailable,
    RepositoryError,
    ServiceException,
)
from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.sessionless_team_repository import (
    SessionlessTeamRepository,
)
from faultmaven.modules.auth.api.teams import router as teams_router
from faultmaven.modules.auth.domain.services.team_service import TeamService
from tests.unit.infrastructure.persistence.test_database_unavailable_2670 import (
    closed_loopback_port,
)
from tests.unit.modules.case.test_turn_database_timeout_2280 import (
    _assert_database_unavailable,
    _post_turn,
    _stubbed_service,
)

pytestmark = pytest.mark.unit

NOT_SAVED = "not saved"
MAY_HAVE_BEEN_SAVED = (
    "The database could not be reached. Reload before retrying: the request "
    "may have been saved."
)


def _app() -> FastAPI:
    """The production handlers, registered as ``main.py`` registers them."""
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(500, internal_server_error_handler)
    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


def _assert_read_answer(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"
    assert NOT_SAVED in response.json()["detail"]


def _assert_write_answer(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert "retry-after" not in response.headers, "a resend is not invited"
    assert response.json()["detail"] == MAY_HAVE_BEEN_SAVED


# ============================================================
# The regression: a real route, a closed port
# ============================================================


@pytest.fixture
async def unreachable_database(monkeypatch):
    """The engine pointed at a loopback port nothing listens on."""
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_engine_bounds", None)
    monkeypatch.setattr(database, "_session_factory", None)
    database.get_engine(
        f"postgresql+asyncpg://fm@127.0.0.1:{closed_loopback_port()}/fm"
    )
    yield
    await database._engine.dispose()


def _teams_app() -> FastAPI:
    """``/teams`` with a team service whose reads and writes go through
    ``get_db_session``."""
    app = _app()
    app.include_router(teams_router, prefix="/api/v1")
    app.state.team_service = TeamService(
        SessionlessTeamRepository(),
        enterprise_repository=MagicMock(),
        user_repository=MagicMock(),
    )
    app.dependency_overrides[require_authentication] = lambda: SimpleNamespace(
        user_id="user_2670", enterprise_id="7d3f2670-0000-4000-8000-000000002670"
    )
    return app


@pytest.mark.skipif(
    importlib.util.find_spec("asyncpg") is None,
    reason="asyncpg is not installed (the cloud and test extras carry it)",
)
class TestAnUnreachableDatabase:
    async def test_a_read_route_answers_503_not_saved(self, unreachable_database):
        _assert_read_answer(await _call(_teams_app(), "GET", "/api/v1/teams"))

    async def test_a_write_route_answers_503_may_have_been_saved(
        self, unreachable_database
    ):
        response = await _call(
            _teams_app(), "POST", "/api/v1/teams", json={"name": "Payments"}
        )

        _assert_write_answer(response)


# ============================================================
# Whatever carries it to the boundary
# ============================================================


def _raised_from(*links: BaseException) -> BaseException:
    for outer, inner in zip(links, links[1:]):
        outer.__cause__ = inner
    return links[0]


_CARRIERS = {
    "a bare timeout": lambda: DatabaseTimeout("t"),
    "a bare unavailable": lambda: DatabaseUnavailable("u"),
    "a ServiceException": lambda: _raised_from(
        ServiceException("wrapped"), DatabaseTimeout("t")
    ),
    "a ServiceError": lambda: _raised_from(
        RepositoryError("wrapped"), DatabaseUnavailable("u")
    ),
    "an exception nothing maps": lambda: _raised_from(
        RuntimeError("wrapped"), DatabaseUnavailable("u")
    ),
}


def _raising_app(factory) -> FastAPI:
    app = _app()

    async def route():
        raise factory()

    app.add_api_route("/fault-2670", route, methods=["GET", "HEAD", "POST", "DELETE"])
    return app


class TestAtTheBoundary:
    @pytest.mark.parametrize("factory", _CARRIERS.values(), ids=_CARRIERS)
    async def test_a_read_gets_the_not_saved_503(self, factory):
        _assert_read_answer(await _call(_raising_app(factory), "GET", "/fault-2670"))

    @pytest.mark.parametrize("factory", _CARRIERS.values(), ids=_CARRIERS)
    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    async def test_a_write_gets_the_neutral_503(self, factory, method):
        _assert_write_answer(await _call(_raising_app(factory), method, "/fault-2670"))

    async def test_a_head_is_a_read(self):
        response = await _call(
            _raising_app(_CARRIERS["a bare unavailable"]), "HEAD", "/fault-2670"
        )

        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"


class TestACommitThatMayHaveLanded:
    async def test_a_commit_timeout_keeps_the_unmapped_500(self):
        app = _raising_app(
            lambda: _raised_from(DatabaseTimeout("t"), CommitOutcomeUnknown("c"))
        )

        response = await _call(app, "POST", "/fault-2670")

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"
        assert "x-error-code" not in response.headers

    async def test_one_in_a_service_exception_keeps_the_service_error_500(self):
        app = _raising_app(
            lambda: _raised_from(
                ServiceException("w"), DatabaseTimeout("t"), CommitOutcomeUnknown("c")
            )
        )

        response = await _call(app, "POST", "/fault-2670")

        assert response.status_code == 500
        assert response.headers["x-error-code"] == "SERVICE_ERROR"


# ============================================================
# The turn route keeps its own answer
# ============================================================


async def test_an_unreachable_database_in_the_turn_preparation_is_the_not_saved_503():
    """The turn route knows its preparation committed nothing, so it keeps
    the "not saved" 503 with ``Retry-After`` on a POST."""
    response = await _post_turn(
        _stubbed_service(AsyncMock(side_effect=DatabaseUnavailable("refused")))
    )

    _assert_database_unavailable(response)
