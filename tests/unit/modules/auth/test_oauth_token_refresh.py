"""Unit tests for mode-aware token refresh (`POST /auth/refresh`, ADR-015 D6).

Historically `/auth/refresh` was gated to local mode and always built an HS256
generator, so an oauth/cloud deployment 404'd the dashboard's refresh call and
every WorkOS-minted session died at first access-token expiry. The route is now
mode-aware: oauth mode validates and mints with the RS256 generator attached by
the composition root (`app.state.jwt_token_generator`) — the same instance that
issued the tokens — with identical rotation and revocation semantics.

These tests exercise the oauth path by calling the handler directly with a
minimal fake request carrying a real ``RS256JWTTokenGenerator`` (ephemeral RSA
keys) and an in-memory revocation store. The local path keeps its own coverage
in ``test_local_token_refresh.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from faultmaven.config.settings import AuthMode
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import TokenRefreshRequest
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    RS256JWTTokenGenerator,
)
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio


def _generate_test_rsa_keypair() -> tuple[str, str]:
    """Generate an ephemeral in-memory RSA key pair for this test run."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


class _FakeUserStore:
    def __init__(self, user: DevUser | None) -> None:
        self._user = user

    async def get_user(self, user_id: str) -> DevUser | None:
        if self._user and self._user.user_id == user_id:
            return self._user
        return None


def _make_user(**overrides) -> DevUser:
    defaults = dict(
        user_id="user_sso_refresh_1",
        username="sso-refresher",
        email="sso-refresher@example.com",
        display_name="SSO Refresher",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return DevUser(**defaults)


def _make_rs256_generator(revocation_store) -> RS256JWTTokenGenerator:
    private_pem, public_pem = _generate_test_rsa_keypair()
    return RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=revocation_store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _fake_request(user_store, jwt_token_generator):
    """Oauth-mode request: no token_manager needed, generator on app.state."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store,
                jwt_token_generator=jwt_token_generator,
            )
        )
    )


def _oauth_settings_patch():
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.OAUTH,
            jwt_access_token_expire_minutes=60,
        )
    )
    return patch.object(auth_routes, "get_settings", return_value=settings_stub)


async def test_oauth_refresh_returns_new_pair_and_rotates():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    old_refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc)
    )

    request = _fake_request(_FakeUserStore(user), generator)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

    assert result.access_token
    assert result.refresh_token
    assert result.refresh_token != old_refresh
    assert result.token_type == "bearer"
    assert result.expires_in == 60 * 60

    # Token response must not be cached (RFC 6749 §5.1).
    assert response.headers["Cache-Control"] == "no-store"

    # New pair validates under the RS256 generator that issued it.
    assert await generator.validate_access_token(result.access_token) is not None
    assert await generator.validate_refresh_token(result.refresh_token) is not None

    # Rotation: the presented refresh token is now revoked.
    assert await generator.validate_refresh_token(old_refresh) is None


async def test_oauth_refresh_rejects_replayed_rotated_token():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    old_refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc)
    )

    request = _fake_request(_FakeUserStore(user), generator)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=old_refresh), request, response
            )
    assert exc.value.status_code == 401


async def test_oauth_refresh_rejects_access_token_in_place_of_refresh():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    access_token = await generator.generate_access_token(
        user, state_read_at=datetime.now(timezone.utc)
    )

    request = _fake_request(_FakeUserStore(user), generator)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=access_token), request, response
            )
    assert exc.value.status_code == 401


async def test_oauth_refresh_rejects_when_user_gone():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc)
    )

    request = _fake_request(_FakeUserStore(None), generator)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=refresh), request, response
            )
    assert exc.value.status_code == 401


async def test_oauth_refresh_rejects_inactive_user():
    # Minted while the account was live, THEN deactivated — the real sequence,
    # and now the only expressible one: the generator refuses to mint for an
    # already-deactivated account, so the previous shortcut of minting straight
    # from an inactive user no longer models anything that can happen.
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc)
    )
    user.is_active = False

    request = _fake_request(_FakeUserStore(user), generator)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=refresh), request, response
            )
    assert exc.value.status_code == 401


async def test_oauth_refresh_503_when_generator_not_attached():
    """Oauth mode with no composition-root generator must 503, not 500."""
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_rs256_generator(store)
    refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc)
    )

    request = _fake_request(_FakeUserStore(user), jwt_token_generator=None)
    response = SimpleNamespace(headers={})

    with _oauth_settings_patch():
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=refresh), request, response
            )
    assert exc.value.status_code == 503
