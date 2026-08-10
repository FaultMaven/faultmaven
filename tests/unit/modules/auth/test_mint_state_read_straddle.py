"""#831: ``iat`` is stamped from a pre-read instant, so a straddling mint dies.

Per-user revocation (#769) rejects tokens whose ``iat`` is at or before the
user's watermark, and ``UserService`` persists state before watermarking so a
login cannot complete entirely inside the gap. What neither closed is a
request that STRADDLES the whole sequence: it reads the user row (old
password hash, old roles) or validates a still-valid refresh token, the admin
action persists and watermarks, and only then does the request mint. Stamped
at mint time, that token's ``iat`` postdates the watermark and survives the
revocation while carrying pre-change state.

The fix: every ``IJWTTokenGenerator`` mint requires ``state_read_at`` —
captured by the caller before its first read of any state the claims derive
from — and stamps ``iat``/``exp`` from it. A straddling mint then necessarily
carries ``iat <= watermark`` and dies with it.

Covered here:
1. The straddle itself, at the generator surface: a token minted from a
   pre-revocation ``state_read_at`` is rejected by validation, for access,
   refresh and password-reset tokens, under both algorithms. These go RED on
   the pre-#831 code, where ``iat`` was mint-time ``now``.
2. The negative control: a mint whose ``state_read_at`` postdates the
   watermark still validates — revocation does not become "revoke forever".
3. The stamp property: ``iat`` equals the captured instant and ``exp`` keeps
   the configured lifetime relative to it, for every token kind (one golden
   vector, not per-path spot checks).
4. Decoy parity: the reset decoy stamps the same instant as a real reset
   mint, so ``iat`` cannot leak the account lookup's latency.
5. Misuse guards: a naive or clearly-future ``state_read_at`` is refused; a
   marginally-future one degrades to ``now`` (the smaller, more-revocable
   stamp), never to the future.
6. Capture placement at the handler surface: ``POST /auth/login`` and
   ``POST /auth/refresh`` capture before their first store read, proven by a
   user store that writes the watermark *during* that read and a minted pair
   that must then be dead. These go RED if a handler captures after its
   reads, whatever the generator does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pytest

from faultmaven.config.settings import AuthMode
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import (
    DevLoginRequest,
    TokenRefreshRequest,
)
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    PASSWORD_RESET_TOKEN_EXPIRY_HOURS,
    STATE_READ_AT_FUTURE_TOLERANCE_SECONDS,
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    revocation_reason,
)
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"
SECRET = "unit-test-secret-key-please-ignore"
USER_ID = "user-831"
ACCESS_MINUTES = 60
REFRESH_DAYS = 7

#: Ceiling for the watermark entries written by these tests.
TTL = 30 * 86400


def _user(user_id: str = USER_ID):
    return SimpleNamespace(
        user_id=user_id,
        is_active=True,
        username="straddler",
        email="straddler@local.faultmaven",
        roles=["user"],
        organization_id="org-831",
    )


def _hs256(store) -> HS256JWTTokenGenerator:
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=store,
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _rs256(store) -> RS256JWTTokenGenerator:
    """Built with an ephemeral key pair so nothing here is a usable key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


GENERATORS = {"hs256": _hs256, "rs256": _rs256}


def _claims(token: str) -> dict:
    """Read claims without verifying — these tests assert on values, not trust."""
    return pyjwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE)


async def _watermark_now(store, user_id: str = USER_ID) -> int:
    """Write the user's watermark at the current instant, as a revocation does."""
    revoked_at = int(datetime.now(timezone.utc).timestamp())
    await store.revoke_user_tokens_before(user_id, revoked_at, TTL)
    return revoked_at


# ---------------------------------------------------------------------------
# 1. The straddle: pre-revocation read, post-revocation mint => dead token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", GENERATORS)
async def test_access_token_from_straddling_mint_is_rejected(algo):
    """The #831 scenario, in order: read starts, revocation lands, mint runs.

    RED on the pre-#831 code: with ``iat`` stamped at mint time, the token
    postdates the watermark and validates successfully.
    """
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    state_read_at = datetime.now(timezone.utc)  # the handler's capture
    await _watermark_now(store)  # admin action lands mid-request

    token = await generator.generate_access_token(_user(), state_read_at=state_read_at)
    assert await generator.validate_access_token(token) is None


