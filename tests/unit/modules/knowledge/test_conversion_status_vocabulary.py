"""``ConversionStatus``, the ORM CHECK, and the head migration are ONE vocabulary.

Three places spell the permitted set of ``conversion_jobs.status``:

- ``ConversionStatus`` (the enum ``get_conversion`` constructs from the column),
- ``ConversionJobModel``'s ``CheckConstraint`` (what ``create_all`` builds, i.e.
  what every test database enforces),
- migration 047's CHECK (what a real deployment enforces).

They were kept in step by a comment. That is exactly the arrangement migration
022's docstring records as the invisible failure: a divergence between the ORM
metadata and the migration is **unobservable on SQLite** — tests build their
schema from the ORM and pass — and surfaces only as a 500 on PostgreSQL, where
the schema came from alembic.

The two live halves of #520's ``ConversionStatus`` arm were both real:

- ``PARTIAL`` was in the enum and NOT in the CHECK, so the ordinary outcome of a
  multi-failure-mode document could not be written at all (bare
  ``IntegrityError``). Migration 047 closed it.
- ``'cancelled'`` was in the CHECK and NOT in the enum, so a row the database
  accepts raised ``ValueError`` in ``ConversionStatus(job.status)`` — a 500 on
  ``GET /knowledge/conversions/{id}``. Closed by widening the enum, which is the
  side that cannot reject an existing row.

#520's ``DraftStatus`` and ``CaseReport`` arms are a different subsystem and
remain open; this file covers ``ConversionStatus`` only.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from faultmaven.infrastructure.persistence.models import ConversionJobModel
from faultmaven.modules.knowledge.domain.models.conversion import ConversionStatus

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _statuses_in(expr: str) -> set[str]:
    """The quoted values from a ``status IN ('a', 'b', ...)`` CHECK expression."""
    return set(re.findall(r"'([^']+)'", expr))


def _orm_check_expression() -> str:
    for constraint in ConversionJobModel.__table__.constraints:
        if getattr(constraint, "name", None) == "conversion_jobs_status_check":
            return str(constraint.sqltext)
    raise AssertionError(
        "conversion_jobs_status_check is gone from the ORM model — this test has "
        "gone blind rather than passed"
    )


def _migration_047():
    path = next(
        (_REPO_ROOT / "alembic" / "versions").glob(
            "*_047_conversion_job_partial_status.py"
        )
    )
    spec = importlib.util.spec_from_file_location("mig047_vocab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_orm_check_is_present_and_parses():
    """Guards every assertion below: an unparsed expression is an empty set,
    and an empty set is a subset of everything."""
    parsed = _statuses_in(_orm_check_expression())
    assert len(parsed) >= 4, f"CHECK expression did not parse: {parsed}"


def test_the_orm_check_matches_the_head_migration():
    """The divergence class migration 022 records as SQLite-invisible.

    ``create_all`` (tests) and ``alembic upgrade`` (deployments) must build the
    same constraint, or a value is writable in CI and rejected in production.
    """
    assert _statuses_in(_orm_check_expression()) == _statuses_in(_migration_047()._NEW)


def test_the_enum_admits_every_value_the_database_does():
    """``get_conversion`` does ``ConversionStatus(job.status)``.

    A value the CHECK permits but the enum lacks is a row that can be written
    and then cannot be read — a 500 on read, not a refusal on write. The enum
    must therefore be a SUPERSET of the CHECK, and this is the direction that
    matters.
    """
    permitted = _statuses_in(_orm_check_expression())
    known = {s.value for s in ConversionStatus}
    unreadable = permitted - known
    assert not unreadable, (
        f"{sorted(unreadable)} can be stored but ConversionStatus cannot parse "
        f"them; get_conversion would raise ValueError -> 500"
    )


def test_every_enum_value_is_storable():
    """The other direction, which #520 also names: ``PARTIAL`` was here and not
    in the CHECK, so the value the code selects could not be committed."""
    permitted = _statuses_in(_orm_check_expression())
    unstorable = {s.value for s in ConversionStatus} - permitted
    assert not unstorable, (
        f"{sorted(unstorable)} is selectable in code but rejected by the CHECK; "
        f"a job in that state fails to commit with a bare IntegrityError"
    )


def test_cancelled_is_readable_rather_than_merely_permitted():
    """The specific 500 this closes, executed rather than argued."""
    assert ConversionStatus("cancelled") is ConversionStatus.CANCELLED


def test_partial_is_readable():
    assert ConversionStatus("partial") is ConversionStatus.PARTIAL
