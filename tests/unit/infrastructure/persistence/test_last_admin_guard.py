"""The last-admin guard's two halves must keep agreeing with each other.

The Alembic baseline installs the constraint trigger (fm#1161, carried over from
migration 044); ``organization_repository`` carries the constant and the
recogniser that let a caller turn the trigger's refusal back into its own
friendly error. The trigger is exercised for real
against PostgreSQL in ``tests/integration/test_last_admin_constraint_trigger.py``
— what is checked here is the seam between the two, which no amount of
integration testing would catch: a rename or a role-id change on one side leaves
both sides working and the connection between them broken.

The migration has to name the ``admin`` role inside SQL, so it carries a literal
copy of ``SYSTEM_ROLE_IDS[Role.ADMIN]`` — the same frozen-snapshot convention it
uses when it seeds the roles, and for the same reason: a migration states the
value it was written against instead of importing runtime code that can move
underneath it.

The cost of that convention is that the copy can drift, and drift here is
silent and total: a trigger counting a role id nobody holds would find zero
admins on every demotion and refuse writes that are perfectly legitimate, or —
if the ids were merely swapped — count viewers and let the last real admin go.
This test is what makes the snapshot safe to freeze.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from faultmaven.infrastructure.persistence.organization_repository import (
    LAST_ADMIN_CONSTRAINT,
    is_last_admin_violation,
)
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS

#: Globbed rather than named: there is exactly one migration (the ADR-017
#: baseline), and pinning its filename would make a future baseline rename look
#: like a missing guard rather than the rename it is. The assertion below still
#: fails loudly if no file matches.
_VERSIONS = Path(__file__).resolve().parents[4] / "alembic" / "versions"
MIGRATION = next(iter(sorted(_VERSIONS.glob("*_enterprise_baseline.py"))), None)


@pytest.mark.unit
def test_the_baseline_admin_role_id_matches_the_seeded_role():
    assert (
        MIGRATION is not None and MIGRATION.exists()
    ), f"the enterprise baseline is missing from {_VERSIONS}"
    source = MIGRATION.read_text()

    match = re.search(r'^_ADMIN_ROLE_ID = "([0-9a-f-]+)"$', source, re.MULTILINE)
    assert match, "the baseline no longer declares _ADMIN_ROLE_ID as a literal"

    assert match.group(1) == SYSTEM_ROLE_IDS[Role.ADMIN], (
        "the baseline's frozen admin role id has drifted from "
        "rbac_seed.SYSTEM_ROLE_IDS[Role.ADMIN]. The trigger names this id in "
        "SQL; a stale copy makes the last-admin guard count a role nobody "
        "holds. Changing a system role id needs a new migration that rewrites "
        "the trigger, not an edit to the constant."
    )


@pytest.mark.unit
def test_the_baseline_trigger_name_matches_the_shipped_constant():
    """The name the trigger reports itself by is the name callers recognise it by.

    ``is_last_admin_violation`` matches on PostgreSQL's ``constraint_name``
    field, which the migration populates via ``RAISE ... USING CONSTRAINT``. If
    the migration renamed the trigger, that recogniser would return ``False``
    for a refusal it is meant to catch — and the Cloud service would turn a
    409 "you cannot demote the last admin" into an unhandled database fault,
    silently, with the guard itself still working perfectly.
    """
    source = MIGRATION.read_text()
    match = re.search(r'^_LAST_ADMIN_TRIGGER = "([a-z_]+)"$', source, re.MULTILINE)
    assert match, "the baseline no longer declares _LAST_ADMIN_TRIGGER as a literal"
    assert match.group(1) == LAST_ADMIN_CONSTRAINT


@pytest.mark.unit
def test_the_baseline_trigger_is_deferred_and_security_definer():
    """Two properties the guard cannot work without, asserted on the DDL text.

    Deferral is what lets a transaction demote one admin while promoting
    another, and what lets the cascade exemptions recognise an already-deleted
    parent. ``SECURITY DEFINER`` is what stops the RLS policies on
    ``organization_members`` (fail-closed on the bound enterprise) from making
    the guard count zero admins whenever no tenant is bound. Losing either is a silent
    behaviour change in a file nobody re-reads.
    """
    source = MIGRATION.read_text()
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog, public" in source


class _PgError(Exception):
    """Stand-in for the driver exception SQLAlchemy wraps.

    A real ``asyncpg`` error object, not a ``Mock``: a Mock answers every
    attribute with a truthy Mock, so a recogniser reading ``sqlstate`` and
    ``constraint_name`` off one would "match" no matter what it asked for.
    """

    def __init__(self, sqlstate=None, constraint_name=None):
        super().__init__("boom")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _wrapped(sqlstate=None, constraint_name=None) -> DBAPIError:
    """Build the exception shape SQLAlchemy actually raises.

    ``DBAPIError.orig`` is the DBAPI-level exception and the driver error is its
    ``__cause__`` — the two-hop walk the recogniser has to make.
    """
    orig = _PgError()
    orig.__cause__ = _PgError(sqlstate=sqlstate, constraint_name=constraint_name)
    return DBAPIError("UPDATE organization_members", {}, orig)


@pytest.mark.unit
def test_recognises_the_trigger_refusing():
    assert is_last_admin_violation(
        _wrapped(sqlstate="23514", constraint_name=LAST_ADMIN_CONSTRAINT)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "sqlstate, constraint_name, why",
    [
        (
            "23514",
            "operator_access_audit_action_valid",
            "another CHECK on another table",
        ),
        ("23503", LAST_ADMIN_CONSTRAINT, "a foreign-key violation, not this guard"),
        ("40001", None, "a serialization failure — the write is retryable"),
        (None, None, "a driver error carrying no SQLSTATE at all"),
    ],
)
def test_does_not_claim_unrelated_database_errors(sqlstate, constraint_name, why):
    """The recogniser has to be narrow in the direction that matters.

    A caller turns a match into "you cannot demote the last admin" and a 409. A
    recogniser that over-matched would answer a foreign-key fault or a lost
    connection with a confident, wrong explanation, and the real failure would
    never be reported. Both fields must agree before it claims a refusal.
    """
    assert not is_last_admin_violation(
        _wrapped(sqlstate=sqlstate, constraint_name=constraint_name)
    ), why


@pytest.mark.unit
def test_does_not_claim_a_plain_exception():
    """Not every failure on the write path is a DBAPI error."""
    assert not is_last_admin_violation(RuntimeError("connection lost"))
