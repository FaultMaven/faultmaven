"""Revision 014's frozen copies and both its directions (#1926).

Revision 014 redefines ``admin_case_metadata_page`` with one more result
column, the case's effective driver. ``CREATE OR REPLACE`` cannot change a
result type, so it drops the function and creates it again — and everything a
drop takes with it (the ``PUBLIC`` revoke, the comment, the runtime role's
grant) has to be stated again. Migrations are history, so it writes out both
definitions — its own and revision 005's, which the downgrade restores — and a
copy can drift. Checked here:

* the previous definition IS revision 005's, character for character;
* the new one is 005's plus exactly one result column and one select-list line;
* every relation the body reads stays ``public.``-qualified, by 005's own check;
* each direction issues the five statements, in order, and nothing on SQLite.

What the redefined function returns and that it keeps its hardening on a real
PostgreSQL: ``tests/integration/security/test_admin_case_metadata_postgres.py``.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.modules.case.domain.models.turn import TurnOutcome
from tests.unit.infrastructure.persistence.test_definer_search_path_migration import (
    _code,
    _relations,
)

pytestmark = pytest.mark.unit

_VERSIONS = Path(__file__).resolve().parents[4] / "alembic" / "versions"


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_rev_{filename}", _VERSIONS / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REV_003 = _load("20260930_0900_baa28e79ebab_003_admin_case_metadata.py")
_REV_005 = _load("20261001_1500_1c5a2ad13a65_005_definer_search_path_without_public.py")
_REV_014 = _load("20261012_1200_831be28d1434_014_admin_case_metadata_driver.py")

#: The one line the body gains, and the one column the result gains.
_DRIVER_LINE = "        COALESCE(c.driver_id, c.user_id)::text,\n"
_DRIVER_COLUMN = "    driver_id text,\n"


def test_the_previous_definition_is_005s_verbatim():
    """What the downgrade restores is what 005 created — not a re-typing."""
    assert _REV_014._PREVIOUS_PAGE_BODY == _REV_005._PAGE_BODY
    assert _REV_014._PREVIOUS_PAGE_RESULT == _REV_005._PAGE_RESULT


def test_the_new_definition_is_005s_plus_the_driver_alone():
    """One select-list line after ``c.user_id::text`` and one result column
    after ``user_id``, in matching positions; nothing else moved."""
    body, result = _REV_014._PAGE_BODY, _REV_014._PAGE_RESULT
    assert body.count(_DRIVER_LINE) == 1
    assert result.count(_DRIVER_COLUMN) == 1
    assert body.replace(_DRIVER_LINE, "") == _REV_005._PAGE_BODY
    assert result.replace(_DRIVER_COLUMN, "") == _REV_005._PAGE_RESULT
    assert "        c.user_id::text,\n" + _DRIVER_LINE in body
    assert "    user_id text,\n" + _DRIVER_COLUMN in result


def test_the_driver_column_carries_no_name():
    """The bound (ADR-012 D9): the driver is an id. No display name, and no
    other table is joined to find one."""
    for text in (_REV_014._PAGE_RESULT, _REV_014._PAGE_BODY):
        assert "display_name" not in text
        assert "users" not in text


@pytest.mark.parametrize(
    "body", ["_PAGE_BODY", "_PREVIOUS_PAGE_BODY"], ids=["upgrade", "downgrade"]
)
def test_every_relation_is_qualified_and_only_relations_are(body):
    """Revision 005's check, over both bodies 014 creates: with ``public`` off
    the path an unqualified relation does not resolve, and ``public.`` on a
    function call would send it back to a schema a caller can create in."""
    from faultmaven.infrastructure.persistence.models import Base

    tokens = _code(getattr(_REV_014, body))
    relations = _relations(tokens)
    assert relations, "found no relation, so nothing below was checked"
    assert [r for r in relations if not r.startswith("public.")] == []
    for i, token in enumerate(tokens):
        if token.startswith("public."):
            assert token.removeprefix("public.") in Base.metadata.tables, token
            assert tokens[i + 1] != "(", f"{token}(...) pins a function to public"


def test_the_out_of_band_literal_is_the_enum_value():
    assert _REV_014.OUT_OF_BAND_OUTCOME == TurnOutcome.OUT_OF_BAND.value
    for body in (_REV_014._PAGE_BODY, _REV_014._PREVIOUS_PAGE_BODY):
        assert f"= '{_REV_014.OUT_OF_BAND_OUTCOME}'" in body


def test_the_create_keeps_005s_hardening():
    for result, body in (
        (_REV_014._PAGE_RESULT, _REV_014._PAGE_BODY),
        (_REV_014._PREVIOUS_PAGE_RESULT, _REV_014._PREVIOUS_PAGE_BODY),
    ):
        ddl = _REV_014._create(result, body)
        assert ddl.startswith("CREATE FUNCTION public.admin_case_metadata_page(")
        assert "SET search_path = pg_catalog, pg_temp\nSET row_security = off\n" in ddl
        assert "\nLANGUAGE sql\nSTABLE\nSECURITY DEFINER\n" in ddl
    # The previous definition, created, is 005's DDL but for OR REPLACE.
    assert _REV_014._create(
        _REV_014._PREVIOUS_PAGE_RESULT, _REV_014._PREVIOUS_PAGE_BODY
    ) == _REV_005._create("admin_case_metadata_page").replace(
        "CREATE OR REPLACE FUNCTION", "CREATE FUNCTION", 1
    )


def test_the_names_are_003s():
    """The signature GRANT, REVOKE, COMMENT and DROP name, the runtime role and
    the comment — what a drop removes is put back as 003 first stated it."""
    page_signature, _count_signature = _REV_003._SIGNATURES
    assert _REV_014._SIGNATURE == f"public.{page_signature}"
    assert _REV_014.RUNTIME_ROLE == _REV_003.RUNTIME_ROLE
    assert _REV_014._COMMENT == _REV_003._COMMENT
    assert _REV_014.SEARCH_PATH == _REV_005.SEARCH_PATH


class _Op:
    """Records what a direction issues, as PostgreSQL or as SQLite."""

    def __init__(self, dialect: str):
        self.dialect = dialect
        self.issued: list[str] = []

    def get_context(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect), as_sql=False)

    def execute(self, sql):
        self.issued.append(str(sql))


def _run(direction: str, dialect: str) -> list[str]:
    op = _Op(dialect)
    original = _REV_014.op
    _REV_014.op = op
    try:
        getattr(_REV_014, direction)()
    finally:
        _REV_014.op = original
    return op.issued


_SIGNATURE = "public.admin_case_metadata_page(text, text, bigint, bigint)"


@pytest.mark.parametrize(
    "direction, result, body",
    [
        ("upgrade", "_PAGE_RESULT", "_PAGE_BODY"),
        ("downgrade", "_PREVIOUS_PAGE_RESULT", "_PREVIOUS_PAGE_BODY"),
    ],
)
def test_each_direction_drops_creates_revokes_comments_and_grants(
    direction, result, body
):
    """Five statements, in this order. The DROP has no ``IF EXISTS``: a database
    without the function fails rather than gaining one. ``PUBLIC`` is revoked
    and the comment restored before the grant, as 003 does."""
    issued = _run(direction, "postgresql")
    assert issued == [
        f"DROP FUNCTION {_SIGNATURE}",
        _REV_014._create(getattr(_REV_014, result), getattr(_REV_014, body)),
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC",
        f"COMMENT ON FUNCTION {_SIGNATURE} IS '{_REV_003._COMMENT}'",
        _REV_014._GRANT_TO_RUNTIME_ROLE,
    ]
    grant = _REV_014._GRANT_TO_RUNTIME_ROLE
    assert "pg_catalog.pg_roles WHERE rolname = 'faultmaven_app'" in grant
    assert f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO faultmaven_app;" in grant
    assert "admin_case_metadata_count" not in "\n".join(issued)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_has_no_function_to_touch(direction):
    assert _run(direction, "sqlite") == []
