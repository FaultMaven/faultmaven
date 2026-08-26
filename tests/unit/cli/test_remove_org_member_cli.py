"""``fm-remove-org-member`` behaviour (#874).

This command exists to make the operator path *paired*: removing a membership
and ending the removed member's live sessions in one step, because membership is
verified at login only and the runbook's two-step SQL left the second step to
memory. So the coverage is behavioural — what it writes, what it refuses, and
what it tells the operator — not a "the entrypoint resolves" smoke test.

The guard with the most teeth is the FakeRedis refusal. In a standalone
deployment the revocation store is in-process FakeRedis, so a watermark written
in this CLI process would be invisible to the running API: the command would
print success while every token stayed valid. Reporting a revocation that did not
land is worse than refusing, and that is the case this pins.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from faultmaven.cli import remove_org_member
from faultmaven.config import tenant_context
from faultmaven.exceptions import UserLookupFailed
from faultmaven.infrastructure.persistence.organization_repository import (
    LAST_ADMIN_CONSTRAINT,
)
from faultmaven.modules.auth.domain.services.organization_membership_service import (
    MembershipRemovalIncomplete,
)

pytestmark = pytest.mark.unit

ORG_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
USER_ID = "225bae2f-f459-4a54-9c08-2da5c2b3a961"
REVOKED_AT = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)


def _run_main(argv):
    original = sys.argv
    sys.argv = argv
    try:
        remove_org_member.main()
    finally:
        sys.argv = original


class _FakeRedisClient:
    """Stands in for the in-process FakeRedis client.

    ``is_fakeredis`` identifies it by module name, so the predicate under test is
    the real one — this only has to satisfy it.
    """

    __module__ = "fakeredis.aioredis"


@pytest.fixture
def wiring(monkeypatch):
    """Wire the CLI's container and repository to test doubles.

    Returns a namespace of the doubles so a test can vary one of them. The CLI
    imports these inside the function, so patching the source modules is what
    reaches it.
    """
    orgs = AsyncMock()
    orgs.get_organization.return_value = SimpleNamespace(
        organization_id=ORG_ID, name="Acme"
    )
    orgs.get_member_role.return_value = "role-uuid"
    orgs.remove_member.return_value = True

    user_store = AsyncMock()
    user_store.get_user_by_username.return_value = SimpleNamespace(
        user_id=USER_ID, username="alice", email="alice@acme.example"
    )

    auth_service = AsyncMock()
    auth_service.revoke_user_tokens.return_value = REVOKED_AT

    revocation_store = SimpleNamespace(redis=object())  # real-Redis-shaped

    container = SimpleNamespace(
        initialize=AsyncMock(),
        get_auth_service=lambda: auth_service,
        get_user_store=lambda: user_store,
        get_service=lambda name: (
            revocation_store if name == "token_revocation_store" else None
        ),
    )

    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.sessionless_organization_repository"
        ".SessionlessOrganizationRepository",
        lambda: orgs,
    )
    return SimpleNamespace(
        orgs=orgs,
        user_store=user_store,
        auth_service=auth_service,
        container=container,
        revocation_store=revocation_store,
    )


# =============================================================================
# Refusals that cost nothing
# =============================================================================


def test_refusing_without_yes_exits_1_before_connecting(capsys):
    """Neither --yes nor --dry-run: refuse ahead of container init."""
    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["fm-remove-org-member", "--organization-id", ORG_ID, "--user", "alice"]
        )

    assert exc.value.code == 1
    assert "Refusing to run without --yes" in capsys.readouterr().out


def test_help_works_without_docstrings(capsys):
    """``python -OO`` strips ``__doc__``; argparse's description is a literal."""
    assert remove_org_member._SUMMARY and isinstance(remove_org_member._SUMMARY, str)

    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-remove-org-member", "--help"])

    assert exc.value.code == 0
    assert remove_org_member._SUMMARY in capsys.readouterr().out.replace("\n", " ")


