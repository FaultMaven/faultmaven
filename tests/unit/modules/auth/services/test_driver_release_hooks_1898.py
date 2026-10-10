"""The auth-side release points (ADR-020 D3, #1898): leaving a team and
deactivating an account hand a driver's cases back to their creators FIRST.

Both writes commit in a store the case store cannot share a transaction with
(the team row lock; the account row), so the case module's release port is
called BEFORE them. Release-first fails safe: a leave then refused hands the
cases back needlessly — the residual these tests pin.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.modules.auth.exceptions import TeamOperationRefused
from tests.unit.modules.auth.services.test_team_consent_failure_modes import _build
from tests.unit.modules.auth.services.test_team_invitations import ACME, ALICE, BOB

pytestmark = [pytest.mark.unit, pytest.mark.security]


class _RecordingRelease:
    """The case module's ``ICaseDriverRelease`` port, recording each call into
    a log shared with the write it must precede."""

    def __init__(self, log: List[str]):
        self.log = log
        self.calls: List[dict] = []

    async def release_drivers_for_team_leave(self, **kwargs):
        self.log.append("release")
        self.calls.append({"hook": "team_leave", **kwargs})
        return 1

    async def release_drivers_for_deactivation(self, **kwargs):
        self.log.append("release")
        self.calls.append({"hook": "deactivation", **kwargs})
        return 1


async def _team_of_two(service, teams):
    team = await service.create_team(
        enterprise_id=ACME, creator_user_id=ALICE.user_id, name="payments"
    )
    await teams.add_member(ACME, team.team_id, BOB.user_id)
    return team


def _log_leaves(teams, log):
    real = teams.leave_team

    async def logged(*args, **kwargs):
        log.append("leave")
        return await real(*args, **kwargs)

    teams.leave_team = logged


class TestLeavingATeam:
    async def test_the_release_runs_before_the_membership_write(self):
        service, teams, _ = await _build()
        log: List[str] = []
        release = _RecordingRelease(log)
        service.bind_case_driver_release(release)
        team = await _team_of_two(service, teams)
        _log_leaves(teams, log)

        await service.leave_team(
            enterprise_id=ACME, team_id=team.team_id, user_id=BOB.user_id
        )

        # Before the membership write, and again after it: a reassignment can
        # land in between (ADR-020 D3).
        assert log == ["release", "leave", "release"]
        assert release.calls == 2 * [
            {
                "hook": "team_leave",
                "enterprise_id": ACME,
                "team_id": team.team_id,
                "user_id": BOB.user_id,
            }
        ]
        assert not teams.has_member(team.team_id, BOB.user_id)

    async def test_residual_a_refused_leave_has_already_released(self):
        """The last admin may not leave while others remain (409). The release
        ran first, so the leaver's cases went back to their creators
        needlessly — accepted and audited (ADR-020 D3)."""
        service, teams, _ = await _build()
        log: List[str] = []
        release = _RecordingRelease(log)
        service.bind_case_driver_release(release)
        team = await _team_of_two(service, teams)

        with pytest.raises(TeamOperationRefused):
            await service.leave_team(
                enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
            )

        # The first pass ran; no second pass for a leave that did not happen.
        assert [c["hook"] for c in release.calls] == ["team_leave"]
        assert teams.has_member(team.team_id, ALICE.user_id)

    async def test_an_unbound_port_still_leaves(self):
        """Standalone wires no case release into an unwired team service; the
        method must not depend on it being bound."""
        service, teams, _ = await _build()
        team = await _team_of_two(service, teams)

        await service.leave_team(
            enterprise_id=ACME, team_id=team.team_id, user_id=BOB.user_id
        )

        assert not teams.has_member(team.team_id, BOB.user_id)


class TestRosterRead:
    async def test_member_ids_of_live_teams_need_no_membership_of_the_caller(self):
        service, teams, _ = await _build()
        team = await _team_of_two(service, teams)

        rosters = await service.list_member_ids_of_teams(
            enterprise_id=ACME, team_ids=[team.team_id, "team-nonexistent"]
        )

        assert {k: sorted(v) for k, v in rosters.items()} == {
            team.team_id: sorted([ALICE.user_id, BOB.user_id])
        }

    async def test_another_enterprises_team_contributes_nothing(self):
        service, teams, _ = await _build()
        team = await _team_of_two(service, teams)

        assert (
            await service.list_member_ids_of_teams(
                enterprise_id="ent-other", team_ids=[team.team_id]
            )
            == {}
        )


class TestDeactivation:
    def _service(self, log):
        repo = MagicMock()
        user = MagicMock(user_id="u-driver", is_active=True)
        repo.get = AsyncMock(return_value=user)

        async def save(saved):
            log.append("deactivate")
            return saved

        repo.save = AsyncMock(side_effect=save)
        auth = MagicMock()
        auth.revoke_user_tokens = AsyncMock()
        service = UserService(
            user_repo=repo, auth_service=auth, token_generator=MagicMock()
        )
        return service, user

    async def test_the_release_runs_before_the_account_write(self):
        log: List[str] = []
        service, user = self._service(log)
        release = _RecordingRelease(log)
        service.bind_case_driver_release(release)

        await service.deactivate_user_admin(
            user_id="u-driver", enterprise_id=ACME, admin_user_id="u-admin"
        )

        assert log == ["release", "deactivate", "release"]
        assert release.calls == 2 * [
            {"hook": "deactivation", "user_id": "u-driver", "actor_user_id": "u-admin"}
        ]
        assert user.is_active is False

    async def test_a_failing_release_never_blocks_the_deactivation(self, monkeypatch):
        """Turning an account off is a security action: if the case store
        errors, the failure is logged and counted and the account is
        deactivated anyway. Its creators reclaim the cases it still drives."""
        from faultmaven.modules.auth.domain.services import user_service as module

        counted = []
        monkeypatch.setattr(
            module,
            "case_driver_release_failed_total",
            type(
                "C",
                (),
                {
                    "labels": lambda self, **kw: type(
                        "L", (), {"inc": lambda s: counted.append(kw)}
                    )()
                },
            )(),
        )
        log: List[str] = []
        service, user = self._service(log)

        class _Broken:
            async def release_drivers_for_deactivation(self, **kwargs):
                log.append("release-raised")
                raise RuntimeError("case store unavailable")

        service.bind_case_driver_release(_Broken())

        await service.deactivate_user_admin(
            user_id="u-driver", enterprise_id=ACME, admin_user_id="u-admin"
        )

        assert user.is_active is False
        assert log == ["release-raised", "deactivate", "release-raised"]
        assert counted == [{"hook": "deactivation"}] * 2
        service.auth_service.revoke_user_tokens.assert_awaited_once_with("u-driver")

    async def test_an_unknown_account_releases_nothing(self):
        from faultmaven.exceptions import NotFoundError

        log: List[str] = []
        service, _ = self._service(log)
        service.user_repo.get = AsyncMock(return_value=None)
        release = _RecordingRelease(log)
        service.bind_case_driver_release(release)

        with pytest.raises(NotFoundError):
            await service.deactivate_user("u-nobody")

        assert release.calls == []
