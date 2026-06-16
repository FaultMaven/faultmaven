"""Regression guard: PostgreSQL typed-cast writes keep their bound params.

The on-prem cluster's first real-PostgreSQL run 500'd on every case save
("syntax error at or near ':'"). Root cause: SQLAlchemy 2.0's ``text()``
bind parser treats a ``::`` immediately following a placeholder as the start
of a PostgreSQL cast and silently DROPS the preceding ``:name`` bind. So
``:inquiry::jsonb`` compiled to a literal ``:inquiry::jsonb`` while sibling
columns became ``$N`` — mixed param styles asyncpg rejects.

The whole class was dark because:
  - this repository runs only on PostgreSQL (SQLite uses a different repo),
  - its PG path is behind ``@pytest.mark.cloud`` (no PG in CI), and
  - the dialect tests mocked the session and only substring-checked the SQL
    string, never binding params — so a dropped bind was invisible.

These tests need no real Postgres: the drop is observable in
``text(sql)._bindparams``, and the structural scan catches any future
reintroduction at ANY site.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from faultmaven.modules.case.infrastructure import (
    postgresql_hybrid_case_repository as _repo_module,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)

# Resolve from the module itself, not CWD, so the scan is robust wherever
# pytest is invoked from.
_REPO_SOURCE = Path(_repo_module.__file__)


def _repo_for(dialect_name: str | None) -> PostgreSQLHybridCaseRepository:
    session = MagicMock()
    if dialect_name is None:
        session.bind = None
    else:
        dialect = MagicMock()
        dialect.name = dialect_name
        session.bind = MagicMock()
        session.bind.dialect = dialect
    return PostgreSQLHybridCaseRepository(session)


@pytest.mark.unit
class TestCastHelper:
    def test_postgresql_emits_cast_expression(self):
        repo = _repo_for("postgresql")
        assert repo._cast("metadata") == "CAST(:metadata AS JSONB)"
        assert (
            repo._cast("generated_at", "TIMESTAMPTZ")
            == "CAST(:generated_at AS TIMESTAMPTZ)"
        )

    def test_sqlite_emits_bare_placeholder(self):
        repo = _repo_for("sqlite")
        assert repo._cast("metadata") == ":metadata"
        assert repo._cast("generated_at", "TIMESTAMPTZ") == ":generated_at"

    def test_no_bind_defaults_to_bare_placeholder(self):
        # Mirrors _upsert_case_record's "no bind -> sqlite" fallback.
        assert _repo_for(None)._cast("metadata") == ":metadata"

    def test_detects_postgresql_via_get_bind_when_dot_bind_is_none(self):
        """A session whose ``.bind`` is None but whose ``get_bind()`` resolves
        to PostgreSQL must still cast. The repo-selection factory uses that
        same ``.bind`` -> ``get_bind()`` fallback to route such a session to
        THIS repository, so _cast must agree or it would emit SQLite-style
        bare ``:name`` (no CAST) on a live PostgreSQL connection — the exact
        missing-cast failure the fix prevents."""
        session = MagicMock()
        session.bind = None
        pg_bind = MagicMock()
        pg_bind.dialect.name = "postgresql"
        session.get_bind = MagicMock(return_value=pg_bind)

        repo = PostgreSQLHybridCaseRepository(session)
        assert repo._is_pg is True
        assert repo._cast("metadata") == "CAST(:metadata AS JSONB)"

    def test_cast_form_keeps_the_bind_colon_cast_drops_it(self):
        """The crux: CAST(:name AS T) keeps :name bound; :name::T drops it."""
        repo = _repo_for("postgresql")
        good = text(
            f"UPDATE cases SET inquiry = {repo._cast('inquiry')}, "
            f"metadata = {repo._cast('metadata')} WHERE case_id = :case_id"
        )
        assert {"inquiry", "metadata", "case_id"} <= set(good._bindparams)

        # Document the failure mode the fix removes.
        bad = text("UPDATE cases SET inquiry = :inquiry::jsonb WHERE id = :id")
        assert "inquiry" not in bad._bindparams  # dropped by the :: adjacency


@pytest.mark.unit
class TestNoColonCastInSource:
    # Strip only backtick-quoted spans (``...``) before scanning — NOT the
    # whole physical line. Whole-line skipping (the original guard) had a
    # hole: an executable ``text("... :x::jsonb ...")`` line that also
    # carried an unrelated ``:name``-style backtick comment would be skipped
    # despite a real bug. By design, every doc mention of the anti-pattern in
    # the repo is backtick-quoted, so span-stripping exempts exactly the prose
    # and nothing executable.
    _BACKTICK_SPAN = re.compile(r"``[^`]*``")
    _COLON_CAST = re.compile(r":[a-zA-Z_][a-zA-Z0-9_]*::")

    def test_source_has_no_bindparam_colon_cast(self):
        """Structural guard against the entire class: no ``:name::type`` may
        appear in executable SQL. Casts must go through ``_cast()`` (which
        renders CAST(...))."""
        offenders = []
        for lineno, line in enumerate(_REPO_SOURCE.read_text().splitlines(), start=1):
            code = self._BACKTICK_SPAN.sub("", line)
            if self._COLON_CAST.search(code):
                offenders.append(f"{lineno}: {line.strip()}")
        assert not offenders, (
            "Found :name::type colon-casts (SQLAlchemy 2.0 drops the bind — "
            "use self._cast()). If documenting the anti-pattern, wrap it in "
            "double backticks:\n" + "\n".join(offenders)
        )
