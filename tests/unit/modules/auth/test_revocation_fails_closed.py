"""The request-path revocation check refuses when the store cannot answer (#1478).

``AuthService._is_revoked`` used to catch every exception and return ``False``.
A revoked token was therefore ACCEPTED whenever the store could not be read,
with a log line as the only signal — the silent failure of the exact control the
store exists to provide, and indistinguishable from the control working.

That was written when standalone's store was the in-process FakeRedis singleton,
which could not fail. #828/#1469 moved standalone's request-path check onto a
SQLite table, so the path became reachable there for the first time: a write
lock held past ``busy_timeout``, disk-full, the mid-migration window where
``token_revocations`` does not exist, or pool exhaustion on PostgreSQL.

What this module pins, in the order the failure travels:

1. **Both deployments' real stores, driven into a real failure**, refuse. Not a
   double raising a chosen class — the SQLite arm reads a database with no
   ``token_revocations`` table, and the Redis arm talks to a disconnected
   server, so the exception each produces is the one production would see.
   Standalone is SQLite and cloud is Redis, and a fix verified on one says
   nothing about the other.
1b. **A stored value that will not parse refuses the same way**, on both arms
   and by both routes into it (non-numeric text -> ``ValueError``, a ``NULL``
   column -> ``TypeError``). A row that exists and cannot be interpreted is
   the purest "we could not find out", and it used to answer a generic 401 —
   literally the sentence "your token was revoked" about a value nobody could
   read.
2. **The distinction is real**: the refusal is its own exception type carrying
   its own error code, so "your token was revoked" and "we could not find out"
   are not the same answer.
3. **A programming error is NOT laundered into it.** A ``TypeError`` in our own
   code must not be reported to a caller — or to whoever is paged — as
   "revocation state unknown". It still refuses, via the callers' catch-alls;
   it is simply not named or counted as a storage fault.
4. **It is counted**, so the frequency is measured rather than inferred from
   logs that roll.
5. **Every request path that checks revocation answers 503 with the distinct
   code** — all four of them, because the value of a distinct code is zero if
   one seam flattens it back into a 401 or swallows it into "unauthenticated".
"""

from __future__ import annotations

