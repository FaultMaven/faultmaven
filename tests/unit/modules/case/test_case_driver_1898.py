"""The case driver (ADR-020, #1898): the write split, reassignment, candidates
and the release points, against the real ``CaseService`` over the in-memory
repository.

The world: one enterprise; CREATOR opened the case and shared it with team
``t1`` (CREATOR, DRIVER, TEAMMATE, SERVICE, IDLE are members) and DRIVER is also
in ``t2``. OUTSIDER reads nothing. SERVICE is a service account and IDLE is
deactivated, so neither is a candidate. ``STRANGER`` lives in another
enterprise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Set

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.exceptions import (
    CASE_TERMINAL,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.models.api_models import CaseAccess, CaseListFilter, CaseSearchRequest
from faultmaven.modules.case.contracts import (
    Case,
    CaseDriverChangeReason,
    CaseState,
    InquiryData,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENTERPRISE = "ent_driver_0001"
OTHER_ENTERPRISE = "ent_driver_0002"
CREATOR = "u_creator"
DRIVER = "u_driver"
TEAMMATE = "u_teammate"
SERVICE = "u_service"
IDLE = "u_idle"
OUTSIDER = "u_outsider"
STRANGER = "u_stranger"
CASE_ID = "case_0000000000d1"


class _Teams:
    """``team_service``: membership and rosters, mutable so a test can leave."""

    def __init__(self, members: Dict[str, Set[str]]):
        self.members = members  # team_id -> user ids
        self.calls: List[str] = []

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        return sorted(t for t, users in self.members.items() if user_id in users)

    async def list_member_ids_of_teams(
        self, *, enterprise_id: str, team_ids: List[str]
    ) -> Dict[str, List[str]]:
        assert enterprise_id == ENTERPRISE
        return {t: sorted(self.members[t]) for t in team_ids if t in self.members}


class _Shares:
    """``resource_shares``: case id -> team ids."""

    def __init__(self, shares: Dict[str, Set[str]], log: List[str]):
        self.shares = shares
        self.log = log

    async def list_resource_ids(
        self, *, resource_type, scope_type, scope_ids, enterprise_id
    ):
        return sorted(c for c, teams in self.shares.items() if teams & set(scope_ids))

    async def list_scopes_for_resources(self, resource_type, resource_ids):
        return {
            cid: [SimpleNamespace(scope_type="team", scope_id=t) for t in sorted(teams)]
            for cid, teams in self.shares.items()
            if cid in resource_ids
        }

    async def unshare(self, *, resource_type, resource_id, scope_type, scope_id):
        self.log.append(f"unshare:{scope_id}")
        teams = self.shares.get(resource_id, set())
        if scope_id not in teams:
            return False
        teams.discard(scope_id)
        return True


class _Accounts:
    """The account store's ``get_many_in_enterprise``."""

    def __init__(self):
        def account(uid, enterprise=ENTERPRISE, active=True, kind="individual"):
            return SimpleNamespace(
                user_id=uid,
                display_name=f"Name of {uid}",
                is_active=active,
                account_kind=kind,
                enterprise_id=enterprise,
                email=f"{uid}@example.com",
            )

        self.accounts = {
            CREATOR: account(CREATOR),
            DRIVER: account(DRIVER),
            TEAMMATE: account(TEAMMATE),
            SERVICE: account(SERVICE, kind="service"),
            IDLE: account(IDLE, active=False),
            OUTSIDER: account(OUTSIDER),
            STRANGER: account(STRANGER, enterprise=OTHER_ENTERPRISE),
        }

    async def get_many_in_enterprise(self, enterprise_id, user_ids):
        return [
            a
            for uid, a in self.accounts.items()
            if uid in user_ids and a.enterprise_id == enterprise_id
        ]


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


class World(SimpleNamespace):
    pass


