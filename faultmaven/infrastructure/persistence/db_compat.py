"""Database dialect-compatibility helpers.

SQLAlchemy's ``INSERT ... ON CONFLICT`` is dialect-specific: the construct
returned by ``sqlalchemy.dialects.sqlite.insert`` is **not** interchangeable
with ``sqlalchemy.dialects.postgresql.insert``. Building a SQLite upsert and
executing it against PostgreSQL raises::

    'OnConflictDoUpdate' object has no attribute 'constraint_target'

because the PostgreSQL compiler inspects attributes the SQLite construct does
not have. The repositories were originally written against SQLite (local/dev)
and silently broke on the production PostgreSQL backend.

This module centralizes the dialect choice so every upsert site stays portable
across SQLite (local) and PostgreSQL (production). Both dialects expose the same
``on_conflict_do_update(index_elements=..., set_=...)`` and
``on_conflict_do_nothing(index_elements=...)`` signatures, so callers build the
``ON CONFLICT`` clause identically regardless of backend.

It also gives every SQLite connection the functions the repositories' SQL
names that SQLite does not provide (:data:`SQLITE_CASEFOLD`).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

#: The case-folding function the account search applies on SQLite, where the
#: built-in ``lower()`` folds ASCII letters only: Python's ``str.lower``, so a
#: search matches on SQLite as it does in memory ("élodie" finds "Élodie").
#: Separately named rather than replacing ``lower()``, which the username and
#: email lookups and their uniqueness checks rely on as it is.
SQLITE_CASEFOLD = "fm_casefold"


def _casefold(value: Optional[str]) -> Optional[str]:
    return value.lower() if isinstance(value, str) else value


def _is_sqlite(dbapi_connection: Any) -> bool:
    if isinstance(dbapi_connection, sqlite3.Connection):
        return True
    from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_connection

    return isinstance(dbapi_connection, AsyncAdapt_aiosqlite_connection)


def _register_sqlite_functions(dbapi_connection: Any, _connection_record: Any) -> None:
    """Every new SQLite connection of every engine in the process — the
    application's, a CLI's, a test's — gets :data:`SQLITE_CASEFOLD`."""
    if _is_sqlite(dbapi_connection):
        dbapi_connection.create_function(
            SQLITE_CASEFOLD, 1, _casefold, deterministic=True
        )


# On the Engine CLASS, so it covers every connection opened after this import,
# whichever engine opens it. Importing this module is what arms it; the user
# repository imports it at module level for that reason.
event.listen(Engine, "connect", _register_sqlite_functions)


def dialect_insert(session: Any, model: Any) -> Any:
    """Return a dialect-appropriate INSERT construct for ``model``.

    Use this instead of importing ``insert`` from a specific dialect so that
    ``.on_conflict_do_update`` / ``.on_conflict_do_nothing`` compile correctly
    against whichever engine the ``session`` is bound to.

    Args:
        session: An (async) SQLAlchemy session bound to the target engine.
        model: The ORM model / table to insert into.

    Returns:
        A dialect-specific ``Insert`` supporting the ``on_conflict_*`` helpers.
    """
    bind = session.get_bind() if hasattr(session, "get_bind") else session.bind
    dialect_name = bind.dialect.name if bind is not None else "sqlite"

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(model)

    # SQLite (local/dev) and any other backend that shares the ON CONFLICT
    # upsert syntax. SQLite is the safe default for local single-user mode.
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert(model)