def test_organization_id_and_user_are_required():
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-remove-org-member", "--yes"])
    assert exc.value.code == 2


def test_argparse_usage_error_does_not_collide_with_the_half_state_code():
    """argparse owns 2, so the half-state must not be 2.

    The runbook tells operators that the half-state code means "removed but not
    revoked". If a mistyped flag produced the same code, an operator would read
    a command that never ran as one that half-ran.
    """
    assert remove_org_member.EXIT_REVOCATION_INCOMPLETE != 2
    assert remove_org_member.EXIT_MEMBERSHIP_NOT_REMOVED != 2


def test_dry_run_with_yes_is_a_usage_error(capsys):
    """The two documented invocations differ by one flag; both is not a preference."""
    with pytest.raises(SystemExit) as exc:
        _run_main(
            [
                "fm-remove-org-member",
                "--organization-id",
                ORG_ID,
                "--user",
                "alice",
                "--dry-run",
                "--yes",
            ]
        )

    assert exc.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


# =============================================================================
# The revocation-store preflight
# =============================================================================


async def test_refuses_when_the_revocation_store_is_process_local(wiring, capsys):
    """A watermark written against in-process FakeRedis is invisible to the API."""
    wiring.container.get_service = lambda name: SimpleNamespace(
        redis=_FakeRedisClient()
    )

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    assert "in-process FakeRedis" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()


async def test_refuses_when_the_revocation_store_has_no_client(wiring, capsys):
    """A store built without a client fails on the watermark write — after the
    delete has landed. That half-state is detectable here, before the write."""
    wiring.container.get_service = lambda name: SimpleNamespace(redis=None)

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    assert "no client" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()


async def test_unrecognised_store_shape_is_not_refused(wiring):
    """The guard identifies the known-broken shapes; it does not demand proof of
    health, so a future store implementation is not an outage here."""
    wiring.container.get_service = lambda name: SimpleNamespace()  # no `.redis`

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 0
    wiring.orgs.remove_member.assert_awaited_once()


async def test_refuses_when_nothing_can_revoke(wiring, capsys):
    """No auth service means no revocation, so the membership is left alone."""
    wiring.container.get_auth_service = lambda: None

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    assert "cannot be revoked" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()


# =============================================================================
# The write
# =============================================================================


async def test_removal_goes_through_the_paired_service(wiring, capsys):
    """Both writes land, and the operator is told the revocation instant."""
    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 0
    wiring.orgs.remove_member.assert_awaited_once_with(ORG_ID, USER_ID)
    wiring.auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    out = capsys.readouterr().out
    assert "Membership removed" in out
    assert REVOKED_AT.isoformat() in out


async def test_tenant_context_is_bound_to_the_target_org(wiring, monkeypatch):
    """RLS (migration 018) scopes both tables by ``app.current_org_id``.

    Without this the lookups and the DELETE run against whatever org the context
    defaulted to — the Standalone sentinel — and silently affect nothing.
    """
    bound: list[str] = []
    real_set = tenant_context.set_current_org_id

    def _record(org_id):
        bound.append(org_id)
        real_set(org_id)

    monkeypatch.setattr("faultmaven.config.tenant_context.set_current_org_id", _record)

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert bound == [ORG_ID]
    # It must also be bound *before* the write, or the service refuses.
    assert code == 0
    wiring.orgs.remove_member.assert_awaited_once_with(ORG_ID, USER_ID)


async def test_dry_run_writes_nothing(wiring, capsys):
    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=True
    )

    assert code == 0
    assert "Dry run" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()
    wiring.auth_service.revoke_user_tokens.assert_not_awaited()


async def test_a_non_member_is_refused_not_revoked(wiring, capsys):
    """The cross-tenant footgun: `users` is not tenant-scoped.

    A mistyped --organization-id resolves a real account that belongs to some
    *other* organization. Revoking it would end every session that user has while
    removing nothing — an unrelated tenant's user signed out during someone
    else's offboarding.
    """
    wiring.orgs.get_member_role.return_value = None

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    assert "not a member" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()
    wiring.auth_service.revoke_user_tokens.assert_not_awaited()


