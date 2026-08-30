"""The conversion status vocabularies: enum ↔ ORM CHECK ↔ owning migration.

Covers both conversion status columns — ``conversion_jobs.status``
(``ConversionStatus``) and ``conversion_drafts.status`` (``DraftStatus``).

Three places spell each permitted set:

- the enum the row is hydrated into,
- the model's ``CheckConstraint`` (what ``create_all`` builds, i.e. what every
  test database enforces),
- the CHECK in the migration that owns it (what a real deployment enforces) —
  migration 047 for jobs, migration 011 for drafts.

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

#520's ``DraftStatus`` arm turned out to be **already closed on the vocabulary**
but not pinned. The issue (2026-06) describes an enum carrying ``DELETED``
against a CHECK admitting ``rejected``/``archived``; neither is current.
Migration 011 rewrote the CHECK to ``draft|verified|discarded`` and mapped the
existing ``rejected``/``archived`` rows onto ``discarded``, and the enum was
rewritten to match. What was missing is the third leg: the pre-existing guard
compared the enum against the ORM only, which is precisely the comparison
migration 022 records as unable to see the divergence that matters. The
DraftStatus section below adds the migration leg.

#520's ``CaseReport`` arms live in ``tests/unit/modules/case/test_report_vocabulary.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionStatus,
    DraftStatus,
)

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


# ============================================================================
# conversion_drafts.status — DraftStatus (#520 arm 2)
# ============================================================================

_DRAFT_CHECK = "conversion_drafts_status_check"

#: Every migration that mentions the drafts CHECK: the clean baseline created it
#: as ``draft|verified|rejected|archived``, migration 011 rewrote it to
#: ``draft|verified|discarded`` and migrated the rows. Asserted rather than
#: assumed, so a LATER migration that rewrites it again fails this file instead
#: of leaving it comparing the ORM against a superseded definition.
_DRAFT_CHECK_MIGRATIONS = [
    "20260507_0121_c4689af8aa3f_001_clean_baseline.py",
    "20260512_1900_011_conversion_drafts_status_discarded.py",
]
_DRAFT_CHECK_OWNER = _DRAFT_CHECK_MIGRATIONS[-1]


def _draft_orm_check_expression() -> str:
    for constraint in ConversionDraftModel.__table__.constraints:
        if getattr(constraint, "name", None) == _DRAFT_CHECK:
            return str(constraint.sqltext)
    raise AssertionError(
        f"{_DRAFT_CHECK} is gone from the ORM model — this test has gone blind "
        f"rather than passed"
    )


def _migration_011_source() -> str:
    return (_REPO_ROOT / "alembic" / "versions" / _DRAFT_CHECK_OWNER).read_text()


#: Migration 011 spells the constraint FOUR times: raw SQLite table-rebuild DDL
#: and a PostgreSQL ``create_check_constraint``, once each in ``upgrade`` and in
#: ``downgrade``. The helpers below return them in source order — [0] upgrade,
#: [1] downgrade — and assert the count, so a fifth spelling (or a lost one)
#: fails rather than silently shifting which one is compared.
_SPELLINGS_PER_BRANCH = 2


def _draft_checks_from_postgres_branch() -> list[str]:
    """Every expression migration 011 hands ``op.create_check_constraint``.

    Read from the AST, so a mention in a comment or docstring cannot be
    mistaken for the constraint itself.
    """
    found = []
    tree = ast.parse(_migration_011_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if fname != "create_check_constraint" or len(node.args) < 3:
            continue
        first, expr = node.args[0], node.args[2]
        if (
            isinstance(first, ast.Constant)
            and first.value == _DRAFT_CHECK
            and isinstance(expr, ast.Constant)
        ):
            found.append((node.lineno, expr.value))
    assert len(found) == _SPELLINGS_PER_BRANCH, (
        f"expected {_SPELLINGS_PER_BRANCH} create_check_constraint calls for "
        f"{_DRAFT_CHECK} in {_DRAFT_CHECK_OWNER}, found {len(found)}"
    )
    return [expr for _, expr in sorted(found)]


def _draft_checks_from_sqlite_branch() -> list[str]:
    """The same constraint as spelled in 011's SQLite table-rebuild DDL.

    SQLite cannot ALTER a CHECK, so 011 rebuilds the table from raw DDL. That
    branch is the one every developer machine and every CI database actually
    runs, and it is a SEPARATE spelling of the constraint from the PostgreSQL
    branch — so the two can disagree, and nothing but this would say so.
    """
    found = re.findall(
        rf"CONSTRAINT\s+{_DRAFT_CHECK}\s*\n?\s*CHECK\s*\(([^)]*\([^)]*\))\s*\)",
        _migration_011_source(),
    )
    assert len(found) == _SPELLINGS_PER_BRANCH, (
        f"expected {_SPELLINGS_PER_BRANCH} raw-DDL spellings of {_DRAFT_CHECK} "
        f"in {_DRAFT_CHECK_OWNER}, found {len(found)}"
    )
    return found


def _draft_check_from_postgres_branch() -> str:
    return _draft_checks_from_postgres_branch()[0]


def _draft_check_from_sqlite_branch() -> str:
    return _draft_checks_from_sqlite_branch()[0]


def test_the_draft_orm_check_is_present_and_parses():
    """Guards every assertion below: an unparsed expression is an empty set,
    and an empty set is a subset of everything."""
    parsed = _statuses_in(_draft_orm_check_expression())
    assert len(parsed) >= 3, f"CHECK expression did not parse: {parsed}"


def test_both_draft_migration_spellings_parse():
    assert len(_statuses_in(_draft_check_from_postgres_branch())) >= 3
    assert len(_statuses_in(_draft_check_from_sqlite_branch())) >= 3


def test_the_pin_names_every_migration_that_touches_the_draft_check():
    versions = _REPO_ROOT / "alembic" / "versions"
    mentions = sorted(
        p.name for p in versions.glob("*.py") if _DRAFT_CHECK in p.read_text()
    )
    assert mentions == _DRAFT_CHECK_MIGRATIONS


def test_the_two_branches_of_migration_011_agree():
    """A backend-conditional migration writes the constraint twice; a value
    added to one branch only is a vocabulary that differs by backend."""
    assert _statuses_in(_draft_check_from_sqlite_branch()) == _statuses_in(
        _draft_check_from_postgres_branch()
    )


def test_the_downgrade_restores_the_baseline_vocabulary_on_both_branches():
    """A downgrade that does not restore what it replaced leaves the schema in a
    state no migration describes. Both branches' downgrade spellings must put
    back the clean baseline's four values."""
    baseline = {"draft", "verified", "rejected", "archived"}
    assert _statuses_in(_draft_checks_from_sqlite_branch()[1]) == baseline
    assert _statuses_in(_draft_checks_from_postgres_branch()[1]) == baseline


