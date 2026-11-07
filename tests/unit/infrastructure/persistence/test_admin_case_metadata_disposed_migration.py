"""Revision 025's frozen copies and both its directions (#2100).

Revision 025 redefines ``admin_case_metadata_page`` with one more result
column, ``mitigation_disposed``, the stage's fifth input. Like 014 it drops
and re-creates the function and writes out both definitions — its own and
014's, which the downgrade restores. Checked here:

* the previous definition IS revision 014's, character for character;
* the new one is 014's plus exactly one result column and one select-list
  expression, after ``mitigation_verified``;
* every relation the body reads stays ``public.``-qualified, by 005's own check;
* each direction issues 014's five statements, in order, and nothing on SQLite.

What the function returns for each stored shape, on a real PostgreSQL:
``tests/integration/security/test_admin_case_metadata_postgres.py``.
"""

import typing

import pytest

from faultmaven.modules.case.domain.models.progress import MitigationDisposition
from tests.unit.infrastructure.persistence.test_admin_case_metadata_driver_migration import (
    _REV_014,
    _SIGNATURE,
    _load,
    _Op,
)
from tests.unit.infrastructure.persistence.test_definer_search_path_migration import (
    _code,
    _relations,
)

pytestmark = pytest.mark.unit

_REV_025 = _load(
    "20261107_1200_45970adab01d_025_admin_case_metadata_mitigation_disposed.py"
)

_COLUMN = "    mitigation_disposed boolean,\n"
_VERIFIED_END = (
    "THEN (c.progress -> 'mitigation' ->> 'verified')::boolean\n"
    "                  END\n        END,\n"
)


def _added_expression() -> str:
    """The select-list text 025 adds, right after the verified expression: as
    long as the two bodies differ."""
    body = _REV_025._PAGE_BODY
    start = body.index(_VERIFIED_END) + len(_VERIFIED_END)
    return body[start : start + len(body) - len(_REV_014._PAGE_BODY)]


def test_the_previous_definition_is_014s_verbatim():
    assert _REV_025._PREVIOUS_PAGE_BODY == _REV_014._PAGE_BODY
    assert _REV_025._PREVIOUS_PAGE_RESULT == _REV_014._PAGE_RESULT
    assert _REV_025.down_revision == "d6cfb801d9f5"  # pragma: allowlist secret


def test_the_new_definition_is_014s_plus_the_disposition_alone():
    body, result = _REV_025._PAGE_BODY, _REV_025._PAGE_RESULT
    added = _added_expression()
    start = body.index(added)
    assert body[:start] + body[start + len(added) :] == _REV_014._PAGE_BODY
    assert added.startswith("        -- The mitigation's disposition (#2100)")
    assert added.endswith("        END,\n")
    assert added.count("CASE WHEN jsonb_typeof(c.progress)") == 1
    assert result.count(_COLUMN) == 1
    assert result.replace(_COLUMN, "") == _REV_014._PAGE_RESULT
    assert "    mitigation_verified boolean,\n" + _COLUMN in result


def test_the_two_literals_are_the_dispositions():
    """Frozen snapshot of ``MitigationRecord.disposition``'s values."""
    assert _REV_025.DISPOSITIONS == typing.get_args(MitigationDisposition)
    added = _added_expression()
    assert "IN ('declared_stable', 'proceed_unmitigated')" in added
    # Null and absent read false; nothing else does.
    assert added.count("THEN false") == 2 and added.count("THEN true") == 1


@pytest.mark.parametrize("body", ["_PAGE_BODY", "_PREVIOUS_PAGE_BODY"])
def test_every_relation_is_qualified(body):
    tokens = _code(getattr(_REV_025, body))
    relations = _relations(tokens)
    assert relations
    assert [r for r in relations if not r.startswith("public.")] == []


def _run(direction: str, dialect: str) -> list[str]:
    op = _Op(dialect)
    original = _REV_025.op
    _REV_025.op = op
    try:
        getattr(_REV_025, direction)()
    finally:
        _REV_025.op = original
    return op.issued


@pytest.mark.parametrize(
    "direction, result, body",
    [
        ("upgrade", "_PAGE_RESULT", "_PAGE_BODY"),
        ("downgrade", "_PREVIOUS_PAGE_RESULT", "_PREVIOUS_PAGE_BODY"),
    ],
)
def test_each_direction_issues_014s_five_statements(direction, result, body):
    issued = _run(direction, "postgresql")
    created = _REV_025._create(getattr(_REV_025, result), getattr(_REV_025, body))
    assert issued == [
        f"DROP FUNCTION {_SIGNATURE}",
        created,
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC",
        f"COMMENT ON FUNCTION {_SIGNATURE} IS '{_REV_014._COMMENT}'",
        _REV_014._GRANT_TO_RUNTIME_ROLE,
    ]
    # 014's DDL, settings included, for the same definition.
    assert created == _REV_014._create(
        getattr(_REV_025, result), getattr(_REV_025, body)
    )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_has_no_function_to_touch(direction):
    assert _run(direction, "sqlite") == []