async def test_finish_interrupted_revokes_a_non_member(wiring, capsys):
    """The recovery path the refusal above must not block.

    After a run that deleted the row and failed to revoke, the user is no longer
    a member and the outstanding revocation still has to be written.
    """
    wiring.orgs.get_member_role.return_value = None
    wiring.orgs.remove_member.return_value = False

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID,
        user_identifier="alice",
        dry_run=False,
        finish_interrupted=True,
    )

    assert code == 0
    wiring.auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert "already gone" in capsys.readouterr().out


async def test_failed_revocation_reports_the_half_state(wiring, capsys):
    """Distinct from a refusal: the membership is gone and tokens are still live."""
    wiring.auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is gone")

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == remove_org_member.EXIT_REVOCATION_INCOMPLETE == 3
    out = capsys.readouterr().out
    assert "STILL VALID" in out
    assert "Re-run" in out


async def test_a_delete_that_matches_nothing_is_not_reported_as_success(wiring, capsys):
    """The command read the row as present, then deleted nothing.

    Saying "already gone" here would tell an operator access is cut while the row
    survives and only the tokens died — the user is back in on their next login.
    """
    wiring.orgs.get_member_role.return_value = "role-uuid"
    wiring.orgs.remove_member.return_value = False

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == remove_org_member.EXIT_MEMBERSHIP_NOT_REMOVED == 4
    out = capsys.readouterr().out
    assert "matched no row" in out
    assert "may survive" in out


# =============================================================================
# Resolution
# =============================================================================


async def test_unknown_organization_is_refused(wiring, capsys):
    wiring.orgs.get_organization.return_value = None

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    assert "id, not a slug" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()


async def test_user_resolves_by_email_then_by_id(wiring):
    """Username first, then email, then id — a username shaped like an id still
    resolves as a username."""
    wiring.user_store.get_user_by_username.return_value = None
    wiring.user_store.get_user_by_email.return_value = SimpleNamespace(
        user_id=USER_ID, username="alice", email="alice@acme.example"
    )

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice@acme.example", dry_run=False
    )

    assert code == 0
    wiring.user_store.get_user.assert_not_awaited()
    wiring.orgs.remove_member.assert_awaited_once_with(ORG_ID, USER_ID)


async def test_unknown_user_is_refused(wiring, capsys):
    wiring.user_store.get_user_by_username.return_value = None
    wiring.user_store.get_user_by_email.return_value = None
    wiring.user_store.get_user.return_value = None

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="nobody", dry_run=False
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "No user matches" in out
    # Now that a failed lookup raises (#1043), this message may state plainly
    # that the account is absent. It used to have to hedge, because "absent" and
    # "the lookup broke" arrived here identically.
    assert "completed and matched nothing" in out
    wiring.orgs.remove_member.assert_not_awaited()


@pytest.mark.parametrize(
    "failing_lookup",
    ["get_user_by_username", "get_user_by_email", "get_user"],
)
async def test_a_failed_lookup_is_refused_as_a_failure_not_as_absence(
    wiring, capsys, failing_lookup
):
    """The operator-path complaint in #1043, on the command it was found on.

    An unavailable user store used to print ``No user matches 'alice'`` and exit
    1. The operator then went hunting for the right username — during an
    offboarding, with the cutoff not yet made and the real fault invisible. The
    three lookups are parametrised because the first one to break is whichever
    one the identifier happens to reach.
    """
    # Everything before the failing lookup completes and matches nothing, so the
    # resolver actually gets there.
    order = ["get_user_by_username", "get_user_by_email", "get_user"]
    for name in order[: order.index(failing_lookup)]:
        getattr(wiring.user_store, name).return_value = None
    getattr(wiring.user_store, failing_lookup).side_effect = UserLookupFailed(
        "the database is unavailable",
        lookup="username",
        identifier="alice",
    )

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "not 'no such user'" in out
    assert "NOTHING has been removed or revoked" in out
    # The refusal is before any write, which is what makes it safe to retry.
    wiring.orgs.remove_member.assert_not_awaited()
    wiring.auth_service.revoke_user_tokens.assert_not_awaited()