def test_the_draft_orm_check_matches_the_owning_migration():
    """The leg the pre-existing enum-vs-ORM guard could not see.

    ``create_all`` (tests) and ``alembic upgrade`` (deployments) must build the
    same constraint, or a value is writable in CI and rejected in production.
    """
    assert _statuses_in(_draft_orm_check_expression()) == _statuses_in(
        _draft_check_from_postgres_branch()
    )


def test_the_draft_enum_admits_every_value_the_database_does():
    """A value the CHECK permits but the enum lacks is a row that can be written
    and then cannot be read — a 500 on read, not a refusal on write."""
    permitted = _statuses_in(_draft_orm_check_expression())
    unreadable = permitted - {s.value for s in DraftStatus}
    assert (
        not unreadable
    ), f"{sorted(unreadable)} can be stored but DraftStatus cannot parse them"


def test_every_draft_enum_value_is_storable():
    permitted = _statuses_in(_draft_orm_check_expression())
    unstorable = {s.value for s in DraftStatus} - permitted
    assert not unstorable, (
        f"{sorted(unstorable)} is selectable in code but rejected by the CHECK; "
        f"a draft in that state fails to commit with a bare IntegrityError"
    )


def test_the_retired_draft_statuses_are_gone_from_both_halves():
    """#520 named ``DELETED`` in the enum against ``rejected``/``archived`` in
    the CHECK. Migration 011 retired all three — ``rejected``/``archived`` rows
    were rewritten to ``discarded`` before the constraint was narrowed, so the
    narrowing could not reject a row that existed. Pinned so the issue's
    description cannot be reintroduced on either side."""
    retired = {"deleted", "rejected", "archived"}
    assert not retired & {s.value for s in DraftStatus}
    assert not retired & _statuses_in(_draft_orm_check_expression())


def test_discarded_is_readable_rather_than_merely_permitted():
    assert DraftStatus("discarded") is DraftStatus.DISCARDED


def test_the_draft_check_rejects_a_retired_status_and_accepts_discarded():
    """Executed round trip: a REJECTED row plus an ACCEPTED control, so the
    rejection above is the constraint biting and not a broken INSERT."""
    import sqlalchemy as sa
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    ConversionDraftModel.__table__.create(engine)

    def row(**over):
        base = dict(
            id="d1",
            organization_id="org-1",
            conversion_id="conv-1",
            runbook_id="rb-1",
            title="A draft",
            file_path="/tmp/rb-1.md",
            status="discarded",
            source_type="document",
            validation_passed=True,
        )
        base.update(over)
        return base

    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError, match=_DRAFT_CHECK):
            conn.execute(
                sa.insert(ConversionDraftModel).values(
                    **row(id="d_r", status="rejected")
                )
            )
    with engine.begin() as conn:
        conn.execute(sa.insert(ConversionDraftModel).values(**row(id="d_ok")))
    with engine.connect() as conn:
        stored = (
            conn.execute(sa.select(ConversionDraftModel.__table__.c.status))
            .scalars()
            .all()
        )
    assert stored == ["discarded"]
