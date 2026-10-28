"""The value-free error listener never raises, and every app engine carries it (#2377).

What it strips is measured on the real drivers in
``tests/integration/test_failed_writes_carry_no_values_2377.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.bootstrap import data_init
from faultmaven.infrastructure.persistence.value_free_errors import (
    strip_server_values,
)

pytestmark = pytest.mark.unit

ENV_PY = Path(__file__).resolve().parents[4] / "alembic" / "env.py"


class _UnreadableError(Exception):
    @property
    def diag(self):
        raise RuntimeError("a driver field that cannot be read")


def test_the_listener_never_raises():
    """A listener's exception would replace the error being handled."""
    error = _UnreadableError("the database refused the write")

    ctx = SimpleNamespace(original_exception=error, sqlalchemy_exception=None)

    assert strip_server_values(ctx) is None
    assert str(error) == "the database refused the write"


async def test_the_migration_state_engine_hides_values(tmp_path):
    built = []

    def recording(*args, **kwargs):
        built.append(create_async_engine(*args, **kwargs))
        return built[-1]

    with patch("sqlalchemy.ext.asyncio.create_async_engine", recording):
        await data_init._database_revisions(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")

    [engine] = built
    assert engine.sync_engine.hide_parameters
    assert event.contains(engine.sync_engine, "handle_error", strip_server_values)


def test_the_migration_engine_hides_values():
    """``env.py`` runs migrations at import, so its engine is pinned by its source."""
    pinned = ("hide_parameters=True,", "install_value_free_errors(connectable)")
    assert all(line in ENV_PY.read_text() for line in pinned)
