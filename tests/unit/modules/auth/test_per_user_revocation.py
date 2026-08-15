"""Unit tests for per-user token revocation (#769).

Before #769, bulk per-user revocation revoked nothing: the admin endpoint
called ``RedisTokenManager.revoke_user_tokens``, which flipped ``is_revoked``
flags on opaque-token metadata that no request path reads and that JWTs are
never written into — so the endpoint answered 200 "Revoked all 0 tokens for
user" while every outstanding JWT kept authenticating until expiry.
``AuthService.revoke_user_tokens`` (called by the deactivate/delete, password
and role-change flows) was a documented no-op returning 0.

The fix is a per-user revocation watermark in the one deployment-wide store
(#767): revocation records *when* it happened, and every validate path
rejects tokens whose ``iat`` is at or before that instant.

These tests use the PRODUCTION store class (``RedisTokenRevocationStore`` over
FakeRedis), not a test double, and assert on the ENDPOINT RESPONSE as well as
the store — the original bug was precisely that a caller reported success for
a revocation that never happened.

Covered:
1. The admin endpoint actually invalidates outstanding tokens, and reports the
   watermark rather than a fabricated count.
2. The watermark covers tokens from every mint path (HS256 and RS256
   generators, and AuthService's own synchronous mint) — the completeness
   property a per-user JTI index could not guarantee.
3. Tokens minted AFTER the revocation still work (re-login is not broken).
4. Failure posture: the write propagates, so the endpoint 500s rather than
   confirming a revocation that did not land.
5. The two revocation arms (per-jti and per-user) are independent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from faultmaven.exceptions import ServiceError
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthService,
    TokenRevocationError,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"
pytestmark = pytest.mark.asyncio

SECRET = "unit-test-secret-key-please-ignore"
USER_ID = "user-769"


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _store(redis=None):
    return RedisTokenRevocationStore(
        redis if redis is not None else _fake_redis(),
        key_prefix="revoked:token:",
    )


def _generator(store):
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _rs256_generator(store):
    """The cloud/OAuth mint, the other half of the ``IJWTTokenGenerator`` set.

    Built with an ephemeral key pair — never a hardcoded one — so nothing
    committed here is a usable signing key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        RS256JWTTokenGenerator,
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    return RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _user(user_id: str = USER_ID):
    return SimpleNamespace(
        user_id=user_id,
        # Real user types all declare is_active; a stand-in that omits it
        # is not modelling a user. The mint gate refuses on absence.
        is_active=True,
        username="revoked-user",
        email="revoked@local.faultmaven",
        roles=["user"],
        organization_id="org-769",
    )


def _auth_service_settings():
    """Settings stub matching the HS256 generator's fixed iss/aud.

    Expiry appears on the auth half ONLY, as production declares it: that is
    the single source every minting path and the revocation watermark read
    (#888). A security half carrying the same field names is what let the two
    disagree.
    """
    return SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="local",
            jwt_refresh_token_expire_days=7,
            jwt_access_token_expire_minutes=60,
        ),
        security=SimpleNamespace(
            jwt_algorithm="HS256",
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
            token_revocation_prefix="revoked:token:",
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )


def _auth_service(store):
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_auth_service_settings(),
    ):
        return AuthService(revocation_store=store)


