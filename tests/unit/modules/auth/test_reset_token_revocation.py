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

import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pytest

from faultmaven.exceptions import ServiceError, ValidationException
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
)
from faultmaven.modules.auth.domain.services.user_service import (
    RESET_REFUSED_CODE,
    RESET_REFUSED_MESSAGE,
    UserService,
)
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


async def _build(store=None):
    """Real AuthService + UserService sharing one revocation store."""
    store = InMemoryRevocationStore() if store is None else store
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
        assert exc_info.value.error_code == RESET_REFUSED_CODE

        stored = await user_service.user_repo.get(user.user_id)
        assert verify_password(OLD_PASSWORD, stored.hashed_password)


class TestRevocationStateMustBeKnowable:
    """No store means no answer about revocation — refuse, do not assume."""

    async def test_missing_store_refuses_before_committing_the_password(self):
        """Fail-open here would be worse than useless.

        `reset_password` ends by revoking the user's tokens, which already
        raises without a store — but only AFTER the new password is committed.
        Refusing at the revocation check keeps the write from happening at all.
        """
        user_service, auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        auth_service._revocation_store = None

        with pytest.raises(ServiceError):
            await user_service.reset_password(reset_token, NEW_PASSWORD)

        stored = await user_service.user_repo.get(user.user_id)
        assert verify_password(OLD_PASSWORD, stored.hashed_password)


class TestTheOneTimeLinkSurvivesARefusedAttempt:
    """The link is consumed only by an attempt that actually resets."""

    async def test_weak_password_does_not_burn_the_link(self):
        user_service, _auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        with pytest.raises(ValidationException):
            await user_service.reset_password(reset_token, "weak")

        # The same link still works, which is the whole point of one attempt
        # not costing the user their only way back in.
        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)
        assert verify_password(NEW_PASSWORD, updated.hashed_password)

    async def test_deactivated_account_does_not_burn_the_link(self):
        user_service, _auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)
        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]

        stored = await user_service.user_repo.get(user.user_id)
        stored.is_active = False
        await user_service.user_repo.save(stored)

        with pytest.raises(AuthenticationError):
            await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert await user_service.redis_client.get(f"password_reset:{jti}") is not None

    async def test_a_successful_reset_consumes_the_link_exactly_once(self):
        """Reuse is refused by two independent mechanisms, in this order.

        The successful reset revokes the user's tokens, so the watermark rejects
        the reused token at the revocation check before the one-time gate is
        reached — the key is gone either way. Every refusal is the same
        observable now, so only the logs record which guard fired.
        """
        user_service, _auth_service, _store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)
        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]

        await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert await user_service.redis_client.get(f"password_reset:{jti}") is None
        with pytest.raises(AuthenticationError):
            await user_service.reset_password(reset_token, "Y3t-An0ther-P4ss!")

    async def test_reuse_without_a_watermark_hits_the_one_time_gate(self):
        """The gate itself, isolated from the watermark that usually precedes it."""
        user_service, _auth_service, store, user = await _build()
        reset_token = await user_service.request_password_reset(email=EMAIL)

        await user_service.reset_password(reset_token, NEW_PASSWORD)
        # Drop the watermark the successful reset wrote, so the one-time key is
        # the only thing standing between the token and a second use.
        store.user_watermarks.clear()

        with pytest.raises(AuthenticationError) as exc_info:
            await user_service.reset_password(reset_token, "Y3t-An0ther-P4ss!")
        assert exc_info.value.error_code == RESET_REFUSED_CODE


class TestEveryRefusalLooksIdentical:
    """One observable for every unusable link; the logs keep the distinction.

    Distinguishable refusals turn `reset_password` into an account oracle: the
    dummy token `request_password_reset` hands back for an unknown address would
    be identifiable by the error it provokes, defeating the anti-enumeration
    measure that produced it.
    """

    async def _refusal(self, caplog, make_token_and_state):
        """Run one refusal, returning its observable and its logged reason."""
        user_service, auth_service, store, user = await _build()
        token = await make_token_and_state(user_service, auth_service, store, user)

        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(AuthenticationError) as exc_info:
                await user_service.reset_password(token, NEW_PASSWORD)

        # structlog renders the event dict into the message, so the reason is
        # read back out of the emitted line rather than off a record attribute.
        reasons = [
            match.group(1)
            for record in caplog.records
            for match in [
                re.search(r"'refusal_reason': '([^']+)'", record.getMessage())
            ]
            if match
        ]
        return (exc_info.value.error_code, str(exc_info.value)), reasons

    async def _dummy(self, user_service, auth_service, store, user):
        """A token for an address with no account."""
        return user_service._generate_dummy_reset_token()

    async def _spent(self, user_service, auth_service, store, user):
        token = await user_service.request_password_reset(email=EMAIL)
        await user_service.reset_password(token, NEW_PASSWORD)
        store.user_watermarks.clear()  # isolate the one-time gate
        return token

    async def _revoked(self, user_service, auth_service, store, user):
        token = await user_service.request_password_reset(email=EMAIL)
        await auth_service.revoke_user_tokens(user.user_id)
        return token

    async def _inactive(self, user_service, auth_service, store, user):
        token = await user_service.request_password_reset(email=EMAIL)
        stored = await user_service.user_repo.get(user.user_id)
        stored.is_active = False
        await user_service.user_repo.save(stored)
        return token

    async def _unverifiable(self, user_service, auth_service, store, user):
        return "not-a-token"

    async def test_all_refusals_share_one_observable(self, caplog):
        observables = {}
        logged = {}
        for name in ("_dummy", "_spent", "_revoked", "_inactive", "_unverifiable"):
            observable, reasons = await self._refusal(caplog, getattr(self, name))
            observables[name] = observable
            logged[name] = reasons

        # Same code AND same message for every one of them.
        assert set(observables.values()) == {
            (RESET_REFUSED_CODE, RESET_REFUSED_MESSAGE)
        }, observables

        # ...while the logs still say which is which.
        distinct = {name: r for name, r in logged.items() if r}
        assert len(distinct) == len(logged), logged
        assert len({tuple(r) for r in distinct.values()}) == len(distinct), distinct

    async def test_the_message_names_no_account_state(self):
        """Belt and braces: the text itself must not leak what the code hides."""
        lowered = RESET_REFUSED_MESSAGE.lower()
        for leak in (
            "deactivat",
            "inactive",
            "not found",
            "no such",
            "already been used",
        ):
            assert leak not in lowered
