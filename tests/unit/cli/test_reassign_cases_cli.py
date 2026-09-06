"""``fm-reassign-cases`` guards and reporting (faultmaven-slack-agent#61 step 4).

The transaction itself is pinned against a real database in
``tests/integration/test_reassign_cases_cli.py``. What is covered here is
everything that decides whether that transaction runs at all — because the
damage this command can do is not a bad UPDATE, it is a *correct* UPDATE aimed
at the wrong account or the wrong set of cases.

The guard with the most teeth is the two-source cross-check: the backend records
no Slack workspace on a case, so the caller's file and the organization sweep are
independent evidence about the same set, and any disagreement between them is a
refusal that names the ids rather than a partial move.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.cli import reassign_cases
from faultmaven.cli.reassign_cases import (
    _Refused,
    describe_set_mismatch,
    read_case_ids,
)

pytestmark = pytest.mark.unit

ORG = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
OLD = "225bae2f-f459-4a54-9c08-2da5c2b3a961"
NEW = "9f1c5d20-1111-4222-8333-444455556666"
TEAM = "team-aaaa-1111"
IDS = ["case_aaa111", "case_bbb222"]


# --- the case-id file ----------------------------------------------------


def test_read_case_ids_parses_ids_skipping_comments_and_blanks(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text(
        "# ids from thread_cases where team_id='T0B9XNZDR44'\n"
        "case_aaa111\n"
        "\n"
        "  case_bbb222  # the second one\n"
    )
    assert read_case_ids(str(path)) == IDS


def test_read_case_ids_refuses_an_empty_file(tmp_path):
    """An empty file is what a failed extraction looks like, not 'nothing to do'."""
    path = tmp_path / "ids.txt"
    path.write_text("# nothing here\n\n")
    with pytest.raises(_Refused, match="names no case ids"):
        read_case_ids(str(path))


def test_read_case_ids_refuses_duplicates(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("case_aaa111\ncase_bbb222\ncase_aaa111\n")
    with pytest.raises(_Refused, match="case_aaa111"):
        read_case_ids(str(path))


def test_read_case_ids_refuses_an_unreadable_file(tmp_path):
    with pytest.raises(_Refused, match="could not read"):
        read_case_ids(str(tmp_path / "absent.txt"))


# --- the two-source cross-check -----------------------------------------


def test_mismatch_names_ids_that_are_named_but_not_owned():
    message = describe_set_mismatch(["case_a", "case_b"], {"case_a"}, "slack-agent")
    assert "named but NOT owned" in message
    assert "case_b" in message


def test_mismatch_names_ids_that_are_owned_but_not_named():
    """The direction that catches a second workspace's history on one account."""
    message = describe_set_mismatch(["case_a"], {"case_a", "case_c"}, "slack-agent")
    assert "NOT named in the file" in message
    assert "case_c" in message


# --- the run -------------------------------------------------------------


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    """Wire the command to test doubles, returning them so a test can vary one.

    The command imports its collaborators inside the function, so patching the
    source modules is what reaches it.
    """
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("\n".join(IDS) + "\n")

    orgs = AsyncMock()
    orgs.get_organization.return_value = SimpleNamespace(
        organization_id=ORG, name="Acme"
    )
    orgs.get_member_role.return_value = "role-uuid"

    users = {
        "slack-agent": SimpleNamespace(
            user_id=OLD, username="slack-agent", email="a@x.test", is_active=True
        ),
        "slack-T0B9XNZDR44": SimpleNamespace(
            user_id=NEW, username="slack-T0B9XNZDR44", email="b@x.test", is_active=True
        ),
    }
    user_store = AsyncMock()
    user_store.get_user_by_username.side_effect = lambda n: users.get(n)
    user_store.get_user_by_email.return_value = None
    user_store.get_user.return_value = None

    teams = AsyncMock()
    teams.list_user_teams.side_effect = lambda uid: (
        [SimpleNamespace(team_id=TEAM, organization_id=ORG)] if uid == NEW else []
    )

    container = SimpleNamespace(
        initialize=AsyncMock(), get_user_store=lambda: user_store
    )

    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence."
        "sessionless_organization_repository.SessionlessOrganizationRepository",
        lambda: orgs,
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence."
        "sessionless_team_repository.SessionlessTeamRepository",
        lambda: teams,
    )
    monkeypatch.setattr(
        reassign_cases, "_swept_case_ids", AsyncMock(return_value=set(IDS))
    )
    apply_mock = AsyncMock()
    monkeypatch.setattr(reassign_cases, "_apply", apply_mock)

    return SimpleNamespace(
        orgs=orgs,
        users=users,
        user_store=user_store,
        teams=teams,
        apply=apply_mock,
        ids_file=str(ids_file),
    )


