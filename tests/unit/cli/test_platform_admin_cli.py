"""``fm-promote-platform-admin`` / ``fm-demote-platform-admin`` behaviour (#887).

These commands are the only way a deployment operator comes into existence
(ADR-012 D9: no login path grants elevated roles), so what they write to an
account is an auth critical path and gets behavioural coverage, not just a
"the entrypoint resolves" smoke test.

The store and container are mocked — the questions here are *which roles end up
on the account* and *what the operator is told* — but the account itself is a
real :class:`DevUser`, so ``__post_init__``'s least-privilege default and the
real field names are in the loop. A dict or SimpleNamespace stand-in would let a
typo in an attribute name pass.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.bootstrap.data_init import DEFAULT_ADMIN_USERNAME
from faultmaven.cli import demote_platform_admin, promote_platform_admin
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.contracts import (
    PLATFORM_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE_SET,
)
from faultmaven.modules.auth.domain.models.auth import DevUser

pytestmark = pytest.mark.unit


def _user(username="alice", roles=None) -> DevUser:
    return DevUser(
        user_id="225bae2f-f459-4a54-9c08-2da5c2b3a961",
        username=username,
        email=f"{username}@dev.faultmaven.local",
        display_name=username.title(),
        created_at=datetime.now(timezone.utc),
        roles=roles,
    )


@pytest.fixture
def store():
    """A user store that hands back whatever ``update_user`` was given."""
    store = AsyncMock()
    store.update_user.side_effect = lambda user: user
    return store


@pytest.fixture
def audit(monkeypatch):
    """Capture the operator-role audit calls both CLIs make (fm#1050).

    Substituted rather than allowed through: ``record_operator_role_change``
    opens a real database session, and these are unit tests. The recorder keeps
    the kwargs so tests can assert *what* was recorded, not merely that
    something was.
    """
    recorder = AsyncMock()
    for module in (promote_platform_admin, demote_platform_admin):
        monkeypatch.setattr(module, "record_operator_role_change", recorder)
    return recorder


@pytest.fixture
def auth_service():
    """An auth service whose revocation succeeds and records the watermark."""
    service = AsyncMock()
    service.revoke_user_tokens.return_value = datetime.now(timezone.utc)
    return service


@pytest.fixture
def wire(monkeypatch, store, audit, auth_service):
    """Point both CLI modules' container at ``store``."""

    def _wire(module, user):
        container = AsyncMock()
        container.get_user_store = lambda: store
        container.get_auth_service = lambda: auth_service
        store.get_user_by_username.return_value = user
        monkeypatch.setattr(module, "container", container)
        return store

    return _wire


# =============================================================================
# Promote
# =============================================================================


async def test_promote_grants_exactly_the_missing_operator_roles(wire, store):
    user = _user(roles=["user"])
    wire(promote_platform_admin, user)

    assert await promote_platform_admin.promote_to_platform_admin("alice") is True

    store.update_user.assert_awaited_once()
    written = store.update_user.await_args.args[0]
    # The whole operator set, and nothing beyond it.
    assert set(written.roles) == set(PLATFORM_ADMIN_ROLE_SET)
    # Pre-existing roles are preserved, not replaced.
    assert written.roles[0] == "user"


async def test_promote_repairs_a_partially_granted_account(wire, store):
    """Holding platform_admin without the org-scoped admin is a real state — an
    operator with no authority inside its own organization. The promote path
    must complete the set rather than report "already an admin" and leave it."""
    user = _user(roles=["user", PLATFORM_ADMIN_ROLE])
    wire(promote_platform_admin, user)

    assert await promote_platform_admin.promote_to_platform_admin("alice") is True

    written = store.update_user.await_args.args[0]
    assert set(written.roles) == set(PLATFORM_ADMIN_ROLE_SET)
    assert "admin" in written.roles


async def test_promote_is_idempotent_and_writes_nothing(wire, store, capsys):
    user = _user(roles=list(PLATFORM_ADMIN_ROLE_SET))
    wire(promote_platform_admin, user)

    assert await promote_platform_admin.promote_to_platform_admin("alice") is True

    store.update_user.assert_not_awaited()
    assert "already holds every operator role" in capsys.readouterr().out


async def test_promote_reports_not_found_and_writes_nothing(wire, store, capsys):
    wire(promote_platform_admin, None)

    assert await promote_platform_admin.promote_to_platform_admin("ghost") is False

    store.update_user.assert_not_awaited()
    out = capsys.readouterr().out
    assert "not found" in out
    # The hint must be usable where the command runs — a pod has no checkout.
    assert "/api/v1/admin/users" in out


def test_promote_main_exits_1_when_the_user_is_missing(wire, store):
    wire(promote_platform_admin, None)

    with pytest.raises(SystemExit) as exc:
        _run_main(promote_platform_admin, ["fm-promote-platform-admin", "ghost"])
    assert exc.value.code == 1


def test_promote_main_rejects_extra_arguments(wire, store):
    """argparse, not ``len(sys.argv) < 2`` — a second positional is an error,
    not silently ignored."""
    with pytest.raises(SystemExit) as exc:
        _run_main(promote_platform_admin, ["fm-promote-platform-admin", "a", "b"])
    assert exc.value.code == 2  # argparse usage error


def test_promote_main_requires_a_username():
    with pytest.raises(SystemExit) as exc:
        _run_main(promote_platform_admin, ["fm-promote-platform-admin"])
    assert exc.value.code == 2


# =============================================================================
# Demote
# =============================================================================


async def test_demote_removes_platform_admin_but_keeps_org_admin(wire, store):
    """Withdrawing operator status must not also strip authority inside the
    user's own organization — those are different scopes (ADR-012 D9)."""
    user = _user(username="bob", roles=list(PLATFORM_ADMIN_ROLE_SET))
    wire(demote_platform_admin, user)

    assert await demote_platform_admin.demote_from_platform_admin("bob") is True

    written = store.update_user.await_args.args[0]
    assert PLATFORM_ADMIN_ROLE not in written.roles
    assert "admin" in written.roles
    assert "user" in written.roles


async def test_demote_is_a_no_op_on_a_non_operator(wire, store, capsys):
    user = _user(username="bob", roles=["user"])
    wire(demote_platform_admin, user)

    assert await demote_platform_admin.demote_from_platform_admin("bob") is True

    store.update_user.assert_not_awaited()
    assert "is not a platform admin" in capsys.readouterr().out


async def test_demoting_the_bootstrap_account_warns_that_startup_re_grants(
    wire, store, monkeypatch, capsys
):
    """``assign_operator_roles`` runs on EVERY startup, so this demotion does
    not survive a restart. Saying so is the contract data_init.py documents —
    reporting a success that quietly reverts would be worse than refusing."""
    user = _user(username=DEFAULT_ADMIN_USERNAME, roles=list(PLATFORM_ADMIN_ROLE_SET))
    wire(demote_platform_admin, user)
    monkeypatch.setattr("builtins.input", lambda *_: "yes")

    assert (
        await demote_platform_admin.demote_from_platform_admin(DEFAULT_ADMIN_USERNAME)
        is True
    )

    out = capsys.readouterr().out
    assert "bootstrap operator account" in out
    assert "undone by the next restart" in out
    store.update_user.assert_awaited_once()


async def test_declining_the_bootstrap_prompt_writes_nothing(
    wire, store, monkeypatch, capsys
):
    user = _user(username=DEFAULT_ADMIN_USERNAME, roles=list(PLATFORM_ADMIN_ROLE_SET))
    wire(demote_platform_admin, user)
    monkeypatch.setattr("builtins.input", lambda *_: "no")

    assert (
        await demote_platform_admin.demote_from_platform_admin(DEFAULT_ADMIN_USERNAME)
        is False
    )

    store.update_user.assert_not_awaited()
    assert "Cancelled" in capsys.readouterr().out


async def test_demote_reports_not_found_and_writes_nothing(wire, store, capsys):
    wire(demote_platform_admin, None)

    assert await demote_platform_admin.demote_from_platform_admin("ghost") is False

    store.update_user.assert_not_awaited()
    assert "not found" in capsys.readouterr().out


def test_demote_main_rejects_extra_arguments():
    with pytest.raises(SystemExit) as exc:
        _run_main(demote_platform_admin, ["fm-demote-platform-admin", "a", "b"])
    assert exc.value.code == 2


def _run_main(module, argv):
    """Invoke a CLI module's ``main()`` with a given argv."""
    import sys

    original = sys.argv
    sys.argv = argv
    try:
        module.main()
    finally:
        sys.argv = original


# =============================================================================
# Audit trail (fm#1050)
#
# Granting platform_admin is the highest-privilege operation the deployment
# offers, and it recorded nothing: after the fm#819 cutover the audit table held
# one row (SSO JIT provisioning) and none for the promotion.
# =============================================================================


async def test_promote_records_the_grant(wire, store, audit):
    wire(promote_platform_admin, _user(roles=["user"]))

    assert await promote_platform_admin.promote_to_platform_admin("alice") is True

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] is OperatorAction.ROLE_GRANTED
    assert kwargs["user"].username == "alice"
    assert PLATFORM_ADMIN_ROLE in kwargs["roles_changed"]
    assert kwargs["invoked_via"] == "fm-promote-platform-admin"


