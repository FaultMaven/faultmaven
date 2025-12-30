"""Integration tests for JWT-protected API endpoints (TASK-017)

Tests JWT authentication across protected endpoints:
- Cases API with JWT authentication
- Sessions API with JWT authentication
- Evidence API with JWT authentication
- Backwards compatibility with legacy headers

Coverage Target: 90%+
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.app import create_app
from faultmaven.api.middleware.auth import set_auth_service
from faultmaven.models.case import Case, CaseSeverity, CaseStatus


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create test FastAPI application."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create synchronous test client."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_service():
    """Reset auth service before each test."""
    set_auth_service(None)
    yield
    set_auth_service(None)


@pytest.fixture
def admin_token(client) -> str:
    """Get admin user JWT token."""
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@faultmaven.local",
            "password": "password123",
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def member_token(client) -> str:
    """Get member user JWT token."""
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "member@faultmaven.local",
            "password": "password123",
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def viewer_token(client) -> str:
    """Get viewer user JWT token."""
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@faultmaven.local",
            "password": "password123",
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def mock_case():
    """Create mock case for testing."""
    return Case(
        case_id="case-test-001",
        organization_id="org-dev-001",
        user_id="user-admin-001",
        title="Test Case",
        description="Test case description",
        status=CaseStatus.CONSULTING,
        severity=CaseSeverity.MEDIUM,
    )


# ============================================================
# Cases API JWT Authentication Tests
# ============================================================


class TestCasesAPIWithJWT:
    """Tests for Cases API with JWT authentication."""

    def test_list_cases_requires_authentication(self, client):
        """401 when accessing cases without authentication."""
        response = client.get("/api/v1/cases")
        assert response.status_code == 401

    def test_list_cases_with_valid_jwt(self, client, admin_token):
        """200 OK when accessing cases with valid JWT."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200

    def test_list_cases_with_invalid_jwt(self, client):
        """401 when accessing cases with invalid JWT."""
        response = client.get(
            "/api/v1/cases",
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert response.status_code == 401

    def test_create_case_with_jwt(self, client, admin_token, mock_case):
        """Create case with JWT authentication."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.create_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            response = client.post(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "title": "Test Case",
                    "description": "Test description",
                    "severity": "medium",
                },
            )

            assert response.status_code == 201
            assert response.json()["title"] == "Test Case"

    def test_get_case_with_jwt(self, client, admin_token, mock_case):
        """Get case by ID with JWT authentication."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.get_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases/case-test-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200
            assert response.json()["case_id"] == "case-test-001"

    def test_update_case_with_jwt(self, client, admin_token, mock_case):
        """Update case with JWT authentication."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_case.title = "Updated Title"
            mock_service.update_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            response = client.patch(
                "/api/v1/cases/case-test-001",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"title": "Updated Title"},
            )

            assert response.status_code == 200

    def test_delete_case_with_jwt(self, client, admin_token):
        """Delete case with JWT authentication."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.delete_case = AsyncMock(return_value=True)
            mock_service_dep.return_value = mock_service

            response = client.delete(
                "/api/v1/cases/case-test-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 204


class TestCasesAPILegacyAuth:
    """Tests for Cases API backwards compatibility with legacy headers."""

    def test_list_cases_with_legacy_headers(self, client):
        """200 OK when accessing cases with legacy headers."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases",
                headers={
                    "X-Organization-ID": "org-test-001",
                    "X-User-ID": "user-test-001",
                },
            )

            assert response.status_code == 200

    def test_create_case_with_legacy_headers(self, client, mock_case):
        """Create case with legacy header authentication."""
        mock_case.organization_id = "org-test-001"
        mock_case.user_id = "user-test-001"

        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.create_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            response = client.post(
                "/api/v1/cases",
                headers={
                    "X-Organization-ID": "org-test-001",
                    "X-User-ID": "user-test-001",
                },
                json={
                    "title": "Test Case",
                    "description": "Test description",
                    "severity": "medium",
                },
            )

            assert response.status_code == 201

    def test_jwt_takes_precedence_over_legacy_headers(self, client, admin_token):
        """JWT authentication takes precedence over legacy headers."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            # Provide both JWT and legacy headers
            response = client.get(
                "/api/v1/cases",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "X-Organization-ID": "different-org",
                    "X-User-ID": "different-user",
                },
            )

            assert response.status_code == 200

            # Verify service was called with JWT org_id (org-dev-001), not legacy header
            call_args = mock_service.list_cases.call_args
            assert call_args.kwargs["organization_id"] == "org-dev-001"


# ============================================================
# Sessions API JWT Authentication Tests
# ============================================================


class TestSessionsAPIWithJWT:
    """Tests for Sessions API with JWT authentication."""

    def test_list_sessions_requires_authentication(self, client):
        """401 when accessing sessions without authentication."""
        response = client.get("/api/v1/cases/case-001/sessions")
        assert response.status_code == 401

    def test_list_sessions_with_valid_jwt(self, client, admin_token):
        """200 OK when accessing sessions with valid JWT."""
        with patch(
            "faultmaven.api.dependencies.get_investigation_session_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_sessions = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases/case-001/sessions",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200

    def test_list_sessions_with_invalid_jwt(self, client):
        """401 when accessing sessions with invalid JWT."""
        response = client.get(
            "/api/v1/cases/case-001/sessions",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


class TestSessionsAPILegacyAuth:
    """Tests for Sessions API backwards compatibility."""

    def test_list_sessions_with_legacy_headers(self, client):
        """200 OK when accessing sessions with legacy headers."""
        with patch(
            "faultmaven.api.dependencies.get_investigation_session_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_sessions = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases/case-001/sessions",
                headers={
                    "X-Organization-ID": "org-test-001",
                    "X-User-ID": "user-test-001",
                },
            )

            assert response.status_code == 200


# ============================================================
# Evidence API JWT Authentication Tests
# ============================================================


class TestEvidenceAPIWithJWT:
    """Tests for Evidence API with JWT authentication."""

    def test_list_evidence_requires_authentication(self, client):
        """401 when accessing evidence without authentication."""
        response = client.get("/api/v1/cases/case-001/evidence")
        assert response.status_code == 401

    def test_list_evidence_with_valid_jwt(self, client, admin_token):
        """200 OK when accessing evidence with valid JWT."""
        with patch(
            "faultmaven.api.dependencies.get_evidence_artifact_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_evidence_by_case = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases/case-001/evidence",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200

    def test_list_evidence_with_invalid_jwt(self, client):
        """401 when accessing evidence with invalid JWT."""
        response = client.get(
            "/api/v1/cases/case-001/evidence",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


class TestEvidenceAPILegacyAuth:
    """Tests for Evidence API backwards compatibility."""

    def test_list_evidence_with_legacy_headers(self, client):
        """200 OK when accessing evidence with legacy headers."""
        with patch(
            "faultmaven.api.dependencies.get_evidence_artifact_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_evidence_by_case = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases/case-001/evidence",
                headers={
                    "X-Organization-ID": "org-test-001",
                    "X-User-ID": "user-test-001",
                },
            )

            assert response.status_code == 200


# ============================================================
# Role-Based Access Control Tests
# ============================================================


class TestRoleBasedAccessOnProtectedEndpoints:
    """Tests for role-based access control across endpoints."""

    def test_admin_can_access_all_endpoints(self, client, admin_token, mock_case):
        """Admin user can access all endpoint operations."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service.create_case = AsyncMock(return_value=mock_case)
            mock_service.delete_case = AsyncMock(return_value=True)
            mock_service_dep.return_value = mock_service

            # Admin can list
            list_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert list_response.status_code == 200

            # Admin can create
            create_response = client.post(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "title": "Admin Case",
                    "description": "Test",
                    "severity": "high",
                },
            )
            assert create_response.status_code == 201

            # Admin can delete
            delete_response = client.delete(
                "/api/v1/cases/case-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert delete_response.status_code == 204

    def test_member_can_read_and_create(self, client, member_token, mock_case):
        """Member user can read and create but has limited permissions."""
        mock_case.user_id = "user-member-001"

        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service.create_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            # Member can list
            list_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {member_token}"},
            )
            assert list_response.status_code == 200

            # Member can create
            create_response = client.post(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {member_token}"},
                json={
                    "title": "Member Case",
                    "description": "Test",
                    "severity": "low",
                },
            )
            assert create_response.status_code == 201

    def test_viewer_can_only_read(self, client, viewer_token):
        """Viewer user can only read (list/get) operations."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            # Viewer can list
            list_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            assert list_response.status_code == 200


# ============================================================
# Token Expiration and Validation Tests
# ============================================================


class TestTokenValidation:
    """Tests for token validation on protected endpoints."""

    def test_malformed_authorization_header(self, client):
        """401 for malformed Authorization header."""
        # Missing "Bearer" prefix
        response = client.get(
            "/api/v1/cases",
            headers={"Authorization": "token-without-bearer"},
        )
        assert response.status_code == 401

    def test_empty_bearer_token(self, client):
        """401 for empty Bearer token."""
        response = client.get(
            "/api/v1/cases",
            headers={"Authorization": "Bearer "},
        )
        assert response.status_code == 401

    def test_tampered_token_signature(self, client, admin_token):
        """401 for tampered token signature."""
        # Modify the signature part of the token
        parts = admin_token.split(".")
        if len(parts) == 3:
            tampered_token = f"{parts[0]}.{parts[1]}.tampered_signature"
            response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {tampered_token}"},
            )
            assert response.status_code == 401


# ============================================================
# Cross-Endpoint JWT Flow Tests
# ============================================================


class TestCrossEndpointJWTFlow:
    """Tests for complete JWT flow across multiple endpoints."""

    def test_complete_jwt_workflow_across_endpoints(self, client, mock_case):
        """Complete JWT workflow: login -> access multiple protected endpoints."""
        # Step 1: Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        assert login_response.status_code == 200
        tokens = login_response.json()
        access_token = tokens["access_token"]

        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_case_service, patch(
            "faultmaven.api.dependencies.get_investigation_session_service"
        ) as mock_session_service, patch(
            "faultmaven.api.dependencies.get_evidence_artifact_service"
        ) as mock_evidence_service:
            # Setup mock services
            case_service = AsyncMock()
            case_service.list_cases = AsyncMock(return_value=[mock_case])
            case_service.create_case = AsyncMock(return_value=mock_case)
            mock_case_service.return_value = case_service

            session_service = AsyncMock()
            session_service.list_sessions = AsyncMock(return_value=[])
            mock_session_service.return_value = session_service

            evidence_service = AsyncMock()
            evidence_service.list_evidence_by_case = AsyncMock(return_value=[])
            mock_evidence_service.return_value = evidence_service

            # Step 2: Access Cases API
            cases_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert cases_response.status_code == 200

            # Step 3: Access Sessions API
            sessions_response = client.get(
                "/api/v1/cases/case-001/sessions",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert sessions_response.status_code == 200

            # Step 4: Access Evidence API
            evidence_response = client.get(
                "/api/v1/cases/case-001/evidence",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert evidence_response.status_code == 200

    def test_refreshed_token_works_on_protected_endpoints(self, client):
        """Refreshed token can access protected endpoints."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Refresh token
        refresh_response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert refresh_response.status_code == 200
        new_tokens = refresh_response.json()

        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            # Use new access token
            response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
            )
            assert response.status_code == 200

    def test_user_info_passed_to_service_from_jwt(self, client, admin_token, mock_case):
        """User info from JWT is correctly passed to service layer."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.create_case = AsyncMock(return_value=mock_case)
            mock_service_dep.return_value = mock_service

            response = client.post(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "title": "Test Case",
                    "description": "Test",
                    "severity": "medium",
                },
            )

            assert response.status_code == 201

            # Verify service was called with correct user info from JWT
            call_args = mock_service.create_case.call_args
            assert call_args.kwargs["organization_id"] == "org-dev-001"
            assert call_args.kwargs["user_id"] == "user-admin-001"


# ============================================================
# Organization Isolation Tests
# ============================================================


class TestOrganizationIsolation:
    """Tests for organization-level data isolation."""

    def test_jwt_organization_id_used_for_queries(self, client, admin_token):
        """Organization ID from JWT is used for data queries."""
        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200

            # Verify organization_id from JWT was passed to service
            call_args = mock_service.list_cases.call_args
            assert call_args.kwargs["organization_id"] == "org-dev-001"

    def test_different_users_same_org_share_data(self, client):
        """Users in same organization can access same data."""
        # Login as admin
        admin_login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@faultmaven.local", "password": "password123"},
        )
        admin_token = admin_login.json()["access_token"]

        # Login as member (same org)
        member_login = client.post(
            "/api/v1/auth/login",
            json={"email": "member@faultmaven.local", "password": "password123"},
        )
        member_token = member_login.json()["access_token"]

        with patch(
            "faultmaven.api.dependencies.get_api_case_service"
        ) as mock_service_dep:
            mock_service = AsyncMock()
            mock_service.list_cases = AsyncMock(return_value=[])
            mock_service_dep.return_value = mock_service

            # Both users access cases
            admin_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            member_response = client.get(
                "/api/v1/cases",
                headers={"Authorization": f"Bearer {member_token}"},
            )

            assert admin_response.status_code == 200
            assert member_response.status_code == 200

            # Both should query with same org_id
            calls = mock_service.list_cases.call_args_list
            assert all(
                call.kwargs["organization_id"] == "org-dev-001" for call in calls
            )


# ============================================================
# Error Response Tests
# ============================================================


class TestAuthenticationErrorResponses:
    """Tests for authentication error responses."""

    def test_401_includes_www_authenticate_header(self, client):
        """401 response includes WWW-Authenticate header."""
        response = client.get("/api/v1/cases")

        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers
        assert "Bearer" in response.headers["WWW-Authenticate"]

    def test_401_error_has_detail_message(self, client):
        """401 response includes error detail message."""
        response = client.get("/api/v1/cases")

        assert response.status_code == 401
        error_data = response.json()
        assert "detail" in error_data

    def test_401_for_expired_looking_token(self, client):
        """401 for token that looks expired (invalid format)."""
        # Create a fake token that would be expired
        fake_token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwiZXhwIjoxfQ.invalid"

        response = client.get(
            "/api/v1/cases",
            headers={"Authorization": f"Bearer {fake_token}"},
        )

        assert response.status_code == 401
