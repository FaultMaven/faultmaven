"""An API restart must not resurrect revoked tokens (#828).

The defect, end to end: in standalone the revocation store was built over the
in-process FakeRedis singleton, which has no persistence. Logout, OAuth
``/revoke``, admin revoke-tokens, password change and role downgrade all wrote
there — so restarting the API brought every revoked-but-unexpired token back
for the remainder of its natural life. Account *deactivation* survived (it is a
database column), which is why the live gap was revocation of accounts that
stay active.

These tests drive the PRODUCTION composition — ``create_token_revocation_store``,
``AuthService`` and a real generator — rather than a store in isolation, because
the bug was in the wiring, not in either store. The restart is modelled the way
a new process experiences one: the FakeRedis singleton is discarded and the
store is rebuilt from the factory.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.container.providers.services import create_token_revocation_store
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthService,
    TokenRevocationError,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    SqlTokenRevocationStore,
)

SECRET = "unit-test-secret-key-please-ignore"
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"
USER_ID = "user-828"


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


class _NotFakeRedis:
    """Stands in for a real Redis client — anything not from ``fakeredis``."""


def _user():
    return SimpleNamespace(
        user_id=USER_ID,
        is_active=True,
        username="revoked-user",
        email="revoked@local.faultmaven",
        roles=["user"],
        organization_id=None,
    )


@pytest.fixture
def durable_db(tmp_path, monkeypatch):
    """Point the store's default session factory at a throwaway SQLite FILE.

    A file, not ``:memory:``: the whole question is whether state outlives the
    process that wrote it, and an in-memory database cannot answer it. Returns
    a callable that rebinds the module attribute to a FRESH engine — which is
    what a restart does, and what makes the second store a genuinely new
    reader.
    """
    import faultmaven.modules.auth.infrastructure.stores.token_revocation_store as mod

    url = f"sqlite+aiosqlite:///{tmp_path / 'revocations.db'}"
    created = {"done": False}
    engines = []

    def rebind():
        engine = create_async_engine(url)
        engines.append(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def factory():
            if not created["done"]:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                created["done"] = True
            session = sessions()
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        monkeypatch.setattr(mod, "get_db_session", factory)

    rebind()
    yield rebind

    # Every engine this fixture opened, the pre-restart ones included: their
    # only remaining reference is this list. ``sync_engine.dispose()`` is the
    # synchronous form, usable from a sync teardown; an undisposed async
    # engine leaves its pool and aiosqlite's thread alive for the session.
    for engine in engines:
        engine.sync_engine.dispose()


def _settings():
    """The settings shape production reads here, as the sibling #769 suite
    builds it: expiry on the auth half only, the HS256 secret on the security
    half beside the revocation key prefix."""
    return SimpleNamespace(
        is_cloud=False,
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


def _boot(settings, cache_client):
    """Everything the composition root builds for revocation, in order."""
    store = create_token_revocation_store(settings, cache_client=cache_client)
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        service = AuthService(revocation_store=store)
    generator = HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    return store, service, generator


class TestTheStoreIsChosenByConfigurationNotByProbe:
    """Which store a deployment gets must be the SAME one every boot.

    Keying on ``is_fakeredis(cache_client)`` looked more precise and was worse:
    ``get_async_redis_client`` substitutes FakeRedis whenever real Redis fails
    its ping (standalone warns rather than raising), and
    ``SKIP_SERVICE_CHECKS=true`` substitutes it outright — so a standalone
    deployment with real Redis resolved the SQL store for that process's life,
    could not see anything already in Redis, and flipped back on the next
    healthy boot, orphaning whatever it wrote (#828 review). Configuration
    cannot flap.
    """

    def test_standalone_gets_the_durable_store(self):
        store = create_token_revocation_store(_settings(), cache_client=_fake_redis())

        assert isinstance(store, SqlTokenRevocationStore)

    def test_cloud_keeps_the_redis_store(self):
        """Its cache is an external service that outlives the pod, and it is
        the only store read on the authenticated request path."""
        settings = _settings()
        settings.is_cloud = True

        store = create_token_revocation_store(settings, cache_client=_NotFakeRedis())

        assert isinstance(store, RedisTokenRevocationStore)

    @pytest.mark.parametrize(
        "make_cache",
        [
            pytest.param(_fake_redis, id="booted-while-redis-was-down"),
            pytest.param(_NotFakeRedis, id="booted-while-redis-was-up"),
        ],
    )
    def test_one_deployment_resolves_one_store_whatever_the_cache_turned_out_to_be(
        self, make_cache
    ):
        """The regression itself: a Redis flap must not move the store.

        Both arms are the SAME standalone deployment — one booted while Redis
        was answering, one while it was not. If the store differs, the
        revocations one boot wrote are invisible to the next.
        """
        store = create_token_revocation_store(_settings(), cache_client=make_cache())

        assert isinstance(store, SqlTokenRevocationStore)

    def test_cloud_without_a_cache_does_not_silently_write_to_the_wrong_place(self):
        """A cloud pod with no cache client is a composition error, and cloud
        never reaches it — ``fakeredis_or_fail`` refuses the boot first. It
        falls back to the durable store rather than to nothing, because the
        alternative is a store with no backing at all (#767)."""
        settings = _settings()
        settings.is_cloud = True

        store = create_token_revocation_store(settings, cache_client=None)

        assert isinstance(store, SqlTokenRevocationStore)


class TestARestartPreservesRevocations:
    """The #828 done-when, through the production composition."""

    async def test_a_per_user_revocation_survives_a_restart(self, durable_db):
        settings = _settings()
        _store, service, generator = _boot(settings, _fake_redis())
        token = await generator.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        await service.revoke_user_tokens(USER_ID)
        with pytest.raises(TokenRevocationError):
            await service.verify_token_with_revocation_check(token, "access")

        # --- restart: a new process, a new FakeRedis, a new store ---
        durable_db()
        _store2, service2, _ = _boot(settings, _fake_redis())

        with pytest.raises(TokenRevocationError):
            await service2.verify_token_with_revocation_check(token, "access")

    async def test_a_revoked_refresh_token_stays_revoked_across_a_restart(
        self, durable_db
    ):
        """The per-jti arm: logout and OAuth ``/revoke`` write here."""
        import jwt

        settings = _settings()
        _store, _service, generator = _boot(settings, _fake_redis())
        refresh = await generator.generate_refresh_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )
        await generator.revoke_refresh_token(refresh)
        jti = jwt.decode(refresh, options={"verify_signature": False})["jti"]

        durable_db()
        store2, _service2, _ = _boot(settings, _fake_redis())

        assert await store2.is_revoked(jti) is True

    @pytest.mark.slow
    async def test_a_token_minted_after_the_revocation_still_works_afterwards(
        self, durable_db
    ):
        """The restart must preserve the revocation, not become one.

        A watermark that survived as "revoke everything" would lock the account
        out at every restart — the failure a durability fix can introduce while
        looking correct.
        """
        settings = _settings()
        _store, service, generator = _boot(settings, _fake_redis())
        await service.revoke_user_tokens(USER_ID)

        durable_db()
        _store2, service2, generator2 = _boot(settings, _fake_redis())
        # Past the whole-second `iat <= watermark` comparison. ``asyncio``'s
        # sleep, not the blocking one: this is an async test, and blocking
        # the loop stalls every other task the session is running.
        await asyncio.sleep(1.1)
        fresh = await generator2.generate_access_token(
            _user(), state_read_at=datetime.now(timezone.utc)
        )

        claims = await service2.verify_token_with_revocation_check(fresh, "access")
        assert claims["sub"] == USER_ID
