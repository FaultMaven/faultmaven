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

from faultmaven.cli import remove_org_member
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


# =============================================================================
# The FakeRedis guard
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


async def test_unrecognised_store_shape_is_not_refused(wiring):
    """The guard identifies the known-broken case; it does not demand proof of health."""
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
    monkeypatch.setattr(
        "faultmaven.config.tenant_context.set_current_org_id", bound.append
    )

    await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert bound == [ORG_ID]


async def test_dry_run_writes_nothing(wiring, capsys):
    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=True
    )

    assert code == 0
    assert "Dry run" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()
    wiring.auth_service.revoke_user_tokens.assert_not_awaited()


async def test_absent_membership_still_revokes(wiring, capsys):
    """The state a half-completed previous run leaves behind: finish it."""
    wiring.orgs.get_member_role.return_value = None
    wiring.orgs.remove_member.return_value = False

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == 0
    wiring.auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert "already gone" in capsys.readouterr().out


async def test_failed_revocation_exits_2_and_says_it_is_unfinished(wiring, capsys):
    """Distinct from a refusal: the membership is gone and tokens are still live."""
    wiring.auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is gone")

    code = await remove_org_member.remove_org_member(
        organization_id=ORG_ID, user_identifier="alice", dry_run=False
    )

    assert code == remove_org_member.EXIT_REVOCATION_INCOMPLETE == 2
    out = capsys.readouterr().out
    assert "STILL VALID" in out
    assert "Re-run" in out


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
    assert "No user matches" in capsys.readouterr().out
    wiring.orgs.remove_member.assert_not_awaited()


def test_membership_removal_incomplete_is_the_documented_failure():
    """The CLI's exit code 2 is tied to the service's half-state exception."""
    assert issubclass(MembershipRemovalIncomplete, Exception)
    assert remove_org_member.EXIT_REVOCATION_INCOMPLETE == 2
