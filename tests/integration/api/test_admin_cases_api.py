"""Integration tests for the admin cross-tenant case listing endpoint (ADR-012 D9).

Covers:
- GET /api/v1/admin/cases (200) — admin sees cases across multiple users/orgs
- GET /api/v1/admin/cases (403) — non-admin is rejected
- GET /api/v1/admin/cases (403) — blocked in cloud deployment (break-glass deferred)
- Query params (state/limit/offset) are forwarded to the service filter
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from faultmaven.main import app as main_app
from faultmaven.models.api_models import CaseSummary
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import CaseState

pytestmark = pytest.mark.integration


def _summary(
    case_id: str, user_id: str, org_id: str, title: str = "Case"
) -> CaseSummary:
    now = datetime.now(timezone.utc)
    return CaseSummary(
        case_id=case_id,
        title=title,
        state=CaseState.INVESTIGATING,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        user_id=user_id,
        organization_id=org_id,
        current_turn=1,
        milestones_completed=1,
        total_milestones=8,
        is_terminal=False,
        description="desc",
        resolved_at=None,
        closed_at=None,
        closure_reason=None,
    )


@pytest.fixture
def admin_user():
    return AuthenticatedUser(
        user_id="admin_1",
        organization_id="org_1",
        email="admin@example.com",
        roles=["admin"],
        permissions=["admin:all"],
    )


@pytest.fixture
def member_user():
    return AuthenticatedUser(
        user_id="member_1",
        organization_id="org_1",
        email="member@example.com",
        roles=["member"],
        permissions=["cases:read"],
    )


@pytest.fixture
def mock_case_service():
    service = AsyncMock()
    return service


def _make_app(current_user, mock_case_service):
    """Wire dependency overrides for require_admin's user + the case service."""
    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.api.routes.admin_cases import get_case_service

    async def _get_user():
        return current_user

    async def _get_service():
        return mock_case_service

    main_app.dependency_overrides[get_current_user] = _get_user
    main_app.dependency_overrides[get_case_service] = _get_service
    return main_app


@pytest.fixture
def cleanup_overrides():
    yield
    main_app.dependency_overrides.clear()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_admin_list_all_cases_success(
    admin_user, mock_case_service, cleanup_overrides
):
    """Admin sees cases from multiple users in one response (standalone)."""
    summaries = [
        _summary("case_a", "copilot_user", "org_1", "Copilot case"),
        _summary("case_b", "slack-agent", "org_1", "Slack case"),
    ]
    mock_case_service.list_all_cases.return_value = (summaries, 2)
    app = _make_app(admin_user, mock_case_service)

    async with await _client(app) as client:
        resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["total_count"] == 2
    owners = {c["user_id"] for c in body["cases"]}
    assert owners == {"copilot_user", "slack-agent"}
    assert body["has_more"] is False


async def test_admin_list_all_cases_forbidden_for_non_admin(
    member_user, mock_case_service, cleanup_overrides
):
    """A non-admin user is rejected by require_admin."""
    app = _make_app(member_user, mock_case_service)

    async with await _client(app) as client:
        resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    mock_case_service.list_all_cases.assert_not_called()


async def test_admin_list_all_cases_blocked_in_cloud(
    admin_user, mock_case_service, cleanup_overrides
):
    """Cloud deployment fails closed until audited break-glass exists."""
    app = _make_app(admin_user, mock_case_service)
    fake_settings = MagicMock(is_cloud=True, deployment_mode="cloud")

    with patch(
        "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "cloud" in resp.json()["detail"].lower()
    mock_case_service.list_all_cases.assert_not_called()


async def test_admin_list_all_cases_forwards_filters(
    admin_user, mock_case_service, cleanup_overrides
):
    """state/limit/offset query params reach the service filter."""
    mock_case_service.list_all_cases.return_value = ([], 0)
    app = _make_app(admin_user, mock_case_service)

    async with await _client(app) as client:
        resp = await client.get(
            "/api/v1/admin/cases",
            params={"state": "investigating", "limit": 10, "offset": 20},
        )

    assert resp.status_code == status.HTTP_200_OK
    mock_case_service.list_all_cases.assert_awaited_once()
    (filters,) = mock_case_service.list_all_cases.await_args.args
    assert filters.limit == 10
    assert filters.offset == 20
    assert filters.state == CaseState.INVESTIGATING
