"""A slow PostgreSQL during a turn's preparation answers 503, on a real server (#2280).

The first bound a live but slow server meets is its own ``statement_timeout``,
which the engine's ``begin`` listener sets per transaction below the client's
command timeout. PostgreSQL then cancels the statement and sends SQLSTATE
57014; the connection survives. The turn must answer that the way it answers
the client's own timeout: 503 ``DATABASE_UNAVAILABLE``, nothing committed. A
``pg_sleep`` longer than the statement timeout is the slow statement.

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 127.0.0.1:5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv-cloud/bin/pytest tests/integration/test_turn_statement_timeout_postgres_2280.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from tests.unit.modules.agent.test_one_commit_per_turn_1882 import _engine, _service
from tests.unit.modules.case.test_turn_database_timeout_2280 import (
    _assert_database_unavailable,
    _post_turn,
)
from tests.utils import reset_settings_singleton

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def one_second_statement_timeout(monkeypatch):
    """The app's engine on the real server, its statement_timeout 1 s (a 3 s
    command timeout less a 2 s margin)."""
    monkeypatch.setenv("DATABASE_COMMAND_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS", "2")
    reset_settings_singleton()
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_engine_bounds", None)
    monkeypatch.setattr(database, "_session_factory", None)
    engine = database.get_engine(os.environ["DATABASE_URL"])
    assert database.active_database_bounds().statement_timeout_ms == 1000
    yield engine
    await engine.dispose()
    monkeypatch.undo()
    reset_settings_singleton()


class _SlowCaseRead(SessionlessCaseRepository):
    """The turn's first read, the case load, as a statement the server cancels."""

    backend_pid = None

    async def get(self, case_id):
        async with get_db_session() as session:
            self.backend_pid = (
                await session.execute(text("SELECT pg_backend_pid()"))
            ).scalar()
            await session.execute(text("SELECT pg_sleep(5)"))


async def test_the_servers_statement_timeout_answers_503(one_second_statement_timeout):
    repository = _SlowCaseRead()
    service = _service(repository, _engine(repository))
    service.commit_turn = AsyncMock()

    response = await _post_turn(service)

    _assert_database_unavailable(response)
    service.commit_turn.assert_not_awaited()
    # The server cancelled its own statement; the client did not abort the
    # connection, so the SAME backend is back in the pool and answers the next
    # read (a client abort would hand out a fresh connection, another pid).
    assert one_second_statement_timeout.pool.checkedout() == 0
    async with get_db_session() as session:
        pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar()
    assert repository.backend_pid is not None
    assert pid == repository.backend_pid
