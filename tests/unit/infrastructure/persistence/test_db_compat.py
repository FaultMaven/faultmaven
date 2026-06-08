"""Tests for dialect-aware upsert helper (db_compat.dialect_insert).

Regression coverage for the production bug where repositories hardcoded
``from sqlalchemy.dialects.sqlite import insert`` and the resulting
``on_conflict_do_update`` construct failed to compile under the PostgreSQL
dialect with::

    'OnConflictDoUpdate' object has no attribute 'constraint_target'

This broke default-admin creation (and org/team/llm-config/token-revocation
upserts) on the production PostgreSQL backend while passing on local SQLite.
"""

import pytest
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects import postgresql, sqlite

from faultmaven.infrastructure.persistence.db_compat import dialect_insert

_meta = MetaData()
_table = Table(
    "db_compat_t",
    _meta,
    Column("id", String, primary_key=True),
    Column("v", String),
)


class _FakeBind:
    def __init__(self, name: str):
        self.dialect = type("D", (), {"name": name})()


class _FakeSession:
    """Minimal stand-in exposing get_bind() like a SQLAlchemy session."""

    def __init__(self, name):
        self._bind = _FakeBind(name) if name else None

    def get_bind(self):
        return self._bind


def _upsert(session):
    return (
        dialect_insert(session, _table)
        .values(id="x", v="1")
        .on_conflict_do_update(index_elements=["id"], set_={"v": "1"})
    )


def test_postgres_session_yields_pg_construct_that_compiles():
    """The PG branch must compile under the PostgreSQL dialect (the bug)."""
    stmt = _upsert(_FakeSession("postgresql"))
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in compiled.upper()


def test_postgres_upsert_does_not_raise_constraint_target():
    """Exact regression: must not raise the constraint_target AttributeError."""
    stmt = _upsert(_FakeSession("postgresql"))
    # Should not raise:
    stmt.compile(dialect=postgresql.dialect())


def test_sqlite_session_yields_sqlite_construct_that_compiles():
    stmt = _upsert(_FakeSession("sqlite"))
    compiled = str(stmt.compile(dialect=sqlite.dialect()))
    assert "ON CONFLICT" in compiled.upper()


def test_none_bind_falls_back_to_sqlite():
    """A session with no bound engine defaults to the SQLite construct."""
    stmt = (
        dialect_insert(_FakeSession(None), _table)
        .values(id="x")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    compiled = str(stmt.compile(dialect=sqlite.dialect()))
    assert "ON CONFLICT" in compiled.upper()


@pytest.mark.parametrize(
    "name,expected_module",
    [
        ("postgresql", "postgresql"),
        ("sqlite", "sqlite"),
    ],
)
def test_returns_dialect_specific_module(name, expected_module):
    stmt = dialect_insert(_FakeSession(name), _table)
    assert expected_module in type(stmt).__module__
