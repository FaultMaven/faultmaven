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
"""

from __future__ import annotations

from typing import Any


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