def _admin_request(auth_service):
    """Fake request wired the way production wires the admin endpoint."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_service=auth_service))
    )


class _FakeUserStore:
    """Resolves only the ids it was given, like the real store."""

    def __init__(self, *user_ids: str):
        self._ids = set(user_ids)

    async def get_user(self, user_id: str):
        return _user(user_id) if user_id in self._ids else None


class TestAdminEndpointActuallyRevokes:
    """The observable the original bug got wrong: the endpoint's own response.

    A store-only assertion would have passed against the broken
    implementation too, because the broken path wrote to a real (but unread)
    store. These assert the response AND that the token stops authenticating.
    """

    async def test_endpoint_invalidates_outstanding_token(self):
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)
        user = _user()

        access = await generator.generate_access_token(
            user, state_read_at=datetime.now(timezone.utc)
        )
        # Sanity: the token authenticates before revocation.
        assert await generator.validate_access_token(access) is not None

        response = await auth_routes.revoke_user_tokens(
            USER_ID,
            _admin_request(auth_service),
            _=user,
            user_store=_FakeUserStore(USER_ID),
        )

        # The token no longer authenticates on any path.
        assert await generator.validate_access_token(access) is None
        with pytest.raises(TokenRevocationError):
            await auth_service.verify_token_with_revocation_check(
                access, token_type="access"
            )

        # ...and the response describes what actually happened.
        assert response.message == "All tokens revoked for user"
        watermark = datetime.fromisoformat(response.revoked_before)
        assert watermark.tzinfo is not None

    async def test_response_carries_no_fabricated_token_count(self):
        """Regression: the old response claimed "Revoked all N tokens"."""
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        auth_service = _auth_service(store)

        response = await auth_routes.revoke_user_tokens(
            USER_ID,
            _admin_request(auth_service),
            _=_user(),
            user_store=_FakeUserStore(USER_ID),
        )

        assert not hasattr(response, "revoked_tokens")
        assert "0 tokens" not in response.message

    async def test_unknown_user_is_404_not_a_vacuous_success(self):
        """A watermark write succeeds for ANY string, so the id must be real.

        An admin who pastes a username, or mistypes an id, while containing a
        compromised account would otherwise get a "revoked" confirmation while
        the real account kept authenticating — the same false confirmation this
        endpoint was fixed for.
        """
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.revoke_user_tokens(
                "revoked-user",  # the username, not the user_id
                _admin_request(auth_service),
                _=_user(),
                user_store=_FakeUserStore(USER_ID),
            )

        assert exc_info.value.status_code == 404
        # The real user is untouched — the mistyped id revoked nothing of theirs.
        assert await generator.validate_access_token(access) is not None

    async def test_revocation_is_not_gated_on_the_user_lookup(self):
        """An auth-DB outage must not be able to suppress a revocation.

        ``DatabaseUserStore.get_user`` swallows its exceptions and returns
        None, so a DB failure is indistinguishable from an absent user (#703
        is exactly that DB freezing). If the revocation were gated on the
        lookup, a DB blip would answer "user not found" to an admin containing
        a live compromise, having revoked nothing. Revocation needs only
        Redis, so it must land on Redis alone.
        """
        from faultmaven.modules.auth.api import auth as auth_routes

        class _BrokenUserStore:
            """Stands in for the store swallowing a DB error into None."""

            async def get_user(self, user_id: str):
                return None

        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.revoke_user_tokens(
                USER_ID,
                _admin_request(auth_service),
                _=_user(),
                user_store=_BrokenUserStore(),
            )

        # The caller is told the lookup failed...
        assert exc_info.value.status_code == 404
        # ...but the tokens are genuinely dead regardless.
        assert await generator.validate_access_token(access) is None

    async def test_endpoint_500s_when_the_write_fails(self):
        """A failed revocation must never read as a successful one."""
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        auth_service = _auth_service(store)

        async def boom(user_id, revoked_at, ttl):
            raise ConnectionError("store down")

        store.revoke_user_tokens_before = boom

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.revoke_user_tokens(
                USER_ID,
                _admin_request(auth_service),
                _=_user(),
                user_store=_FakeUserStore(USER_ID),
            )
        assert exc_info.value.status_code == 500


class TestWatermarkCoversEveryMintPath:
    """Completeness: the property that motivated a watermark over a JTI index.

    FaultMaven mints tokens from three implementations, one of them
    synchronous. A watermark covers all of them without mint-time bookkeeping;
    an index would silently under-revoke whenever a mint path forgot to
    register while still reporting a complete revocation.
    """

    async def test_hs256_generator_tokens_are_revoked(self):
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        refresh = await generator.generate_refresh_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(access) is None
        assert await generator.validate_refresh_token(refresh) is None

    async def test_rs256_generator_tokens_are_revoked(self):
        store = _store()
        generator = _rs256_generator(store)
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        refresh = await generator.generate_refresh_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(access) is None
        assert await generator.validate_refresh_token(refresh) is None

    async def test_every_minter_emits_the_claims_the_watermark_needs(self):
        """The watermark matches on ``sub`` + ``iat``; a token missing either
        skips the per-user arm entirely and survives revocation.

        "Complete by construction" is only true while every mint path emits
        both, and keys ``sub`` to the same user_id the watermark is written
        under. Nothing in the type system enforces that, so pin it here: a new
        or altered minter that drops ``iat`` must fail this test rather than
        silently open a hole.

        Since #853 the ``IJWTTokenGenerator`` implementations are the whole set:
        ``AuthService``'s parallel mint path was dead and was removed. So the
        sweep covers BOTH of them. Covering HS256 alone would have let an RS256
        mint that dropped ``iat`` survive ``revoke_user_tokens`` with this test
        green — on the algorithm ``AUTH_MODE=oauth`` uses, i.e. the cloud
        deployment where per-user revocation matters most.
        """
        import jwt as jwt_lib

        store = _store()
        hs256 = _generator(store)
        rs256 = _rs256_generator(store)
        user = _user()

        tokens = {
            "hs256_access": await hs256.generate_access_token(
                user, state_read_at=datetime.now(timezone.utc)
            ),
            "hs256_refresh": await hs256.generate_refresh_token(
                user, state_read_at=datetime.now(timezone.utc)
            ),
            "rs256_access": await rs256.generate_access_token(
                user, state_read_at=datetime.now(timezone.utc)
            ),
            "rs256_refresh": await rs256.generate_refresh_token(
                user, state_read_at=datetime.now(timezone.utc)
            ),
        }

        for name, token in tokens.items():
            claims = jwt_lib.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )
            assert claims.get("iat") is not None, f"{name} emits no iat"
            assert claims.get("sub") == USER_ID, f"{name} sub is not the user_id"

    async def test_other_users_are_unaffected(self):
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        victim_token = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        bystander_token = await generator.generate_access_token(
            _user("user-other"), state_read_at=datetime.now(timezone.utc)
        )

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(victim_token) is None
        assert await generator.validate_access_token(bystander_token) is not None


class TestReLoginStillWorks:
    """The watermark must not lock a user out permanently.

    Time is controlled by placing the watermark in the past rather than by
    mocking the decode path, so these exercise the real generator, the real
    production store, and the real validation rule. ``iat`` has whole-second
    granularity, and the rule is ``iat <= watermark`` (fail-secure), so a
    token minted in the same second as the revocation is rejected on purpose.

    That rounding is why the login path clears the watermark outright
    (``clear_user_revocation``, covered below) rather than relying on the next
    second arriving: once ordinary logout became account-scoped, "retry a
    second later" stopped being an admin-path curiosity and became a routine
    logout→login sequence. The rule itself is unchanged — these still pin it.
    """

    async def test_token_minted_after_the_watermark_is_accepted(self):
        store = _store()
        generator = _generator(store)

        # A revocation that happened a minute ago.
        past = int(datetime.now(timezone.utc).timestamp()) - 60
        await store.revoke_user_tokens_before(USER_ID, past, ttl=3600)

        # The user logs back in; the fresh token is strictly newer.
        fresh = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        assert await generator.validate_access_token(fresh) is not None

    async def test_token_minted_before_the_watermark_is_still_rejected(self):
        """The contrast case, on the same clock: older tokens stay dead."""
        store = _store()
        generator = _generator(store)

        stale = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        future = int(datetime.now(timezone.utc).timestamp()) + 60
        await store.revoke_user_tokens_before(USER_ID, future, ttl=3600)

        assert await generator.validate_access_token(stale) is None


class TestRevocationArmsAreIndependent:
    """Per-jti and per-user revocation must not interfere."""

    async def test_per_jti_revocation_does_not_set_a_user_watermark(self):
        store = _store()
        generator = _generator(store)

        first = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        second = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        await generator.revoke_access_token(first)

        assert await generator.validate_access_token(first) is None
        # The user's other token must survive a single-token revocation.
        assert await generator.validate_access_token(second) is not None

    async def test_watermark_survives_independently_of_jti_entries(self):
        """The watermark lands even though nothing was written to ``jti:``.

        Probe the instant the call RETURNED, not a fresh clock read. A second
        read is wrong twice over. It is racy — the watermark is truncated to
        whole seconds (``int(revoked_at.timestamp())``) and the rule is
        ``issued_at <= watermark``, so whenever the integer second ticks
        between the revoke and the probe the probe is ``watermark + 1`` and the
        assertion is False. That is the observed bare ``assert False``: the
        identical head passed on rerun.

        It is also asserting something untrue. A token issued *after* a
        revocation is supposed to survive it — that is exactly what
        ``TestReLoginStillWorks`` pins, and what
        ``test_iat_at_the_watermark_is_revoked`` fixes the boundary of
        (``watermark + 1`` is NOT revoked). A "now" probe only passed because
        both clock reads usually truncated into the same second, so the two
        tests contradicted each other whenever the race lost. The revocation
        instant is the correct probe and states the real property: a token
        issued at or before it is revoked.
        """
        store = _store()
        auth_service = _auth_service(store)

        revoked_at = await auth_service.revoke_user_tokens(USER_ID)

        assert await store.is_user_revoked(USER_ID, int(revoked_at.timestamp()))
        assert await store.is_revoked("some-unrelated-jti") is False


class TestFreshLoginSupersedesTheWatermark:
    """A login completed after instant T is not "issued before T".

    The same-second case is the one that bites: the watermark is
    ``int(now.timestamp())`` and ``iat`` is floored the same way, so a sign-in
    landing in the same wall-clock second as a sign-out mints an access AND a
    refresh token that both fail ``iat <= watermark``. The login reports
    success and every subsequent request 401s, with no way back — the refresh
    token is dead too. On multi-replica deployments the window is not one
    second but the clock skew between the pod that wrote the watermark and the
    pod that mints.

    Fixed by clearing at the mint site rather than by weakening the comparison,
    which has to keep holding for the admin paths that have no login after
    them — and clearing ONLY a watermark that predates the login's own
    ``state_read_at``, so the #831 straddle (a revocation landing *during* the
    login's reads) still kills the pair it mints. Second granularity cannot
    tell those two apart, which is why the stored watermark keeps its fraction.
    """

    async def test_same_second_relogin_is_locked_out_without_the_clear(self):
        """The failure being fixed, pinned so the fix cannot be quietly undone."""
        store = _store()
        generator = _generator(store)

        now = datetime.now(timezone.utc)
        await store.revoke_user_tokens_before(USER_ID, now.timestamp(), ttl=3600)

        # Same second as the revocation — the sign-in immediately after a
        # sign-out, or any replica whose clock has not yet ticked past it.
        fresh = await generator.generate_access_token(_user(), state_read_at=now)

        assert await generator.validate_access_token(fresh) is None

    @staticmethod
    def _within_one_second(*offsets: float):
        """Instants sharing one integer second, all safely in the past.

        Anchored to the START of the previous whole second rather than to
        ``now``, so the sub-second offsets cannot straddle a second boundary
        (which would make these tests pass or fail on when they happened to
        run) and every instant stays ``<= now``, as ``_mint_instant`` requires.
        """
        anchor = float(int(datetime.now(timezone.utc).timestamp()) - 1)
        return tuple(
            datetime.fromtimestamp(anchor + offset, timezone.utc) for offset in offsets
        )

    async def test_clearing_first_lets_the_same_second_relogin_through(self):
        store = _store()
        generator = _generator(store)

        # The sign-out, then a sign-in whose reads begin after it completed —
        # inside the same integer second, the case second granularity cannot
        # express.
        revoked_at, state_read_at = self._within_one_second(0.1, 0.5)
        await store.revoke_user_tokens_before(USER_ID, revoked_at.timestamp(), ttl=3600)

        cleared = await store.clear_user_revocation_if_before(
            USER_ID, state_read_at.timestamp()
        )
        fresh = await generator.generate_access_token(
            _user(), state_read_at=state_read_at
        )

        assert cleared is True
        assert await generator.validate_access_token(fresh) is not None

    async def test_a_watermark_written_during_the_login_is_not_cleared(self):
        """The #831 straddle, which this must not undo.

        An admin persists a role change and revokes while the login is still
        reading the pre-change user row. The watermark postdates the login's
        capture, so it stays, and the pair the login mints stays dead — exactly
        what stamping ``iat`` from the capture is for. Same integer second as
        the case above, and the opposite outcome.
        """
        store = _store()
        generator = _generator(store)

        state_read_at, revoked_at = self._within_one_second(0.5, 0.9)
        await store.revoke_user_tokens_before(USER_ID, revoked_at.timestamp(), ttl=3600)

        cleared = await store.clear_user_revocation_if_before(
            USER_ID, state_read_at.timestamp()
        )
        straddling = await generator.generate_access_token(
            _user(), state_read_at=state_read_at
        )

        assert cleared is False
        assert await generator.validate_access_token(straddling) is None

    async def test_clear_is_scoped_to_the_one_user(self):
        """One user's login must not resurrect another's revoked tokens."""
        store = _store()
        past = datetime.now(timezone.utc).timestamp() - 60
        await store.revoke_user_tokens_before(USER_ID, past, ttl=3600)
        await store.revoke_user_tokens_before("other-user", past, ttl=3600)

        await store.clear_user_revocation_if_before(
            USER_ID, datetime.now(timezone.utc).timestamp()
        )

        assert await store.is_user_revoked(USER_ID, int(past)) is False
        assert await store.is_user_revoked("other-user", int(past)) is True

    async def test_clear_leaves_per_jti_revocations_alone(self):
        """The two arms stay independent: a login does not un-revoke a token
        that was revoked by identifier."""
        store = _store()
        past = datetime.now(timezone.utc).timestamp() - 60
        await store.revoke_user_tokens_before(USER_ID, past, ttl=3600)
        await store.add_revoked_token("jti-1", ttl=3600)

        await store.clear_user_revocation_if_before(
            USER_ID, datetime.now(timezone.utc).timestamp()
        )

        assert await store.is_revoked("jti-1") is True

    async def test_clear_is_a_no_op_on_an_unrevoked_user(self):
        store = _store()

        cleared = await store.clear_user_revocation_if_before(
            "never-revoked", datetime.now(timezone.utc).timestamp()
        )

        assert cleared is False
        assert await store.is_user_revoked("never-revoked", 0) is False

    async def test_a_malformed_watermark_is_left_alone_rather_than_deleted(self):
        """Deleting an unparseable watermark would turn a corrupt entry into no
        revocation at all. It is left for ``is_user_revoked`` to raise on, where
        each caller's error posture already decides what to do about it."""
        redis = _fake_redis()
        store = _store(redis)
        await redis.set(f"revoked:token:user:{USER_ID}", "not-a-number")

        cleared = await store.clear_user_revocation_if_before(
            USER_ID, datetime.now(timezone.utc).timestamp()
        )

        assert cleared is False
        assert await redis.get(f"revoked:token:user:{USER_ID}") == "not-a-number"

    async def test_a_pre_fraction_watermark_still_reads_and_clears(self):
        """Values written by an earlier build are plain integers."""
        redis = _fake_redis()
        store = _store(redis)
        past = int(datetime.now(timezone.utc).timestamp()) - 60
        await redis.set(f"revoked:token:user:{USER_ID}", str(past))

        assert await store.is_user_revoked(USER_ID, past) is True
        assert (
            await store.clear_user_revocation_if_before(
                USER_ID, datetime.now(timezone.utc).timestamp()
            )
            is True
        )


