"""Integration tests for the admin cross-tenant case listing endpoint (ADR-012 D9).

Covers:
- GET /api/v1/admin/cases (200) — admin sees cases across multiple users/orgs
- GET /api/v1/admin/cases (403) — non-admin is rejected
- The D9 deployment split: standalone serves full summaries, cloud serves
  ambient metadata with no user free text
- GET /api/v1/admin/cases (403) — refused under multi-tenant cloud, where RLS
  would make an "all tenants" list silently one tenant's
- Query params (state/limit/offset) are forwarded to the service filter
- The durable audit row records which of the two views was served
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from faultmaven.main import app as main_app
from faultmaven.models.api_models import CASE_SUMMARY_CONTENT_FIELDS, CaseSummary
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import CaseState, InvestigationStage

pytestmark = pytest.mark.integration


def _summary(
    case_id: str,
    user_id: str,
    enterprise_id: str,
    title: str = "Case",
    organization_id: str | None = None,
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
        enterprise_id=enterprise_id,
        organization_id=organization_id,
        current_turn=1,
        stage=InvestigationStage.DIAGNOSIS,
        turns_without_progress=0,
        is_terminal=False,
        description="desc",
        resolved_at=None,
        closed_at=None,
        closure_reason=None,
    )


def _cloud_settings(tenant_provider: str = "single"):
    """A settings double for the cloud arm, plus the tenancy it runs under.

    Returns ``(settings, patch_context)``. The tenancy check is a module-level
    function call rather than a settings attribute, so it has to be patched
    separately from ``get_settings``.
    """
    return MagicMock(is_cloud=True, deployment_mode="cloud"), patch(
        "faultmaven.api.routes.admin_cases.requested_tenant_provider",
        return_value=tenant_provider,
    )


@pytest.fixture
def admin_user():
    return AuthenticatedUser(
        user_id="admin_1",
        enterprise_id="org_1",
        email="admin@example.com",
        roles=["admin", "platform_admin"],
        permissions=["admin:all"],
    )


@pytest.fixture
def member_user():
    return AuthenticatedUser(
        user_id="member_1",
        enterprise_id="org_1",
        email="member@example.com",
        roles=["member"],
        permissions=["cases:read"],
    )


@pytest.fixture
def mock_case_service():
    service = AsyncMock()
    return service


@pytest.fixture
def mock_audit_repo():
    """The operator audit trail (ADR-012 D8/D9).

    The route resolves this as a dependency and fails closed without it, so it
    must be wired here even for the cloud-403 case — dependencies resolve
    before the handler body runs.
    """
    return AsyncMock()


def _make_app(current_user, mock_case_service, mock_audit_repo=None):
    """Wire overrides for require_platform_admin's user, the case service, and
    the operator audit repository."""
    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.api.operator_audit import get_operator_audit_repository
    from faultmaven.api.routes.admin_cases import get_case_service

    async def _get_user():
        return current_user

    async def _get_service():
        return mock_case_service

    async def _get_audit_repo():
        return mock_audit_repo if mock_audit_repo is not None else AsyncMock()

    main_app.dependency_overrides[get_current_user] = _get_user
    main_app.dependency_overrides[get_case_service] = _get_service
    main_app.dependency_overrides[get_operator_audit_repository] = _get_audit_repo
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
        _summary("case_a", "copilot_user", "ent_1", "Copilot case"),
        _summary("case_b", "slack-agent", "ent_1", "Slack case"),
    ]
    mock_case_service.list_all_cases.return_value = (summaries, 2)
    app = _make_app(admin_user, mock_case_service)

    async with await _client(app) as client:
        resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["view"] == "full"
    assert body["total_count"] == 2
    owners = {c["user_id"] for c in body["cases"]}
    assert owners == {"copilot_user", "slack-agent"}
    assert body["has_more"] is False
    # Standalone is unchanged by the D9 split: the operator still sees titles.
    assert {c["title"] for c in body["cases"]} == {"Copilot case", "Slack case"}


async def test_admin_list_all_cases_records_the_access(
    admin_user, mock_case_service, mock_audit_repo, cleanup_overrides
):
    """The cross-tenant read leaves a durable audit row (ADR-012 D8/D9).

    Asserted end-to-end through the real app rather than only at the unit
    level: this is the whole-stack path an auditor's evidence comes from.
    """
    from faultmaven.models.interfaces_operator_audit import OperatorAction

    mock_case_service.list_all_cases.return_value = ([], 0)
    app = _make_app(admin_user, mock_case_service, mock_audit_repo)

    async with await _client(app) as client:
        resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    mock_audit_repo.record_access.assert_awaited_once()
    kwargs = mock_audit_repo.record_access.await_args.kwargs
    assert kwargs["action"] is OperatorAction.LIST
    assert kwargs["operator_user_id"] == admin_user.user_id
    # A cross-tenant list spans every tenant, so it is stamped with no org.
    assert kwargs["target_enterprise_id"] is None


async def test_admin_list_all_cases_forbidden_for_non_admin(
    member_user, mock_case_service, cleanup_overrides
):
    """A non-admin user is rejected by require_platform_admin."""
    app = _make_app(member_user, mock_case_service)

    async with await _client(app) as client:
        resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    mock_case_service.list_all_cases.assert_not_called()


async def test_admin_list_in_cloud_returns_metadata_rows(
    admin_user, mock_case_service, cleanup_overrides
):
    """Cloud serves the list, projected to ambient metadata (ADR-012 D9)."""
    mock_case_service.list_all_cases.return_value = (
        [
            _summary(
                "case_a",
                "copilot_user",
                "ent_1",
                "Payments DB down at ACME",
                organization_id="org_1",
            )
        ],
        1,
    )
    app = _make_app(admin_user, mock_case_service)
    fake_settings, tenancy = _cloud_settings()

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
        ),
        tenancy,
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["view"] == "metadata"
    assert body["total_count"] == 1

    (row,) = body["cases"]
    # The triage facts an operator needs are all present...
    assert row["case_id"] == "case_a"
    # The ENTERPRISE is the tenant the row was scoped by (ADR-017 D1) and is
    # what "which tenant has cases stuck in INVESTIGATING?" is asking about;
    # the organization rides along as billing attribution and may be absent.
    assert row["enterprise_id"] == "ent_1"
    assert row["organization_id"] == "org_1"
    assert row["state"] == CaseState.INVESTIGATING.value
    assert row["current_turn"] == 1
    # ...and the content fields are absent as *keys*, not present-and-null: a
    # client must not be able to read "withheld" as "this case has no title".
    for field in CASE_SUMMARY_CONTENT_FIELDS:
        assert field not in row


async def test_cloud_response_body_contains_no_user_free_text(
    admin_user, mock_case_service, cleanup_overrides
):
    """Sweep every content field, not one example of one.

    Each declared content field is filled with its own sentinel and the whole
    serialized body — not just the parsed row — is searched for it. Checking the
    raw text is the point: it catches a leak through any route out of the
    handler, including one nested somewhere a field-name assertion would miss.
    A metadata sentinel is planted too, so a body that leaked nothing because it
    served nothing cannot pass.
    """
    summary = _summary("case_a", "copilot_user", "SENTINEL-ENT-METADATA")
    for i, field in enumerate(sorted(CASE_SUMMARY_CONTENT_FIELDS)):
        setattr(summary, field, f"SENTINEL-CONTENT-{i}-{field}")

    mock_case_service.list_all_cases.return_value = ([summary], 1)
    app = _make_app(admin_user, mock_case_service)
    fake_settings, tenancy = _cloud_settings()

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
        ),
        tenancy,
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    assert "SENTINEL-CONTENT" not in resp.text, (
        "A cloud operator list disclosed user free text: " f"{resp.text}"
    )
    # The endpoint really did serve a row — otherwise the assertion above is vacuous.
    assert "SENTINEL-ENT-METADATA" in resp.text


async def test_admin_list_blocked_under_multi_tenant_cloud(
    admin_user, mock_case_service, cleanup_overrides
):
    """Refused where RLS would make the cross-tenant list silently partial.

    Under ``TENANT_PROVIDER=multi`` the web process's RLS-enforcing DB role
    scopes the query to the operator's own organization, so a 200 here would
    claim to span every tenant while showing one. Fail closed until the bounded
    cross-tenant read lands with break-glass (#815).
    """
    app = _make_app(admin_user, mock_case_service)
    fake_settings, tenancy = _cloud_settings(tenant_provider="multi")

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
        ),
        tenancy,
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "partial" in resp.json()["detail"].lower()
    mock_case_service.list_all_cases.assert_not_called()


async def test_multi_tenant_refusal_is_keyed_on_tenancy_not_deployment_mode(
    admin_user, mock_case_service, cleanup_overrides
):
    """The refusal follows ``multi``, not ``cloud``.

    ``multi`` cannot boot outside cloud today (``create_tenant_provider``
    refuses), so the two conditions coincide — but the hazard is RLS scoping,
    which belongs to tenancy. Pinning it here means a future change that lets
    ``multi`` run elsewhere inherits the refusal instead of silently serving a
    one-tenant list under a "standalone" label.
    """
    app = _make_app(admin_user, mock_case_service)
    standalone_settings = MagicMock(is_cloud=False, deployment_mode="standalone")

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings",
            return_value=standalone_settings,
        ),
        patch(
            "faultmaven.api.routes.admin_cases.requested_tenant_provider",
            return_value="multi",
        ),
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    mock_case_service.list_all_cases.assert_not_called()


async def test_multi_tenant_refusal_records_no_access(
    admin_user, mock_case_service, mock_audit_repo, cleanup_overrides
):
    """A refused request read nothing, so it is not an access.

    The audit table is the record of operator reads of tenant data; stamping a
    row for a request that was denied before any query would dilute exactly the
    evidence it exists to hold.
    """
    app = _make_app(admin_user, mock_case_service, mock_audit_repo)
    fake_settings, tenancy = _cloud_settings(tenant_provider="multi")

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
        ),
        tenancy,
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    mock_audit_repo.record_access.assert_not_awaited()


@pytest.mark.parametrize(
    "is_cloud,expected_view",
    [(False, "full"), (True, "metadata")],
)
async def test_audit_row_records_which_view_was_served(
    is_cloud,
    expected_view,
    admin_user,
    mock_case_service,
    mock_audit_repo,
    cleanup_overrides,
):
    """The trail must say whether titles were disclosed.

    ``action=LIST`` alone no longer answers that question now the endpoint has
    two shapes, and an auditor reconstructing an incident cannot recover the
    deployment mode of a past request from anywhere else.
    """
    mock_case_service.list_all_cases.return_value = ([], 0)
    app = _make_app(admin_user, mock_case_service, mock_audit_repo)
    fake_settings = MagicMock(
        is_cloud=is_cloud, deployment_mode="cloud" if is_cloud else "standalone"
    )

    with (
        patch(
            "faultmaven.api.routes.admin_cases.get_settings", return_value=fake_settings
        ),
        patch(
            "faultmaven.api.routes.admin_cases.requested_tenant_provider",
            return_value="single",
        ),
    ):
        async with await _client(app) as client:
            resp = await client.get("/api/v1/admin/cases")

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["view"] == expected_view
    kwargs = mock_audit_repo.record_access.await_args.kwargs
    assert kwargs["details"]["view"] == expected_view


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
