"""The boot gate for revocation storage (#828 delta review).

The defect this exists for is not hypothetical, it is the outcome of one very
ordinary operation. ``token_revocations`` is created by the single
``001_enterprise_baseline`` migration, edited in place as the campaign rule
requires. A standalone deployment that **upgrades its image instead of wiping**
is already stamped ``a1e0c17bd001``, so ``alembic upgrade head`` is a no-op and
the table never appears — and then:

* ``AuthService._is_revoked`` catches everything and returns ``False``, so every
  revoked token is ACCEPTED;
* the only signal is a log line, which rolls out of ``kubectl logs``.

Every earlier in-place edit to that baseline failed LOUDLY there. This one fails
open, on a security control, which is why it gets a gate rather than a warning.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.config.revocation_storage import (
    RevocationStorageUnavailableError,
    probe_revocation_storage,
    validate_revocation_storage,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    SqlTokenRevocationStore,
)


def _store(tmp_path, *, with_table: bool):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = {"done": False}

    @asynccontextmanager
    async def factory():
        if with_table and not created["done"]:
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

    return SqlTokenRevocationStore(session_factory=factory), engine


class TestTheGateRefusesWhatFailsOpen:
    async def test_a_missing_table_refuses_the_boot(self, tmp_path):
        store, engine = _store(tmp_path, with_table=False)
        try:
            with pytest.raises(RevocationStorageUnavailableError) as exc:
                await validate_revocation_storage(store)
        finally:
            await engine.dispose()

        message = str(exc.value)
        # The remediation has to name the trap, because the obvious move is a
        # no-op: this database is already stamped with the only migration.
        assert "fails OPEN" in message
        assert "alembic upgrade head" in message
        assert "fm-wipe-deployment --wipe" in message

    async def test_a_present_table_passes(self, tmp_path):
        store, engine = _store(tmp_path, with_table=True)
        try:
            await validate_revocation_storage(store)  # does not raise
        finally:
            await engine.dispose()

    async def test_it_is_a_deployment_coherence_failure(self, tmp_path):
        """Same class as the other boot refusals: the deployment contradicts
        what its configuration promises."""
        store, engine = _store(tmp_path, with_table=False)
        try:
            with pytest.raises(DeploymentCoherenceError):
                await validate_revocation_storage(store)
        finally:
            await engine.dispose()


class TestTheGateIsScopedToWhatItCanJudge:
    async def test_the_cache_store_is_not_probed(self):
        """Its backing is a client, proven at composition (``fakeredis_or_fail``,
        and ``get_async_redis_client``'s ping). Probing it here would duplicate
        a check that already refuses the boot, in a gate that would then own two
        rules."""
        import fakeredis.aioredis as fakeredis_aio

        store = RedisTokenRevocationStore(
            fakeredis_aio.FakeRedis(decode_responses=True), key_prefix="revoked:token:"
        )

        assert await probe_revocation_storage(store) is None

    async def test_an_unrecognised_store_is_left_alone(self):
        """A future implementation must not become an outage in a gate that
        cannot understand it — the same tolerance the operator preflight keeps."""

        class SomeFutureStore:
            pass

        assert await probe_revocation_storage(SomeFutureStore()) is None

    async def test_no_store_is_not_this_gate_s_finding(self):
        """Absence is already fatal at the composition root, which says so with
        context this function does not have."""
        await validate_revocation_storage(None)  # does not raise

    async def test_the_probe_writes_nothing(self, tmp_path):
        """A gate that wrote would leave a row behind on every boot."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import TokenRevocationModel

        store, engine = _store(tmp_path, with_table=True)
        try:
            await validate_revocation_storage(store)
            async with store._session_factory() as session:
                rows = await session.execute(
                    select(func.count(TokenRevocationModel.subject))
                )
                assert rows.scalar_one() == 0
        finally:
            await engine.dispose()
