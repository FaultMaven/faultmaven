"""Password-reset tokens obey the one revocation rule (#829).

Reset tokens were validated on their own path — signature, ``type``, and the
one-time ``password_reset:{jti}`` key — with no revocation check, so "revoke all
tokens for this user" left an outstanding reset link usable for its full hour,
and a deactivated account could still complete a reset.

Both arms of ``revocation_reason`` (per-jti and per-user watermark) are asserted
here through the real ``AuthService`` + ``UserService``, because the point of
the fix is that reset tokens are governed by the SAME rule as access and refresh
tokens rather than by a second, reset-specific cleanup path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pytest

from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
)
from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.utils.password import verify_password
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

SECRET = "unit-test-secret-key-please-ignore"
EMAIL = "reset@local.faultmaven"
OLD_PASSWORD = "Str0ng-P4ssw0rd!"
NEW_PASSWORD = "An0ther-P4ssw0rd!"


def _settings():
    """One settings shape for both services, so signing and verification agree."""
    return SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="local",
            jwt_access_token_expire_minutes=60,
            jwt_refresh_token_expire_days=7,
        ),
        security=SimpleNamespace(
            jwt_algorithm="HS256",
            jwt_access_token_expire_minutes=60,
            jwt_refresh_token_expire_days=7,
            jwt_issuer="faultmaven",
            jwt_audience="faultmaven-api",
            token_revocation_prefix="revoked:token:",
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _build():
    """Real AuthService + UserService sharing one revocation store."""
    store = InMemoryRevocationStore()
    settings = _settings()

    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        auth_service = AuthService(revocation_store=store)
    # HS256: the reset-token signing and verification keys are the same secret.
    auth_service._private_key = SECRET
    auth_service._public_key = SECRET

    with patch(
        "faultmaven.modules.auth.domain.services.user_service.get_settings",
        return_value=settings,
    ):
        user_service = UserService(
            user_repo=InMemoryUserRepository(),
            auth_service=auth_service,
            redis_client=_fake_redis(),
        )

    user = await user_service.register_user(
        email=EMAIL, password=OLD_PASSWORD, full_name="Reset Check"
    )
    return user_service, auth_service, store, user


class TestRevokedResetTokensAreRefused:
    """Both revocation arms reach the reset path."""

    async def test_token_issued_before_the_watermark_is_refused(self):
        """The containment action an admin believes they took must hold."""
        user_service, auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        await auth_service.revoke_user_tokens(user.user_id)

        with pytest.raises(AuthenticationError):
            await user_service.reset_password(reset_token, NEW_PASSWORD)

        stored = await user_service.user_repo.get(user.user_id)
        assert verify_password(OLD_PASSWORD, stored.hashed_password)

    async def test_token_with_a_revoked_jti_is_refused(self):
        user_service, _auth_service, store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)
        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]

        await store.add_revoked_token(jti, 3600)

        with pytest.raises(AuthenticationError):
            await user_service.reset_password(reset_token, NEW_PASSWORD)

    async def test_refusal_does_not_consume_the_one_time_key(self):
        """The revocation check runs before the token is burned.

        A store outage must not destroy a legitimate token, so the check sits
        ahead of the one-time key delete — which also means a refused attempt
        leaves the key intact.
        """
        user_service, auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)
        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]

        await auth_service.revoke_user_tokens(user.user_id)
        with pytest.raises(AuthenticationError):
            await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert await user_service.redis_client.get(f"password_reset:{jti}") is not None


class TestUnrevokedResetsStillWork:
    """The rule must not swallow legitimate resets."""

    async def test_token_issued_after_the_watermark_is_accepted(self):
        user_service, _auth_service, store, user = await _build()

        # Watermark strictly in the past: `iat` has whole-second granularity and
        # the rule is `iat <= watermark`, so a same-second mint is refused by
        # design.
        past = int(datetime.now(timezone.utc).timestamp()) - 5
        await store.revoke_user_tokens_before(user.user_id, past, ttl=3600)

        reset_token = await user_service.request_password_reset(email=EMAIL)
        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert verify_password(NEW_PASSWORD, updated.hashed_password)

    async def test_active_user_happy_path(self):
        user_service, _auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert verify_password(NEW_PASSWORD, updated.hashed_password)
        assert not verify_password(OLD_PASSWORD, updated.hashed_password)

    async def test_another_users_revocation_does_not_leak(self):
        """The watermark is keyed by `sub`; only that user's tokens die."""
        user_service, auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        await auth_service.revoke_user_tokens("some-other-user")

        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)
        assert verify_password(NEW_PASSWORD, updated.hashed_password)


class TestDeactivatedAccounts:
    """A reset must not resurrect an account someone deliberately disabled."""

    async def test_deactivated_user_cannot_complete_a_reset(self):
        user_service, _auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        # Flipped directly rather than via deactivate_user, which also writes a
        # watermark — that would refuse the token on the revocation arm and
        # leave this check unexercised.
        stored = await user_service.user_repo.get(user.user_id)
        stored.is_active = False
        await user_service.user_repo.save(stored)

        with pytest.raises(AuthenticationError) as exc_info:
            await user_service.reset_password(reset_token, NEW_PASSWORD)
        assert exc_info.value.error_code == "ACCOUNT_INACTIVE"

        stored = await user_service.user_repo.get(user.user_id)
        assert verify_password(OLD_PASSWORD, stored.hashed_password)
