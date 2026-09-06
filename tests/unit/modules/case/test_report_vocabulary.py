"""``reports.report_type`` and ``reports.format`` are each ONE vocabulary.

Same shape as ``tests/unit/modules/knowledge/test_conversion_status_vocabulary.py``
and for the same reason: three places spell each permitted set —

- the Python type the row is hydrated into (``ReportType`` / the ``format``
  Literal on ``CaseReport``),
- ``ReportModel``'s ``CheckConstraint`` (what ``create_all`` builds, i.e. what
  every test database enforces),
- the migration's CHECK (what a real deployment enforces),

and migration 022's docstring records the failure mode when they diverge: an
ORM-vs-migration gap is **unobservable on SQLite** — tests build their schema
from the ORM and pass — and surfaces only as a 500 on PostgreSQL, where the
schema came from alembic.

The two report arms of #520 were both real, and they diverge in OPPOSITE
directions, which is why they are fixed on opposite sides:

- ``format`` was ``Literal["markdown"]`` while ``reports_format_check`` admits
  ``'html'``. A stored ``html`` row is hydrated by ``_row_to_report`` straight
  into ``CaseReport`` — so a row the database accepts raised ``ValidationError``
  on READ. Fixed by widening the Literal, the side that cannot reject a row that
  already exists.
- ``ReportType`` has ``RUNBOOK``, which ``reports_type_check`` rejects. Here the
  CHECK is right and stays: a runbook is not a ``reports`` row, it is a
  ``conversion_drafts``/``knowledge_items`` artifact. ``RUNBOOK`` also stays,
  because it is live API surface (the ``?report_type=runbook`` projection). What
  was missing is the declared subset — ``PERSISTED_REPORT_TYPES`` — and a check
  that the write path screens against it instead of discovering the constraint
  four layers down.

The executed round trip below runs against the ORM-built schema; the string pins
above it are what carry that result over to the deployed schema.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from faultmaven.infrastructure.persistence import models as persistence_models
from faultmaven.infrastructure.persistence.models import ReportModel
from faultmaven.modules.case.contracts import (
    PERSISTED_REPORT_TYPES,
    CaseReport,
    ReportType,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VERSIONS = _REPO_ROOT / "alembic" / "versions"

#: The migrations that mention each CHECK. Asserted, not assumed: a NEW
#: migration that rewrites one of these constraints makes this list wrong and
#: fails the test, which is the point — the pin must be re-aimed at whatever now
#: owns the constraint rather than silently keep checking a superseded file.
#: Globbed rather than spelled: the baseline's filename carries its revision
#: hash, so a rename would read here as a MISSING constraint rather than as the
#: rename it is — the same trap Stage 1 fixed in the last-admin guard.
_BASELINE = sorted(_VERSIONS.glob("*_001_enterprise_baseline.py"))
_OWNING_MIGRATIONS = {
    "reports_type_check": [p.name for p in _BASELINE],
    "reports_format_check": [p.name for p in _BASELINE],
}


def _values_in(expr: str) -> set[str]:
    """The quoted values from a ``col IN ('a', 'b', ...)`` CHECK expression."""
    return set(re.findall(r"'([^']+)'", expr))


def _orm_check_expression(name: str) -> str:
    for constraint in ReportModel.__table__.constraints:
        if getattr(constraint, "name", None) == name:
            return str(constraint.sqltext)
    raise AssertionError(
        f"{name} is gone from ReportModel — this test has gone blind rather "
        f"than passed"
    )


def _migration_files_mentioning(name: str) -> list[str]:
    return sorted(p.name for p in _VERSIONS.glob("*.py") if name in p.read_text())


def _migration_check_expression(name: str) -> str:
    """The CHECK expression the owning migration writes, read from its AST.

    AST rather than a text search so a mention in a comment or a docstring
    cannot be mistaken for the constraint itself.
    """
    owners = _OWNING_MIGRATIONS[name]
    tree = ast.parse((_VERSIONS / owners[-1]).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if fname not in ("CheckConstraint", "create_check_constraint"):
            continue
        named = any(
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == name
            for kw in node.keywords
        )
        if not named:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
    raise AssertionError(f"{name} not found as a CheckConstraint in {owners[-1]}")


# ----------------------------------------------------------------------------
# Guards on the guards: an unparsed expression is an empty set, and an empty set
# is a subset of everything, so every assertion below would pass vacuously.
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,minimum", [("reports_type_check", 2), ("reports_format_check", 2)]
)
def test_the_orm_check_is_present_and_parses(name, minimum):
    parsed = _values_in(_orm_check_expression(name))
    assert len(parsed) >= minimum, f"CHECK expression did not parse: {parsed}"


@pytest.mark.parametrize("name", ["reports_type_check", "reports_format_check"])
def test_the_pin_names_every_migration_that_touches_the_check(name):
    """Re-aims the pin when a migration rewrites the constraint.

    Without this, a later migration could widen or narrow the deployed CHECK
    while this file kept comparing the ORM against the clean baseline.
    """
    assert _migration_files_mentioning(name) == _OWNING_MIGRATIONS[name]


@pytest.mark.parametrize(
    "name,minimum", [("reports_type_check", 2), ("reports_format_check", 2)]
)
def test_the_migration_check_is_present_and_parses(name, minimum):
    parsed = _values_in(_migration_check_expression(name))
    assert len(parsed) >= minimum, f"migration CHECK did not parse: {parsed}"


# ----------------------------------------------------------------------------
# ORM ↔ migration: the SQLite-invisible leg
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["reports_type_check", "reports_format_check"])
def test_the_orm_check_matches_the_owning_migration(name):
    assert _values_in(_orm_check_expression(name)) == _values_in(
        _migration_check_expression(name)
    )


# ----------------------------------------------------------------------------
# Arm 3: report_type — the API vocabulary is a declared superset of storage
# ----------------------------------------------------------------------------


def test_the_persisted_subset_is_exactly_what_the_check_admits():
    """``PERSISTED_REPORT_TYPES`` is the single source of truth for what a
    repository may write, so it must equal the constraint exactly — in both
    directions. A member the CHECK lacks fails to commit with a bare
    ``IntegrityError``; a CHECK value missing from it is a row nothing will
    write and ``ReportType`` had better still parse (next test)."""
    assert {t.value for t in PERSISTED_REPORT_TYPES} == _values_in(
        _orm_check_expression("reports_type_check")
    )


def test_the_enum_admits_every_value_the_database_does():
    """``_row_to_report`` does ``ReportType(row.report_type)``.

    A value the CHECK permits but the enum lacks is a row that can be written
    and then cannot be read — a 500 on read, not a refusal on write.
    """
    permitted = _values_in(_orm_check_expression("reports_type_check"))
    unreadable = permitted - {t.value for t in ReportType}
    assert not unreadable, (
        f"{sorted(unreadable)} can be stored but ReportType cannot parse them; "
        f"_row_to_report would raise ValueError -> 500"
    )


def test_the_only_non_persistable_report_type_is_runbook():
    """The declared asymmetry, pinned.

    A new ``ReportType`` member lands in neither half by default and fails here,
    which forces the author to say whether it is a ``reports`` row (add it to
    PERSISTED_REPORT_TYPES *and* to the CHECK, via a migration) or a projection
    like RUNBOOK (add it here).
    """
    assert set(ReportType) - PERSISTED_REPORT_TYPES == {ReportType.RUNBOOK}


def test_the_persistence_layer_enum_is_the_storage_vocabulary():
    """``persistence.models.ReportType`` is a third spelling of the same set and
    must track the CHECK it sits next to, not the wider API enum."""
    assert {t.value for t in persistence_models.ReportType} == _values_in(
        _orm_check_expression("reports_type_check")
    )


# ----------------------------------------------------------------------------
# Arm 4: format — the database is the wider half
# ----------------------------------------------------------------------------


def _format_literal_values() -> set[str]:
    import typing

    return set(typing.get_args(CaseReport.model_fields["format"].annotation))


def test_case_report_can_hydrate_every_format_the_database_admits():
    """The #520 arm: a stored ``html`` row must not 500 on read."""
    permitted = _values_in(_orm_check_expression("reports_format_check"))
    unreadable = permitted - _format_literal_values()
    assert not unreadable, (
        f"{sorted(unreadable)} can be stored but CaseReport.format rejects it; "
        f"_row_to_report would raise ValidationError -> 500"
    )


