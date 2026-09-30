"""Which database a process reports is read from its engine, not its settings.

``get_engine`` is a first-caller-wins singleton that accepts a URL override, so
the configured ``DATABASE_URL`` is only a claim about which database serves
queries. Both reporters here — the health check and the accessor
``GET /admin/config/status`` names its answer from — read the engine instead.
"""

import pytest

from faultmaven.infrastructure.persistence import database


@pytest.fixture
async def fresh_engine(monkeypatch):
    """No engine built yet; whatever a test builds is disposed afterwards."""
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()


async def test_health_reports_the_dialect_that_answered_not_the_configured_url(
    fresh_engine, monkeypatch
):
    monkeypatch.setattr(
        database,
        "get_database_url",
        lambda: "postgresql+asyncpg://app@db.internal/faultmaven",
    )

    out = await database.check_database_health("sqlite+aiosqlite:///:memory:")

    assert out == {"status": "healthy", "database_type": "sqlite"}


async def test_active_backend_is_none_before_an_engine_exists(fresh_engine):
    assert database.active_database_backend() is None


async def test_active_backend_is_the_dialect_of_the_engine_built(fresh_engine):
    database.get_engine("sqlite+aiosqlite:///:memory:")

    assert database.active_database_backend() == "sqlite"
