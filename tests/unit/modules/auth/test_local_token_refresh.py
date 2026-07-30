"""Unit tests for the local-mode token-refresh route (`POST /auth/refresh`).

Local auth issues a short-lived access token and a long-lived refresh token.
A stateless access token cannot be extended by activity, so an active client
must mint a new one via this route rather than being force-logged-out at
expiry. These tests pin that behaviour and the refresh-token rotation that
makes a replayed token unusable after a successful refresh.

The route is exercised by calling the handler directly with a minimal fake
request (the ``get_user_store`` / ``get_token_revocation_store`` dependencies read from
``request.app.state``), a real ``HS256JWTTokenGenerator``, and an in-memory
revocation store — no full-app boot required.
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
    HS256JWTTokenGenerator,
)
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio


class _FakeUserStore:
    def __init__(self, user: DevUser | None) -> None:
        self._user = user

    async def get_user(self, user_id: str) -> DevUser | None:
        if self._user and self._user.user_id == user_id:
            return self._user
        return None


def _make_user() -> DevUser:
    return DevUser(
        user_id="user_refresh_1",
        username="refresher",
        email="refresher@local.faultmaven",
        display_name="Refresher",
        created_at=datetime.now(timezone.utc),
    )


def _make_generator(revocation_store) -> HS256JWTTokenGenerator:
    return HS256JWTTokenGenerator(
        secret_key="unit-test-secret-key-please-ignore",
        revocation_store=revocation_store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _fake_request(user_store, revocation_store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store, token_revocation_store=revocation_store
            )
        )
    )


def _patches(generator):
    """Patch the route's generator factory + settings to use the test generator."""
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL,
            jwt_access_token_expire_minutes=60,
        )
    )
    return (
        patch.object(auth_routes, "_build_local_jwt_generator", return_value=generator),
        patch.object(auth_routes, "get_settings", return_value=settings_stub),
    )


async def test_refresh_returns_new_token_pair_and_rotates():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_generator(store)
    old_refresh = await generator.generate_refresh_token(user)

    request = _fake_request(_FakeUserStore(user), revocation_store=store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

    # New pair returned, both fields populated and not the old refresh token.
    assert result.access_token
    assert result.refresh_token
    assert result.refresh_token != old_refresh
    assert result.token_type == "bearer"
    assert result.expires_in == 60 * 60

    # Token response must not be cached (RFC 6749 §5.1).
    assert response.headers["Cache-Control"] == "no-store"

    # The new access token validates; the new refresh token is usable.
    assert await generator.validate_access_token(result.access_token) is not None
    assert await generator.validate_refresh_token(result.refresh_token) is not None

    # Rotation: the old refresh token is now revoked.
    assert await generator.validate_refresh_token(old_refresh) is None


async def test_refresh_rejects_reused_old_token_after_rotation():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_generator(store)
    old_refresh = await generator.generate_refresh_token(user)

    request = _fake_request(_FakeUserStore(user), revocation_store=store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

        # Replaying the now-revoked refresh token must be rejected.
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=old_refresh), request, response
            )
    assert exc.value.status_code == 401


async def test_refresh_rejects_an_access_token_in_place_of_refresh():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_generator(store)
    access_token = await generator.generate_access_token(user)

    request = _fake_request(_FakeUserStore(user), revocation_store=store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=access_token), request, response
            )
    assert exc.value.status_code == 401


async def test_refresh_rejects_when_user_no_longer_exists():
    user = _make_user()
    store = InMemoryRevocationStore()
    generator = _make_generator(store)
    refresh = await generator.generate_refresh_token(user)

    # User store no longer has the account.
    request = _fake_request(_FakeUserStore(None), revocation_store=store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        with pytest.raises(HTTPException) as exc:
            await auth_routes.refresh_tokens(
                TokenRefreshRequest(refresh_token=refresh), request, response
            )
    assert exc.value.status_code == 401
