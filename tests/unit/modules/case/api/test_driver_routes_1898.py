"""The driver routes and the ``access`` filter over HTTP (ADR-020 D4, D8).

Driven through the real case router and the app's own exception handlers, with
the real ``CaseService`` over the in-memory repository behind them — the
refusal SHAPES are the contract here: 404 for a caller who cannot read the
case, 403 for a reader who neither created nor drives it, 422 for a target who
is not a candidate, 409 ``CASE_TERMINAL`` for a terminal case.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.unit.modules.case.test_case_driver_1898 import (
    CASE_ID,
    CREATOR,
    DRIVER,
    OUTSIDER,
    TEAMMATE,
    _Accounts,
    _case,
    _Shares,
    _Teams,
    _tenant,  # noqa: F401 - autouse fixture: binds the enterprise
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

DRIVER_PATH = f"/api/v1/cases/{CASE_ID}/driver"
CANDIDATES_PATH = f"/api/v1/cases/{CASE_ID}/driver-candidates"


@pytest.fixture
def stack(build_app):
    repository = InMemoryCaseRepository()
    service = CaseService(
        repository,
        team_service=_Teams({"t1": {CREATOR, DRIVER, TEAMMATE}}),
        share_repository=_Shares({CASE_ID: {"t1"}}, []),
        account_reader=_Accounts(),
    )
    app = build_app(case_service=service)

    def as_user(user_id):
        async def _user():
            return SimpleNamespace(user_id=user_id, enterprise_id="ent_driver_0001")

        app.dependency_overrides[require_authentication] = _user
        return app

    return SimpleNamespace(repository=repository, service=service, as_user=as_user)


class TestCandidates:
    async def test_the_creator_reads_them(self, stack, call_api):
        await _case(stack.repository)

        response = await call_api(stack.as_user(CREATOR), "GET", CANDIDATES_PATH)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "candidates": [
                {"user_id": CREATOR, "display_name": f"Name of {CREATOR}"},
                {"user_id": DRIVER, "display_name": f"Name of {DRIVER}"},
                {"user_id": TEAMMATE, "display_name": f"Name of {TEAMMATE}"},
            ]
        }
        assert "@" not in response.text

    @pytest.mark.parametrize(
        "caller, status",
        [(TEAMMATE, 403), (OUTSIDER, 404)],
        ids=["reader", "non-reader"],
    )
    async def test_everyone_else_is_refused(self, stack, call_api, caller, status):
        await _case(stack.repository)

        response = await call_api(stack.as_user(caller), "GET", CANDIDATES_PATH)

        assert response.status_code == status, response.text


class TestReassign:
    async def test_the_creator_hands_the_case_on(self, stack, call_api):
        await _case(stack.repository)

        response = await call_api(
            stack.as_user(CREATOR), "PUT", DRIVER_PATH, json={"driver_id": DRIVER}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["user_id"], body["driver_id"]) == (CREATOR, DRIVER)
        assert body["driver_display_name"] == f"Name of {DRIVER}"
        assert body["creator_display_name"] == f"Name of {CREATOR}"
        assert body["shared_team_ids"] == ["t1"]
        assert (await stack.repository.get(CASE_ID)).driver_id == DRIVER

    @pytest.mark.parametrize(
        "caller, target, status",
        [
            pytest.param(TEAMMATE, TEAMMATE, 403, id="reader-takes-the-wheel"),
            pytest.param(OUTSIDER, OUTSIDER, 404, id="non-reader"),
            pytest.param(CREATOR, OUTSIDER, 422, id="not-a-candidate"),
        ],
    )
    async def test_refusals(self, stack, call_api, caller, target, status):
        await _case(stack.repository)

        response = await call_api(
            stack.as_user(caller), "PUT", DRIVER_PATH, json={"driver_id": target}
        )

        assert response.status_code == status, response.text
        assert (await stack.repository.get(CASE_ID)).driver_id is None

    async def test_a_terminal_case_may_change_driver(self, stack, call_api):
        await _case(stack.repository, state=CaseState.CLOSED)

        response = await call_api(
            stack.as_user(CREATOR), "PUT", DRIVER_PATH, json={"driver_id": DRIVER}
        )

        assert response.status_code == 200, response.text
        assert response.json()["driver_id"] == DRIVER

    async def test_an_empty_target_is_a_validation_error(self, stack, call_api):
        await _case(stack.repository)

        response = await call_api(
            stack.as_user(CREATOR), "PUT", DRIVER_PATH, json={"driver_id": ""}
        )

        assert response.status_code == 422


class TestAccessFilter:
    async def test_write_lists_only_what_the_caller_drives(self, stack, call_api):
        await _case(stack.repository, driver_id=DRIVER)
        await _case(stack.repository, case_id="case_0000000000d2")

        read = await call_api(stack.as_user(CREATOR), "GET", "/api/v1/cases")
        write = await call_api(
            stack.as_user(CREATOR), "GET", "/api/v1/cases?access=write"
        )

        assert read.status_code == write.status_code == 200
        assert {c["case_id"] for c in read.json()["cases"]} == {
            CASE_ID,
            "case_0000000000d2",
        }
        assert [c["case_id"] for c in write.json()["cases"]] == ["case_0000000000d2"]
        assert write.json()["total_count"] == 1

    async def test_an_unknown_access_value_is_refused(self, stack, call_api):
        response = await call_api(
            stack.as_user(CREATOR), "GET", "/api/v1/cases?access=drive"
        )

        assert response.status_code == 422


class TestReclassifyRouteForwardsTheReadFact:
    """The PATCH route tells the service whether the caller READS the case,
    the one fact the service cannot resolve for itself (ADR-020 D2)."""

    @pytest.fixture
    def recording(self, stack, monkeypatch):
        from faultmaven.config.settings import get_settings

        monkeypatch.setattr(get_settings().preprocessing, "reclassify_enabled", True)
        investigation = SimpleNamespace(
            reclassify_evidence=AsyncMock(
                return_value=SimpleNamespace(
                    evidence_id="ev_aaaaaaaaaaaa",
                    source_type="logs",
                    summary="",
                    metadata={},
                )
            )
        )

        async def _investigation():
            return investigation

        stack.as_user(CREATOR).dependency_overrides[
            get_investigation_service
        ] = _investigation
        return investigation

    @pytest.mark.parametrize(
        "caller, reads",
        [(DRIVER, True), (OUTSIDER, False)],
        ids=["reader", "non-reader"],
    )
    async def test_it_forwards_whether_the_caller_reads(
        self, stack, call_api, recording, caller, reads
    ):
        await _case(stack.repository, driver_id=DRIVER)

        await call_api(
            stack.as_user(caller),
            "PATCH",
            f"/api/v1/cases/{CASE_ID}/evidence/ev_aaaaaaaaaaaa/classification",
            json={"data_type": "logs_and_errors"},
        )

        kwargs = recording.reclassify_evidence.await_args.kwargs
        assert kwargs["caller_reads_case"] is reads
        assert kwargs["user_id"] == caller