async def _case(repository, *, driver_id=None, case_id=CASE_ID, state=None, **extra):
    fields = dict(
        case_id=case_id,
        user_id=CREATOR,
        driver_id=driver_id,
        enterprise_id=ENTERPRISE,
        title="Checkout latency",
    )
    if state is CaseState.CLOSED:
        fields.update(
            state=CaseState.CLOSED,
            closure_reason="inquiry_only",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            closed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    fields.update(extra)
    return await repository.save(Case(**fields))


@pytest.fixture
def world():
    log: List[str] = []
    teams = _Teams(
        {
            "t1": {CREATOR, DRIVER, TEAMMATE, SERVICE, IDLE},
            "t2": {DRIVER},
        }
    )
    shares = _Shares({CASE_ID: {"t1"}}, log)
    repository = InMemoryCaseRepository()
    service = CaseService(
        repository,
        team_service=teams,
        share_repository=shares,
        account_reader=_Accounts(),
    )
    return World(
        service=service, repository=repository, teams=teams, shares=shares, log=log
    )


# ============================================================
# The resolver (ADR-020 D2): read / driver_only / creator_only
# ============================================================


class TestTheResolver:
    async def test_the_creator_drives_while_driver_id_is_null(self, world):
        await _case(world.repository)
        get = world.service.get_case

        assert await get(CASE_ID, CREATOR) is not None
        assert await get(CASE_ID, CREATOR, driver_only=True) is not None
        assert await get(CASE_ID, CREATOR, creator_only=True) is not None
        assert await get(CASE_ID, TEAMMATE) is not None
        assert await get(CASE_ID, TEAMMATE, driver_only=True) is None
        assert await get(CASE_ID, OUTSIDER) is None

    async def test_an_assigned_driver_writes_and_the_creator_governs(self, world):
        await _case(world.repository, driver_id=DRIVER)
        get = world.service.get_case

        assert await get(CASE_ID, DRIVER, driver_only=True) is not None
        assert await get(CASE_ID, CREATOR, driver_only=True) is None
        assert await get(CASE_ID, CREATOR, creator_only=True) is not None
        assert await get(CASE_ID, DRIVER, creator_only=True) is None
        assert await get(CASE_ID, TEAMMATE, driver_only=True) is None

    async def test_a_driver_who_cannot_read_is_refused_without_any_release(self, world):
        """Security does not depend on the release hooks (ADR-020 D3): a stale
        driver_id is refused by the read half of the driver gate."""
        await _case(world.repository, driver_id=OUTSIDER)

        assert await world.service.get_case(CASE_ID, OUTSIDER, driver_only=True) is None

    async def test_both_flags_at_once_is_a_programming_error(self, world):
        case = await _case(world.repository)

        with pytest.raises(ValueError):
            await world.service._may_access(
                case, CREATOR, driver_only=True, creator_only=True
            )


# ============================================================
# The write split on the service's own writes (ADR-020 D2)
# ============================================================


class TestTheWriteSplit:
    async def test_the_creator_cannot_edit_while_another_drives(self, world):
        await _case(world.repository, driver_id=DRIVER)

        assert (
            await world.service.update_case(CASE_ID, {"title": "x"}, CREATOR) is False
        )
        assert await world.service.update_case(CASE_ID, {"title": "Renamed"}, DRIVER)
        assert (await world.repository.get(CASE_ID)).title == "Renamed"

    @pytest.mark.parametrize("flip", [False, True], ids=["control", "flipped"])
    async def test_a_state_edit_rechecks_the_driver_on_the_fresh_load(
        self, world, flip
    ):
        """A reassignment landing between the gate and the versioned write must
        not let the former driver write as a driver."""
        await _case(world.repository, driver_id=DRIVER)
        gate = world.service.get_case

        async def gate_then_hand_back(case_id, user_id=None, **kw):
            found = await gate(case_id, user_id, **kw)
            if flip:
                await world.repository.release_driver(
                    case_id, driver_id=DRIVER, change=SimpleNamespace()
                )
            return found

        world.service.get_case = gate_then_hand_back
        edited = await world.service.update_case(
            CASE_ID, {"title": "Renamed", "state": CaseState.INQUIRY}, DRIVER
        )

        assert edited is (not flip)
        title = (await world.repository.get(CASE_ID)).title
        assert title == ("Checkout latency" if flip else "Renamed")

    async def test_close_is_the_drivers(self, world):
        await _case(world.repository, driver_id=DRIVER)

        with pytest.raises(NotFoundError):
            await world.service.close_case(CASE_ID, CREATOR)
        closed = await world.service.close_case(CASE_ID, DRIVER)
        assert closed.state == CaseState.CLOSED

    async def test_delete_is_the_creators(self, world):
        await _case(world.repository, driver_id=DRIVER)

        assert await world.service.hard_delete_case(CASE_ID, DRIVER) is False
        assert await world.repository.get(CASE_ID) is not None
        assert await world.service.hard_delete_case(CASE_ID, CREATOR) is True
        assert await world.repository.get(CASE_ID) is None

    async def test_unshare_is_the_creators(self, world):
        await _case(world.repository, driver_id=DRIVER)

        with pytest.raises(ValidationException, match="creator"):
            await world.service.unshare_case_from_team(CASE_ID, "t1", DRIVER)
        assert world.shares.shares[CASE_ID] == {"t1"}


# ============================================================
# Candidates (ADR-020 D4)
# ============================================================


class TestCandidates:
    async def test_the_creator_then_active_individual_members(self, world):
        await _case(world.repository)

        candidates = await world.service.list_driver_candidates(CASE_ID, CREATOR)

        assert [(c.user_id, c.display_name) for c in candidates] == [
            (CREATOR, f"Name of {CREATOR}"),
            (DRIVER, f"Name of {DRIVER}"),
            (TEAMMATE, f"Name of {TEAMMATE}"),
        ]

    async def test_names_never_carry_an_email(self, world):
        await _case(world.repository)

        for candidate in await world.service.list_driver_candidates(CASE_ID, CREATOR):
            assert "@" not in (candidate.display_name or "")
            assert set(vars(candidate)) == {"user_id", "display_name"}

    async def test_a_service_account_creator_is_still_a_candidate(self, world):
        """The creator, whatever its account kind (ADR-020 D4)."""
        await _case(world.repository, user_id=SERVICE)
        world.shares.shares[CASE_ID] = {"t1"}

        ids = [
            c.user_id
            for c in await world.service.list_driver_candidates(CASE_ID, SERVICE)
        ]

        assert ids[0] == SERVICE
        assert SERVICE not in ids[1:]

    async def test_the_driver_may_read_them(self, world):
        await _case(world.repository, driver_id=DRIVER)

        assert await world.service.list_driver_candidates(CASE_ID, DRIVER)

    async def test_another_reader_is_forbidden(self, world):
        await _case(world.repository, driver_id=DRIVER)

        with pytest.raises(AuthorizationError):
            await world.service.list_driver_candidates(CASE_ID, TEAMMATE)

    async def test_a_non_reader_is_told_nothing(self, world):
        await _case(world.repository)

        with pytest.raises(NotFoundError):
            await world.service.list_driver_candidates(CASE_ID, OUTSIDER)

    async def test_standalone_offers_the_creator_alone(self):
        repository = InMemoryCaseRepository()
        service = CaseService(repository, account_reader=_Accounts())
        await _case(repository)

        candidates = await service.list_driver_candidates(CASE_ID, CREATOR)

        assert [c.user_id for c in candidates] == [CREATOR]


# ============================================================
# Reassignment (ADR-020 D4)
# ============================================================


class TestReassignment:
    async def test_the_creator_hands_the_case_to_a_teammate(self, world):
        before = await _case(world.repository)
        version = before.version

        case = await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)

        stored = await world.repository.get(CASE_ID)
        assert stored.driver_id == DRIVER == case.driver_id
        assert stored.version == version + 1
        (change,) = world.repository.driver_changes
        assert change.reason is CaseDriverChangeReason.REASSIGNED
        assert (change.from_driver_id, change.to_driver_id) == (CREATOR, DRIVER)
        assert change.actor_user_id == CREATOR
        assert change.enterprise_id == ENTERPRISE

    async def test_naming_the_creator_stores_null(self, world):
        await _case(world.repository, driver_id=DRIVER)

        await world.service.reassign_driver(CASE_ID, DRIVER, CREATOR)

        assert (await world.repository.get(CASE_ID)).driver_id is None
        assert world.repository.driver_changes[-1].to_driver_id == CREATOR

    async def test_the_driver_may_hand_it_on(self, world):
        await _case(world.repository, driver_id=DRIVER)

        await world.service.reassign_driver(CASE_ID, DRIVER, TEAMMATE)

        assert (await world.repository.get(CASE_ID)).driver_id == TEAMMATE

    async def test_the_creator_may_take_it_back(self, world):
        await _case(world.repository, driver_id=DRIVER)

        await world.service.reassign_driver(CASE_ID, CREATOR, CREATOR)

        assert (await world.repository.get(CASE_ID)).driver_id is None

    async def test_naming_the_current_driver_writes_nothing(self, world):
        before = await _case(world.repository, driver_id=DRIVER)
        version = before.version

        await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)

        assert (await world.repository.get(CASE_ID)).version == version
        assert world.repository.driver_changes == []

    async def test_another_reader_may_not_take_the_wheel(self, world):
        """No open takeover (ADR-020, rejected alternatives)."""
        await _case(world.repository, driver_id=DRIVER)

        with pytest.raises(AuthorizationError):
            await world.service.reassign_driver(CASE_ID, TEAMMATE, TEAMMATE)
        assert (await world.repository.get(CASE_ID)).driver_id == DRIVER

    async def test_a_non_reader_gets_the_absent_case_answer(self, world):
        await _case(world.repository)

        with pytest.raises(NotFoundError):
            await world.service.reassign_driver(CASE_ID, OUTSIDER, OUTSIDER)

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(OUTSIDER, id="not-in-a-shared-team"),
            pytest.param(STRANGER, id="another-enterprise"),
            pytest.param(SERVICE, id="service-account"),
            pytest.param(IDLE, id="deactivated"),
            pytest.param("u_nobody", id="no-such-account"),
        ],
    )
    async def test_a_non_candidate_is_refused(self, world, target):
        if target == STRANGER:
            world.teams.members["t1"].add(STRANGER)
        await _case(world.repository)

        with pytest.raises(ValidationException, match="candidate"):
            await world.service.reassign_driver(CASE_ID, CREATOR, target)
        assert (await world.repository.get(CASE_ID)).driver_id is None
        assert world.repository.driver_changes == []

    async def test_a_terminal_case_keeps_its_driver(self, world):
        await _case(world.repository, state=CaseState.CLOSED)

        with pytest.raises(ConflictError) as refused:
            await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)
        assert refused.value.error_code == CASE_TERMINAL

    async def test_standalone_refuses_anyone_but_the_creator_with_422(self):
        repository = InMemoryCaseRepository()
        service = CaseService(repository, account_reader=_Accounts())
        await _case(repository)

        with pytest.raises(ValidationException):
            await service.reassign_driver(CASE_ID, CREATOR, DRIVER)

    async def test_the_bump_makes_an_in_flight_turn_conflict(self, world):
        """A reassignment during a turn is not refused; the turn's save is."""
        await _case(world.repository)
        in_flight = await world.repository.get(CASE_ID)

        await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)

        in_flight.title = "written by the turn"
        with pytest.raises(StaleCaseException):
            await world.repository.save(in_flight)
        assert (await world.repository.get(CASE_ID)).driver_id == DRIVER

    async def test_a_lost_race_rereads_and_decides_again(self, world):
        await _case(world.repository)
        real = world.repository.reassign_driver
        attempts = []

        async def lose_once(case_id, **kw):
            attempts.append(kw["expected_version"])
            if len(attempts) == 1:
                return None
            return await real(case_id, **kw)

        world.repository.reassign_driver = lose_once

        await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)

        assert len(attempts) == 2
        assert (await world.repository.get(CASE_ID)).driver_id == DRIVER

    async def test_a_race_lost_every_time_is_a_labelled_409(self, world):
        await _case(world.repository)

        async def always_lose(case_id, **kw):
            return None

        world.repository.reassign_driver = always_lose

        with pytest.raises(ConflictError) as refused:
            await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)
        assert refused.value.error_code == "CASE_VERSION_CONFLICT"

    async def test_the_driver_never_rides_the_metadata_channel(self, world):
        """A metadata-only edit takes the unversioned channel; it carries no
        driver, so it cannot undo a reassignment (risk 1)."""
        await _case(world.repository, driver_id=DRIVER)
        seen = {}
        real = world.repository.update_metadata_fields

        async def spy(case_id, **fields):
            seen.update(fields)
            return await real(case_id, **fields)

        world.repository.update_metadata_fields = spy

        await world.service.update_case(CASE_ID, {"title": "t"}, DRIVER)

        assert "driver_id" not in seen