@pytest.mark.parametrize("algo", GENERATORS)
async def test_refresh_token_from_straddling_mint_is_rejected(algo):
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    state_read_at = datetime.now(timezone.utc)
    await _watermark_now(store)

    token = await generator.generate_refresh_token(_user(), state_read_at=state_read_at)
    assert await generator.validate_refresh_token(token) is None


@pytest.mark.parametrize("algo", GENERATORS)
async def test_reset_token_from_straddling_mint_is_revoked(algo):
    """A reset link minted across a revoke-all dies with it.

    Reset tokens are watermark-checked at redemption through the same shared
    rule as every other token (#829); ``reset_password`` refuses on any
    non-None reason. Asserting through that rule, not a reimplementation.
    """
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    state_read_at = datetime.now(timezone.utc)
    await _watermark_now(store)

    mint = await generator.generate_password_reset_token(
        _user(), state_read_at=state_read_at
    )
    reason = await revocation_reason(store, _claims(mint.token))
    assert reason == "user_revoked"


# ---------------------------------------------------------------------------
# 2. Negative control: revocation does not outlive its instant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", GENERATORS)
async def test_mint_read_after_the_revocation_still_validates(algo):
    """A re-login after the revocation must work — the gate can pass.

    The watermark is backdated well past ``iat`` granularity so the assertion
    cannot ride on same-second rounding.
    """
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    backdated = datetime.now(timezone.utc) - timedelta(seconds=10)
    await store.revoke_user_tokens_before(USER_ID, int(backdated.timestamp()), TTL)

    token = await generator.generate_access_token(
        _user(), state_read_at=datetime.now(timezone.utc)
    )
    assert await generator.validate_access_token(token) is not None


# ---------------------------------------------------------------------------
# 3. The stamp property: one golden vector per token kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", GENERATORS)
async def test_iat_and_exp_derive_from_state_read_at(algo):
    """``iat`` IS the captured instant; ``exp`` keeps the configured lifetime.

    A clearly-past instant, so the expected values are exact — a partial
    regression (e.g. ``iat`` captured but ``exp`` still mint-time) shows up as
    a wrong lifetime, not as flake.
    """
    generator = GENERATORS[algo](InMemoryRevocationStore())
    captured = datetime.now(timezone.utc) - timedelta(seconds=5)
    expected_iat = int(captured.timestamp())

    access = _claims(
        await generator.generate_access_token(_user(), state_read_at=captured)
    )
    assert access["iat"] == expected_iat
    assert access["exp"] == expected_iat + ACCESS_MINUTES * 60

    refresh = _claims(
        await generator.generate_refresh_token(_user(), state_read_at=captured)
    )
    assert refresh["iat"] == expected_iat
    assert refresh["exp"] == expected_iat + REFRESH_DAYS * 86400

    reset = _claims(
        (
            await generator.generate_password_reset_token(
                _user(), state_read_at=captured
            )
        ).token
    )
    assert reset["iat"] == expected_iat
    assert reset["exp"] == expected_iat + PASSWORD_RESET_TOKEN_EXPIRY_HOURS * 3600


# ---------------------------------------------------------------------------
# 4. Decoy parity: the lookup's latency must not reach ``iat``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", GENERATORS)
async def test_decoy_and_real_reset_mints_stamp_the_same_instant(algo):
    generator = GENERATORS[algo](InMemoryRevocationStore())
    captured = datetime.now(timezone.utc) - timedelta(seconds=5)

    real = await generator.generate_password_reset_token(
        _user(), state_read_at=captured
    )
    decoy = await generator.generate_dummy_reset_token(
        "straddler@local.faultmaven", state_read_at=captured
    )
    assert _claims(real.token)["iat"] == _claims(decoy.token)["iat"]
    assert _claims(real.token)["exp"] == _claims(decoy.token)["exp"]


# ---------------------------------------------------------------------------
# 5. Misuse guards on the argument itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", GENERATORS)
async def test_naive_state_read_at_is_refused(algo):
    generator = GENERATORS[algo](InMemoryRevocationStore())
    with pytest.raises(ValueError):
        await generator.generate_access_token(
            _user(), state_read_at=datetime.now()  # noqa: DTZ005 — the point
        )


