"""Revision 003's frozen literals and its SQLite half (ADR-012 D9).

The migration states the out-of-band outcome as text rather than importing
``TurnOutcome`` — migrations are history — so this is what stops that text from
drifting away from the enum the rest of the system writes. If they disagreed,
the operator list would count every aside as investigation work and report
investigation turns no other surface reports.

What the functions return, and that they bound the read, is proven on a real
PostgreSQL in ``tests/integration/security/test_admin_case_metadata_postgres.py``.
"""

import importlib.util
from pathlib import Path

import pytest

from faultmaven.modules.case.domain.models.turn import TurnOutcome

pytestmark = pytest.mark.unit

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "alembic"
    / "versions"
    / "20260930_0900_baa28e79ebab_003_admin_case_metadata.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_rev_003", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_out_of_band_literal_is_the_enum_value():
    assert _load().OUT_OF_BAND_OUTCOME == TurnOutcome.OUT_OF_BAND.value


def test_the_function_body_names_the_literal_it_declares():
    """The constant is what the SQL compares against, not a label beside it."""
    migration = _load()
    assert (
        f"= '{migration.OUT_OF_BAND_OUTCOME}'" in migration._CREATE_PAGE_FUNCTION
    ), "the page function no longer compares outcomes to OUT_OF_BAND_OUTCOME"


def test_both_functions_are_security_definer_with_pg_temp_last():
    """A definer function with an open search_path runs whatever a caller's
    schema puts first — and one that leaves ``pg_temp`` out searches it FIRST
    for relations, so it must be listed, last."""
    migration = _load()
    path = [part.strip() for part in migration.SEARCH_PATH.split(",")]
    assert path[0] == "pg_catalog" and path[-1] == "pg_temp", path
    for ddl in (migration._CREATE_PAGE_FUNCTION, migration._CREATE_COUNT_FUNCTION):
        assert "SECURITY DEFINER" in ddl
        assert f"SET search_path = {migration.SEARCH_PATH}\n" in ddl


def test_execute_is_revoked_from_public_and_granted_to_the_runtime_role():
    """What the upgrade issues, in order: each function is created, PUBLIC is
    revoked, and the runtime role is granted — guarded on the role existing."""
    migration = _load()
    issued = []

    class _Op:
        @staticmethod
        def get_context():
            class _Ctx:
                class dialect:
                    name = "postgresql"

            return _Ctx()

        @staticmethod
        def execute(sql):
            issued.append(" ".join(str(sql).split()))

    migration.op = _Op
    migration.upgrade()

    for signature in migration._SIGNATURES:
        revoke = f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
        assert revoke in issued, f"{signature}: PUBLIC keeps EXECUTE"
        grant = f"GRANT EXECUTE ON FUNCTION {signature} TO faultmaven_app;"
        (block,) = [sql for sql in issued if sql.startswith("DO $$")]
        assert grant in block
        assert issued.index(revoke) < issued.index(block)
    assert "rolname = 'faultmaven_app'" in block
    assert "GRANT" not in " ".join(
        sql for sql in issued if not sql.startswith("DO $$")
    ), "a grant outside the role-existence guard would fail without the role"
