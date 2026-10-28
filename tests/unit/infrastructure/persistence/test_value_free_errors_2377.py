"""The value-free error listener: what it leaves alone, how it fails, where it runs (#2377).

What it strips is measured on the real drivers in
``tests/integration/test_failed_writes_carry_no_values_2377.py``.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import StatementError

from faultmaven.infrastructure.persistence import value_free_errors
from faultmaven.infrastructure.persistence.value_free_errors import (
    strip_server_values,
)

pytestmark = pytest.mark.unit

ENV_PY = Path(__file__).resolve().parents[4] / "alembic" / "env.py"


def test_an_exception_of_no_driver_is_left_alone():
    """A ``StatementError`` has a ``detail`` too: a list, which ``str()`` reads."""
    error = StatementError("the bind failed", "SELECT 1", {"x": 1}, None)

    strip_server_values(
        SimpleNamespace(original_exception=error, sqlalchemy_exception=error)
    )

    assert error.detail == []
    assert str(error).startswith("the bind failed")


class _UnreadableContext:
    @property
    def original_exception(self):
        raise RuntimeError("a context that cannot be read")


def test_a_failed_strip_is_logged_and_never_raised(caplog):
    """A listener's exception would replace the error being handled."""
    with caplog.at_level(logging.WARNING, logger=value_free_errors.__name__):
        assert strip_server_values(_UnreadableContext()) is None

    assert [r.getMessage() for r in caplog.records] == [
        "Could not strip values from a database error: RuntimeError"
    ]


def test_the_migration_engine_hides_values():
    """``env.py`` runs migrations at import, so its online engine is read from source."""
    tree = ast.parse(ENV_PY.read_text())
    [online] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_migrations_online"
    ]
    [build] = [
        node
        for node in ast.walk(online)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "engine_from_config"
    ]
    keywords = {k.arg: getattr(k.value, "value", None) for k in build.value.keywords}
    calls = {
        ast.unparse(node) for node in ast.walk(online) if isinstance(node, ast.Call)
    }

    assert [target.id for target in build.targets] == ["connectable"]
    assert keywords.get("hide_parameters") is True
    assert "install_value_free_errors(connectable)" in calls
