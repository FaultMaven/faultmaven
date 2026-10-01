"""Revision 005's frozen copies, what its bodies name, and both its directions.

Revision 005 re-creates the chain's four ``SECURITY DEFINER`` functions with
``search_path = pg_catalog, pg_temp``, ``row_security = off`` and every relation
schema-qualified. Migrations are history, so it writes the bodies out rather
than importing them from 001 and 003 — and a copy can drift. Two things are
checked here, and each covers what the other cannot see:

* each body is its original with ``public.`` added and line breaks moved,
  nothing else — which by itself cannot tell a missing qualifier from a present
  one, nor a qualified table from ``public.`` pinned onto a function call;
* every relation a body reads or writes IS ``public.``-qualified, and every
  ``public.`` names a table — never a function, which must keep resolving in
  ``pg_catalog``.

What the settings do — a planted operator or aggregate never runs, nothing but
settings and bodies changes in the catalog, a missing function is refused,
``PUBLIC`` loses ``CREATE`` on ``public`` — is shown on a real PostgreSQL in
``tests/integration/security/test_definer_functions_postgres.py``.
"""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.persistence.models import Base

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
_ORIGINALS = {
    "organization_members_last_admin_guard": _BASELINE._CREATE_LAST_ADMIN_FUNCTION,
    "team_members_same_enterprise_guard": (
        _BASELINE._CREATE_TEAM_MEMBER_ENTERPRISE_FUNCTION
    ),
    "admin_case_metadata_page": _REV_003._CREATE_PAGE_FUNCTION,
    "admin_case_metadata_count": _REV_003._CREATE_COUNT_FUNCTION,
}


def _body(name: str) -> str:
    return _REV_005._FUNCTIONS[name][4]


def _original_body(name: str) -> str:
    match = re.search(r"\bAS \$\$(.*)\$\$", _ORIGINALS[name], re.DOTALL)
    assert match, name
    return match.group(1)


def _words(sql: str) -> str:
    return " ".join(sql.split())


def test_every_definer_function_is_re_created():
    assert sorted(_REV_005._FUNCTIONS) == sorted(_ORIGINALS)


@pytest.mark.parametrize("name", sorted(_ORIGINALS))
def test_each_body_is_its_original_with_qualifiers_added(name):
    assert "public." not in _original_body(name)
    assert _words(_body(name).replace("public.", "")) == _words(
        _original_body(name)
    ), f"{name}: the body differs from its original by more than its qualifiers"


def _code(body: str) -> list[str]:
    """The body's tokens: comments dropped, every string literal emptied (so a
    ``'UPDATE'`` or a message is never read as SQL), identifiers kept whole
    with their dots."""
    body = re.sub(r"--[^\n]*", "", body)
    body = re.sub(r"'(?:[^']|'')*'", "''", body)
    return re.findall(r"[A-Za-z_][\w.$]*|\S", body)


def _relations(tokens: list[str]) -> list[str]:
    """Every name a body reads or writes as a relation: after ``FROM`` (not
    ``IS DISTINCT FROM``), ``JOIN``, ``UPDATE`` and ``INSERT INTO``; not a
    subquery, ``LATERAL``, a set-returning function call or a ``WITH`` name."""
    ctes = {
        tokens[i + 1]
        for i, token in enumerate(tokens[:-2])
        if token.upper() == "WITH" and tokens[i + 2].upper() == "AS"
    }
    names = []
    for i, token in enumerate(tokens[:-2]):
        keyword = token.upper()
        previous = tokens[i - 1].upper() if i else ""
        if keyword == "FROM" and previous == "DISTINCT":
            continue
        if keyword == "INTO" and previous != "INSERT":
            continue
        if keyword not in {"FROM", "JOIN", "UPDATE", "INTO"}:
            continue
        name, after = tokens[i + 1], tokens[i + 2]
        if name == "(" or name.upper() == "LATERAL" or after == "(" or name in ctes:
            continue
        names.append(name)
    return names


_TABLES = set(Base.metadata.tables)


@pytest.mark.parametrize("name", sorted(_ORIGINALS))
def test_every_relation_is_qualified_and_only_relations_are(name):
    """With ``public`` off the path, an unqualified relation does not resolve —
    or resolves to a caller's temporary table — and ``public.`` on a function
    call would send it back to the schema a caller can create in."""
    tokens = _code(_body(name))
    relations = _relations(tokens)
    assert relations, f"{name}: found no relation, so nothing below was checked"
    unqualified = [r for r in relations if not r.startswith("public.")]
    assert not unqualified, f"{name}: unqualified relations {unqualified}"

    for i, token in enumerate(tokens):
        if not token.startswith("public."):
            continue
        assert (
            token.removeprefix("public.") in _TABLES
        ), f"{name}: {token} is not a table"
        assert tokens[i + 1] != "(", f"{name}: {token}(...) pins a function to public"


def test_the_path_holds_no_schema_a_caller_can_create_in():
    assert _REV_005.SEARCH_PATH == "pg_catalog, pg_temp"
    for name in _REV_005._FUNCTIONS:
        ddl = _REV_005._create(name)
        assert ddl.startswith(f"CREATE OR REPLACE FUNCTION public.{name}(")
        assert "SET search_path = pg_catalog, pg_temp\nSET row_security = off\n" in ddl


def test_the_signatures_are_003s():
    """What GRANT, REVOKE and the existence check name — 003's signatures, in
    ``public``, and 003's runtime role."""
    assert [_REV_005._signature(name) for name in _REV_005._READS] == [
        f"public.{signature}" for signature in _REV_003._SIGNATURES
    ]
    assert _REV_005.RUNTIME_ROLE == _REV_003.RUNTIME_ROLE


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


def test_upgrade_checks_replaces_re_grants_and_closes_public_in_that_order():
    """The existence check runs before anything is replaced, so a missing
    function fails the revision before ``CREATE OR REPLACE`` can create it; the
    grants are re-stated after the replacements; ``public`` is closed last."""
    assert _run(_REV_005, "upgrade", "postgresql") == [
        _REV_005._REQUIRE_EXISTING,
        *(_REV_005._create(name) for name in _REV_005._FUNCTIONS),
        *(
            f"REVOKE ALL ON FUNCTION {_REV_005._signature(name)} FROM PUBLIC"
            for name in _REV_005._READS
        ),
        _REV_005._GRANT_TO_RUNTIME_ROLE,
        _REV_005._REVOKE_OR_WARN,
    ]
    for name in _REV_005._FUNCTIONS:
        signature = _REV_005._signature(name)
        assert f"to_regprocedure('{signature}') IS NULL" in _REV_005._REQUIRE_EXISTING
    assert _REV_005.REVOKE_PUBLIC_CREATE in _REV_005._REVOKE_OR_WARN


def test_downgrade_restores_the_settings_004_left_and_grants_nothing_back():
    """Read off the revisions that wrote them: 004 gave the baseline's guards the
    path and ``row_security`` 003 gave its reads, so all four end 004 alike —
    and the downgrade puts back that path, leaving ``row_security = off``
    where both revisions set it. It never grants ``CREATE`` on ``public`` back
    to ``PUBLIC``."""
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

    issued = _run(_REV_005, "downgrade", "postgresql")
    assert issued == [
        f"ALTER FUNCTION {_REV_005._signature(name)} SET search_path = {path}"
        for name in _REV_005._FUNCTIONS
    ]
    assert not any("GRANT" in statement for statement in issued)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_has_no_definer_functions_to_touch(direction):
    assert _run(_REV_005, direction, "sqlite") == []
