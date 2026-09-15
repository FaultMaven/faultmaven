"""Shared fixtures for the case-module unit tests.

Holds ONE thing: the prebuilt SQLite database that every test standing up a
real case repository starts from. It lives here rather than in a test module
because two files need it and it was copied between them once already —
``_build_schema_template`` in ``test_declared_filters_reach_the_query.py`` and
``_sqlite_template`` in ``test_list_applies_declared_source_1424.py`` differed
only in two string literals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, text

from faultmaven.infrastructure.persistence.models import Base

#: What a template is keyed on: the tenancy rows written into it.
TemplateKey = tuple[str, str]


def _build(path: Path, enterprise_id: str, user_id: str) -> None:
    """Create the whole schema, plus the tenancy rows, into ``path``.

    Built with a SYNC engine deliberately. ``Base.metadata.create_all`` issues
    DDL for 41 tables, and aiosqlite hops to a worker thread per statement:
    measured at ~0.7s through the async driver against 0.139s synchronously,
    for identical output.

    The tenancy rows are the parents ``cases`` carries foreign keys to. They
    are needed wherever foreign keys are enforced — the application engine sets
    ``PRAGMA foreign_keys=ON`` per connection — so writing them into the
    template keeps every SQLite-backed arm seeded identically rather than
    leaving one of them depending on the pragma being off.
    """
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:eid, :name, :slug)"
                ),
                {
                    "eid": enterprise_id,
                    "name": f"Test Enterprise {enterprise_id}",
                    "slug": enterprise_id.replace("_", "-"),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, "
                    "email, display_name) "
                    "VALUES (:uid, :eid, :uid, :email, 'Seed Owner')"
                ),
                {
                    "uid": user_id,
                    "eid": enterprise_id,
                    "email": f"{user_id}@test",
                },
            )
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def case_schema_template(tmp_path_factory) -> Callable[[str, str], Path]:
    """Build the case schema ONCE per (enterprise, owner) pair; return its path.

    Callers ``shutil.copyfile`` the returned template into their own ``tmp_path``
    and open an engine on the copy — about a millisecond, against 139ms to
    rebuild. That is also why these are file-backed rather than ``:memory:``: a
    ``:memory:`` database lives inside its engine's pooled connection, so it
    cannot be built once and reused, and a cache keyed on the URL would never
    hit.

    Isolation gets STRONGER, not weaker, than a per-test rebuild: every test
    still owns a private file, so no row a test writes can reach another. The
    property under test is always the WHERE clause, never the storage medium.

    Session-scoped and memoised because the cost being avoided is per-test DDL;
    a function-scoped fixture would rebuild and buy nothing.
    """
    root = tmp_path_factory.mktemp("case-schema-templates")
    built: dict[TemplateKey, Path] = {}

    def template_for(enterprise_id: str, user_id: str) -> Path:
        key = (enterprise_id, user_id)
        if key not in built:
            path = root / f"{enterprise_id}--{user_id}.db"
            _build(path, enterprise_id, user_id)
            built[key] = path
        return built[key]

    return template_for
