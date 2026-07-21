"""Unit tests for token revocation on the optional-auth path (issue #761).

``get_current_user_optional`` (``api/v1/auth_dependencies``) previously did a
bare ``jwt.decode``: a revoked-but-unexpired access token still authenticated
on every optional-auth endpoint (and on the ``require_authentication`` /
``require_admin`` wrappers built on it). It now delegates to the same
``AuthService.verify_token_with_revocation_check`` the mandatory-auth
middleware and the tenant binder use.

These tests run the real ``AuthService`` (HS256 local mode) against FakeRedis:
they mint a genuine access token, then prove revocation flips the optional
path to unauthenticated — plus the two hardenings the convergence brings for
free (refresh tokens and ``jti``-less tokens are not valid identities).
"""

from unittest.mock import MagicMock, patch

import fakeredis.aioredis as fakeredis
import jwt as pyjwt
import pytest
from fastapi import HTTPException

from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

USER_ID = "11111111-1111-1111-1111-111111111111"
ORG_ID = "22222222-2222-2222-2222-222222222222"
SECRET = "test-secret-key-0123456789abcdef"  # 32+ bytes: HS256 minimum


def _mock_settings():
    settings = MagicMock()
    settings.auth.auth_mode = "local"
    settings.security.jwt_algorithm = "HS256"
    settings.security.jwt_access_token_expire_minutes = 15
    settings.security.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = "faultmaven"
    settings.security.jwt_audience = "faultmaven-api"
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = None
    settings.security.jwt_public_key = None
    settings.security.jwt_private_key_path = None
    settings.security.jwt_public_key_path = None
    settings.security.jwt_secret_key = MagicMock()
    settings.security.jwt_secret_key.get_secret_value.return_value = SECRET
    return settings


@pytest.fixture
def auth_service():
    """Real AuthService: HS256 local mode, production store over FakeRedis."""
    store = RedisTokenRevocationStore(
        fakeredis.FakeRedis(decode_responses=True), key_prefix="revoked:token:"
    )
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_mock_settings(),
    ):
        yield AuthService(revocation_store=store)


def _mint_access_token(auth_service: AuthService) -> str:
    return auth_service.generate_access_token(
        user_id=USER_ID,
        organization_id=ORG_ID,
        email="user@example.com",
        roles=["member"],
    )


def _claims_of(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


async def _resolve(auth_service: AuthService, token: str):
    return await get_current_user_optional(
        request=MagicMock(), token=token, auth_service=auth_service
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_valid_access_token_authenticates(auth_service):
    """Baseline: a freshly minted, unrevoked access token yields a DevUser."""
    token = _mint_access_token(auth_service)

    user = await _resolve(auth_service, token)

    assert user is not None
    assert user.user_id == USER_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_revoked_token_is_unauthenticated(auth_service):
    """The #761 regression: a revoked-but-unexpired token must NOT authenticate."""
    token = _mint_access_token(auth_service)
    claims = _claims_of(token)
    await auth_service.revoke_token(claims["jti"], claims["exp"])

    user = await _resolve(auth_service, token)

    assert user is None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_revoked_token_gets_401_on_mandatory_wrapper(auth_service):
    """require_authentication (built on the optional path) rejects a revoked token."""
    token = _mint_access_token(auth_service)
    claims = _claims_of(token)
    await auth_service.revoke_token(claims["jti"], claims["exp"])

    user = await _resolve(auth_service, token)
    with pytest.raises(HTTPException) as exc_info:
        await require_authentication(user=user)

    assert exc_info.value.status_code == 401


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_refresh_token_is_not_an_identity(auth_service):
    """Convergence hardening: a refresh token (type != access) is rejected —
    the old bare decode never checked the ``type`` claim."""
    refresh_token = auth_service.generate_refresh_token(
        user_id=USER_ID, organization_id=ORG_ID
    )

    user = await _resolve(auth_service, refresh_token)

    assert user is None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_token_without_jti_is_rejected(auth_service):
    """Convergence hardening: a token missing ``jti`` can never be revoked, so
    it is not accepted as an identity (verify_token requires the claim)."""
    token = _mint_access_token(auth_service)
    claims = _claims_of(token)
    del claims["jti"]
    jtiless = pyjwt.encode(claims, SECRET, algorithm="HS256")

    user = await _resolve(auth_service, jtiless)

    assert user is None