@pytest.mark.parametrize("algo", GENERATORS)
async def test_clearly_future_state_read_at_is_refused(algo):
    """A derived time (an expiry, ``now + lifetime``) must fail loudly.

    Minting from it would stamp a future ``iat`` immune to every future
    watermark — the straddle reopened, silently, by one miswired caller.
    """
    generator = GENERATORS[algo](InMemoryRevocationStore())
    with pytest.raises(ValueError):
        await generator.generate_access_token(
            _user(),
            state_read_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )


async def test_marginally_future_state_read_at_degrades_to_now():
    """Sub-tolerance clock slew clamps to ``now`` — never a future stamp."""
    generator = _hs256(InMemoryRevocationStore())
    ahead = datetime.now(timezone.utc) + timedelta(
        seconds=STATE_READ_AT_FUTURE_TOLERANCE_SECONDS - 1
    )
    token = await generator.generate_access_token(_user(), state_read_at=ahead)
    now_after = int(datetime.now(timezone.utc).timestamp())
    assert _claims(token)["iat"] <= now_after


# ---------------------------------------------------------------------------
# 6. Capture placement at the handler surface
# ---------------------------------------------------------------------------
#
# The generator can only honour what the handler captured. These prove the
# handlers capture BEFORE their first store read, using a user store that
# writes the watermark during that read — the admin action landing exactly
# inside the straddle window. If a handler moved its capture after the read,
# the minted pair would postdate the watermark and these go RED, with the
# generator fix intact.


class _StraddlingUserStore:
    """Returns the user, but a revocation lands during the read."""

    def __init__(self, user: DevUser, store: InMemoryRevocationStore) -> None:
        self._user = user
        self._store = store

    async def _read(self, matches: bool) -> DevUser | None:
        revoked_at = int(datetime.now(timezone.utc).timestamp())
        await self._store.revoke_user_tokens_before(self._user.user_id, revoked_at, TTL)
        return self._user if matches else None

    async def get_user_by_username(self, username: str) -> DevUser | None:
        return await self._read(self._user.username == username)

    async def get_user(self, user_id: str) -> DevUser | None:
        return await self._read(self._user.user_id == user_id)


def _dev_user() -> DevUser:
    return DevUser(
        user_id=USER_ID,
        username="straddler",
        email="straddler@local.faultmaven",
        display_name="Straddler",
        created_at=datetime.now(timezone.utc),
    )


def _fake_request(user_store, revocation_store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store, token_revocation_store=revocation_store
            )
        )
    )


class _FakeSessionService:
    async def create_session(self, user_id: str, metadata: dict):
        return SimpleNamespace(session_id="session-831")


def _patches(generator):
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL,
            jwt_access_token_expire_minutes=ACCESS_MINUTES,
        )
    )
    return (
        patch.object(auth_routes, "_build_local_jwt_generator", return_value=generator),
        patch.object(auth_routes, "get_settings", return_value=settings_stub),
    )


async def test_login_that_straddles_a_revocation_mints_a_dead_pair():
    user = _dev_user()
    store = InMemoryRevocationStore()
    generator = _hs256(store)

    request = _fake_request(_StraddlingUserStore(user, store), store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.local_login(
            DevLoginRequest(username=user.username),
            request,
            response,
            session_service=_FakeSessionService(),
        )

    # The login itself succeeds — the revocation landed after its reads began
    # — but everything it minted predates the watermark and is already dead.
    assert await generator.validate_access_token(result.access_token) is None
    assert await generator.validate_refresh_token(result.refresh_token) is None


async def test_refresh_that_straddles_a_revocation_mints_a_dead_pair():
    user = _dev_user()
    store = InMemoryRevocationStore()
    generator = _hs256(store)

    # A refresh token minted long before any revocation (clearly-past capture,
    # so the straddling watermark below cannot collide with its ``iat``).
    old_refresh = await generator.generate_refresh_token(
        user,
        state_read_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    request = _fake_request(_StraddlingUserStore(user, store), store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

    assert await generator.validate_access_token(result.access_token) is None
    assert await generator.validate_refresh_token(result.refresh_token) is None