def test_case_report_cannot_name_a_format_the_database_rejects():
    """The other direction: a value the model permits and the CHECK does not
    fails the commit with a bare ``IntegrityError``."""
    permitted = _values_in(_orm_check_expression("reports_format_check"))
    unstorable = _format_literal_values() - permitted
    assert (
        not unstorable
    ), f"{sorted(unstorable)} is selectable in code but rejected by the CHECK"


def test_html_is_readable_rather_than_merely_permitted():
    """The specific 500 this closes, executed rather than argued."""
    report = CaseReport(
        case_id="case_0123456789ab",
        report_type=ReportType.RESOLUTION_SUMMARY,
        title="A sufficiently long report title",
        content="<p>body</p>",
        format="html",
        generation_status="completed",
        generation_time_ms=5,
    )
    assert report.format == "html"


# ----------------------------------------------------------------------------
# One definition per concept
# ----------------------------------------------------------------------------


def test_the_legacy_models_layer_does_not_fork_the_vocabulary():
    """``faultmaven.models.report`` used to redefine ``ReportType`` and
    ``CaseReport``, so both #520 arms existed twice and a fix to the canonical
    models would not have reached the copy. It re-exports now; identity is what
    proves it is a re-export and not a fresh definition that happens to agree
    today."""
    from faultmaven.models import report as legacy

    assert legacy.ReportType is ReportType
    assert legacy.CaseReport is CaseReport
    assert legacy.PERSISTED_REPORT_TYPES is PERSISTED_REPORT_TYPES


