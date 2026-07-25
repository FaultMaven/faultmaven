"""Operator access audit — route behaviour (ADR-012 D8/D9, #813).

The properties under test are governance properties, not plumbing:

1. The access is recorded BEFORE the data is served.
2. A failed audit write REFUSES the request rather than serving it unaudited.
3. A cross-tenant list records a NULL target organization (it spans all
   tenants), not the operator's own org.
4. Reading the trail is not itself recorded as an access.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin_cases import (
    get_case_service,
    get_operator_audit_repository,
    router,
)
from faultmaven.models.interfaces_operator_audit import (
    OperatorAccessAudit,
    OperatorAction,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        organization_id="org-operator-own",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


@pytest.fixture
def audit_repo():
    repo = AsyncMock()
    repo.record_access = AsyncMock(return_value=True)
    repo.list_access = AsyncMock(return_value=([], 0))
    return repo


@pytest.fixture
def case_service():
    service = AsyncMock()
    service.list_all_cases = AsyncMock(return_value=([], 0))
    return service


@pytest.fixture
def client(audit_repo, case_service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_operator_audit_repository] = lambda: audit_repo
    app.dependency_overrides[get_case_service] = lambda: case_service
    return TestClient(app)


@pytest.mark.unit
@pytest.mark.security
class TestListAccessIsRecorded:
    def test_records_the_access(self, client, audit_repo):
        resp = client.get("/api/v1/admin/cases")
        assert resp.status_code == 200
        audit_repo.record_access.assert_awaited_once()
        assert (
            audit_repo.record_access.await_args.kwargs["action"] is OperatorAction.LIST
        )

    def test_records_the_operator_identity(self, client, audit_repo):
        client.get("/api/v1/admin/cases")
        kwargs = audit_repo.record_access.await_args.kwargs
        assert kwargs["operator_user_id"] == "op-1"
        assert kwargs["operator_username"] == "operator@example.com"

    def test_cross_tenant_list_records_no_target_org(self, client, audit_repo):
        """A list spanning every tenant must not be stamped with ONE org.

        Stamping the operator's own organization would make the trail read as
        though the access were scoped to it — the opposite of what happened.
        """
        client.get("/api/v1/admin/cases")
        assert (
            audit_repo.record_access.await_args.kwargs["target_organization_id"] is None
        )

    def test_records_the_deployment_mode_as_its_value(self, client, audit_repo):
        """The stored value must be "standalone", not "DeploymentMode.STANDALONE".

        `DeploymentMode` is a str-Enum whose `str()` is the member repr, so
        `str(mode)` silently writes the wrong text. The rows are append-only,
        which makes a wrong value here uncorrectable: no later fix can repair
        rows already written, leaving the system of record permanently mixed.
        """
        client.get("/api/v1/admin/cases")
        recorded = audit_repo.record_access.await_args.kwargs["deployment_mode"]
        assert recorded in ("standalone", "cloud"), recorded
        assert "DeploymentMode" not in recorded

    def test_deployment_mode_is_unwrapped_when_settings_hold_the_enum(
        self, client, audit_repo, monkeypatch
    ):
        """`settings.deployment_mode` is a plain str on some paths, the enum on
        others. The str path alone would pass even with a bare `str()`, so pin
        the enum path explicitly — that is where the wrong text comes from.
        """
        from faultmaven.api.routes import admin_cases
        from faultmaven.config.settings import DeploymentMode

        real_settings = admin_cases.get_settings()

        class _EnumModeSettings:
            deployment_mode = DeploymentMode.STANDALONE
            is_cloud = False

            def __getattr__(self, name):
                return getattr(real_settings, name)

        monkeypatch.setattr(admin_cases, "get_settings", _EnumModeSettings)

        client.get("/api/v1/admin/cases")

        assert (
            audit_repo.record_access.await_args.kwargs["deployment_mode"]
            == "standalone"
        )

    def test_records_the_filters_applied(self, client, audit_repo):
        client.get("/api/v1/admin/cases?limit=10&offset=20")
        details = audit_repo.record_access.await_args.kwargs["details"]
        assert details["limit"] == 10
        assert details["offset"] == 20

    def test_audit_is_written_before_cases_are_read(
        self, client, audit_repo, case_service
    ):
        """Ordering matters: a crash mid-request must leave evidence, not silence."""
        order = []
        audit_repo.record_access = AsyncMock(
            side_effect=lambda **_: order.append("audit") or True
        )
        case_service.list_all_cases = AsyncMock(
            side_effect=lambda *_: order.append("cases") or ([], 0)
        )
        client.get("/api/v1/admin/cases")
        assert order == ["audit", "cases"]


@pytest.mark.unit
@pytest.mark.security
class TestFailsClosed:
    def test_audit_write_failure_refuses_the_request(
        self, client, audit_repo, case_service
    ):
        """No audit row => no data. Serving it unaudited would void the control."""
        audit_repo.record_access = AsyncMock(side_effect=RuntimeError("db down"))

        resp = client.get("/api/v1/admin/cases")

        assert resp.status_code == 503
        case_service.list_all_cases.assert_not_awaited()

    def test_missing_audit_repository_refuses_the_request(self, case_service):
        """A deployment without the audit path must not serve operator reads."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_platform_admin] = _operator
        app.dependency_overrides[get_case_service] = lambda: case_service
        # app.state.operator_audit_repository deliberately absent.

        resp = TestClient(app).get("/api/v1/admin/cases")

        assert resp.status_code == 503
        case_service.list_all_cases.assert_not_awaited()


@pytest.mark.unit
class TestAuditQueryPath:
    def test_returns_the_trail(self, client, audit_repo):
        audit_repo.list_access = AsyncMock(
            return_value=(
                [
                    OperatorAccessAudit(
                        audit_id=1,
                        operator_user_id="op-1",
                        operator_username="operator@example.com",
                        action=OperatorAction.LIST,
                        created_at=datetime.now(timezone.utc),
                    )
                ],
                1,
            )
        )

        resp = client.get("/api/v1/admin/audit/operator-access")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["entries"][0]["action"] == "list"
        assert body["entries"][0]["operator_user_id"] == "op-1"

    def test_passes_filters_through(self, client, audit_repo):
        client.get(
            "/api/v1/admin/audit/operator-access?target_case_id=case-9&action=list"
        )
        kwargs = audit_repo.list_access.await_args.kwargs
        assert kwargs["target_case_id"] == "case-9"
        assert kwargs["action"] is OperatorAction.LIST

    def test_reading_the_trail_is_not_itself_recorded(self, client, audit_repo):
        """Otherwise the table grows under its own review without adding evidence."""
        client.get("/api/v1/admin/audit/operator-access")
        audit_repo.record_access.assert_not_awaited()
