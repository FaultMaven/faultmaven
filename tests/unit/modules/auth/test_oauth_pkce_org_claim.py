"""The organization claim survives the OAuth-PKCE token chain (#872).

The copilot authenticates through ``GET|POST /auth/oauth/authorize`` (an
authenticated dashboard request) and then redeems the code at
``POST /auth/oauth/token`` (**unauthenticated** — it carries a code and a PKCE
verifier, no bearer token). The ``users`` row the exchange loads has no
organization: tenancy lives in the token chain, not the user table. So unless the
authorize leg captures the tenant and the exchange re-attaches it, every copilot
session under ``TENANT_PROVIDER=multi`` mints an empty ``organization_id`` and is
refused at ``bind_request_org_context`` on its first API call.

These tests assert at the **surface that renders the claim** — the decoded JWT —
rather than on the user object handed to the generator. Attaching the org to the
user is the mechanism; the claim in the token is the guarantee, and only the
decoded token proves ``resolve_organization_claim`` did not drop it on the way
out.

The chain is exercised leg by leg, and then end to end: authorize → exchange →
refresh → refresh again, because a claim that survives the first mint but not
rotation still strands the extension a few minutes later.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import jwt
import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.config.tenant_context import usable_tenant_id
from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO, OAuthCodeDTO
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    InMemoryOAuthCodeRepository,
    RedisOAuthCodeRepository,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

SECRET = "test-secret-key-for-hs256-signing-only"
TENANT = "org_acme_7f3c"
OTHER_TENANT = "org_globex_9b1d"
REDIRECT = "chrome-extension://abc123/callback.html"

_MULTI = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_MULTI,
)
_SINGLE = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_SINGLE,
)


def _claims(token: str) -> dict:
    """Decode a minted token — the surface the claim actually has to reach."""
    return jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
    )


@pytest.fixture
def token_generator():
    """A REAL HS256 generator over a REAL revocation store on fakeredis.

    Both are the production types. A mock generator would accept any user and
    return a fixed string, so it could not tell a token that carries the tenant
    from one that does not — the whole property under test. A stubbed revocation
    store would likewise let the rotation legs pass without the presented token
    ever actually being retired, so "the chain still works after rotation" would
    prove nothing.
    """
    fakeredis = pytest.importorskip("fakeredis")
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=RedisTokenRevocationStore(
            fakeredis.aioredis.FakeRedis(decode_responses=True)
        ),
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


@pytest.fixture
def user_repository():
    """A user store that returns an org-less user — as the real one does.

    The ``users`` table has no organization column, which is precisely why the
    claim has to travel with the code. A fixture that pre-stamped the tenant
    would make the fix untestable: the test would pass without it.
    """
    repo = AsyncMock()
    user = Mock(spec=["user_id", "username", "email", "roles", "is_active"])
    user.user_id = "user_123"
    user.username = "testuser"
    user.email = "testuser@acme.example"
    user.roles = ["user"]
    user.is_active = True
    repo.get = AsyncMock(return_value=user)
    return repo


@pytest.fixture
def oauth_service(token_generator, user_repository):
    """OAuth service on the real in-memory code repository."""
    return OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=user_repository,
        token_generator=token_generator,
        settings=AuthSettings(
            oauth_allowed_clients=["faultmaven-copilot"],
            oauth_redirect_uri_patterns=[
                r"^chrome-extension://[a-z0-9]+/callback\.html$"
            ],
        ),
    )


def _authorization_request(code_challenge: str) -> OAuthAuthorizationDTO:
    return OAuthAuthorizationDTO(
        client_id="faultmaven-copilot",
        redirect_uri=REDIRECT,
        state="state_abc",
        code_challenge=code_challenge,
        code_challenge_method="S256",
        scope="openid profile email",
    )


@pytest.fixture
def pkce_pair():
    """A verifier and its S256 challenge."""
    import base64
    import hashlib
    import secrets

    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )
    return verifier, challenge


# =============================================================================
# The full chain: authorize -> exchange -> refresh -> refresh
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_org_claim_survives_authorize_exchange_and_repeated_rotation(
    oauth_service, pkce_pair
):
    """Every token the chain mints carries the authorizing session's tenant.

    Rotation is exercised twice, not once: the first refresh reads the claim from
    the token the exchange minted, the second reads it from a token the *refresh*
    minted. A fix that attaches the org at exchange but drops it when re-minting
    would pass a single-rotation test and still strand the extension on its
    second hour.
    """
    verifier, challenge = pkce_pair

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=TENANT
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

        assert _claims(tokens.access_token)["organization_id"] == TENANT
        assert _claims(tokens.refresh_token)["organization_id"] == TENANT

        for _ in range(2):
            tokens = await oauth_service.refresh_access_token(
                refresh_token=tokens.refresh_token, client_id="faultmaven-copilot"
            )
            assert _claims(tokens.access_token)["organization_id"] == TENANT
            assert _claims(tokens.refresh_token)["organization_id"] == TENANT


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_without_a_captured_org_fails_closed_under_multi(
    oauth_service, pkce_pair
):
    """A code carrying no tenant mints a claim no tenanted request can use.

    The negative control, and it asserts BOTH halves on purpose.

    ``resolve_organization_claim`` (which emits the claim) and ``usable_tenant_id``
    (which decides whether a claim is a tenant) are two independent copies of the
    same sentinel-rejection rule. So an assertion routed only through
    ``usable_tenant_id`` cannot detect ``resolve_organization_claim`` breaking:
    mutating it to emit the Standalone sentinel under multi leaves such an
    assertion green, because the predicate collapses the sentinel to ``None``
    too. An earlier version of this test made exactly that mistake, on the
    reasoning that the shared predicate was the stronger assertion — it is the
    weaker one for this failure, and adversarial review demonstrated it.

    So: the literal pins what is actually emitted, and the predicate pins the
    downstream consequence. Neither is redundant, because each is the only check
    on its own copy of the rule.
    """
    verifier, challenge = pkce_pair

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=None
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

        for token in (tokens.access_token, tokens.refresh_token):
            claim = _claims(token)["organization_id"]
            # What was emitted — catches a mint-side default to the sentinel.
            # `== ""` already excludes the sentinel, which is non-empty. An
            # extra `!= sentinel` line here looked like a second guard but could
            # never fail once this one passed.
            assert claim == ""
            # What it means downstream — catches the predicate going permissive.
            assert usable_tenant_id(claim) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_exchange_still_claims_the_standalone_org(
    oauth_service, pkce_pair
):
    """Standalone is unaffected: no captured org, sentinel claim, as before.

    The authorize leg on a Standalone deployment passes the sentinel through, and
    a deployment upgraded mid-flight can redeem a code that predates the column
    and carries nothing. Both must keep working.
    """
    verifier, challenge = pkce_pair

    for captured in (None, SingleTenantProvider.DEFAULT_ORG_ID):
        with _SINGLE:
            code = await oauth_service.create_authorization_code(
                "user_123",
                _authorization_request(challenge),
                organization_id=captured,
            )
            tokens = await oauth_service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )

        claims = _claims(tokens.access_token)
        assert claims["organization_id"] == SingleTenantProvider.DEFAULT_ORG_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_deactivated_account_cannot_redeem_a_code(
    oauth_service, user_repository, token_generator, pkce_pair
):
    """Deactivation must stop the exchange, not just the tokens already out.

    The revocation watermark cannot cover this: ``is_user_revoked`` compares
    ``iat <= watermark``, and a token minted *by this exchange* is newer than the
    watermark by construction.

    The watermark half is asserted in BOTH directions, and that is the whole
    point of it. Asserting only that a now-minted token escapes the watermark
    proves nothing, because ``is_user_revoked`` returns ``False`` for a key that
    was never written — the assertion passes just as happily if the watermark
    does not exist at all. An earlier version of this test did exactly that, and
    adversarial review killed it by making ``revoke_user_tokens_before`` a no-op
    with the suite still green. Showing the watermark is LIVE first is what makes
    the escape meaningful.

    An authorization code lives ten minutes, which is a wide enough window for
    one issued just before deactivation to be redeemed just after it.
    """
    verifier, challenge = pkce_pair
    store = token_generator.revocation_store

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=TENANT
        )

        # The account is deactivated while the code is still live, exactly as
        # user_service.deactivate_user does: flag cleared, then tokens revoked.
        user_repository.get.return_value.is_active = False
        watermark = int(datetime.now(timezone.utc).timestamp())
        await store.revoke_user_tokens_before("user_123", watermark, 3600)

        with pytest.raises(InvalidGrantError) as refusal:
            await oauth_service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )

    assert refusal.value.error_code == "USER_INACTIVE"

    # The watermark is real and it does kill the account's OLDER tokens...
    assert await store.is_user_revoked("user_123", watermark - 1) is True
    # ...and yet a token minted after it walks straight through, which is why the
    # explicit guard above has to exist.
    assert await store.is_user_revoked("user_123", watermark + 1) is False


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_ignores_any_org_on_the_stored_user(
    oauth_service, user_repository, pkce_pair
):
    """The code's tenant wins over whatever the user store hands back.

    ``DevUser.__post_init__`` stamps the Standalone sentinel on every user the
    store loads, so the object arriving at mint time routinely carries an org
    that is not this session's tenant. The authorization code — issued under a
    verified, RLS-bound session — is the authority, and a mutation that reversed
    this precedence would silently pool tenants.
    """
    verifier, challenge = pkce_pair
    user_repository.get.return_value.organization_id = OTHER_TENANT

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=TENANT
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

    assert _claims(tokens.access_token)["organization_id"] == TENANT


# =============================================================================
# Each storage hop must carry the field
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_memory_repository_keeps_the_org_across_mark_used():
    """``claim_code`` must not rebuild the DTO into a lossy subset."""
    repo = InMemoryOAuthCodeRepository()
    await repo.save_code(
        OAuthCodeDTO(
            code="code_1",
            user_id="user_123",
            redirect_uri=REDIRECT,
            code_challenge="challenge",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            organization_id=TENANT,
        )
    )

    assert await repo.claim_code("code_1") is True

    stored = await repo.get_code("code_1")
    assert stored.used is True
    assert stored.organization_id == TENANT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_repository_round_trips_the_org():
    """The cloud repository — the one multi-tenant actually runs on."""
    fakeredis = pytest.importorskip("fakeredis")
    repo = RedisOAuthCodeRepository(fakeredis.aioredis.FakeRedis(decode_responses=True))

    await repo.save_code(
        OAuthCodeDTO(
            code="code_2",
            user_id="user_123",
            redirect_uri=REDIRECT,
            code_challenge="challenge",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            organization_id=TENANT,
        )
    )

    assert (await repo.get_code("code_2")).organization_id == TENANT

    # …and across the used-marking rewrite, which re-serializes the payload.
    assert await repo.claim_code("code_2") is True
    stored = await repo.get_code("code_2")
    assert stored.used is True
    assert stored.organization_id == TENANT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_repository_reads_a_payload_written_before_the_column():
    """A rolling deploy must not 500 on a code the user legitimately holds.

    Mid-rollout this store holds payloads written by the previous version, which
    have no ``organization_id`` key at all. Those must decode to "no tenant
    captured" — which then fails closed at mint — rather than raising.
    """
    import json

    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisOAuthCodeRepository(client)

    await client.setex(
        "oauth:code:legacy",
        600,
        json.dumps(
            {
                "code": "legacy",
                "user_id": "user_123",
                "redirect_uri": REDIRECT,
                "code_challenge": "challenge",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
                "used": False,
            }
        ),
    )

    stored = await repo.get_code("legacy")
    assert stored is not None
    assert stored.organization_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_repository_tolerates_a_field_the_dto_no_longer_declares():
    """The other rolling-deploy direction: a payload with a RETIRED field.

    ``get_code`` filters to the DTO's declared fields. Without that filter,
    ``OAuthCodeDTO(**data)`` raises ``TypeError`` on an unexpected key — which the
    service does not catch, so it surfaces as a 500 on a code the user
    legitimately holds rather than as a clean grant error.

    This guard is not reachable today (nothing writes an undeclared key), so it is
    tested rather than left to be discovered the first time a field is retired
    during a straddled deploy. Adversarial review found it was the one guard in
    the change that no test exercised.
    """
    import json

    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisOAuthCodeRepository(client)

    await client.setex(
        "oauth:code:retired",
        600,
        json.dumps(
            {
                "code": "retired",
                "user_id": "user_123",
                "redirect_uri": REDIRECT,
                "code_challenge": "challenge",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
                "used": False,
                "organization_id": TENANT,
                "a_field_a_later_version_removed": "zzz",
            }
        ),
    )

    stored = await repo.get_code("retired")
    assert stored is not None
    assert stored.organization_id == TENANT


# =============================================================================
# The mint-time mutation, against the real user types
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_org_attaches_to_the_real_user_types_not_just_a_mock(pkce_pair):
    """The exchange's ``setattr`` must work on what the store really returns.

    The suite above uses a ``Mock`` for the user, which cannot establish this:
    the divergence that matters is that a real ``DevUser`` arrives already
    carrying the Standalone sentinel (stamped by ``__post_init__``) where the
    mock carries nothing, so only the real type exercises the exchange
    OVERWRITING a competing value rather than filling a blank.

    Note what this does *not* prove: ``DevUser`` is a plain mutable dataclass, so
    the ``setattr`` itself is trivially satisfied and a frozen-dataclass or
    ``validate_assignment`` hazard is not exercised here — no such type is on
    this path. The sentinel-overwrite property is the load-bearing half.

    So this runs the real exchange over a real ``DevUser``. That is the concrete
    type, traced rather than assumed: the container passes ``container.user_store``
    as the service's ``user_repository``, and the wired store exposing ``.get()``
    is ``DatabaseUserStore``, whose ``get()`` returns ``DevUser``. (The
    ``domain/models/user.py`` ``User`` is deliberately NOT exercised here — it has
    no ``username``, which the generators read, so it cannot be on this path.)

    It runs over BOTH generators. Everything else in this file uses HS256, but
    ``AUTH_MODE=oauth`` — the cloud deployment that is the entire reason #872
    exists — signs with RS256. The re-attach and the RS256 payload are each
    covered separately elsewhere; without this the composition of the two is only
    inferred, on the generator that deployment does not use.

    Each leg builds its own user, because the legs are not independent otherwise:
    the single-tenant assertion would pass off the multi leg's leftover value.
    Under multi the captured tenant reaches the claim; under single the Standalone
    sentinel survives the ``or None`` wipe because ``resolve_organization_claim``
    restores it — the "no-op there" the source comment claims, checked against the
    type that actually stamps that sentinel.
    """
    from faultmaven.modules.auth.domain.models.auth import DevUser

    verifier, challenge = pkce_pair
    fakeredis = pytest.importorskip("fakeredis")

    def _fresh_service(generator):
        """A service over a FRESH real DevUser, so no leg inherits another's org."""
        real_user = DevUser(
            user_id="user_123",
            username="testuser",
            email="testuser@acme.example",
            display_name="Test User",
            created_at=datetime.now(timezone.utc),
        )
        # The premise this test exists for: a real DevUser arrives already
        # carrying the sentinel, so the exchange overwrites rather than fills.
        assert real_user.organization_id == SingleTenantProvider.DEFAULT_ORG_ID
        users = AsyncMock()
        users.get = AsyncMock(return_value=real_user)
        return OAuthServiceImpl(
            code_repository=InMemoryOAuthCodeRepository(),
            user_repository=users,
            token_generator=generator,
            settings=AuthSettings(
                oauth_allowed_clients=["faultmaven-copilot"],
                oauth_redirect_uri_patterns=[
                    r"^chrome-extension://[a-z0-9]+/callback\.html$"
                ],
            ),
        )

    def _store():
        return RedisTokenRevocationStore(
            fakeredis.aioredis.FakeRedis(decode_responses=True)
        )

    def _rs256():
        """RS256 over a throwaway key pair — the cloud/AUTH_MODE=oauth signer."""
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
        generator = RS256JWTTokenGenerator(
            private_key=private_pem,
            public_key=public_pem,
            revocation_store=_store(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        return generator, {"key": public_pem, "algorithms": ["RS256"]}

    def _hs256():
        generator = HS256JWTTokenGenerator(
            secret_key=SECRET,
            revocation_store=_store(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        return generator, {"key": SECRET, "algorithms": ["HS256"]}

    for build in (_hs256, _rs256):
        generator, decode_with = build()
        algorithm = decode_with["algorithms"][0]

        def claim_of(token):
            return jwt.decode(
                token,
                audience=AUDIENCE,
                issuer=ISSUER,
                **decode_with,
            )["organization_id"]

        with _MULTI:
            service = _fresh_service(generator)
            code = await service.create_authorization_code(
                "user_123", _authorization_request(challenge), organization_id=TENANT
            )
            tokens = await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )
        assert claim_of(tokens.access_token) == TENANT, algorithm
        assert claim_of(tokens.refresh_token) == TENANT, algorithm

        with _SINGLE:
            service = _fresh_service(generator)
            code = await service.create_authorization_code(
                "user_123", _authorization_request(challenge), organization_id=None
            )
            tokens = await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )
        assert (
            claim_of(tokens.access_token) == SingleTenantProvider.DEFAULT_ORG_ID
        ), algorithm