import errno
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import redis.exceptions as redis_exceptions
import sqlalchemy.exc as sa_exc
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import REVOCATION_STATE_UNKNOWN
from faultmaven.api.middleware.auth import get_current_user, get_current_user_optional
from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional as get_current_dev_user_optional,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.domain.services import auth_service as auth_service_module
from faultmaven.modules.auth.domain.services.auth_service import (
    STORE_READ_FAILURES,
    AuthenticationError,
    AuthService,
    RevocationStateUnknownError,
    TokenRevocationError,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    CorruptRevocationEntry,
    ITokenRevocationStore,
    SequentialRevocationState,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    SqlTokenRevocationStore,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.utils import (
    InMemoryRevocationStore,
    forge_access_token,
    request_with_authorization,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

USER_ID = "user-1478"
ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
SECRET = "test-secret-key-min-32-bytes!!!!!"
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"


# ============================================================
# Harness
# ============================================================


def _settings():
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


def _auth_service(store) -> AuthService:
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_settings(),
    ):
        return AuthService(revocation_store=store)


def _token(service: AuthService) -> str:
    return forge_access_token(
        service,
        user_id=USER_ID,
        enterprise_id=ENTERPRISE_ID,
        email="revoked@local.faultmaven",
        roles=["user"],
    )


class _RaisingStore(SequentialRevocationState, ITokenRevocationStore):
    """A store whose reads raise a caller-chosen exception.

    Subclasses the interface rather than duck-typing it, for the reason the
    house double does: a store that silently does not implement the contract is
    the #767 bug, and it must break loudly at construction rather than read as
    "not revoked".
    """

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def add_revoked_token(self, jti, ttl):  # pragma: no cover - unused
        raise self._exc

    async def is_revoked(self, jti):
        raise self._exc

    async def revoke_user_tokens_before(self, user_id, revoked_at, ttl):
        raise self._exc  # pragma: no cover - unused

    async def is_user_revoked(self, user_id, issued_at):
        raise self._exc  # pragma: no cover - reached only without a jti

    async def clear_user_revocation_if_before(self, user_id, instant):
        raise self._exc  # pragma: no cover - unused

    async def cleanup_expired(self):  # pragma: no cover - unused
        raise self._exc


def _operational_error() -> sa_exc.OperationalError:
    """The shape SQLAlchemy raises for a locked / absent / full SQLite file."""
    return sa_exc.OperationalError("SELECT 1", {}, Exception("database is locked"))


# ============================================================
# 1. Both deployments, real stores, real failures
# ============================================================


@asynccontextmanager
async def _sqlite_store_with_no_table(tmp_path):
    """``SqlTokenRevocationStore`` over a database that has never been migrated.

    The standalone mid-migration window, and the shape #828's boot gate exists
    for: the file is there, the table is not, so every read raises
    ``OperationalError: no such table: token_revocations``. Nothing is mocked —
    the exception is the driver's own.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        session = sessions()
        try:
            yield session
        finally:
            await session.close()

    try:
        yield SqlTokenRevocationStore(session_factory=factory)
    finally:
        await engine.dispose()


def _disconnected_redis_store() -> RedisTokenRevocationStore:
    """``RedisTokenRevocationStore`` over a server that refuses commands.

    ``fakeredis``'s own disconnected mode, so the failure is raised by the
    client the cloud arm actually speaks to and arrives as a genuine
    ``redis.exceptions.ConnectionError`` rather than one this test chose.
    """
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    server = fakeredis.FakeServer()
    # Set after construction: ``connected`` is an attribute of the server, not
    # a constructor argument (fakeredis 2.37). Every command against it then
    # raises ``redis.exceptions.ConnectionError`` from the client itself.
    server.connected = False
    return RedisTokenRevocationStore(
        fakeredis_aio.FakeRedis(server=server, decode_responses=True),
        key_prefix="revoked:token:",
    )


class TestBothDeploymentsRefuse:
    """Standalone is SQLite and cloud is Redis; the failure shapes differ.

    Parametrised over both production stores rather than over one double, so a
    posture fixed on one arm cannot be reported as fixed on the other.
    """

    async def test_the_standalone_sqlite_store_refuses(self, tmp_path):
        async with _sqlite_store_with_no_table(tmp_path) as store:
            service = _auth_service(store)
            token = _token(service)

            with pytest.raises(RevocationStateUnknownError) as exc_info:
                await service.verify_token_with_revocation_check(token)

        assert exc_info.value.kind == "OperationalError"
        assert isinstance(exc_info.value.__cause__, sa_exc.OperationalError)

    async def test_the_cloud_redis_store_refuses(self):
        service = _auth_service(_disconnected_redis_store())
        token = _token(service)

        with pytest.raises(RevocationStateUnknownError) as exc_info:
            await service.verify_token_with_revocation_check(token)

        assert exc_info.value.kind == "ConnectionError"
        assert isinstance(exc_info.value.__cause__, redis_exceptions.ConnectionError)

    async def test_a_working_store_still_answers_normally(self):
        """The positive control.

        Without it a test that refuses everything — including a store that is
        perfectly readable — would read as a pass.
        """
        store = InMemoryRevocationStore()
        service = _auth_service(store)
        token = _token(service)

        claims = await service.verify_token_with_revocation_check(token)
        assert claims["sub"] == USER_ID

        await store.add_revoked_token(claims["jti"], 600)
        with pytest.raises(TokenRevocationError):
            await service.verify_token_with_revocation_check(token)


@asynccontextmanager
async def _sqlite_store(tmp_path):
    """``SqlTokenRevocationStore`` over a real, migrated SQLite file."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        session = sessions()
        try:
            yield session
            await session.commit()
        finally:
            await session.close()

    try:
        yield SqlTokenRevocationStore(session_factory=factory), factory
    finally:
        await engine.dispose()


async def _write_corrupt_watermark(factory, subject: str, value) -> None:
    """Put an uninterpretable ``revoked_at`` on a LIVE watermark row.

    Raw SQL on purpose. SQLite's typing is dynamic, so a text body in a float
    column is a state the file can genuinely reach — a partial write, a
    hand-edited row, a restore from a differently-typed dump — and the ORM
    would coerce it away before it ever hit the column.
    """
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO token_revocations (scope, subject, revoked_at, "
                "expires_at) VALUES ('user', :s, :v, :e)"
            ),
            {
                "s": subject,
                "v": value,
                "e": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        )


class TestACorruptStoredValueIsAlsoUnknown:
    """A row that exists and cannot be read is "we could not find out" (#1478).

    This is the case the first cut of #1478 left unclassified. The catch in
    ``_is_revoked`` is narrowed to storage families, and a bare
    ``ValueError``/``TypeError`` is deliberately not one of them — so a corrupt
    watermark fell through and the mandatory auth dependency answered a generic
    401, which is the sentence "your token was revoked" about a value nobody
    could read. Exactly the confusion the ruling's distinct code exists to
    remove.

    Fixed at the PARSE, not in the tuple: the store raises
    ``CorruptRevocationEntry`` from a three-line ``try`` around
    ``int(float(...))``, where the context proves the cause. Both routes into
    the corruption are covered, because catching only ``ValueError`` would
    leave a ``NULL`` column arriving as ``TypeError`` still unclassified.
    """

    async def test_the_standalone_sqlite_store_refuses_a_text_watermark(self, tmp_path):
        """The ``ValueError`` route, through the batched query the request
        path actually calls (``revocation_state``)."""
        async with _sqlite_store(tmp_path) as (store, factory):
            await _write_corrupt_watermark(factory, USER_ID, "not-a-number")
            service = _auth_service(store)
            token = _token(service)

            with pytest.raises(RevocationStateUnknownError) as exc_info:
                await service.verify_token_with_revocation_check(token)

        assert exc_info.value.kind == "CorruptRevocationEntry"
        assert isinstance(exc_info.value.__cause__, CorruptRevocationEntry)

    async def test_the_standalone_sqlite_store_refuses_a_null_watermark(self, tmp_path):
        """The ``TypeError`` route — ``int(float(None))``.

        The half a ``ValueError``-only catch would have missed, which is why
        it is a test and not a line in a docstring.

        The table is built BY HAND with ``revoked_at`` nullable, because the
        model declares it ``nullable=False`` and a schema this code created
        therefore cannot hold a NULL — the first cut of this test tried and
        SQLite refused the insert. A schema this code did NOT create can hold
        one, and that is not hypothetical: #828's whole premise is the
        standalone deployment that upgraded its image instead of wiping,
        whose ``token_revocations`` does not match the model. So the guard is
        pinned where it is actually reachable — against a database we did not
        write, which is the only kind that can present this value.
        """
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE token_revocations ("
                        "  scope TEXT NOT NULL,"
                        "  subject TEXT NOT NULL,"
                        "  revoked_at REAL,"
                        "  expires_at TIMESTAMP NOT NULL,"
                        "  created_at TIMESTAMP,"
                        "  PRIMARY KEY (scope, subject))"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO token_revocations (scope, subject, "
                        "revoked_at, expires_at) VALUES ('user', :s, NULL, :e)"
                    ),
                    {
                        "s": USER_ID,
                        "e": datetime.now(timezone.utc) + timedelta(hours=1),
                    },
                )

            sessions = async_sessionmaker(engine, expire_on_commit=False)

            @asynccontextmanager
            async def factory():
                session = sessions()
                try:
                    yield session
                finally:
                    await session.close()

            service = _auth_service(SqlTokenRevocationStore(session_factory=factory))
            token = _token(service)

            with pytest.raises(RevocationStateUnknownError) as exc_info:
                await service.verify_token_with_revocation_check(token)
        finally:
            await engine.dispose()

        assert exc_info.value.kind == "CorruptRevocationEntry"
        assert isinstance(exc_info.value.__cause__.__cause__, TypeError), (
            "the TypeError half of the parse catch did not fire — a "
            "ValueError-only catch would leave this unclassified"
        )

    async def test_the_cloud_redis_store_refuses_a_text_watermark(self):
        """The cloud arm, with the body the #769 crafted-jti analysis names.

        A watermark key holding the literal ``"revoked"`` is the value that
        analysis says a namespace collision would write; the namespaces are
        separated so it cannot be written that way any more, but the read has
        to answer sensibly whatever put it there.
        """
        import fakeredis.aioredis as fakeredis_aio

        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        store = RedisTokenRevocationStore(redis, key_prefix="revoked:token:")
        await redis.set(f"revoked:token:user:{USER_ID}", "revoked")

        service = _auth_service(store)
        token = _token(service)

        with pytest.raises(RevocationStateUnknownError) as exc_info:
            await service.verify_token_with_revocation_check(token)

        assert exc_info.value.kind == "CorruptRevocationEntry"

    async def test_it_is_counted_with_its_own_kind(self, tmp_path):
        """Distinguishable in the metric too, not only in the status code."""
        async with _sqlite_store(tmp_path) as (store, factory):
            await _write_corrupt_watermark(factory, USER_ID, "not-a-number")
            service = _auth_service(store)
            token = _token(service)

            with patch.object(
                auth_service_module, "revocation_state_unknown_total"
            ) as counter:
                with pytest.raises(RevocationStateUnknownError):
                    await service.verify_token_with_revocation_check(token)

        counter.labels.assert_called_once_with(kind="CorruptRevocationEntry")
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_a_readable_watermark_on_the_same_row_still_revokes(self, tmp_path):
        """The positive control for this class.

        A test that refuses every watermark — including a perfectly good one —
        would pass every assertion above while having broken revocation
        outright.
        """
        async with _sqlite_store(tmp_path) as (store, _factory):
            service = _auth_service(store)
            token = _token(service)
            claims = await service.verify_token_with_revocation_check(token)
            assert claims["sub"] == USER_ID

            await store.revoke_user_tokens_before(USER_ID, claims["iat"] + 1, ttl=3600)
            with pytest.raises(TokenRevocationError):
                await service.verify_token_with_revocation_check(token)

    async def test_the_request_path_answers_the_distinct_503(self, tmp_path):
        """End to end, because the whole point is what an operator SEES.

        The domain exception is only half the delivery: the ruling's criterion
        is that "your token was revoked" and "we could not find out" stop
        looking identical at the boundary. So this drives the real corrupt
        store through the real mandatory auth dependency and checks the status
        and the error code, not the exception type.
        """
        async with _sqlite_store(tmp_path) as (store, factory):
            await _write_corrupt_watermark(factory, USER_ID, "not-a-number")
            service = _auth_service(store)
            token = _token(service)

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    request_with_authorization(f"Bearer {token}"),
                    credentials=None,
                    auth_service=service,
                )

        _assert_is_the_distinct_refusal(exc_info.value)

    async def test_the_direct_per_user_read_refuses_too(self, tmp_path):
        """``is_user_revoked`` is the third parse site.

        The request path goes through ``revocation_state``, so this one is
        only reachable from the contract suite and the sequential mixin — but
        it parses the same column, and a helper applied to two sites out of
        three is exactly the shape that drifts.
        """
        async with _sqlite_store(tmp_path) as (store, factory):
            await _write_corrupt_watermark(factory, USER_ID, "not-a-number")

            with pytest.raises(CorruptRevocationEntry):
                await store.is_user_revoked(USER_ID, 1_700_000_000)