# ============================================================
# Releases (ADR-020 D3)
# ============================================================


class TestReleaseOnUnshare:
    async def test_the_last_read_path_releases_first(self, world):
        await _case(world.repository, driver_id=TEAMMATE)
        real_release = world.repository.release_driver

        async def logged_release(case_id, **kw):
            world.log.append("release")
            return await real_release(case_id, **kw)

        world.repository.release_driver = logged_release

        assert await world.service.unshare_case_from_team(CASE_ID, "t1", CREATOR)

        assert world.log == ["release", "unshare:t1"]
        assert (await world.repository.get(CASE_ID)).driver_id is None
        (change,) = world.repository.driver_changes
        assert change.reason is CaseDriverChangeReason.UNSHARED
        assert (change.from_driver_id, change.to_driver_id) == (TEAMMATE, CREATOR)
        assert change.actor_user_id == CREATOR

    async def test_a_driver_who_reads_through_another_share_keeps_it(self, world):
        world.shares.shares[CASE_ID] = {"t1", "t2"}
        await _case(world.repository, driver_id=DRIVER)

        await world.service.unshare_case_from_team(CASE_ID, "t1", CREATOR)

        assert (await world.repository.get(CASE_ID)).driver_id == DRIVER
        assert world.repository.driver_changes == []

    async def test_a_creator_driven_case_has_nothing_to_release(self, world):
        await _case(world.repository)

        await world.service.unshare_case_from_team(CASE_ID, "t1", CREATOR)

        assert world.repository.driver_changes == []

    async def test_release_first_residual_a_failed_unshare_still_handed_back(
        self, world
    ):
        """The safe residual (ADR-020 D3): the release committed before the
        share write that then failed; the case is back with its creator,
        audited, and can be reassigned."""
        await _case(world.repository, driver_id=TEAMMATE)

        async def broken_unshare(**kw):
            raise RuntimeError("share store down")

        world.shares.unshare = broken_unshare

        with pytest.raises(RuntimeError):
            await world.service.unshare_case_from_team(CASE_ID, "t1", CREATOR)
        assert (await world.repository.get(CASE_ID)).driver_id is None
        assert (
            world.repository.driver_changes[0].reason is CaseDriverChangeReason.UNSHARED
        )