async def _run(wiring, **overrides):
    kwargs = dict(
        organization_id=ORG,
        from_identifier="slack-agent",
        to_identifier="slack-T0B9XNZDR44",
        case_ids_file=wiring.ids_file,
        allow_no_team=False,
        move_unnamed_too=False,
        dry_run=False,
    )
    kwargs.update(overrides)
    return await reassign_cases.reassign_cases(**kwargs)


@pytest.mark.asyncio
async def test_a_clean_run_applies_the_move(wiring):
    """The positive path: every guard passes and the transaction is handed the
    swept set, the resolved teams, and both principals."""
    assert await _run(wiring) == 0
    wiring.apply.assert_awaited_once()
    kwargs = wiring.apply.await_args.kwargs
    assert kwargs["case_ids"] == IDS
    assert kwargs["from_user_id"] == OLD
    assert kwargs["to_user_id"] == NEW
    assert kwargs["team_ids"] == [TEAM]
    assert kwargs["organization_id"] == ORG


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(wiring):
    assert await _run(wiring, dry_run=True) == 0
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_when_the_organization_is_not_visible(wiring):
    wiring.orgs.get_organization.return_value = None
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_an_unknown_user(wiring):
    assert await _run(wiring, to_identifier="typo-account") == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_when_source_and_target_are_the_same_account(wiring):
    assert await _run(wiring, to_identifier="slack-agent") == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_an_inactive_target(wiring):
    """An inactive owner cannot authenticate — the orphaning this prevents."""
    wiring.users["slack-T0B9XNZDR44"].is_active = False
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_a_target_that_is_not_a_member_of_the_organization(wiring):
    """``users`` is not tenant-scoped, so a mistyped --to-user resolves to a real
    account somewhere. Membership is what ties it to THIS tenant."""
    wiring.orgs.get_member_role.return_value = None
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_require_the_source_to_be_a_member(wiring):
    """The global ``slack-agent`` holds no membership row in any organization —
    the account this command exists to retire. Only the target is checked, so
    ``get_member_role`` is asked about the target and nothing else."""
    assert await _run(wiring) == 0
    asked = [call.args[1] for call in wiring.orgs.get_member_role.await_args_list]
    assert asked == [NEW]


@pytest.mark.asyncio
async def test_refuses_when_a_named_case_is_not_owned_by_the_source(
    wiring, monkeypatch
):
    monkeypatch.setattr(
        reassign_cases, "_swept_case_ids", AsyncMock(return_value={IDS[0]})
    )
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_when_the_source_owns_a_case_the_file_does_not_name(
    wiring, monkeypatch
):
    monkeypatch.setattr(
        reassign_cases,
        "_swept_case_ids",
        AsyncMock(return_value=set(IDS) | {"case_from_another_workspace"}),
    )
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_when_the_target_belongs_to_no_team(wiring):
    """Without a Team the moved cases stay owner-only — invisible to every human
    while every case created after the bind is team-visible."""
    wiring.teams.list_user_teams.side_effect = lambda _uid: []
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_no_team_moves_ownership_only(wiring):
    wiring.teams.list_user_teams.side_effect = lambda _uid: []
    assert await _run(wiring, allow_no_team=True) == 0
    assert wiring.apply.await_args.kwargs["team_ids"] == []


# --- the entrypoint ------------------------------------------------------


def _main(argv):
    original = sys.argv
    sys.argv = argv
    try:
        reassign_cases.main()
    finally:
        sys.argv = original


def test_main_refuses_without_dry_run_or_yes(capsys):
    """A run with neither flag is an operator who has not decided yet. The check
    sits ahead of any database connection, so the refusal cannot half-run."""
    with pytest.raises(SystemExit) as exit_info:
        _main(
            [
                "fm-reassign-cases",
                "--organization-id",
                ORG,
                "--from-user",
                "slack-agent",
                "--to-user",
                "slack-T0B9XNZDR44",
                "--case-ids-file",
                "ids.txt",
            ]
        )
    assert exit_info.value.code == 1
    assert "Refusing to run without --yes" in capsys.readouterr().out


