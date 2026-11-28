"""``CaseService.get_case``: ``None`` means absent or refused, and nothing else (#2996).

It caught every exception from the case read and the access decision and
answered ``None``, the same answer as "no such case", so the operator's
break-glass read said 404 during an outage and the session's case lookup
created a new case. A failure now raises, and never yields the case: the access
decision runs strict, so a failed team or share lookup is not an empty arm.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import DatabaseTimeout, ServiceException
from faultmaven.modules.case.domain.services.case_service import CaseService
from tests.case_builders import make_case

OWNER = "user_owner"
TEAMMATE = "user_teammate"


def _case():
    return make_case(user_id=OWNER, enterprise_id="ent_2996")


def _service(repo_get, *, teams=None, shared=None, session_store=None):
    """A service over a repository whose ``get`` is ``repo_get``; ``teams`` and
    ``shared`` are the team and share lookups (a value, or an exception)."""
    team_service = share_repo = None
    if teams is not None:
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(
            side_effect=teams if isinstance(teams, Exception) else None,
            return_value=teams,
        )
    if shared is not None:
        share_repo = AsyncMock()
        share_repo.list_resource_ids = AsyncMock(
            side_effect=shared if isinstance(shared, Exception) else None,
            return_value=shared,
        )
    return CaseService(
        case_repository=AsyncMock(get=repo_get),
        session_store=session_store,
        team_service=team_service,
        share_repository=share_repo,
    )


@pytest.mark.unit
class TestAFailureRaises:
    async def test_a_failed_case_read_raises(self):
        """It answered ``None``, which every caller reads as "no such case"."""
        svc = _service(AsyncMock(side_effect=DatabaseTimeout()))

        with pytest.raises(DatabaseTimeout):
            await svc.get_case("case_a1b2c3d4e5f6", user_id=OWNER)

    @pytest.mark.parametrize(
        "lookup",
        [
            pytest.param({"teams": DatabaseTimeout(), "shared": []}, id="teams"),
            pytest.param(
                {"teams": ["team_1"], "shared": DatabaseTimeout()}, id="shares"
            ),
        ],
    )
    @pytest.mark.parametrize("driver_only", [False, True], ids=["read", "driver"])
    async def test_a_failed_access_lookup_raises(self, lookup, driver_only):
        """The non-strict resolver collapsed a failed lookup to owner-only, so a
        teammate the case is shared with was told it does not exist."""
        case = _case()
        case.driver_id = TEAMMATE
        svc = _service(AsyncMock(return_value=case), **lookup)

        with pytest.raises(DatabaseTimeout):
            await svc.get_case(case.case_id, TEAMMATE, driver_only=driver_only)

    async def test_a_raising_access_decision_never_yields_the_case(self, monkeypatch):
        case = _case()
        svc = _service(AsyncMock(return_value=case))
        monkeypatch.setattr(
            svc, "_may_access", AsyncMock(side_effect=RuntimeError("resolver broke"))
        )

        with pytest.raises(RuntimeError):
            await svc.get_case(case.case_id, OWNER)


@pytest.mark.unit
class TestNoneStillMeansAbsentOrRefused:
    async def test_a_missing_case_is_none(self):
        assert await _service(AsyncMock(return_value=None)).get_case("case_x") is None

    async def test_an_empty_id_is_none_without_a_read(self):
        get = AsyncMock()
        assert await _service(get).get_case("  ") is None
        get.assert_not_awaited()

    @pytest.mark.parametrize(
        "flags",
        [{}, {"driver_only": True}, {"creator_only": True}],
        ids=["read", "driver", "creator"],
    )
    async def test_a_refused_caller_is_none(self, flags):
        svc = _service(AsyncMock(return_value=_case()), teams=["team_1"], shared=[])

        assert await svc.get_case("case_x", "user_stranger", **flags) is None

    async def test_a_shared_teammate_reads_but_is_refused_the_creators_writes(self):
        case = _case()
        svc = _service(
            AsyncMock(return_value=case), teams=["team_1"], shared=[case.case_id]
        )

        assert await svc.get_case(case.case_id, TEAMMATE) is case
        assert await svc.get_case(case.case_id, TEAMMATE, creator_only=True) is None
        assert await svc.get_case(case.case_id, OWNER, driver_only=True) is case


@pytest.mark.unit
class TestTheSessionLookupCreatesNoCaseOnAFailedRead:
    """``get_or_create_case_for_session``'s inner ``except`` covered the case
    read too, so a failed read of the session's case created a new one."""

    @staticmethod
    def _pointer_store(get):
        store = AsyncMock()
        store.get = get
        return store

    async def test_a_failed_case_read_raises_and_creates_nothing(self):
        svc = _service(
            AsyncMock(side_effect=DatabaseTimeout()),
            session_store=self._pointer_store(
                AsyncMock(return_value="case_a1b2c3d4e5f6")
            ),
        )
        svc.create_case = AsyncMock()

        with pytest.raises(ServiceException) as raised:
            await svc.get_or_create_case_for_session("sess_1", user_id=OWNER)

        # Still the database's failure on the chain, which the handler answers
        # 503 (``service_exception_handler``).
        assert isinstance(raised.value.__cause__, DatabaseTimeout)
        svc.create_case.assert_not_awaited()

    async def test_a_failed_session_store_read_still_creates_a_case(self):
        """The store's own failure is unchanged: no pointer, so a new case."""
        get_case = AsyncMock()
        svc = _service(
            get_case,
            session_store=self._pointer_store(
                AsyncMock(side_effect=ConnectionError("redis down"))
            ),
        )
        svc.create_case = AsyncMock(return_value=_case())

        assert (
            await svc.get_or_create_case_for_session("sess_1", user_id=OWNER)
            == svc.create_case.return_value.case_id
        )
        get_case.assert_not_awaited()

    async def test_an_absent_case_still_creates_a_case(self):
        svc = _service(
            AsyncMock(return_value=None),
            session_store=self._pointer_store(
                AsyncMock(return_value="case_a1b2c3d4e5f6")
            ),
        )
        svc.create_case = AsyncMock(return_value=_case())

        await svc.get_or_create_case_for_session("sess_1", user_id=OWNER)

        svc.create_case.assert_awaited_once()