class TestReleaseOnTeamLeave:
    async def test_only_cases_readable_through_the_left_team_go_back(self, world):
        world.shares.shares.update(
            {
                "case_0000000000d2": {"t1", "t2"},
                "case_0000000000d3": {"t2"},
            }
        )
        await _case(world.repository, driver_id=DRIVER)
        await _case(world.repository, driver_id=DRIVER, case_id="case_0000000000d2")
        await _case(world.repository, driver_id=DRIVER, case_id="case_0000000000d3")

        released = await world.service.release_driver_before_team_leave(
            enterprise_id=ENTERPRISE, team_id="t1", user_id=DRIVER
        )

        assert released == 1
        assert (await world.repository.get(CASE_ID)).driver_id is None
        assert (await world.repository.get("case_0000000000d2")).driver_id == DRIVER
        assert (await world.repository.get("case_0000000000d3")).driver_id == DRIVER
        (change,) = world.repository.driver_changes
        assert change.reason is CaseDriverChangeReason.LEFT_TEAM
        assert change.actor_user_id == DRIVER

    async def test_another_enterprises_case_is_not_touched(self, world):
        await _case(world.repository, driver_id=DRIVER, enterprise_id=OTHER_ENTERPRISE)

        assert (
            await world.service.release_driver_before_team_leave(
                enterprise_id=ENTERPRISE, team_id="t1", user_id=DRIVER
            )
            == 0
        )


