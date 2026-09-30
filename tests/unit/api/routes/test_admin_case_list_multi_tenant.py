"""The operator case list under ``TENANT_PROVIDER=multi`` (ADR-012 D9).

Under multi-tenancy row-level security scopes an ordinary case query to the one
enterprise a request is bound to, so the list is served from the
cross-enterprise metadata read instead. The properties pinned here are the ones
that make that safe:

1. The rows come from the metadata read, never from the case service.
2. The access is recorded BEFORE anything is read, a failed record refuses the
   request, and the record says a metadata view was served.
3. Failure direction: a missing reader or a database without the read's
   functions is a 5xx — never a fall back to the RLS-narrowed case list, which
   would answer with one enterprise's cases under a list that claims them all.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin_cases import (
    get_case_metadata_reader,
    get_case_service,
    get_operator_audit_repository,
    router,
)
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.metadata import (
    CaseMetadata,
    CaseMetadataUnavailableError,
)
from faultmaven.modules.case.domain.models.problem import InvestigationStage

pytestmark = [pytest.mark.unit, pytest.mark.security]

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        enterprise_id="ent-operator",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


def _metadata(case_id: str, enterprise_id: str, **overrides) -> CaseMetadata:
    fields = dict(
        case_id=case_id,
        enterprise_id=enterprise_id,
        organization_id=None,
        user_id=f"user-of-{enterprise_id}",
        state=CaseState.INVESTIGATING,
        source="slack",
        closure_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        last_activity_at=_NOW,
        resolved_at=None,
        closed_at=None,
        current_turn=5,
        investigation_turn=4,
        stage=InvestigationStage.TREATMENT,
        turns_without_progress=1,
        is_terminal=False,
        shared_team_ids=["team-1"],
    )
    fields.update(overrides)
    return CaseMetadata(**fields)


@pytest.fixture
def audit_repo():
    repo = AsyncMock()
    repo.record_access = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def case_service():
    """The single-tenant read. Under multi it must never be reached."""
    service = AsyncMock()
    service.list_all_cases = AsyncMock(return_value=([], 0))
    return service


@pytest.fixture
def reader():
    reader = AsyncMock()
    reader.list_case_metadata = AsyncMock(
        return_value=(
            [_metadata("case-a", "ent-a"), _metadata("case-b", "ent-b")],
            7,
        )
    )
    return reader


def _client(audit_repo, case_service, reader) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_operator_audit_repository] = lambda: audit_repo
    app.dependency_overrides[get_case_service] = lambda: case_service
    app.dependency_overrides[get_case_metadata_reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def multi_tenant():
    with patch(
        "faultmaven.api.routes.admin_cases.requested_tenant_provider",
        return_value="multi",
    ):
        yield


class TestServedFromTheCrossEnterpriseRead:
    def test_rows_span_enterprises_and_never_touch_the_case_service(
        self, audit_repo, case_service, reader
    ):
        resp = _client(audit_repo, case_service, reader).get(
            "/api/v1/admin/cases",
            params={"state": "investigating", "source": "slack", "limit": 2},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["view"] == "metadata"
        assert {row["enterprise_id"] for row in body["cases"]} == {"ent-a", "ent-b"}
        # The total counts every match in every enterprise, not the page.
        assert body["total_count"] == 7
        assert body["has_more"] is True
        row = body["cases"][0]
        assert row["stage"] == "treatment"
        assert row["investigation_turn"] == 4
        assert row["shared_team_ids"] == ["team-1"]
        reader.list_case_metadata.assert_awaited_once_with(
            state=CaseState.INVESTIGATING, source="slack", limit=2, offset=0
        )
        case_service.list_all_cases.assert_not_awaited()

    def test_keyed_on_tenancy_not_on_deployment_mode(
        self, audit_repo, case_service, reader, monkeypatch
    ):
        """``multi`` cannot boot outside cloud today, but the hazard is RLS
        scoping, which belongs to tenancy: a multi deployment labelled
        standalone still gets the metadata read, never the full summaries."""
        from faultmaven.api.routes import admin_cases

        class _StandaloneSettings:
            is_cloud = False
            deployment_mode = "standalone"

        monkeypatch.setattr(admin_cases, "get_settings", _StandaloneSettings)

        resp = _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert resp.status_code == 200
        assert resp.json()["view"] == "metadata"
        case_service.list_all_cases.assert_not_awaited()

    def test_a_case_whose_owner_was_deleted_is_dropped_as_on_the_other_path(
        self, audit_repo, case_service, reader
    ):
        """``CaseService.list_all_cases`` drops a case whose summary does not
        validate — today, one whose owner account was deleted. The metadata
        path serves the same rows, and pages by the true total."""
        reader.list_case_metadata = AsyncMock(
            return_value=(
                [
                    _metadata("case-a", "ent-a"),
                    _metadata("case-x", "ent-b", user_id=None),
                ],
                2,
            )
        )

        resp = _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert resp.status_code == 200
        body = resp.json()
        assert [row["case_id"] for row in body["cases"]] == ["case-a"]
        assert body["total_count"] == 2


class TestAuditComesFirst:
    def test_the_access_is_recorded_before_the_read(
        self, audit_repo, case_service, reader
    ):
        order = []
        audit_repo.record_access = AsyncMock(
            side_effect=lambda **_: order.append("audit") or True
        )
        reader.list_case_metadata = AsyncMock(
            side_effect=lambda **_: order.append("read") or ([], 0)
        )

        _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert order == ["audit", "read"]

    def test_the_record_names_the_metadata_view_and_no_single_tenant(
        self, audit_repo, case_service, reader
    ):
        _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        kwargs = audit_repo.record_access.await_args.kwargs
        assert kwargs["action"] is OperatorAction.LIST
        assert kwargs["details"]["view"] == "metadata"
        assert kwargs["target_enterprise_id"] is None

    def test_a_failed_record_refuses_the_request_before_reading(
        self, audit_repo, case_service, reader
    ):
        audit_repo.record_access = AsyncMock(side_effect=RuntimeError("db down"))

        resp = _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert resp.status_code == 503
        reader.list_case_metadata.assert_not_awaited()
        case_service.list_all_cases.assert_not_awaited()


class TestFailsClosed:
    def test_no_reader_is_a_5xx_not_the_rls_scoped_list(self, audit_repo, case_service):
        resp = _client(audit_repo, case_service, None).get("/api/v1/admin/cases")

        assert resp.status_code == 503
        case_service.list_all_cases.assert_not_awaited()
        # Nothing was read, so nothing is recorded as an access.
        audit_repo.record_access.assert_not_awaited()

    def test_a_database_without_the_functions_is_a_5xx(
        self, audit_repo, case_service, reader
    ):
        reader.list_case_metadata = AsyncMock(
            side_effect=CaseMetadataUnavailableError("not migrated")
        )

        resp = _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert resp.status_code == 503
        case_service.list_all_cases.assert_not_awaited()

    def test_any_other_read_failure_is_a_5xx_too(
        self, audit_repo, case_service, reader
    ):
        reader.list_case_metadata = AsyncMock(side_effect=RuntimeError("boom"))

        resp = _client(audit_repo, case_service, reader).get("/api/v1/admin/cases")

        assert resp.status_code >= 500
        case_service.list_all_cases.assert_not_awaited()
