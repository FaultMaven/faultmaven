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

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pytest

from faultmaven.config.settings import AuthMode
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.contracts import OAuthCodeDTO
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
from faultmaven.modules.auth.domain.services.oauth_service import (
    OAuthServiceImpl,
)
from faultmaven.modules.auth.domain.services.sso_login_service import SSOLoginService
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


def _ephemeral_keypair() -> tuple[str, str]:
    """One runtime-generated pair, shared module-wide: nothing committed is a
    usable key, no test mutates key material, and one keygen replaces eight."""
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
    return private_pem, public_pem


_PRIVATE_PEM, _PUBLIC_PEM = _ephemeral_keypair()


def _rs256(store) -> RS256JWTTokenGenerator:
    return RS256JWTTokenGenerator(
        private_key=_PRIVATE_PEM,
        public_key=_PUBLIC_PEM,
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


# ---------------------------------------------------------------------------
# 1. The straddle: pre-revocation read, post-revocation mint => dead token
# ---------------------------------------------------------------------------
#
# The window is simulated by backdating: the read's capture sits 10s in the
# past, the watermark 5s in the past, and the mint runs now. That keeps the
# ordering of the real race (read began, THEN the revocation landed, THEN the
# mint ran) while spanning whole seconds — necessary because ``iat`` has
# 1-second granularity and the comparison is ``<=``: with all three events in
# the same second, even a mint-time ``iat`` equals the watermark and is
# rejected, and the test would pass against the pre-#831 code it exists to
# refute. Verified by mutation: with ``_mint_instant`` returning ``now``,
# these go RED; a same-second version stayed GREEN.

#: read capture ... 5s ... watermark ... 5s ... mint (now)
STRADDLE_READ_AGE = timedelta(seconds=10)
STRADDLE_WATERMARK_AGE = timedelta(seconds=5)


async def _straddle(store, user_id: str = USER_ID) -> datetime:
    """Arrange the straddle; returns the pre-read capture to mint with."""
    state_read_at = datetime.now(timezone.utc) - STRADDLE_READ_AGE
    watermark = datetime.now(timezone.utc) - STRADDLE_WATERMARK_AGE
    await store.revoke_user_tokens_before(user_id, int(watermark.timestamp()), TTL)
    return state_read_at


@pytest.mark.parametrize("algo", GENERATORS)
async def test_access_token_from_straddling_mint_is_rejected(algo):
    """The #831 scenario, in order: read starts, revocation lands, mint runs.

    RED on the pre-#831 code: with ``iat`` stamped at mint time, the token
    postdates the watermark and validates successfully.
    """
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    state_read_at = await _straddle(store)

    token = await generator.generate_access_token(_user(), state_read_at=state_read_at)
    assert await generator.validate_access_token(token) is None


@pytest.mark.parametrize("algo", GENERATORS)
async def test_refresh_token_from_straddling_mint_is_rejected(algo):
    store = InMemoryRevocationStore()
    generator = GENERATORS[algo](store)

    state_read_at = await _straddle(store)

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

    state_read_at = await _straddle(store)

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
    """Sub-tolerance clock slew clamps to ``now`` — never a future stamp.

    Bounded on BOTH sides, from instants captured around the mint: the upper
    bound alone would pass a clamp that leaked the future value whenever the
    clock crossed a second boundary mid-test, and would pass a clamp mutated
    to return an arbitrarily old instant. ``degrades to now`` means now.
    """
    generator = _hs256(InMemoryRevocationStore())
    ahead = datetime.now(timezone.utc) + timedelta(
        seconds=STATE_READ_AT_FUTURE_TOLERANCE_SECONDS - 1
    )
    now_before = int(datetime.now(timezone.utc).timestamp())
    token = await generator.generate_access_token(_user(), state_read_at=ahead)
    now_after = int(datetime.now(timezone.utc).timestamp())
    iat = _claims(token)["iat"]
    assert now_before <= iat <= now_after


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
#
# The read then stalls past the next whole second before returning. Without
# that, the capture, the watermark and the mint all land in the same second,
# where ``iat <= watermark`` rejects the pair even for a mint-time (or
# post-read) stamp — and the test would pass against exactly the defects it
# exists to catch. Real wall-clock delay, not a mocked clock: the handler's
# capture must be the thing under test.


class _StraddlingUserStore:
    """Returns the user, but a revocation lands (and time passes) mid-read."""

    def __init__(self, user: DevUser, store: InMemoryRevocationStore) -> None:
        self._user = user
        self._store = store

    async def _read(self, matches: bool) -> DevUser | None:
        now = datetime.now(timezone.utc)
        revoked_at = int(now.timestamp())
        await self._store.revoke_user_tokens_before(self._user.user_id, revoked_at, TTL)
        # Stall into the second after the watermark, so anything stamped
        # after this read observably postdates it. One computed sleep of the
        # sub-second remainder, not a poll loop.
        await asyncio.sleep(1.0 - (now.timestamp() % 1.0) + 0.01)
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


# ---------------------------------------------------------------------------
# 7. Two-leg flows: the artifact carries (or implies) its first leg's basis
# ---------------------------------------------------------------------------
#
# The SSO completion code and the OAuth authorization code are minted from
# state read in an EARLIER request, and neither artifact is revocable (no
# ``iat``, no watermark check). Capturing only at the exchange leg would let
# a revoke-all landing between the legs be survived by a pair carrying the
# pre-revocation tenant. The SSO payload carries the callback leg's capture;
# the OAuth code's creation instant is derived from ``expires_at`` minus the
# configured code TTL. Same simulated window as section 1: leg-1 basis 10s
# ago, watermark 5s ago, exchange now — so an exchange-only capture (the
# mutation) mints a SURVIVING pair and these go RED.


async def test_sso_exchange_stamps_from_the_callback_legs_capture():
    store = InMemoryRevocationStore()
    generator = _hs256(store)
    user = _dev_user()

    callback_capture = datetime.now(timezone.utc) - STRADDLE_READ_AGE
    watermark = datetime.now(timezone.utc) - STRADDLE_WATERMARK_AGE
    await store.revoke_user_tokens_before(user.user_id, int(watermark.timestamp()), TTL)

    class _LoginStore:
        async def consume_login(self, code):
            return {
                "user_id": user.user_id,
                "state_read_at": callback_capture.isoformat(),
            }

    class _Users:
        async def get(self, user_id):
            return user

    class _Sessions:
        async def create_session(self, **kwargs):
            return (SimpleNamespace(session_id="session-831-sso"), False)

    service = SSOLoginService(
        identity_provider=SimpleNamespace(provider_name="test-idp"),
        ephemeral_store=_LoginStore(),
        user_repository=_Users(),
        token_generator=generator,
        session_service=_Sessions(),
        dashboard_url="https://dashboard.example",
        access_token_expires_in=ACCESS_MINUTES * 60,
    )

    result = await service.exchange("completion-code")

    assert result is not None
    assert await generator.validate_access_token(result.access_token) is None
    assert await generator.validate_refresh_token(result.refresh_token) is None


async def test_sso_exchange_capture_covers_its_own_reads_without_a_leg1_stamp():
    """A payload from a pre-#831 writer (no callback capture) still gets the
    exchange-entry capture: a revocation landing during the exchange leg's own
    user-row read kills the pair."""
    store = InMemoryRevocationStore()
    generator = _hs256(store)
    user = _dev_user()

    class _LoginStore:
        async def consume_login(self, code):
            return {"user_id": user.user_id}

    class _StraddlingUsers:
        async def get(self, user_id):
            now = datetime.now(timezone.utc)
            await store.revoke_user_tokens_before(
                user.user_id, int(now.timestamp()), TTL
            )
            await asyncio.sleep(1.0 - (now.timestamp() % 1.0) + 0.01)
            return user

    class _Sessions:
        async def create_session(self, **kwargs):
            return (SimpleNamespace(session_id="session-831-sso2"), False)

    service = SSOLoginService(
        identity_provider=SimpleNamespace(provider_name="test-idp"),
        ephemeral_store=_LoginStore(),
        user_repository=_StraddlingUsers(),
        token_generator=generator,
        session_service=_Sessions(),
        dashboard_url="https://dashboard.example",
        access_token_expires_in=ACCESS_MINUTES * 60,
    )

    result = await service.exchange("completion-code")

    assert result is not None
    assert await generator.validate_access_token(result.access_token) is None
    assert await generator.validate_refresh_token(result.refresh_token) is None


async def test_oauth_exchange_stamps_from_the_authorize_legs_instant():
    store = InMemoryRevocationStore()
    generator = _hs256(store)

    code_verifier = "straddle-verifier-" + "x" * 32
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    expiry_seconds = 600
    code_created = datetime.now(timezone.utc) - STRADDLE_READ_AGE
    code_data = OAuthCodeDTO(
        code="authz-code-831",
        user_id=USER_ID,
        redirect_uri="https://callback.example/cb",
        code_challenge=code_challenge,
        expires_at=code_created + timedelta(seconds=expiry_seconds),
        used=False,
    )

    watermark = datetime.now(timezone.utc) - STRADDLE_WATERMARK_AGE
    await store.revoke_user_tokens_before(USER_ID, int(watermark.timestamp()), TTL)

    class _CodeRepo:
        async def get_code(self, code):
            return code_data

        async def claim_code(self, code):
            return True

    class _Users:
        async def get(self, user_id):
            return _user()

    settings = SimpleNamespace(
        oauth_code_expiry_seconds=expiry_seconds,
        jwt_access_token_expire_minutes=ACCESS_MINUTES,
        jwt_refresh_token_expire_days=REFRESH_DAYS,
    )
    service = OAuthServiceImpl(_CodeRepo(), _Users(), generator, settings)

    dto = await service.exchange_code_for_token(
        code="authz-code-831",
        code_verifier=code_verifier,
        redirect_uri="https://callback.example/cb",
    )

    assert await generator.validate_access_token(dto.access_token) is None
    assert await generator.validate_refresh_token(dto.refresh_token) is None


async def test_oauth_refresh_grant_capture_covers_its_own_reads():
    """The refresh grant has no leg-1 artifact (the presented token is itself
    watermark-checked), but its own capture must precede its reads: a
    revocation landing during the user-row load kills the rotated pair."""
    store = InMemoryRevocationStore()
    generator = _hs256(store)
    user = _user()

    old_refresh = await generator.generate_refresh_token(
        user, state_read_at=datetime.now(timezone.utc) - STRADDLE_READ_AGE
    )

    class _StraddlingUsers:
        async def get(self, user_id):
            now = datetime.now(timezone.utc)
            await store.revoke_user_tokens_before(
                user.user_id, int(now.timestamp()), TTL
            )
            await asyncio.sleep(1.0 - (now.timestamp() % 1.0) + 0.01)
            return user

    settings = SimpleNamespace(
        oauth_code_expiry_seconds=600,
        jwt_access_token_expire_minutes=ACCESS_MINUTES,
        jwt_refresh_token_expire_days=REFRESH_DAYS,
    )
    service = OAuthServiceImpl(None, _StraddlingUsers(), generator, settings)

    dto = await service.refresh_access_token(
        refresh_token=old_refresh, client_id="faultmaven-copilot"
    )

    assert await generator.validate_access_token(dto.access_token) is None
    assert await generator.validate_refresh_token(dto.refresh_token) is None


async def test_provisioning_capture_covers_the_account_read():
    """A revocation landing during provisioning's account read yields a
    credential that is already dead — never one that survives the revoke."""
    from faultmaven.modules.auth.domain.services.service_account_provisioning import (
        SERVICE_ACCOUNT_KIND,
        provision_service_account_credential,
    )

    store = InMemoryRevocationStore()
    generator = _hs256(store)
    user = SimpleNamespace(
        user_id=USER_ID,
        is_active=True,
        username="svc-straddler",
        email="svc@local.faultmaven",
        roles=["user"],
        organization_id=None,
        account_kind=SERVICE_ACCOUNT_KIND,
        display_name="svc",
    )

    class _StraddlingUserStore:
        async def get_user_by_username(self, username):
            now = datetime.now(timezone.utc)
            await store.revoke_user_tokens_before(
                user.user_id, int(now.timestamp()), TTL
            )
            await asyncio.sleep(1.0 - (now.timestamp() % 1.0) + 0.01)
            return user

    credential = await provision_service_account_credential(
        username="svc-straddler",
        user_store=_StraddlingUserStore(),
        token_generator=generator,
    )

    assert await generator.validate_refresh_token(credential.refresh_token) is None
