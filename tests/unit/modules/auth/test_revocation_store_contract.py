"""One revocation behaviour, held across every store that implements it (#828).

``ITokenRevocationStore`` now has TWO production implementations, because
"where does revocation state live" turned out to be a durability question:

- ``RedisTokenRevocationStore`` — cloud, where the cache is a real Redis that
  outlives the API process.
- ``SqlTokenRevocationStore`` — standalone, where it is the in-process
  FakeRedis singleton and does not, so an API restart used to resurrect every
  revoked-but-unexpired token.

Two implementations of one security rule is exactly the shape that drifts, and
the drift would be invisible: whichever deployment runs the store that lost a
rule is the one where revocation silently stops working. So every behavioural
test below is parametrised over BOTH classes, and
``test_every_production_store_is_covered_here`` fails if a third
implementation is added to ``faultmaven/`` without joining them.

That last guard names where it looked — the whole ``faultmaven`` package, by
source scan rather than by ``__subclasses__()``, which would only ever see the
classes this module happened to import.
"""

from __future__ import annotations

import ast
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import faultmaven
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    SqlTokenRevocationStore,
)

#: Every store this suite is a contract for. The names are asserted against a
#: scan of the package below, so this list cannot quietly fall behind.
COVERED_STORES = {"RedisTokenRevocationStore", "SqlTokenRevocationStore"}

USER_ID = "user-828"
JTI = "jti-828"


def _sqlite_session_factory(url: str):
    """A session factory over ``url``, creating the schema on first use."""
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = {"done": False}

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

    return factory, engine


def _redis_store():
    import fakeredis.aioredis as fakeredis_aio

    return RedisTokenRevocationStore(
        fakeredis_aio.FakeRedis(decode_responses=True),
        key_prefix="revoked:token:",
    )


def _sql_store():
    factory, _engine = _sqlite_session_factory("sqlite+aiosqlite:///:memory:")
    return SqlTokenRevocationStore(session_factory=factory)


@pytest.fixture(params=["redis", "sql"])
def store(request):
    """One store per implementation, built the way production builds it."""
    return _redis_store() if request.param == "redis" else _sql_store()


class TestPerTokenArm:
    async def test_an_unknown_jti_is_not_revoked(self, store):
        assert await store.is_revoked(JTI) is False

    async def test_a_revoked_jti_reads_back_revoked(self, store):
        await store.add_revoked_token(JTI, ttl=60)

        assert await store.is_revoked(JTI) is True


class TestPerUserArm:
    async def test_no_watermark_means_not_revoked(self, store):
        assert await store.is_user_revoked(USER_ID, int(time.time())) is False

    async def test_tokens_issued_before_the_watermark_are_revoked(self, store):
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now, ttl=60)

        assert await store.is_user_revoked(USER_ID, int(now) - 1) is True

    async def test_a_token_issued_in_the_same_second_is_revoked(self, store):
        """``iat`` has whole-second granularity and the comparison is ``<=``.

        Of the two rounding errors this leaves, honouring a token minted in the
        same second as a revocation is the dangerous one.
        """
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now, ttl=60)

        assert await store.is_user_revoked(USER_ID, int(now)) is True

    async def test_tokens_issued_after_the_watermark_still_work(self, store):
        """Re-login is not broken by a revocation."""
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now, ttl=60)

        assert await store.is_user_revoked(USER_ID, int(now) + 5) is False

    async def test_a_later_revocation_overwrites_an_earlier_one(self, store):
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now - 100, ttl=60)
        await store.revoke_user_tokens_before(USER_ID, now, ttl=60)

        assert await store.is_user_revoked(USER_ID, int(now) - 1) is True


@pytest.mark.slow
class TestEntriesStopRevokingAtTheirDeadline:
    """TTL is the revocation RULE, not a cleanup detail.

    Load-bearing for the SQL store in a way it is not for Redis: there is no
    server deleting the row, so an elapsed entry stops revoking only because
    every read filters on ``expires_at``. Remove that filter and revocations
    become permanent — which fails safe, and is therefore exactly the kind of
    bug nothing else would catch.

    One second of real time, once, because Redis TTL granularity is a second
    and ``SETEX`` refuses anything smaller. Marked ``slow`` for that reason.
    """

    async def test_both_arms_expire(self, store):
        import asyncio

        now = time.time()
        await store.add_revoked_token(JTI, ttl=1)
        await store.revoke_user_tokens_before(USER_ID, now, ttl=1)
        assert await store.is_revoked(JTI) is True
        assert await store.is_user_revoked(USER_ID, int(now)) is True

        await asyncio.sleep(1.2)

        assert await store.is_revoked(JTI) is False
        assert await store.is_user_revoked(USER_ID, int(now)) is False


class TestTheTwoArmsCannotCollide:
    """A jti may not address a user's watermark.

    The jti arrives inside a token submitted to ``POST /auth/oauth/revoke``,
    which RFC 7009 makes unauthenticated by design, so a jti spelled
    ``user:<victim>`` must not be able to write or clear that victim's
    watermark whatever else changes upstream.
    """

    async def test_a_jti_shaped_like_a_user_key_does_not_revoke_that_user(self, store):
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now, ttl=60)
        await store.add_revoked_token(f"user:{USER_ID}", ttl=60)

        # The watermark is untouched...
        assert await store.is_user_revoked(USER_ID, int(now) - 1) is True
        # ...and the crafted jti did not become one.
        assert await store.is_user_revoked(f"user:{USER_ID}", int(now)) is False

    async def test_revoking_a_user_does_not_revoke_a_like_named_jti(self, store):
        await store.revoke_user_tokens_before(USER_ID, time.time(), ttl=60)

        assert await store.is_revoked(USER_ID) is False


