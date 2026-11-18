"""#2050: a full-precision basis, a final watermark stamp, and no clear.

``iat`` is whole seconds, so a sign-in in the same second as a sign-out used to
mint a pair that was dead on arrival, and only ``POST /auth/login`` dodged it by
clearing the watermark — which revived every token minted before it. Now every
token carries ``fm_basis`` (the instant ``iat`` is stamped from, unfloored), the
watermark is compared against it at full precision, and nothing clears.

A precise compare needs the stored watermark to postdate every capture that
missed its write, so ``AuthService.revoke_user_tokens`` writes ``W1 = now`` and
then, once that write has returned, ``W2 = now + REVOCATION_CLOCK_ALLOWANCE``,
again until a final stamp lands within the allowance of its capture.

Instants are injected, never read off the wall clock: ``T`` is a whole second
safely in the past (``_mint_instant`` clamps a future capture and PyJWT refuses
a future ``iat``), the revoker's clock is a patched sequence, and every mint is
handed its ``state_read_at``. Every pin runs on both store arms, and the
token-level ones on both generators. The claim is spelled ``"fm_basis"`` here,
not imported, so the wire name is pinned too.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.api.operator_user_scope import OperatorUserScope
from faultmaven.config.settings import AuthMode
from faultmaven.exceptions import ServiceError
from faultmaven.infrastructure.persistence.models import TokenRevocationModel
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import (
    DevLoginRequest,
    TokenRefreshRequest,
)
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services import auth_service as auth_module
from faultmaven.modules.auth.domain.services import user_service as user_module
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    CorruptRevocationEntry,
    HS256JWTTokenGenerator,
    RevocationBasis,
    RS256JWTTokenGenerator,
    revocation_reason,
)
from faultmaven.modules.auth.domain.services.sso_login_service import SSOLoginService
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    SqlTokenRevocationStore,
)
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"
SECRET = "unit-test-secret-key-please-ignore"
USER_ID = "user-2050"
TTL = 3600

#: ``REVOCATION_CLOCK_ALLOWANCE`` in seconds; the instants below assume it
#: (``test_the_allowance_these_instants_assume``).
A = 0.1


def _anchor() -> float:
    """``T``: a whole second 30s in the past, so ``T + 0.9`` shares its second."""
    return float(int(time.time()) - 30)


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


# Both store arms.


@pytest.fixture(params=["redis", "sql"])
async def store(request):
    if request.param == "redis":
        import fakeredis.aioredis as fakeredis_aio

        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        yield RedisTokenRevocationStore(redis, key_prefix="revoked:token:")
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokenRevocationModel.__table__.create)
    yield SqlTokenRevocationStore(session_factory=async_sessionmaker(engine))
    await engine.dispose()


async def _write_raw_watermark(store, value) -> None:
    """Put ``value`` in the user's watermark exactly as stored, bypassing the write."""
    if isinstance(store, RedisTokenRevocationStore):
        await store.redis.set(f"revoked:token:user:{USER_ID}", value, ex=TTL)
        return
    async with store._session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO token_revocations (scope, subject, revoked_at, "
                "expires_at) VALUES ('user', :s, :v, :e)"
            ),
            {"s": USER_ID, "v": value, "e": datetime.now(timezone.utc) + timedelta(1)},
        )
        await session.commit()


