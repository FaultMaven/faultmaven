"""Unit tests for Authentication Middleware (TASK-017)

Tests get_current_user, get_current_user_optional and require_platform_admin.

Test Categories:
1. get_current_user Tests - Token extraction and verification
2. require_platform_admin Tests - The operator axis

Coverage Target: 90%+
"""

import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from faultmaven.api.middleware.auth import (
    _extract_token,
    get_auth_service,
    get_current_user,
    get_current_user_optional,
    require_platform_admin,
    set_auth_service,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    RevocationStateUnknownError,
    TokenRevocationError,
)
from tests.utils import (
    forge_access_token,
    forge_refresh_token,
    request_with_authorization,
    sign_claims_for,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_auth_service():
    """Create mock AuthService."""
    service = MagicMock()
    service.extract_user_from_token_with_revocation_check = AsyncMock()
    return service


@pytest.fixture
def sample_user() -> AuthenticatedUser:
    """Create sample authenticated user."""
    return AuthenticatedUser(
        user_id="user-123",
        enterprise_id="org-456",
        email="test@example.com",
        roles=["admin", "platform_admin"],
        permissions=[
            "cases:read",
            "cases:write",
            "cases:delete",
            "evidence:read",
            "evidence:upload",
        ],
        token_jti="token-789",
    )


@pytest.fixture
def member_user() -> AuthenticatedUser:
    """Create sample member user (limited permissions)."""
    return AuthenticatedUser(
        user_id="user-456",
        enterprise_id="org-456",
        email="member@example.com",
        roles=["member"],
        permissions=[
            "cases:read",
            "cases:write",
            "evidence:read",
        ],
        token_jti="token-abc",
    )


# ============================================================
# Token Extraction Tests
# ============================================================


class TestTokenExtraction:
    """Tests for _extract_token helper."""

    def test_extract_from_bearer_header(self):
        """Extracts token from Bearer header."""
        token = _extract_token("Bearer abc123", None)
        assert token == "abc123"

    def test_extract_from_credentials(self):
        """Extracts token from HTTPBearer credentials."""
        credentials = MagicMock()
        credentials.credentials = "xyz789"

        token = _extract_token(None, credentials)
        assert token == "xyz789"

    def test_credentials_takes_precedence(self):
        """HTTPBearer credentials take precedence over raw header."""
        credentials = MagicMock()
        credentials.credentials = "from-credentials"

        token = _extract_token("Bearer from-header", credentials)
        assert token == "from-credentials"

    def test_returns_none_when_no_auth(self):
        """Returns None when no authentication provided."""
        token = _extract_token(None, None)
        assert token is None

    def test_returns_none_for_invalid_header_format(self):
        """Returns None for non-Bearer header format."""
        token = _extract_token("Basic abc123", None)
        assert token is None

    def test_returns_none_for_empty_bearer_token(self):
        """Returns None for empty Bearer token."""
        token = _extract_token("Bearer ", None)
        assert token is None

    def test_handles_bearer_with_extra_spaces(self):
        """Handles Bearer token with extra spaces."""
        token = _extract_token("Bearer   abc123  ", None)
        assert token == "abc123"


# ============================================================
# get_current_user Tests
# ============================================================


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_token(self, mock_auth_service, sample_user):
        """Returns AuthenticatedUser for valid token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.return_value = (
            sample_user
        )

        set_auth_service(mock_auth_service)

        user = await get_current_user(
            request_with_authorization("Bearer valid-token"),
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user == sample_user
        assert user.user_id == "user-123"
        assert user.enterprise_id == "org-456"

    @pytest.mark.asyncio
    async def test_raises_401_on_missing_token(self, mock_auth_service):
        """Raises 401 when no token provided."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization(),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_on_invalid_header_format(self, mock_auth_service):
        """Raises 401 for non-Bearer header."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Basic abc123"),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_401_on_expired_token(self, mock_auth_service):
        """Raises 401 for expired token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Token has expired", error_code="TOKEN_EXPIRED")
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer expired-token"),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_raises_401_on_revoked_token(self, mock_auth_service):
        """Raises 401 — not 403 — for a revoked token.

        A revoked token is an invalid credential (RFC 6750 `invalid_token`),
        not a valid one lacking permission. This asserted 403 until #1065 made
        revocation the routine consequence of signing out, at which point a
        client that (correctly) tears down on 401 and treats 403 as a
        permission problem kept its dead tokens and showed an access-denied
        message instead of signing out.
        """
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            TokenRevocationError()
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer revoked-token"),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()
        # The challenge header is what makes it actionable rather than merely
        # correctly-numbered.
        assert "Bearer" in exc_info.value.headers.get("WWW-Authenticate", "")

    @pytest.mark.asyncio
    async def test_every_authentication_failure_is_401(self, mock_auth_service):
        """Every failure that is a VERDICT ON THE CREDENTIAL answers 401.

        The property, not the three instances: 403 on an authentication path
        tells a client its dead credential is live, and the client's only
        sensible response to that is to keep using it. Pins the whole failure
        domain so a future exception type cannot reintroduce the split that
        made a revoked token 401 on case routes and 403 here.

        One deliberate exception, and it is not a widening of this rule:
        ``RevocationStateUnknownError`` answers **503** (#1478), because there
        the credential was never judged — the store that judges it could not be
        read. Pinned in ``test_an_unreadable_revocation_store_is_503_not_401``
        below, which also checks it is not the 403 this test forbids.
        """
        failures = [
            TokenRevocationError(),
            AuthenticationError("Token has expired", error_code="TOKEN_EXPIRED"),
            AuthenticationError("Token decode error", error_code="DECODE_ERROR"),
            RuntimeError("store unreachable"),
        ]

        for failure in failures:
            mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
                failure
            )

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    request_with_authorization("Bearer some-token"),
                    credentials=None,
                    auth_service=mock_auth_service,
                )

            assert exc_info.value.status_code == 401, (
                f"{type(failure).__name__} answered "
                f"{exc_info.value.status_code}, not 401"
            )

    @pytest.mark.asyncio
    async def test_an_unreadable_revocation_store_is_503_not_401(
        self, mock_auth_service
    ):
        """ "We could not find out" is not "your credential is bad" (#1478).

        A 401 here would send a client that tears down on 401 — the copilot
        does — into discarding a perfectly live credential over a storage blip
        a retry clears, and would make a revoked token and an unreadable store
        indistinguishable to whoever is paged. Still not a 403, for the reason
        the test above gives.
        """
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            RevocationStateUnknownError(kind="OperationalError")
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer some-token"),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers["x-error-code"] == "REVOCATION_STATE_UNKNOWN"

    @pytest.mark.asyncio
    async def test_raises_401_on_malformed_token(self, mock_auth_service):
        """Raises 401 for malformed token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Token decode error", error_code="DECODE_ERROR")
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer malformed-token"),
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401


# ============================================================
# The 401 body carries no token- or PyJWT-derived text (#1859)
# ============================================================
#
# `AuthService.verify_token` builds its `AuthenticationError.message` from
# PyJWT's exception text and from the token's own `type` claim. That message is
# the server-side diagnostic; the body must not carry it. These tests drive the
# path that runs the dependency: a FastAPI route depending on
# `get_current_user`, a REAL `AuthService` doing the verification, one request
# per token through `TestClient`. Only `get_auth_service` is overridden.

_INVALID_TOKEN_DETAIL = "Invalid token. Please re-authenticate."
_EXPIRED_TOKEN_DETAIL = "Token has expired. Please refresh or re-authenticate."

#: PyJWT's and `AuthService`'s diagnostic vocabulary. None of it may reach a
#: response body, in any case.
_DIAGNOSTIC_WORDS = ("decode", "segments", "padding", "Signature", "alg", "Expected")


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _live_claims(service: AuthService) -> dict:
    """The claim set the live access generator emits, read back unverified
    from `forge_access_token` (whose shape `test_token_forger_shape_parity.py`
    pins to the generator's). A row that changes one claim of it is refused
    for that claim, not for one a hand-built set forgot."""
    token = forge_access_token(
        service,
        user_id="u1",
        enterprise_id="ent-1",
        email="u1@example.com",
        roles=["member"],
    )
    return jwt.decode(token, options={"verify_signature": False})


#: Every token `get_current_user` refuses with the generic invalid-token 401,
#: one per reason `verify_token` builds a message for.
_REFUSED_TOKENS = {
    "two-segments": lambda s: "abc.def",
    "not-a-jwt": lambda s: "not-a-jwt",
    "bad-header-padding": lambda s: f"eyJhbGciOi.{_b64url(_live_claims(s))}.AAAA",
    "bad-signature": lambda s: (
        f"{_b64url({'alg': 'HS256'})}.{_b64url(_live_claims(s))}.AAAA"
    ),
    "alg-none": lambda s: f"{_b64url({'alg': 'none'})}.{_b64url(_live_claims(s))}.",
    # Correctly signed, wrong `type`: the message interpolates the claim.
    "type-refresh": lambda s: forge_refresh_token(
        s, user_id="u1", enterprise_id="ent-1"
    ),
    "type-script": lambda s: sign_claims_for(
        s, {**_live_claims(s), "type": "<script>x</script>"}
    ),
}


@pytest.fixture(scope="module")
def real_auth_service() -> AuthService:
    """A real `AuthService` in local (HS256) mode over strict settings.

    A `SimpleNamespace`, not a `MagicMock`: it carries only what the service
    and the forging helpers read, so a setting nobody set raises
    `AttributeError` instead of reading as a truthy mock."""
    settings = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="local",
            jwt_access_token_expire_minutes=15,
            jwt_refresh_token_expire_days=7,
        ),
        security=SimpleNamespace(
            jwt_issuer="faultmaven",
            jwt_audience="faultmaven-api",
            jwt_secret_key=SecretStr("test-secret-key-min-32-bytes!!!!!"),
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
        ),
    )
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        return AuthService()


@pytest.fixture(scope="module")
def bearer_client(real_auth_service: AuthService) -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: AuthenticatedUser = Depends(get_current_user)):
        return {"user_id": user.user_id}

    app.dependency_overrides[get_auth_service] = lambda: real_auth_service
    return TestClient(app)


def _assert_no_diagnostic(body: str) -> None:
    leaked = [w for w in _DIAGNOSTIC_WORDS if w.lower() in body.lower()]
    assert not leaked, f"401 body carries diagnostic text {leaked}: {body}"


@pytest.mark.security
class TestUnauthorizedBodyCarriesNoTokenText:
    """No 401 from `get_current_user` carries text derived from the token or
    from PyJWT (#1859)."""

    @pytest.mark.parametrize("name", list(_REFUSED_TOKENS))
    def test_a_refused_token_gets_the_static_detail(
        self, bearer_client, real_auth_service, name
    ):
        token = _REFUSED_TOKENS[name](real_auth_service)

        resp = bearer_client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 401
        assert resp.json() == {"detail": _INVALID_TOKEN_DETAIL}
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        _assert_no_diagnostic(resp.text)
        # The two `type` rows: the claim's value is attacker-chosen content.
        assert "refresh" not in resp.json()["detail"]
        assert "<script>" not in resp.text

    def test_the_refusal_reason_stays_in_the_debug_log(
        self, bearer_client, real_auth_service, caplog
    ):
        """The body is static; the reason is kept server-side, in the debug
        record `get_current_user` writes before it answers."""
        token = _REFUSED_TOKENS["two-segments"](real_auth_service)

        with caplog.at_level(logging.DEBUG, logger="faultmaven.api.middleware.auth"):
            resp = bearer_client.get(
                "/whoami", headers={"Authorization": f"Bearer {token}"}
            )

        assert resp.status_code == 401
        assert resp.json() == {"detail": _INVALID_TOKEN_DETAIL}
        debug = [
            r.getMessage()
            for r in caplog.records
            if r.name == "faultmaven.api.middleware.auth" and r.levelno == logging.DEBUG
        ]
        assert any(
            "DECODE_ERROR" in m and "Not enough segments" in m for m in debug
        ), debug

    def test_an_expired_token_keeps_its_own_static_detail(
        self, bearer_client, real_auth_service
    ):
        token = forge_access_token(
            real_auth_service,
            user_id="u1",
            enterprise_id="ent-1",
            email="u1@example.com",
            roles=["member"],
            expires_in_minutes=-5,
        )

        resp = bearer_client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 401
        assert resp.json() == {"detail": _EXPIRED_TOKEN_DETAIL}
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        _assert_no_diagnostic(resp.text)

    def test_a_valid_token_is_admitted(self, bearer_client, real_auth_service):
        """Positive control: the harness refuses for the token's reason, not
        because the service or the override is broken."""
        token = forge_access_token(
            real_auth_service,
            user_id="u1",
            enterprise_id="ent-1",
            email="u1@example.com",
            roles=["member"],
        )

        resp = bearer_client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"user_id": "u1"}


# ============================================================
# get_current_user_optional Tests
# ============================================================


class TestGetCurrentUserOptional:
    """Tests for get_current_user_optional dependency."""

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_token(self, mock_auth_service, sample_user):
        """Returns AuthenticatedUser for valid token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.return_value = (
            sample_user
        )

        user = await get_current_user_optional(
            request_with_authorization("Bearer valid-token"),
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user == sample_user

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_token(self, mock_auth_service):
        """Returns None when no token provided."""
        user = await get_current_user_optional(
            request_with_authorization(),
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_token(self, mock_auth_service):
        """Returns None for invalid token (doesn't raise)."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Invalid token", error_code="INVALID_TOKEN")
        )

        user = await get_current_user_optional(
            request_with_authorization("Bearer invalid-token"),
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_revoked_token(self, mock_auth_service):
        """Returns None for revoked token (doesn't raise)."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            TokenRevocationError()
        )

        user = await get_current_user_optional(
            request_with_authorization("Bearer revoked-token"),
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None


# ============================================================
# require_platform_admin Tests
# ============================================================


class TestRequirePlatformAdmin:
    """Tests for require_platform_admin dependency."""

    @pytest.mark.asyncio
    async def test_allows_platform_admin_user(self, sample_user):
        """Allows a user holding the platform_admin role."""
        result = await require_platform_admin(current_user=sample_user)
        assert result == sample_user

    @pytest.mark.asyncio
    async def test_raises_403_for_non_admin(self, member_user):
        """Raises 403 for a user with no admin role at all."""
        with pytest.raises(HTTPException) as exc_info:
            await require_platform_admin(current_user=member_user)

        assert exc_info.value.status_code == 403
        assert "Platform administrator" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_403_for_org_admin_without_platform_admin(self):
        """An ORG admin is not a platform admin (ADR-012 D9).

        This is the whole point of the two-role split: org "admin" is
        tenant-bounded, so holding it must NOT grant cross-tenant operator
        reach. If this ever passes, the two axes have been re-conflated.
        """
        org_admin = AuthenticatedUser(
            user_id="user-org-admin",
            enterprise_id="org-456",
            email="orgadmin@example.com",
            roles=["user", "admin"],
            permissions=["cases:read", "cases:write"],
        )

        with pytest.raises(HTTPException) as exc_info:
            await require_platform_admin(current_user=org_admin)

        assert exc_info.value.status_code == 403


# ============================================================
# Auth Service Singleton Tests
# ============================================================


class TestAuthServiceSingleton:
    """Tests for auth service singleton management."""

    @pytest.mark.asyncio
    async def test_set_and_get_auth_service(self, mock_auth_service):
        """set_auth_service and get_auth_service work correctly."""
        set_auth_service(mock_auth_service)
        retrieved = await get_auth_service(request=None)
        assert retrieved == mock_auth_service

    @pytest.mark.asyncio
    async def test_no_composed_service_answers_503(self):
        """With none composed, 503 and never a stand-in (#2103): an
        ``AuthService()`` built here would have no revocation store."""
        set_auth_service(None)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

        with patch(
            "faultmaven.api.middleware.auth.AuthService",
            side_effect=AssertionError("built a stand-in AuthService"),
        ):
            with pytest.raises(HTTPException) as refused:
                await get_auth_service(request=request)

        assert refused.value.status_code == 503
        assert refused.value.detail == "Authentication service unavailable"

    def teardown_method(self, method):
        """Reset auth service after each test."""
        set_auth_service(None)


# ============================================================
# AuthenticatedUser Model Tests
# ============================================================


class TestAuthenticatedUserModel:
    """Tests for AuthenticatedUser model methods."""

    def test_is_platform_admin(self, sample_user, member_user):
        """is_platform_admin returns True only for the platform_admin role."""
        assert sample_user.is_platform_admin() is True
        assert member_user.is_platform_admin() is False

    def test_to_dict(self, sample_user):
        """to_dict returns correct dictionary."""
        d = sample_user.to_dict()

        assert d["user_id"] == "user-123"
        assert d["enterprise_id"] == "org-456"
        assert d["email"] == "test@example.com"
        assert d["roles"] == ["admin", "platform_admin"]
        assert "cases:read" in d["permissions"]
        assert d["token_jti"] == "token-789"

    def test_from_jwt_claims(self):
        """from_jwt_claims creates AuthenticatedUser correctly."""
        claims = {
            "sub": "user-abc",
            "organization_id": "org-def",
            "email": "jwt@example.com",
            "roles": ["member"],
            "permissions": ["cases:read"],
            "jti": "claim-jti",
        }

        user = AuthenticatedUser.from_jwt_claims(claims)

        assert user.user_id == "user-abc"
        assert user.organization_id == "org-def"
        assert user.email == "jwt@example.com"
        assert user.roles == ["member"]
        assert user.permissions == ["cases:read"]
        assert user.token_jti == "claim-jti"