async def test_a_failed_first_lookup_does_not_fall_through_to_the_others(
    wiring, capsys
):
    """Asking the same unavailable store twice more cannot turn failure into absence.

    Falling through would reach the end of the resolver and return None, which
    the caller reports as "no user matches" — the exact misreport #1043 is
    about, reintroduced one layer up.
    """
    wiring.user_store.get_user_by_username.side_effect = UserLookupFailed(
        "the database is unavailable", lookup="username", identifier="alice"
    )

    await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    wiring.user_store.get_user_by_email.assert_not_awaited()
    wiring.user_store.get_user.assert_not_awaited()


def test_membership_removal_incomplete_is_the_documented_failure():
    """The CLI's half-state exit code is tied to the service's half-state exception."""
    assert issubclass(MembershipRemovalIncomplete, Exception)
    assert remove_org_member.EXIT_REVOCATION_INCOMPLETE == 3


# --- the last-admin constraint trigger refusing the delete (#1161) ----------


class _PgError(Exception):
    """Stand-in for the driver error SQLAlchemy wraps.

    A real exception object rather than a ``Mock``, which answers every
    attribute with a truthy value and would make the recogniser "match"
    regardless of what it asked for.
    """

    def __init__(self, sqlstate: str, constraint_name: str) -> None:
        super().__init__("would be left with no admin")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _db_error(sqlstate: str, constraint_name: str) -> DBAPIError:
    orig = _PgError(sqlstate, constraint_name)
    orig.__cause__ = _PgError(sqlstate, constraint_name)
    return DBAPIError("DELETE FROM organization_members", {}, orig)


async def test_last_admin_refusal_is_reported_not_raised(wiring, capsys):
    """This command is one of the writers the constraint trigger exists to cover.

    It never goes near the Cloud service's own last-admin check, so before
    migration 044 nothing stopped it and after 044 nothing explained it — the
    operator would have got a traceback where a refusal belongs.
    """
    wiring.orgs.remove_member.side_effect = _db_error("23514", LAST_ADMIN_CONSTRAINT)

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "last admin" in out
    assert "NOTHING was written" in out
    # A refusal is not the half-state: those codes mean an operator must come
    # back, and this one means nothing happened.
    assert code != remove_org_member.EXIT_REVOCATION_INCOMPLETE
    assert code != remove_org_member.EXIT_MEMBERSHIP_NOT_REMOVED


async def test_last_admin_refusal_revokes_nothing(wiring):
    """Nothing was written, so no session is ended.

    The delete is what raised and the core service revokes only after it
    returns — worth asserting rather than reasoning about, because signing a
    user out of an organization they are still in looks like nothing at all
    from the exit code.
    """
    wiring.orgs.remove_member.side_effect = _db_error("23514", LAST_ADMIN_CONSTRAINT)

    await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    wiring.auth_service.revoke_user_tokens.assert_not_awaited()


async def test_an_unrelated_database_error_is_not_reported_as_last_admin(wiring):
    """Only this guard's refusal gets the friendly message.

    A recogniser matching on the error class alone would tell an operator their
    last admin is protected when the real fault was a foreign key or a dead
    connection, and the real failure would never be reported.
    """
    wiring.orgs.remove_member.side_effect = _db_error(
        "23503", "organization_members_user_id_fkey"
    )

    with pytest.raises(DBAPIError):
        await remove_org_member.remove_org_member(
            organization_id=ORG_ID, user_identifier="alice", dry_run=False
        )

    wiring.auth_service.revoke_user_tokens.assert_not_awaited()