# Both generators, the revoker, its clock.

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE = _KEY.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
).decode()
_PUBLIC = (
    _KEY.public_key()
    .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
_LIFETIMES = dict(
    access_token_expire_minutes=60,
    refresh_token_expire_days=7,
    issuer=ISSUER,
    audience=AUDIENCE,
)


def _hs256(store):
    return HS256JWTTokenGenerator(
        secret_key=SECRET, revocation_store=store, **_LIFETIMES
    )


def _rs256(store):
    return RS256JWTTokenGenerator(
        private_key=_PRIVATE, public_key=_PUBLIC, revocation_store=store, **_LIFETIMES
    )


GENERATORS = {"hs256": _hs256, "rs256": _rs256}


def _user():
    return DevUser(
        user_id=USER_ID,
        username="basis",
        email="basis@local.faultmaven",
        display_name="Basis",
        created_at=datetime.now(timezone.utc),
    )


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


def _auth_service(store):
    with patch.object(auth_module, "get_settings", return_value=_settings()):
        return auth_module.AuthService(revocation_store=store)


@contextmanager
def _revoker_clock(*instants: float, monotonic: tuple[float, ...] = (0.0,)):
    """The revoker reads ``now`` from ``instants``, in order (W1, then each final
    stamp's base), and its monotonic clock from ``monotonic`` (the last reading
    repeats, so by default every final stamp lands the instant it is taken)."""
    readings = iter(_at(i) for i in instants)
    ticks = list(monotonic)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(readings)

    def _monotonic():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    with (
        patch.object(auth_module, "datetime", _Clock),
        patch.object(
            auth_module, "time", SimpleNamespace(monotonic=_monotonic), create=True
        ),
    ):
        yield


class _Holding:
    """The store, with hooks run before or after its Nth watermark write lands
    (a callable means the first), and the writes numbered in ``fail`` failing.

    Models a mint that reads revocation state while a stamp is in flight
    (before it lands) or between two stamps (after one lands).
    """

    def __init__(self, store, *, before=None, after=None, fail=()):
        self._store = store
        self._before = before if isinstance(before, dict) else {1: before}
        self._after = after if isinstance(after, dict) else {1: after}
        self._fail = set(fail)
        self.writes: list[float] = []

    async def revoke_user_tokens_before(self, user_id, revoked_at, ttl, **kwargs):
        self.writes.append(revoked_at)
        n = len(self.writes)
        if self._before.get(n):
            await self._before[n]()
        if n in self._fail:
            raise ConnectionError(f"store went away at write {n}")
        await self._store.revoke_user_tokens_before(user_id, revoked_at, ttl, **kwargs)
        if self._after.get(n):
            await self._after[n]()


async def _live(generator, token: str) -> bool:
    return await generator.validate_access_token(token) is not None


def _claims(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE)


async def test_the_allowance_these_instants_assume():
    from faultmaven.config.settings import REVOCATION_CLOCK_ALLOWANCE

    assert REVOCATION_CLOCK_ALLOWANCE.total_seconds() == A


# The basis rule.


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_sign_in_in_the_revocations_second_past_the_stamp_is_live(store, algo):
    """Pin 1. W1 = T+0.1, W2 = T+0.2, capture T+0.5: ``iat == floor(W2)``.

    On ``main`` both tokens are dead at birth (``iat <= floor(W)``).
    """
    t = _anchor()
    generator = GENERATORS[algo](store)
    with _revoker_clock(t + 0.1, t + 0.1):
        await _auth_service(store).revoke_user_tokens(USER_ID)

    access, refresh = await generator.generate_token_pair(
        _user(), state_read_at=_at(t + 0.5)
    )

    assert await _live(generator, access)
    assert await generator.validate_refresh_token(refresh) is not None


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_basis_equal_to_the_watermark_or_inside_the_allowance_is_dead(
    store, algo
):
    """Pin 5: ``basis == W`` is revoked, and so is a capture at ``W2 - 0.01``
    after the revoke returned (residual a)."""
    t = _anchor()
    generator = GENERATORS[algo](store)
    with _revoker_clock(t + 0.1, t + 0.1):
        w2 = await _auth_service(store).revoke_user_tokens(USER_ID)

    at_w2 = await generator.generate_access_token(_user(), state_read_at=w2)
    inside = await generator.generate_access_token(
        _user(), state_read_at=w2 - timedelta(milliseconds=10)
    )

    assert w2 == _at(t + 0.1) + timedelta(seconds=A)
    assert not await _live(generator, at_w2)
    assert not await _live(generator, inside)


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_mint_captured_before_the_first_stamp_lands_dies(store, algo):
    """Pin 2. Captured at T+0.12, after W1 = T+0.1 was taken and before it
    landed, so it reads no watermark. W2 = T+0.3 kills it; a single write of
    W1 compared precisely would not."""
    t = _anchor()
    generator = GENERATORS[algo](store)
    minted = {}

    async def straddle():
        minted["token"] = await generator.generate_access_token(
            _user(), state_read_at=_at(t + 0.12)
        )
        assert await _live(generator, minted["token"]), "no watermark yet"

    with _revoker_clock(t + 0.1, t + 0.2):
        await _auth_service(_Holding(store, before=straddle)).revoke_user_tokens(
            USER_ID
        )

    assert not await _live(generator, minted["token"])


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_first_write_slower_than_the_allowance_is_still_covered(store, algo):
    """Pin 2b. W1 = T+0.1 lands at T+0.4; the straddler captures at T+0.3;
    W2 = T+0.5 from the fresh clock read → dead. ``W2 = W1 + A`` (T+0.2) and a
    single write of ``now + A`` (T+0.2) both leave it live."""
    t = _anchor()
    generator = GENERATORS[algo](store)
    minted = {}

    async def straddle():
        minted["token"] = await generator.generate_access_token(
            _user(), state_read_at=_at(t + 0.3)
        )

    holding = _Holding(store, before=straddle)
    with _revoker_clock(t + 0.1, t + 0.4):
        await _auth_service(holding).revoke_user_tokens(USER_ID)

    assert holding.writes == [t + 0.1, t + 0.5]
    assert not await _live(generator, minted["token"])


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_refresh_chain_between_the_stamps_dies(store, algo):
    """Pin 3. A straddles W1 (captured T+0.15) and is rotated to B (captured
    T+0.3) after W1 lands and before W2 is taken. Both die at W2 = T+0.5."""
    t = _anchor()
    generator = GENERATORS[algo](store)
    tokens = {}

    async def mint_a():
        tokens["a"] = await generator.generate_refresh_token(
            _user(), state_read_at=_at(t + 0.15)
        )

    async def rotate_to_b():
        assert await generator.validate_refresh_token(tokens["a"]) is not None
        tokens["b"] = await generator.generate_refresh_token(
            _user(), state_read_at=_at(t + 0.3)
        )

    with _revoker_clock(t + 0.1, t + 0.4):
        await _auth_service(
            _Holding(store, before=mint_a, after=rotate_to_b)
        ).revoke_user_tokens(USER_ID)

    assert await generator.validate_refresh_token(tokens["a"]) is None
    assert await generator.validate_refresh_token(tokens["b"]) is None


@pytest.mark.parametrize("algo", GENERATORS)
async def test_a_final_stamp_slower_than_the_allowance_is_stamped_again(store, algo):
    """Review fix 1. W2 = T+0.3 is taken at T+0.2 and held until T+0.5. While
    it is in flight, A (captured T+0.15, live past W1) rotates to B, captured
    at T+0.4: after W2's value, before it lands. W2 landed 0.3s after its
    capture, past the allowance, so a fresh stamp W3 = T+0.6 follows and B
    dies with A. With W2 the last write, B was live."""
    t = _anchor()
    generator = GENERATORS[algo](store)
    tokens = {}

    async def mint_a():
        tokens["a"] = await generator.generate_refresh_token(
            _user(), state_read_at=_at(t + 0.15)
        )

    async def rotate_to_b():
        assert await generator.validate_refresh_token(tokens["a"]) is not None
        tokens["b"] = await generator.generate_refresh_token(
            _user(), state_read_at=_at(t + 0.4)
        )

    holding = _Holding(store, before={1: mint_a, 2: rotate_to_b})
    with _revoker_clock(t + 0.1, t + 0.2, t + 0.5, monotonic=(0.2, 0.5, 0.5, 0.55)):
        await _auth_service(holding).revoke_user_tokens(USER_ID)

    assert await generator.validate_refresh_token(tokens["b"]) is None
    assert await generator.validate_refresh_token(tokens["a"]) is None
    assert holding.writes == [t + 0.1, t + 0.3, t + 0.6]


async def test_three_slow_final_stamps_fail_the_revocation(store):
    """Review fix 1: past three final stamps, each landing later than the
    allowance after its capture, the revocation fails closed."""
    t = _anchor()
    holding = _Holding(store)
    with (
        _revoker_clock(
            *(t + i / 10 for i in range(1, 5)), monotonic=(0, 1, 1, 2, 2, 3)
        ),
        pytest.raises(ServiceError),
    ):
        await _auth_service(holding).revoke_user_tokens(USER_ID)

    assert len(holding.writes) == 4


async def test_only_a_revocations_first_write_sweeps():
    """Review fix 8: the final stamps do not run the SQL store's table-wide
    expiry sweep; the first write already did."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokenRevocationModel.__table__.create)
    store = SqlTokenRevocationStore(session_factory=async_sessionmaker(engine))
    sweep = AsyncMock(return_value=0)
    try:
        with patch.object(store, "_sweep", sweep), _revoker_clock(1.0, 2.0):
            await _auth_service(store).revoke_user_tokens(USER_ID)
    finally:
        await engine.dispose()

    assert sweep.await_count == 1


async def test_the_in_memory_double_reads_through_the_production_rule():
    """Review fix 5: the double answers what the stores answer, including a
    refusal on a non-finite entry, rather than re-implementing the rule."""
    store = InMemoryRevocationStore()
    store.user_watermarks[USER_ID] = (math.inf, TTL)

    with pytest.raises(CorruptRevocationEntry):
        await store.is_user_revoked(USER_ID, RevocationBasis(1, precise=False))


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("0x7FFFFFFFFF", id="hex"),
        pytest.param("0x1p40", id="hex-float"),
        pytest.param(" 1e300 ", id="padded"),
        pytest.param(["x"], id="list-typed-key"),
    ],
)
async def test_redis_keeps_only_a_strict_decimal_watermark(stored):
    """Review fix 2. Lua's ``tonumber`` reads hex and padding, which Python's
    ``float`` refuses or (padding) reads as a far-future watermark; a list
    makes ``GET`` raise inside the script. Each is overwritten."""
    import fakeredis.aioredis as fakeredis_aio

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = RedisTokenRevocationStore(redis, key_prefix="revoked:token:")
    key = f"revoked:token:user:{USER_ID}"
    if isinstance(stored, list):
        await redis.rpush(key, *stored)
    else:
        await redis.set(key, stored)
    t = _anchor()

    await store.revoke_user_tokens_before(USER_ID, t + 0.5, TTL)

    claims = {"jti": "j", "sub": USER_ID, "iat": int(t), "fm_basis": t + 0.6}
    assert await revocation_reason(store, claims) is None
    assert await redis.get(key) == str(t + 0.5)


@pytest.mark.parametrize(
    "ahead,live",
    [
        pytest.param(0.05, False, id="50ms-ahead-dead"),
        pytest.param(0.15, True, id="150ms-ahead-live"),
    ],
)
async def test_a_straddler_on_a_replica_ahead_of_the_revoker(store, ahead, live):
    """Pin 4. W2 = T+0.3 (taken at T+0.2). A straddler captured at the
    revoker's T+0.2, on a replica whose clock runs ``ahead``: within the
    allowance it dies; past it, it lives — residual (b), pinned as stated."""
    t = _anchor()
    generator = _hs256(store)
    with _revoker_clock(t + 0.1, t + 0.2):
        await _auth_service(store).revoke_user_tokens(USER_ID)

    token = await generator.generate_access_token(
        _user(), state_read_at=_at(t + 0.2 + ahead)
    )

    assert await _live(generator, token) is live


async def test_a_corrupt_stored_watermark_is_overwritten_not_compared(store):
    """A text body sorts above every number (SQLite) or is no number (Lua), so
    a max() alone would keep it: the revocation must land instead."""
    t = _anchor()
    await _write_raw_watermark(store, "not-a-number")

    await store.revoke_user_tokens_before(USER_ID, t + 0.5, TTL)

    claims = {"jti": "j", "sub": USER_ID, "iat": int(t), "fm_basis": t + 0.4}
    assert await revocation_reason(store, claims) == "user_revoked"


# Pin 6: the whole-second fallback.

UNUSABLE = [
    pytest.param("absent", id="absent"),
    pytest.param("0.5", id="string"),
    pytest.param(True, id="bool"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(10**400, id="overflow"),
    pytest.param("next-second", id="not-in-iats-second"),
]


@pytest.mark.parametrize("algo", GENERATORS)
@pytest.mark.parametrize("basis", UNUSABLE)
async def test_an_unusable_basis_falls_back_to_whole_seconds(store, algo, basis):
    """With ``W = T+0.2``, a token captured at T+0.5 is live by its basis; with
    its basis unusable it compares ``iat <= floor(W)`` and is revoked. A token
    in the next second is live on the fallback."""
    t = _anchor()
    await store.revoke_user_tokens_before(USER_ID, t + 0.2, TTL)
    token = await GENERATORS[algo](store).generate_access_token(
        _user(), state_read_at=_at(t + 0.5)
    )
    claims = _claims(token)
    assert await revocation_reason(store, claims) is None

    if basis == "absent":
        del claims["fm_basis"]
    elif basis == "next-second":
        claims["fm_basis"] = t + 1.5
    else:
        claims["fm_basis"] = basis

    assert await revocation_reason(store, claims) == "user_revoked"
    assert await revocation_reason(store, {**claims, "iat": int(t) + 1}) is None


async def test_a_non_finite_stored_watermark_refuses(store):
    """``inf`` on both arms; SQLite stores a NaN as NULL, which NOT NULL refuses."""
    await _write_raw_watermark(store, math.inf)
    t = _anchor()
    claims = {"jti": "j", "sub": USER_ID, "iat": int(t), "fm_basis": t + 0.5}

    with pytest.raises(CorruptRevocationEntry):
        await revocation_reason(store, claims)


# Pin 7: no sign-in revives a token.


class _Users:
    def __init__(self, user):
        self._user = user

    async def get_user_by_username(self, username):
        return self._user

    async def get_user(self, user_id):
        return self._user

    async def get(self, user_id):
        return self._user


def _request(store, users):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(user_store=users, token_revocation_store=store)
        )
    )


class _Sessions:
    async def create_session(self, **kwargs):
        return SimpleNamespace(session_id="session-2050")


@contextmanager
def _local_mode(generator):
    settings = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL, jwt_access_token_expire_minutes=60
        )
    )
    with (
        patch.object(auth_routes, "_build_local_jwt_generator", return_value=generator),
        patch.object(auth_routes, "get_settings", return_value=settings),
    ):
        yield


async def test_a_local_login_after_a_revocation_does_not_revive_older_tokens(store):
    """On ``main`` the login clears the watermark and ``older`` comes back."""
    t = _anchor()
    generator = _hs256(store)
    older = await generator.generate_access_token(_user(), state_read_at=_at(t))
    with _revoker_clock(t + 0.1, t + 0.1):
        await _auth_service(store).revoke_user_tokens(USER_ID)

    with _local_mode(generator):
        result = await auth_routes.local_login(
            DevLoginRequest(username="basis"),
            _request(store, _Users(_user())),
            session_service=_Sessions(),
        )

    assert not await _live(generator, older)
    assert await _live(generator, result.access_token)
    assert await generator.validate_refresh_token(result.refresh_token) is not None


async def test_an_sso_sign_in_after_a_revocation_does_not_revive_older_tokens(store):
    t = _anchor()
    generator = _rs256(store)
    older = await generator.generate_access_token(_user(), state_read_at=_at(t))
    with _revoker_clock(t + 0.1, t + 0.1):
        await _auth_service(store).revoke_user_tokens(USER_ID)

    class _Login:
        async def consume_login(self, code):
            return {"user_id": USER_ID, "state_read_at": t + 0.5}

    class _SessionTuple:
        async def create_session(self, **kwargs):
            return (SimpleNamespace(session_id="session-2050-sso"), False)

    result = await SSOLoginService(
        identity_provider=SimpleNamespace(provider_name="test-idp"),
        ephemeral_store=_Login(),
        user_repository=_Users(_user()),
        token_generator=generator,
        session_service=_SessionTuple(),
        dashboard_url="https://dashboard.example",
        access_token_expires_in=3600,
    ).exchange("completion-code")

    assert not await _live(generator, older)
    assert await _live(generator, result.access_token)
    assert await generator.validate_refresh_token(result.refresh_token) is not None


# Pins 9 and 11: the reset and refresh paths, in W2's second.


async def test_a_reset_link_captured_past_the_stamp_in_its_second_resets(store):
    """Pin 9, the half that bites: refused on ``main`` (``iat == floor(W2)``).
    The other half, a link minted before the revocation refused at reset, is
    ``test_reset_token_revocation.py``'s."""
    import fakeredis.aioredis as fakeredis_aio

    t = _anchor()
    auth_service = _auth_service(store)
    with patch.object(user_module, "get_settings", return_value=_settings()):
        users = user_module.UserService(
            user_repo=InMemoryUserRepository(),
            auth_service=auth_service,
            token_generator=_hs256(store),
            redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
        )
    user = await users.register_user(
        email="reset@local.faultmaven", password="Str0ng-P4ssw0rd!", full_name="R"
    )
    with _revoker_clock(t + 0.1, t + 0.1):
        await auth_service.revoke_user_tokens(user.user_id)

    with patch.object(user_module, "capture_state_read_at", return_value=_at(t + 0.5)):
        link = await users.request_password_reset(email="reset@local.faultmaven")

    await users.reset_password(link, "An0ther-P4ssw0rd!")


async def test_a_refresh_token_minted_past_the_stamp_in_its_second_refreshes(store):
    """Pin 11. Refused on ``main``: the presented token's ``iat == floor(W2)``."""
    t = _anchor()
    generator = _hs256(store)
    with _revoker_clock(t + 0.1, t + 0.1):
        await _auth_service(store).revoke_user_tokens(USER_ID)
    presented = await generator.generate_refresh_token(
        _user(), state_read_at=_at(t + 0.5)
    )

    with (
        _local_mode(generator),
        patch.object(auth_routes, "capture_state_read_at", return_value=_at(t + 0.6)),
    ):
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=presented),
            _request(store, _Users(_user())),
            SimpleNamespace(headers={}),
        )

    assert await _live(generator, result.access_token)