# ----------------------------------------------------------------------------
# Executed round trip: a REJECTED row plus an ACCEPTED control, per constraint
# ----------------------------------------------------------------------------


def _reports_engine():
    engine = create_engine("sqlite://")
    ReportModel.__table__.create(engine)
    return engine


def _row(**over):
    base = dict(
        report_id="r1",
        enterprise_id="ent-1",
        organization_id="org-1",
        case_id="case_0123456789ab",
        report_type="resolution_summary",
        version=1,
        is_current=True,
        linked_to_closure=False,
        title="A sufficiently long report title",
        content="body",
        format="markdown",
        generation_status="completed",
        generation_time_ms=5,
    )
    base.update(over)
    return base


def test_the_type_check_rejects_runbook_and_accepts_a_summary():
    engine = _reports_engine()
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError, match="reports_type_check"):
            conn.execute(
                sa.insert(ReportModel).values(
                    **_row(report_id="r_rb", report_type="runbook")
                )
            )
    # ACCEPTED control — without it a broken INSERT would read as a pass.
    with engine.begin() as conn:
        conn.execute(
            sa.insert(ReportModel).values(
                **_row(report_id="r_ok", report_type="closure_summary")
            )
        )
    with engine.connect() as conn:
        stored = (
            conn.execute(sa.select(ReportModel.__table__.c.report_type)).scalars().all()
        )
    assert stored == ["closure_summary"]


def test_the_format_check_accepts_html_and_rejects_an_unlisted_format():
    engine = _reports_engine()
    # ACCEPTED: this is the row that used to 500 on read.
    with engine.begin() as conn:
        conn.execute(
            sa.insert(ReportModel).values(**_row(report_id="r_html", format="html"))
        )
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError, match="reports_format_check"):
            conn.execute(
                sa.insert(ReportModel).values(**_row(report_id="r_pdf", format="pdf"))
            )
    with engine.connect() as conn:
        stored = conn.execute(sa.select(ReportModel.__table__.c.format)).scalars().all()
    assert stored == ["html"]