class TestTheStoreReadFailureFamilies:
    """Every family named in ``STORE_READ_FAILURES``, and why each is there."""

    @pytest.mark.parametrize(
        "exc, kind",
        [
            pytest.param(_operational_error(), "OperationalError", id="sqlite-locked"),
            pytest.param(
                sa_exc.TimeoutError("QueuePool limit reached"),
                "TimeoutError",
                id="postgres-pool-exhausted",
            ),
            pytest.param(
                redis_exceptions.ConnectionError("Connection closed by server"),
                "ConnectionError",
                id="redis-connection",
            ),
            pytest.param(
                redis_exceptions.TimeoutError("Timeout reading from socket"),
                "TimeoutError",
                id="redis-timeout",
            ),
            pytest.param(
                redis_exceptions.BusyLoadingError("Redis is loading the dataset"),
                "BusyLoadingError",
                id="redis-loading",
            ),
            pytest.param(
                OSError(errno.ENOSPC, "No space left on device"),
                "OSError",
                id="disk-full",
            ),
            pytest.param(
                ConnectionError("store down"),
                "ConnectionError",
                id="builtin-connection",
            ),
            pytest.param(TimeoutError("read timed out"), "TimeoutError", id="timeout"),
        ],
    )
    async def test_it_refuses_and_names_the_kind(self, exc, kind):
        service = _auth_service(_RaisingStore(exc))
        token = _token(service)

        with pytest.raises(RevocationStateUnknownError) as exc_info:
            await service.verify_token_with_revocation_check(token)

        assert exc_info.value.kind == kind
        assert exc_info.value.__cause__ is exc

    def test_the_families_are_the_ones_the_stores_can_raise(self):
        """The tuple is a decision, so it is stated rather than inferred.

        Widening it back to ``Exception`` is the regression this whole module
        exists to prevent, and it would otherwise pass every behavioural test
        above. The corrupt-stored-value case is IN the tuple as its own type
        and the bare builtins are still OUT — that pair is the whole design,
        so both halves are asserted here rather than only the half that is
        easy to remember.
        """
        assert Exception not in STORE_READ_FAILURES
        assert BaseException not in STORE_READ_FAILURES
        for laundered in (TypeError, AttributeError, KeyError, ValueError):
            assert not issubclass(laundered, STORE_READ_FAILURES), (
                f"{laundered.__name__} is a programming error and must not be "
                "classified as a store read failure"
            )
        assert issubclass(sa_exc.SQLAlchemyError, STORE_READ_FAILURES)
        assert issubclass(redis_exceptions.RedisError, STORE_READ_FAILURES)
        assert issubclass(OSError, STORE_READ_FAILURES)
        assert issubclass(CorruptRevocationEntry, STORE_READ_FAILURES)
        # And it is its own type, not a ValueError subclass smuggling the
        # builtin in through the back door.
        assert not issubclass(CorruptRevocationEntry, (ValueError, TypeError))


