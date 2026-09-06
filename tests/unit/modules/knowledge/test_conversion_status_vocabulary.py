"""The conversion status vocabularies: enum ↔ ORM CHECK ↔ owning migration.

Covers both conversion status columns — ``conversion_jobs.status``
(``ConversionStatus``) and ``conversion_drafts.status`` (``DraftStatus``).

Three places spell each permitted set:

- the enum the row is hydrated into,
- the model's ``CheckConstraint`` (what ``create_all`` builds, i.e. what every
  test database enforces),
- the CHECK in the migration that owns it (what a real deployment enforces) —
  the schema baseline, which states each vocabulary once.

They were kept in step by a comment. That is exactly the arrangement migration
022's docstring records as the invisible failure: a divergence between the ORM
metadata and the migration is **unobservable on SQLite** — tests build their
schema from the ORM and pass — and surfaces only as a 500 on PostgreSQL, where
the schema came from alembic.

The two live halves of #520's ``ConversionStatus`` arm were both real:

- ``PARTIAL`` was in the enum and NOT in the CHECK, so the ordinary outcome of a
  multi-failure-mode document could not be written at all (bare
  ``IntegrityError``). The baseline's CHECK admits it.
- ``'cancelled'`` was in the CHECK and NOT in the enum, so a row the database
  accepts raised ``ValueError`` in ``ConversionStatus(job.status)`` — a 500 on
  ``GET /knowledge/conversions/{id}``. Closed by widening the enum, which is the
  side that cannot reject an existing row.

#520's ``DraftStatus`` arm turned out to be **already closed on the vocabulary**
but not pinned. The issue (2026-06) describes an enum carrying ``DELETED``
against a CHECK admitting ``rejected``/``archived``; neither is current.
The CHECK is ``draft|verified|discarded`` and the enum matches it. What was
missing is the third leg: the pre-existing guard
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


def _baseline_path() -> Path:
    """The schema baseline. Globbed rather than spelled: the filename carries a
    revision hash, so a rename would read as a MISSING constraint."""
    versions = sorted((_REPO_ROOT / "alembic" / "versions").glob("*_001_*.py"))
    assert (
        len(versions) == 1
    ), f"expected exactly one baseline migration, found {[p.name for p in versions]}"
    return versions[0]


def _check_in_baseline(name: str) -> str:
    """The expression the baseline hands ``sa.CheckConstraint(..., name=name)``.

    Read from the AST, so a mention in a comment or a docstring cannot be
    mistaken for the constraint itself. Asserts it appears exactly once: the
    baseline states each vocabulary in one place, and a second spelling is a
    divergence waiting to happen rather than something to silently pick from.
    """
    found = []
    tree = ast.parse(_baseline_path().read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if fname != "CheckConstraint" or not node.args:
            continue
        named = next(
            (kw.value for kw in node.keywords if kw.arg == "name"),
            None,
        )
        if (
            isinstance(named, ast.Constant)
            and named.value == name
            and isinstance(node.args[0], ast.Constant)
        ):
            found.append(node.args[0].value)
    assert len(found) == 1, (
        f"expected exactly one CheckConstraint named {name} in "
        f"{_baseline_path().name}, found {len(found)}"
    )
    return found[0]


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
    assert _statuses_in(_orm_check_expression()) == _statuses_in(
        _check_in_baseline("conversion_jobs_status_check")
    )


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

#: Every migration that mentions the drafts CHECK. Asserted rather than
#: assumed, so a LATER migration that rewrites the constraint fails this file
#: instead of leaving it comparing the ORM against a superseded definition.
_DRAFT_CHECK_MIGRATIONS = [_baseline_path().name]


def _draft_orm_check_expression() -> str:
    for constraint in ConversionDraftModel.__table__.constraints:
        if getattr(constraint, "name", None) == _DRAFT_CHECK:
            return str(constraint.sqltext)
    raise AssertionError(
        f"{_DRAFT_CHECK} is gone from the ORM model — this test has gone blind "
        f"rather than passed"
    )


def test_the_draft_orm_check_is_present_and_parses():
    """Guards every assertion below: an unparsed expression is an empty set,
    and an empty set is a subset of everything."""
    parsed = _statuses_in(_draft_orm_check_expression())
    assert len(parsed) >= 3, f"CHECK expression did not parse: {parsed}"


def test_the_pin_names_every_migration_that_touches_the_draft_check():
    versions = _REPO_ROOT / "alembic" / "versions"
    mentions = sorted(
        p.name for p in versions.glob("*.py") if _DRAFT_CHECK in p.read_text()
    )
    assert mentions == _DRAFT_CHECK_MIGRATIONS


def test_the_draft_orm_check_matches_the_owning_migration():
    """The leg the pre-existing enum-vs-ORM guard could not see.

    ``create_all`` (tests) and ``alembic upgrade`` (deployments) must build the
    same constraint, or a value is writable in CI and rejected in production.
    """
    assert _statuses_in(_draft_orm_check_expression()) == _statuses_in(
        _check_in_baseline(_DRAFT_CHECK)
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
    the CHECK. All three are retired. Pinned so the issue's description cannot
    be reintroduced on either side."""
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
            enterprise_id="ent-1",
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