class TestStoreContract:
    """Direct unit coverage of the production store's watermark methods."""

    async def test_unknown_user_has_no_watermark(self):
        store = _store()
        assert await store.is_user_revoked("never-revoked", 1_700_000_000) is False

    async def test_iat_at_the_watermark_is_revoked(self):
        """Boundary: `iat == watermark` counts as revoked (fail-secure)."""
        store = _store()
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)

        assert await store.is_user_revoked(USER_ID, 1_700_000_000) is True
        assert await store.is_user_revoked(USER_ID, 1_699_999_999) is True
        assert await store.is_user_revoked(USER_ID, 1_700_000_001) is False

    async def test_watermark_uses_its_own_key_namespace(self):
        """A user watermark must not be readable as a revoked jti."""
        redis = _fake_redis()
        store = _store(redis)
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)

        assert await store.is_revoked(USER_ID) is False
        assert await redis.get(f"revoked:token:user:{USER_ID}") == "1700000000"

    async def test_a_crafted_jti_cannot_forge_a_user_watermark(self):
        """Security regression: jti values are attacker-controlled.

        RFC 7009 revocation (``POST /auth/oauth/revoke``) is unauthenticated
        and reads ``jti`` from a token decoded WITHOUT signature verification,
        so anyone can drive ``add_revoked_token`` with an arbitrary jti. If the
        two namespaces overlapped, a jti of ``user:<victim>`` would write the
        victim's watermark key with the literal body ``"revoked"`` — and the
        watermark read would then raise on ``int("revoked")``, disabling
        per-user revocation for that victim on the fail-open request path while
        locking them out of refresh on the fail-closed generator path.
        """
        redis = _fake_redis()
        store = _store(redis)
        victim = "victim-user"

        await store.add_revoked_token(f"user:{victim}", 3600)

        # The victim has no watermark, and reading it does not raise.
        assert await store.is_user_revoked(victim, 1_700_000_000) is False
        # The crafted entry landed in the jti namespace, where it is inert.
        assert await store.is_revoked(f"user:{victim}") is True
        assert await redis.get(f"revoked:token:user:{victim}") is None

    async def test_watermark_ttl_follows_the_operator_configured_expiry(self):
        """Regression: the TTL must track ``JWT_REFRESH_TOKEN_EXPIRY_DAYS``.

        That knob lands on ``settings.auth.jwt_refresh_token_expire_days``, the
        single source every generator is built from (#888). Reading anything
        else once capped the watermark at the 7-day default while refresh tokens
        lived for 30, and a revoked token resurrected on day 8.
        """
        redis = _fake_redis()
        store = _store(redis)

        settings = _auth_service_settings()
        settings.auth.jwt_refresh_token_expire_days = 30
        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=settings,
        ):
            auth_service = AuthService(revocation_store=store)
            await auth_service.revoke_user_tokens(USER_ID)

        ttl = await redis.ttl(f"revoked:token:user:{USER_ID}")
        assert ttl >= 30 * 86400 - 5

    async def test_later_revocation_overwrites_the_watermark(self):
        store = _store()
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_500, ttl=3600)

        assert await store.is_user_revoked(USER_ID, 1_700_000_400) is True

    async def test_watermark_ttl_outlives_the_longest_token(self):
        """A watermark that expired before the tokens it revokes would
        resurrect them, so the TTL must cover the refresh-token lifetime."""
        redis = _fake_redis()
        store = _store(redis)
        auth_service = _auth_service(store)

        await auth_service.revoke_user_tokens(USER_ID)

        ttl = await redis.ttl(f"revoked:token:user:{USER_ID}")
        assert ttl >= 7 * 86400 - 5