class TestClearIsConditionalAndAtomic:
    """A fresh authentication supersedes a watermark it postdates — only that.

    One statement server-side in both implementations, so a revocation landing
    between a read and a delete cannot be cleared on the strength of the
    PREVIOUS watermark (the #831 straddle).
    """

    async def test_a_watermark_predating_the_instant_is_cleared(self, store):
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now - 1, ttl=60)

        assert await store.clear_user_revocation_if_before(USER_ID, now) is True
        assert await store.is_user_revoked(USER_ID, int(now) - 5) is False

    async def test_a_watermark_at_or_after_the_instant_survives(self, store):
        now = time.time()
        await store.revoke_user_tokens_before(USER_ID, now + 1, ttl=60)

        assert await store.clear_user_revocation_if_before(USER_ID, now) is False
        assert await store.is_user_revoked(USER_ID, int(now)) is True

    async def test_clearing_an_absent_watermark_reports_nothing_cleared(self, store):
        assert (
            await store.clear_user_revocation_if_before(USER_ID, time.time()) is False
        )


class TestSurvivesARestart:
    """The #828 done-when: a restart preserves revocations.

    A restart is modelled the only honest way — the store object, its session
    factory AND its engine are all discarded, and a second store is built over
    the same database file. Nothing carries over except what was written down.

    Deliberately NOT parametrised: this is the property the two stores do not
    share, and asserting it of the Redis arm over FakeRedis would assert the
    opposite of the defect. Cloud's durability is a real Redis's job.
    """

    async def test_a_revoked_jti_is_still_revoked_after_a_restart(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path / 'revocations.db'}"

        factory, engine = _sqlite_session_factory(url)
        await SqlTokenRevocationStore(session_factory=factory).add_revoked_token(
            JTI, ttl=3600
        )
        await engine.dispose()

        restarted_factory, restarted_engine = _sqlite_session_factory(url)
        restarted = SqlTokenRevocationStore(session_factory=restarted_factory)
        try:
            assert await restarted.is_revoked(JTI) is True
        finally:
            await restarted_engine.dispose()

    async def test_a_user_watermark_survives_a_restart(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path / 'revocations.db'}"
        revoked_at = time.time()

        factory, engine = _sqlite_session_factory(url)
        await SqlTokenRevocationStore(
            session_factory=factory
        ).revoke_user_tokens_before(USER_ID, revoked_at, ttl=3600)
        await engine.dispose()

        restarted_factory, restarted_engine = _sqlite_session_factory(url)
        restarted = SqlTokenRevocationStore(session_factory=restarted_factory)
        try:
            assert await restarted.is_user_revoked(USER_ID, int(revoked_at)) is True
            # And a token minted after it still works, across the restart too.
            assert (
                await restarted.is_user_revoked(USER_ID, int(revoked_at) + 5) is False
            )
        finally:
            await restarted_engine.dispose()

    async def test_the_in_process_cache_store_does_NOT_survive_one(self):
        """The defect, stated as a property, so the fix cannot be mistaken for
        one FakeRedis was always going to give us.

        A new process gets a new FakeRedis. If this ever passes, FakeRedis has
        grown persistence and the deployment split above is worth revisiting.
        """
        first = _redis_store()
        await first.add_revoked_token(JTI, ttl=3600)
        assert await first.is_revoked(JTI) is True

        assert await _redis_store().is_revoked(JTI) is False


class TestTheSqlStoreKeepsItselfBounded:
    async def test_a_write_reclaims_entries_that_can_no_longer_revoke(self):
        """No scheduled job sweeps this table; writes do it, so it is bounded
        by construction rather than by remembering to wire a sweeper."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import TokenRevocationModel

        factory, _engine = _sqlite_session_factory("sqlite+aiosqlite:///:memory:")
        store = SqlTokenRevocationStore(session_factory=factory)

        await store.add_revoked_token("live", ttl=3600)
        # An entry whose deadline has passed, written directly: the store's own
        # API cannot produce one (SETEX refuses a non-positive TTL, and every
        # revoke path returns early rather than writing one), so the row is
        # staged the way time would leave it.
        async with factory() as session:
            session.add(
                TokenRevocationModel(
                    scope="jti",
                    subject="stale",
                    revoked_at=0.0,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
            )
        assert await store.is_revoked("stale") is False

        # The next write reclaims it.
        await store.add_revoked_token("another", ttl=3600)

        async with factory() as session:
            rows = await session.execute(
                select(func.count(TokenRevocationModel.subject))
            )
            assert rows.scalar_one() == 2  # "live" and "another"; "stale" is gone
        assert await store.is_revoked("live") is True


def _stores_declared_in_package() -> set[str]:
    """Every class in ``faultmaven/`` declaring ``ITokenRevocationStore`` as a base.

    Source scan, not ``__subclasses__()``: the latter reports only what this
    process has imported, so a new store in a module nothing here touches would
    be invisible to it — precisely the case this guard exists for.
    """
    root = Path(faultmaven.__file__).parent
    found = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                name = (
                    base.id
                    if isinstance(base, ast.Name)
                    else getattr(base, "attr", None)
                )
                if name == "ITokenRevocationStore":
                    found.add(node.name)
    return found


def test_every_production_store_is_covered_here():
    """A third implementation must join this contract suite, not slip past it."""
    declared = _stores_declared_in_package()

    assert declared, (
        "The scan found no ITokenRevocationStore implementations at all, which "
        "means it is looking in the wrong place — not that none exist."
    )
    assert declared == COVERED_STORES, (
        f"ITokenRevocationStore implementations in faultmaven/: {sorted(declared)}; "
        f"covered by this contract suite: {sorted(COVERED_STORES)}. "
        "Add the new store to the `store` fixture and to COVERED_STORES."
    )