async def test_demote_records_the_revocation(wire, store, audit):
    wire(demote_platform_admin, _user(roles=list(PLATFORM_ADMIN_ROLE_SET)))

    assert await demote_platform_admin.demote_from_platform_admin("alice") is True

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] is OperatorAction.ROLE_REVOKED
    assert kwargs["roles_changed"] == [PLATFORM_ADMIN_ROLE]
    assert kwargs["invoked_via"] == "fm-demote-platform-admin"


async def test_a_no_op_promotion_records_nothing(wire, store, audit):
    """No privilege changed, so there is no privilege change to record.
    Recording one would put an event in the trail that never happened."""
    wire(promote_platform_admin, _user(roles=list(PLATFORM_ADMIN_ROLE_SET)))

    assert await promote_platform_admin.promote_to_platform_admin("alice") is True
    audit.assert_not_awaited()


async def test_a_no_op_demotion_records_nothing(wire, store, audit):
    wire(demote_platform_admin, _user(roles=["user"]))

    assert await demote_platform_admin.demote_from_platform_admin("alice") is True
    audit.assert_not_awaited()


async def test_promote_reports_failure_when_the_grant_cannot_be_recorded(
    wire, store, audit, capsys
):
    """The roles are already persisted, so this cannot un-grant them — but it
    must not report success either. An unrecorded privilege escalation that
    exits 0 is the exact failure fm#1050 is about."""
    wire(promote_platform_admin, _user(roles=["user"]))
    audit.side_effect = RuntimeError("operator_access_audit is unreachable")

    assert await promote_platform_admin.promote_to_platform_admin("alice") is False

    out = capsys.readouterr().out
    assert "audit record failed" in out
    assert "WERE granted" in out, "must not imply the grant was rolled back"
    assert "NOT repair" in out, "a retry is idempotent and would audit nothing"


async def test_demote_revokes_outstanding_tokens(wire, store, auth_service):
    """Access tokens carry `roles` in their claims, so until the user's
    revocation watermark moves the demoted operator keeps cross-tenant reach.
    The HTTP role paths already revoke; this one did not (fm#1050)."""
    user = _user(roles=list(PLATFORM_ADMIN_ROLE_SET))
    wire(demote_platform_admin, user)

    assert await demote_platform_admin.demote_from_platform_admin("alice") is True

    auth_service.revoke_user_tokens.assert_awaited_once_with(user.user_id)


async def test_demote_still_demotes_when_revocation_fails(
    wire, store, auth_service, capsys
):
    """Deliberately the opposite posture to fm-remove-org-member, which refuses.
    The role is already off the account; failing closed here would mean handing
    platform_admin back permanently to avoid a window of one token lifetime."""
    wire(demote_platform_admin, _user(roles=list(PLATFORM_ADMIN_ROLE_SET)))
    auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is down")

    assert await demote_platform_admin.demote_from_platform_admin("alice") is True

    out = capsys.readouterr().out
    assert "revocation failed" in out
    assert "keeps platform-admin reach" in out, "the window must be stated"