class TestServiceFailurePosture:
    """Writes propagate; a caller must never report an unrecorded revocation."""

    async def test_revoke_user_tokens_raises_on_store_failure(self):
        store = _store()
        auth_service = _auth_service(store)

        async def boom(user_id, revoked_at, ttl):
            raise ConnectionError("store down")

        store.revoke_user_tokens_before = boom

        with pytest.raises(ServiceError):
            await auth_service.revoke_user_tokens(USER_ID)

    async def test_revoke_user_tokens_raises_without_a_store(self):
        auth_service = _auth_service(None)

        with pytest.raises(ServiceError):
            await auth_service.revoke_user_tokens(USER_ID)

    async def test_userservice_flows_persist_before_revoking(self):
        """Ordering invariant: persist FIRST, revoke second.

        Revoking first opens a TOCTOU. During the gap the DB still holds the
        OLD password/roles/active flag, so a login landing in it authenticates
        against pre-change state and mints a token whose ``iat`` is AFTER the
        watermark — surviving the very revocation meant to kill it. Persisting
        first inverts that: anything minted in the gap has ``iat`` at or before
        the watermark and dies with it.

        The cost is accepted deliberately: a store-write failure leaves the
        change committed while returning an error. That is the #767 posture
        (never report a revocation that did not land), and it is strictly
        preferable to a window that hands out valid old-role credentials.
        """
        from unittest.mock import AsyncMock, MagicMock

        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
        )
        from faultmaven.modules.auth.domain.services.user_service import UserService

        order: list[str] = []

        repo = InMemoryUserRepository()
        real_save = repo.save

        async def tracking_save(user):
            order.append("save")
            return await real_save(user)

        repo.save = tracking_save

        auth_service = MagicMock()

        async def tracking_revoke(user_id):
            order.append("revoke")
            return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

        auth_service.revoke_user_tokens = tracking_revoke
        service = UserService(
            user_repo=repo,
            auth_service=auth_service,
            token_generator=_generator(_store()),
        )

        user = await service.register_user(
            email="ordering@local.faultmaven",
            password="Str0ng-P4ssw0rd!",
            full_name="Ordering Check",
        )

        order.clear()
        await service.change_password(
            user_id=user.user_id,
            current_password="Str0ng-P4ssw0rd!",
            new_password="An0ther-P4ssw0rd!",
        )
        assert order == ["save", "revoke"], "change_password must persist first"

        order.clear()
        await service.deactivate_user(user.user_id)
        assert order == ["save", "revoke"], "deactivate_user must persist first"

    async def test_reset_password_persists_before_revoking(self):
        """The ordering pin `reset_password` was missing.

        It is the sharpest threat model of the five: the flow exists precisely
        because credentials may be compromised, so a gap that mints a token
        authenticated by the OLD password is the worst case. Same code shape as
        the others today — the pin is here to catch a regression.
        """
        from unittest.mock import AsyncMock, MagicMock

        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
        )
        from faultmaven.modules.auth.domain.services.user_service import UserService

        order: list[str] = []
        repo = InMemoryUserRepository()
        real_save = repo.save

        async def tracking_save(user):
            order.append("save")
            return await real_save(user)

        auth_service = MagicMock()

        async def tracking_revoke(user_id):
            order.append("revoke")
            return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

        auth_service.revoke_user_tokens = tracking_revoke
        auth_service.get_revocation_reason = AsyncMock(return_value=None)

        # Reset tokens are signed by the deployment's token generator, which
        # holds its own key — the auth service is asked only about revocation
        # (#959), so no key fabrication on the double is needed or possible.
        redis = _fake_redis()
        service = UserService(
            user_repo=repo,
            auth_service=auth_service,
            token_generator=_rs256_generator(_store()),
            redis_client=redis,
        )

        user = await service.register_user(
            email="reset@local.faultmaven",
            password="Str0ng-P4ssw0rd!",
            full_name="Reset Check",
        )
        reset_token = await service.request_password_reset(email=user.email)

        repo.save = tracking_save
        order.clear()
        await service.reset_password(
            reset_token=reset_token, new_password="An0ther-P4ssw0rd!"
        )

        assert order == ["save", "revoke"], "reset_password must persist first"

    async def test_role_changes_persist_before_revoking(self):
        """Same ordering invariant on the role flows.

        These are the sharpest case: revoking before the save would let a
        login in the gap mint a token carrying the OLD (possibly elevated)
        role with an ``iat`` past the watermark.
        """
        from unittest.mock import MagicMock

        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
        )
        from faultmaven.modules.auth.domain.services.user_service import UserService

        order: list[str] = []
        repo = InMemoryUserRepository()
        real_save = repo.save

        async def tracking_save(user):
            order.append("save")
            return await real_save(user)

        repo.save = tracking_save

        auth_service = MagicMock()

        async def tracking_revoke(user_id):
            order.append("revoke")
            return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

        auth_service.revoke_user_tokens = tracking_revoke
        service = UserService(
            user_repo=repo,
            auth_service=auth_service,
            token_generator=_generator(_store()),
        )

        user = await service.register_user(
            email="roles@local.faultmaven",
            password="Str0ng-P4ssw0rd!",
            full_name="Role Check",
        )

        order.clear()
        await service.assign_role(
            user_id=user.user_id,
            role="admin",
            organization_id="org-769",
            admin_user_id="some-admin",
        )
        assert order == ["save", "revoke"], "assign_role must persist first"

        order.clear()
        await service.remove_role(
            user_id=user.user_id,
            role="admin",
            organization_id="org-769",
            admin_user_id="some-admin",
        )
        assert order == ["save", "revoke"], "remove_role must persist first"

    async def test_request_path_fails_open_on_store_read_failure(self):
        """Documented posture (#767): the request path prefers availability."""
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)
        access = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        async def boom(user_id, issued_at):
            raise ConnectionError("store down")

        store.is_user_revoked = boom

        claims = await auth_service.verify_token_with_revocation_check(
            access, token_type="access"
        )
        assert claims["sub"] == USER_ID