# ============================================================
# 2 + 3. The distinction, and what must not be laundered into it
# ============================================================


class TestTheRefusalIsDistinguishable:
    def test_it_is_not_an_authentication_error(self):
        """Three call sites catch ``AuthenticationError`` and answer
        "unauthenticated". Subclassing it would put this condition straight
        back into the shape it exists to escape."""
        assert not issubclass(RevocationStateUnknownError, AuthenticationError)
        assert not issubclass(RevocationStateUnknownError, TokenRevocationError)
        assert not issubclass(TokenRevocationError, RevocationStateUnknownError)

    def test_it_carries_its_own_error_code(self):
        assert RevocationStateUnknownError.error_code == "REVOCATION_STATE_UNKNOWN"
        assert RevocationStateUnknownError.error_code == REVOCATION_STATE_UNKNOWN
        assert (
            getattr(TokenRevocationError, "error_code", None)
            != RevocationStateUnknownError.error_code
        )


class TestAProgrammingErrorIsNotLaundered:
    """A bug in our own code must not be reported as a storage fault.

    It still refuses the request — the callers' catch-alls see to that, and the
    accompanying request-path test pins it — but it is not named, and not
    counted, as something an operator should go and look at the database about.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(TypeError("revocation_state() takes 3 arguments"), id="type"),
            pytest.param(
                AttributeError("'Store' has no 'revocation_state'"), id="attr"
            ),
            pytest.param(KeyError("sub"), id="key"),
            pytest.param(ValueError("invalid literal for int()"), id="value"),
            pytest.param(RuntimeError("bound to a different event loop"), id="runtime"),
            pytest.param(ZeroDivisionError("division by zero"), id="arithmetic"),
        ],
    )
    async def test_it_propagates_as_itself(self, exc):
        service = _auth_service(_RaisingStore(exc))
        token = _token(service)

        with pytest.raises(type(exc)) as exc_info:
            await service.verify_token_with_revocation_check(token)

        assert exc_info.value is exc

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(TypeError("boom"), id="type"),
            pytest.param(AttributeError("boom"), id="attr"),
        ],
    )
    async def test_it_is_not_counted_as_a_storage_fault(self, exc):
        service = _auth_service(_RaisingStore(exc))
        token = _token(service)

        with patch.object(
            auth_service_module, "revocation_state_unknown_total"
        ) as counter:
            with pytest.raises(type(exc)):
                await service.verify_token_with_revocation_check(token)

        assert counter.labels.call_count == 0

    async def test_it_does_not_become_the_new_exception(self):
        """The negative of the whole section, stated once at the type level."""
        service = _auth_service(_RaisingStore(TypeError("boom")))
        token = _token(service)

        with pytest.raises(BaseException) as exc_info:
            await service.verify_token_with_revocation_check(token)

        assert not isinstance(exc_info.value, RevocationStateUnknownError)


# ============================================================
# 4. It is counted
# ============================================================


class TestTheRefusalIsCounted:
    async def test_the_counter_records_the_failure_kind(self):
        service = _auth_service(_RaisingStore(_operational_error()))
        token = _token(service)

        with patch.object(
            auth_service_module, "revocation_state_unknown_total"
        ) as counter:
            with pytest.raises(RevocationStateUnknownError):
                await service.verify_token_with_revocation_check(token)

        counter.labels.assert_called_once_with(kind="OperationalError")
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_a_readable_store_increments_nothing(self):
        """The counter measures storage faults, not traffic."""
        service = _auth_service(InMemoryRevocationStore())
        token = _token(service)

        with patch.object(
            auth_service_module, "revocation_state_unknown_total"
        ) as counter:
            await service.verify_token_with_revocation_check(token)

        assert counter.labels.call_count == 0


# ============================================================
# 5. Every request path answers the distinct refusal
# ============================================================


def _failing_auth_service() -> AsyncMock:
    """An ``AuthService`` double that reports the store unreadable.

    A double here, not a real service: these tests are about what each request
    seam DOES with the condition, and the condition's own production shapes are
    pinned above against the real stores.
    """
    service = AsyncMock()
    error = RevocationStateUnknownError(kind="OperationalError")
    service.verify_token_with_revocation_check.side_effect = error
    service.extract_user_from_token_with_revocation_check.side_effect = error
    return service


def _assert_is_the_distinct_refusal(exc: HTTPException) -> None:
    assert exc.status_code == 503, f"answered {exc.status_code}, not 503"
    assert exc.headers.get("x-error-code") == REVOCATION_STATE_UNKNOWN
    assert exc.headers.get("Retry-After") == "5"


class TestTheMandatoryAuthDependency:
    async def test_it_answers_the_distinct_refusal(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer some-token"),
                credentials=None,
                auth_service=_failing_auth_service(),
            )

        _assert_is_the_distinct_refusal(exc_info.value)

    async def test_a_revoked_token_still_answers_401(self):
        """The contrast that makes the distinct code worth having.

        Same dependency, same shape of failure to a client that only reads the
        status — and they must not be the same status, because the remedies are
        opposite: discard the credential, versus keep it and retry.
        """
        service = AsyncMock()
        service.extract_user_from_token_with_revocation_check.side_effect = (
            TokenRevocationError()
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request_with_authorization("Bearer revoked-token"),
                credentials=None,
                auth_service=service,
            )

        assert exc_info.value.status_code == 401
        assert exc_info.value.headers.get("x-error-code") is None


class TestTheOptionalAuthDependencies:
    """Both of them, because ``None`` means "anonymous" and that is a lie here.

    A route with optional auth would otherwise serve the request as though no
    credential had been presented, which hides a storage fault behind a 200.
    """

    async def test_the_middleware_one_refuses_rather_than_returning_none(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_optional(
                request_with_authorization("Bearer some-token"),
                credentials=None,
                auth_service=_failing_auth_service(),
            )

        _assert_is_the_distinct_refusal(exc_info.value)

    async def test_the_v1_dependency_refuses_rather_than_returning_none(self):
        """``api/v1/auth_dependencies`` is the seam most at risk.

        Its ``get_current_user_optional`` returns ``None`` for every failure and
        ``require_authentication`` then answers the same 401 a missing header
        gets — so an unreadable store would have been reported as "not signed
        in", which is the flattening #1478 exists to stop.
        """
        with pytest.raises(HTTPException) as exc_info:
            await get_current_dev_user_optional(
                request_with_authorization("Bearer some-token"),
                auth_service=_failing_auth_service(),
            )

        _assert_is_the_distinct_refusal(exc_info.value)


class TestTheTenantBinder:
    """The global dependency, which runs on EVERY request under multi-tenant."""

    async def test_it_refuses_rather_than_binding_the_unscoped_sentinel(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            lambda: BUILTIN_MULTI,
        )

        with pytest.raises(HTTPException) as exc_info:
            await bind_request_enterprise_context(
                request_with_authorization("Bearer some-token"),
                auth_service=_failing_auth_service(),
                organization_repository=None,
            )

        _assert_is_the_distinct_refusal(exc_info.value)

    async def test_it_does_not_let_the_condition_escape_as_a_500(self, monkeypatch):
        """An uncaught exception out of a global dependency is a 500, which
        says "FaultMaven is broken" about a condition that clears itself."""
        monkeypatch.setattr(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            lambda: BUILTIN_MULTI,
        )

        with pytest.raises(BaseException) as exc_info:
            await bind_request_enterprise_context(
                request_with_authorization("Bearer some-token"),
                auth_service=_failing_auth_service(),
                organization_repository=None,
            )

        # An HTTPException, not the raw domain exception: the latter would
        # leave the framework with nothing to map and become a 500.
        assert isinstance(exc_info.value, HTTPException)
