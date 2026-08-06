"""Migration 040's data transform, exercised against real rows.

The migration walk in ``test_alembic_migrations.py`` runs on an EMPTY database,
so 040's mapping was never executed by any test — its data transform could not
fail. That is how a backend-specific type bug survived review: ``cases.progress``
is ``JsonBlob = Text().with_variant(JSONB, "postgresql")``, and a textual SELECT
bypasses SQLAlchemy's result coercion, so SQLite returns the raw JSON *string*
while PostgreSQL returns a dict. An ``isinstance(progress, dict)`` test alone was
False for every SQLite row, mapping every verified-mitigation case to
``closed_insufficient_evidence`` on the default standalone backend — a persisted
false statement about the user's case.

These run the real ``upgrade()`` against seeded rows on SQLite, which is the
shape that was broken.
"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

_RETIRED = "closed_after_investigation"

# Anchored on this file rather than the cwd, so pytest invoked from anywhere
# still finds the migration (matching test_alembic_migrations.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _migration_path() -> Path:
    return next(
        (_REPO_ROOT / "alembic" / "versions").glob("*_040_closure_reason_vocabulary.py")
    )


def _seed(engine, rows):
    """Create a minimal `cases` table and insert (case_id, reason, progress)."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE cases (case_id TEXT, closure_reason TEXT, progress TEXT)"
            )
        )
        for case_id, reason, progress in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO cases (case_id, closure_reason, progress) "
                    "VALUES (:cid, :reason, :progress)"
                ),
                {"cid": case_id, "reason": reason, "progress": progress},
            )


def _run_upgrade(engine):
    """Invoke the migration's upgrade() with op bound to this connection."""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = _migration_path()
    spec = importlib.util.spec_from_file_location("mig040", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(Operations(ctx)):
            module.upgrade()


def _reasons(engine):
    with engine.begin() as conn:
        return dict(
            conn.execute(sa.text("SELECT case_id, closure_reason FROM cases")).all()
        )


@pytest.fixture
def engine(tmp_path):
    return sa.create_engine(f"sqlite:///{tmp_path}/mig040.db")


def test_verified_mitigation_survives_the_sqlite_string_blob(engine):
    """The blocking bug: on SQLite `progress` arrives as a JSON STRING, so a
    dict-only read silently demoted every verified mitigation."""

    _seed(
        engine,
        [
            (
                "c_mitigated",
                _RETIRED,
                json.dumps({"mitigation": {"verified": True}}),
            )
        ],
    )
    _run_upgrade(engine)
    assert _reasons(engine)["c_mitigated"] == "mitigation_sufficient"


def test_a_case_with_nothing_established_maps_to_insufficient_evidence(engine):
    _seed(engine, [("c_bare", _RETIRED, "{}")])
    _run_upgrade(engine)
    assert _reasons(engine)["c_bare"] == "closed_insufficient_evidence"


def test_an_unverified_mitigation_is_not_promoted(engine):
    _seed(
        engine,
        [
            (
                "c_accepted_only",
                _RETIRED,
                json.dumps({"mitigation": {"accepted": True, "verified": False}}),
            )
        ],
    )
    _run_upgrade(engine)
    assert _reasons(engine)["c_accepted_only"] == "closed_insufficient_evidence"


def test_rows_carrying_a_live_reason_are_untouched(engine):
    """The migration must only rewrite the retired value."""

    _seed(
        engine,
        [
            (
                "c_live",
                "mitigation_sufficient",
                json.dumps({"mitigation": {"verified": True}}),
            ),
            ("c_inquiry", "inquiry_only", "{}"),
        ],
    )
    _run_upgrade(engine)
    reasons = _reasons(engine)
    assert reasons["c_live"] == "mitigation_sufficient"
    assert reasons["c_inquiry"] == "inquiry_only"


def test_an_unparseable_blob_degrades_instead_of_failing_the_upgrade(engine):
    """Understating is recoverable; a stuck upgrade on a user's database is not."""

    _seed(engine, [("c_corrupt", _RETIRED, "{not json")])
    _run_upgrade(engine)
    assert _reasons(engine)["c_corrupt"] == "closed_insufficient_evidence"


def test_a_null_progress_blob_does_not_raise(engine):
    _seed(engine, [("c_null", _RETIRED, None)])
    _run_upgrade(engine)
    assert _reasons(engine)["c_null"] == "closed_insufficient_evidence"


def test_no_retired_value_survives_the_migration(engine):
    """The whole point: the retired value is validated on READ, so a surviving
    row fails case hydration rather than merely looking stale."""

    _seed(
        engine,
        [
            ("a", _RETIRED, json.dumps({"mitigation": {"verified": True}})),
            ("b", _RETIRED, "{}"),
            ("c", _RETIRED, None),
        ],
    )
    _run_upgrade(engine)
    assert _RETIRED not in set(_reasons(engine).values())


# -- the PostgreSQL shape -----------------------------------------------------
# psycopg2's JSONB adapter returns a dict, not a string, so the decode added for
# SQLite must pass it through untouched. Asserted rather than reasoned about:
# "the guard only fires for str" is exactly the shape of claim that produced the
# original defect. No live PostgreSQL is needed — what differs between backends
# is the Python TYPE handed back, so the type is what gets substituted.
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeBind:
    """Stands in for a psycopg2-backed connection: JSONB comes back as a dict."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.startswith("SELECT"):
            return _FakeResult(self._rows)
        self.updates.append(params)
        return _FakeResult([])


def _run_upgrade_with_bind(bind):
    import importlib.util
    from types import SimpleNamespace

    path = _migration_path()
    spec = importlib.util.spec_from_file_location("mig040_pg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(get_bind=lambda: bind)
    module.upgrade()
    return bind.updates


def test_a_dict_progress_blob_is_read_without_decoding():
    """The PostgreSQL path: JSONB arrives already parsed."""

    bind = _FakeBind([("c_pg", {"mitigation": {"verified": True}})])
    updates = _run_upgrade_with_bind(bind)
    assert updates == [{"reason": "mitigation_sufficient", "cid": "c_pg"}]


def test_a_dict_blob_without_a_verified_mitigation_maps_bare():
    bind = _FakeBind([("c_pg_bare", {"mitigation": {"verified": False}})])
    updates = _run_upgrade_with_bind(bind)
    assert updates == [{"reason": "closed_insufficient_evidence", "cid": "c_pg_bare"}]


def test_both_backend_shapes_agree_on_the_same_case():
    """The invariant that matters: a case with a verified mitigation gets the
    same reason whichever backend it lives on. The original defect was exactly
    this disagreement, silent and backend-specific."""

    as_dict = _run_upgrade_with_bind(
        _FakeBind([("c", {"mitigation": {"verified": True}})])
    )
    as_text = _run_upgrade_with_bind(
        _FakeBind([("c", json.dumps({"mitigation": {"verified": True}}))])
    )
    assert as_dict == as_text == [{"reason": "mitigation_sufficient", "cid": "c"}]