# Pin 8: failure modes.


def _delete_request(auth_service):
    request = MagicMock()
    request.app.state.auth_service = auth_service
    request.app.state.user_service.count_cases_created_by = AsyncMock(return_value=0)
    return request


async def _delete(request, users):
    with patch.object(auth_routes, "get_user_store", AsyncMock(return_value=users)):
        return await auth_routes.delete_user(
            username="doomed",
            request=request,
            operator=None,
            scope=OperatorUserScope(users=None),
        )


class _DoomedUsers:
    def __init__(self):
        self.users = {"doomed": SimpleNamespace(user_id=USER_ID, username="doomed")}

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def delete_user(self, user_id):
        return self.users.pop("doomed", None) is not None


async def test_a_failed_final_stamp_fails_the_revocation_and_the_delete(store):
    """W1 lands and W2 fails: ``ServiceError`` (fail closed). The delete route's
    own revocation meets the same failure at its W2, so the account survives
    and the route answers its existing 500. Writes are counted per call."""
    with pytest.raises(ServiceError):
        await _auth_service(_Holding(store, fail={2})).revoke_user_tokens(USER_ID)

    users = _DoomedUsers()
    holding = _Holding(store, fail={2})
    with pytest.raises(HTTPException) as refused:
        await _delete(_delete_request(_auth_service(holding)), users)

    assert len(holding.writes) == 2, "the route's W1 landed, its W2 failed"
    assert refused.value.status_code == 500
    assert refused.value.detail["error"] == "internal_error"
    assert "doomed" in users.users


async def test_a_failed_post_delete_stamp_leaves_the_delete_standing(caplog):
    """The delete stands, the route answers success, its message names the
    failed stamp, and an error is logged with the user id. No store is read."""
    calls = []

    async def revoke(user_id):
        calls.append(user_id)
        if len(calls) == 2:
            raise ServiceError("Token revocation failed: store down")
        return datetime.now(timezone.utc)

    auth_service = SimpleNamespace(revoke_user_tokens=revoke)
    users = _DoomedUsers()

    with caplog.at_level(logging.ERROR, logger=auth_routes.logger.name):
        result = await _delete(_delete_request(auth_service), users)

    assert "doomed" not in users.users
    assert calls == [USER_ID, USER_ID]
    assert result["user_id"] == USER_ID
    assert "deleted successfully" in result["message"]
    assert "final revocation stamp failed" in result["message"]
    failures = [r for r in caplog.records if "Post-delete" in r.getMessage()]
    assert failures and failures[0].levelno == logging.ERROR
    assert failures[0].target_user_id == USER_ID
