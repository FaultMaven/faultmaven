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
def wire(monkeypatch, store):
    """Point both CLI modules' container at ``store``."""

    def _wire(module, user):
        container = AsyncMock()
        container.get_user_store = lambda: store
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