class TestReleaseOnDeactivation:
    async def test_every_driven_case_goes_back(self, world):
        world.shares.shares["case_0000000000d2"] = {"t2"}
        await _case(world.repository, driver_id=DRIVER)
        await _case(world.repository, driver_id=DRIVER, case_id="case_0000000000d2")
        await _case(world.repository, case_id="case_0000000000d3")

        released = await world.service.release_driver_before_deactivation(
            user_id=DRIVER, actor_user_id="u_admin"
        )

        assert released == 2
        assert all(
            c.reason is CaseDriverChangeReason.DEACTIVATED
            and c.actor_user_id == "u_admin"
            for c in world.repository.driver_changes
        )
        assert (await world.repository.get(CASE_ID)).driver_id is None


# ============================================================
# The list filter (ADR-020 D8) and the names (D5)
# ============================================================


class TestAccessWrite:
    async def test_write_lists_what_the_caller_drives_with_a_matching_total(
        self, world
    ):
        world.shares.shares["case_0000000000d2"] = {"t1"}
        await _case(world.repository, driver_id=DRIVER)  # read only for CREATOR
        await _case(world.repository, case_id="case_0000000000d2")  # CREATOR drives

        read, read_total = await world.service.list_user_cases(CREATOR)
        write, write_total = await world.service.list_user_cases(
            CREATOR, CaseListFilter(access=CaseAccess.WRITE)
        )

        assert {s.case_id for s in read} == {CASE_ID, "case_0000000000d2"}
        assert read_total == 2
        assert [s.case_id for s in write] == ["case_0000000000d2"]
        assert write_total == 1

    async def test_the_driver_sees_the_handed_case_on_its_write_list(self, world):
        await _case(world.repository, driver_id=DRIVER)

        write, total = await world.service.list_user_cases(
            DRIVER, CaseListFilter(access=CaseAccess.WRITE)
        )

        assert [s.case_id for s in write] == [CASE_ID] and total == 1
        assert write[0].driver_id == DRIVER
        assert write[0].user_id == CREATOR

    async def test_search_narrows_the_same_way(self, world):
        await _case(world.repository, driver_id=DRIVER)

        assert await world.service.search_cases(
            CaseSearchRequest(query="Checkout"), CREATOR
        )
        assert (
            await world.service.search_cases(
                CaseSearchRequest(query="Checkout", access=CaseAccess.WRITE), CREATOR
            )
            == []
        )

    async def test_rows_carry_both_names_and_the_effective_driver(self, world):
        await _case(world.repository)

        (row,), _ = await world.service.list_user_cases(CREATOR)

        assert row.driver_id == CREATOR
        assert row.creator_display_name == f"Name of {CREATOR}"
        assert row.driver_display_name == f"Name of {CREATOR}"

        await world.service.reassign_driver(CASE_ID, CREATOR, DRIVER)
        (row,), _ = await world.service.list_user_cases(CREATOR)
        assert row.driver_id == DRIVER
        assert row.driver_display_name == f"Name of {DRIVER}"

    async def test_a_failed_name_read_leaves_names_null(self, world):
        await _case(world.repository)

        async def broken(enterprise_id, user_ids):
            raise RuntimeError("account store down")

        world.service.account_reader.get_many_in_enterprise = broken

        (row,), total = await world.service.list_user_cases(CREATOR)

        assert total == 1
        assert row.creator_display_name is None and row.driver_display_name is None


# ============================================================
# Investigation state the driver carries through a full save
# ============================================================


async def test_a_full_save_carries_the_stored_driver(world):
    """A turn's save writes every column back; it must carry ``driver_id``."""
    await _case(world.repository, driver_id=DRIVER)
    case = await world.repository.get(CASE_ID)
    case.inquiry = InquiryData(proposed_problem_statement="p")

    await world.repository.save(case)

    assert (await world.repository.get(CASE_ID)).driver_id == DRIVER


def test_effective_driver_falls_back_to_the_creator():
    case = Case(enterprise_id=ENTERPRISE, user_id=CREATOR, title="t")
    assert case.effective_driver_id == CREATOR
    case.driver_id = DRIVER
    assert case.effective_driver_id == DRIVER