def test_main_rejects_dry_run_together_with_yes():
    """The two invocations differ by one flag, so silently taking the dry-run
    branch would exit 0 and read as 'moved'."""
    with pytest.raises(SystemExit) as exit_info:
        _main(
            [
                "fm-reassign-cases",
                "--organization-id",
                ORG,
                "--from-user",
                "slack-agent",
                "--to-user",
                "slack-T0B9XNZDR44",
                "--case-ids-file",
                "ids.txt",
                "--dry-run",
                "--yes",
            ]
        )
    assert exit_info.value.code == 2  # argparse usage error


# -- the RLS binding, and the guards a review proved were untested ------------


@pytest.mark.asyncio
async def test_the_rls_scope_is_bound_before_any_session_is_opened(wiring, monkeypatch):
    """`set_current_enterprise_id` is what makes every later query tenant-scoped.

    The engine applies `app.current_enterprise_id` per transaction from this
    contextvar, so a session opened before the bind runs unscoped (#935 was
    exactly that bug). A code review proved this line could be deleted with the
    whole suite green — a guard no test can see removed is one that will be.
    """
    from faultmaven.config import tenant_context

    bound: list[str] = []
    monkeypatch.setattr(tenant_context, "set_current_enterprise_id", bound.append)
    # The sweep is the first thing to touch the database, so record when it ran.
    monkeypatch.setattr(
        reassign_cases,
        "_swept_case_ids",
        AsyncMock(side_effect=lambda *_a: bound.append("SESSION") or set(IDS)),
    )

    assert await _run(wiring) == 0
    assert bound == [
        ORG,
        "SESSION",
    ], "the org scope must be bound before the first session, not after"


@pytest.mark.asyncio
async def test_teams_in_another_organization_are_not_shared_to(wiring):
    """`list_user_teams` is RLS-scoped in production, but this command must also
    be right on a connection that is not — sharing a case to a stranger's team
    is the error that would print success."""
    wiring.teams.list_user_teams.side_effect = lambda uid: [
        SimpleNamespace(team_id=TEAM, organization_id=ORG),
        SimpleNamespace(team_id="team-elsewhere", organization_id="another-org"),
    ]

    assert await _run(wiring) == 0
    assert wiring.apply.await_args.kwargs["team_ids"] == [TEAM]


@pytest.mark.asyncio
async def test_the_old_owners_teams_are_revoked_except_the_shared_one(wiring):
    wiring.teams.list_user_teams.side_effect = lambda uid: (
        [
            SimpleNamespace(team_id=TEAM, organization_id=ORG),
            SimpleNamespace(team_id="team-both", organization_id=ORG),
        ]
        if uid == NEW
        else [
            SimpleNamespace(team_id="team-old", organization_id=ORG),
            SimpleNamespace(team_id="team-both", organization_id=ORG),
        ]
    )

    assert await _run(wiring) == 0
    assert wiring.apply.await_args.kwargs["revoke_team_ids"] == ["team-old"]


@pytest.mark.asyncio
async def test_an_inactive_flag_that_is_absent_entirely_is_refused(wiring):
    """`getattr(..., True)` passed any object without the attribute — an
    unrecognised user shape is exactly what this guard must not wave through."""
    wiring.users["slack-T0B9XNZDR44"] = SimpleNamespace(
        user_id=NEW, username="slack-T0B9XNZDR44", email="b@x.test"
    )
    assert await _run(wiring) == 1
    wiring.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_unnamed_cases_may_be_left_behind_deliberately(wiring, monkeypatch):
    """The two-workspace case the cross-check exists to detect must not also be
    the one situation the command can never serve."""
    monkeypatch.setattr(
        reassign_cases,
        "_swept_case_ids",
        AsyncMock(return_value=set(IDS) | {"case_other_workspace"}),
    )

    assert await _run(wiring, move_unnamed_too=True) == 0
    assert wiring.apply.await_args.kwargs["case_ids"] == IDS


@pytest.mark.asyncio
async def test_a_named_case_that_is_not_owned_has_no_escape_hatch(wiring, monkeypatch):
    """The other direction stays a hard refusal: it is a typo, the wrong
    organization, or an already-completed run, and no flag makes it right."""
    monkeypatch.setattr(
        reassign_cases, "_swept_case_ids", AsyncMock(return_value={IDS[0]})
    )
    assert await _run(wiring, move_unnamed_too=True) == 1
    wiring.apply.assert_not_awaited()


def test_a_file_that_is_not_utf8_is_refused_not_a_traceback(tmp_path):
    path = tmp_path / "ids.bin"
    path.write_bytes(b"\xff\xfe\x00case_aaa111")
    with pytest.raises(_Refused, match="could not read"):
        read_case_ids(str(path))
