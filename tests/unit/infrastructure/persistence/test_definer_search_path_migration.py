"""Revision 005's frozen copies, its settings and both of its directions.

Revision 005 re-creates the chain's four ``SECURITY DEFINER`` functions with
``search_path = pg_catalog, pg_temp``, ``row_security = off`` and every relation
schema-qualified. Migrations are history, so it writes the bodies out rather
than importing them from 001 and 003 — and a copy can drift. What is checked
here is that it did not: each body is its original with ``public.`` added in
front of relation names and line breaks moved, nothing else.

What the settings do — a planted operator never runs, a temporary table is
never read, nothing but settings and bodies changes in the catalog — is shown on
a real PostgreSQL in
``tests/integration/security/test_definer_functions_postgres.py``.
"""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_VERSIONS = Path(__file__).resolve().parents[4] / "alembic" / "versions"


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_rev_{filename}", _VERSIONS / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BASELINE = _load("20260906_1200_a1e0c17bd001_001_enterprise_baseline.py")
_REV_003 = _load("20260930_0900_baa28e79ebab_003_admin_case_metadata.py")
_REV_004 = _load("20261001_0900_14d4bfdd406e_004_definer_trigger_hardening.py")
_REV_005 = _load("20261001_1500_1c5a2ad13a65_005_definer_search_path_without_public.py")

#: Each re-created function beside the DDL it was first created with.
_COPIES = {
    "organization_members_last_admin_guard": (
        _REV_005._CREATE_LAST_ADMIN_GUARD,
        _BASELINE._CREATE_LAST_ADMIN_FUNCTION,
    ),
    "team_members_same_enterprise_guard": (
        _REV_005._CREATE_TEAM_MEMBER_GUARD,
        _BASELINE._CREATE_TEAM_MEMBER_ENTERPRISE_FUNCTION,
    ),
    "admin_case_metadata_page": (
        _REV_005._CREATE_PAGE_FUNCTION,
        _REV_003._CREATE_PAGE_FUNCTION,
    ),
    "admin_case_metadata_count": (
        _REV_005._CREATE_COUNT_FUNCTION,
        _REV_003._CREATE_COUNT_FUNCTION,
    ),
}


def _body(ddl: str) -> str:
    match = re.search(r"\bAS \$\$(.*)\$\$", ddl, re.DOTALL)
    assert match, ddl[:200]
    return match.group(1)


def _tokens(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_each_body_is_its_original_with_its_relations_qualified(name):
    copy, original = _COPIES[name]
    assert "public." in _body(copy), f"{name}: no relation is qualified"
    assert "public." not in _body(original)
    assert _tokens(_body(copy).replace("public.", "")) == _tokens(
        _body(original)
    ), f"{name}: the body differs from its original by more than its qualifiers"


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_each_function_is_replaced_in_place_with_both_settings(name):
    """``CREATE OR REPLACE`` on the function in ``public``: never a ``DROP``,
    which would take the grants, the comment and the triggers with it."""
    copy, _ = _COPIES[name]
    assert copy.lstrip().startswith(f"CREATE OR REPLACE FUNCTION public.{name}(")
    assert (
        "SECURITY DEFINER\n"
        f"SET search_path = {_REV_005.SEARCH_PATH}\n"
        "SET row_security = off\n"
        "AS $$"
    ) in copy
    assert not re.search(r"\bDROP\b", copy, re.IGNORECASE)


def test_the_path_holds_no_schema_a_caller_can_create_in():
    assert _REV_005.SEARCH_PATH == "pg_catalog, pg_temp"


class _Op:
    """Records what a direction issues, as PostgreSQL or as SQLite."""

    def __init__(self, dialect: str):
        self.dialect = dialect
        self.issued: list[str] = []

    def get_context(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, sql):
        self.issued.append(str(sql))


def _run(module, direction: str, dialect: str) -> list[str]:
    op = _Op(dialect)
    original = module.op
    module.op = op
    try:
        getattr(module, direction)()
    finally:
        module.op = original
    return op.issued


def test_upgrade_issues_the_four_replacements_and_nothing_else():
    assert _run(_REV_005, "upgrade", "postgresql") == [
        _REV_005._CREATE_LAST_ADMIN_GUARD,
        _REV_005._CREATE_TEAM_MEMBER_GUARD,
        _REV_005._CREATE_PAGE_FUNCTION,
        _REV_005._CREATE_COUNT_FUNCTION,
    ]


def test_downgrade_restores_the_settings_004_left():
    """Read off the revisions that wrote them: 004 gave the baseline's guards the
    path and ``row_security`` 003 gave its reads, so all four end 004 alike —
    and the downgrade puts back that path, leaving ``row_security = off``
    where both revisions set it."""
    path = _REV_003.SEARCH_PATH
    assert path == "pg_catalog, public, pg_temp"
    assert "SET row_security = off" in _REV_003._DEFINER
    issued_by_004 = _run(_REV_004, "upgrade", "postgresql")
    for signature in (
        "organization_members_last_admin_guard()",
        "team_members_same_enterprise_guard()",
    ):
        assert f"ALTER FUNCTION {signature} SET search_path = {path}" in issued_by_004
        assert f"ALTER FUNCTION {signature} SET row_security = off" in issued_by_004

    assert _run(_REV_005, "downgrade", "postgresql") == [
        f"ALTER FUNCTION public.{signature} SET search_path = {path}"
        for signature in (
            "organization_members_last_admin_guard()",
            "team_members_same_enterprise_guard()",
            "admin_case_metadata_page(text, text, bigint, bigint)",
            "admin_case_metadata_count(text, text)",
        )
    ]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_has_no_definer_functions_to_touch(direction):
    assert _run(_REV_005, direction, "sqlite") == []
