"""Both tenancy claims survive the OAuth-PKCE token chain (#872, ADR-017).

The copilot authenticates through ``GET|POST /auth/oauth/authorize`` (an
authenticated dashboard request) and then redeems the code at
``POST /auth/oauth/token`` (**unauthenticated** — it carries a code and a PKCE
verifier, no bearer token). ADR-017 splits what used to be one claim in two, and
the two travel by DIFFERENT routes, which is the whole subject of this module:

**Isolation** (``enterprise_id``) is minted at redemption from
``users.enterprise_id``. The column is NOT NULL and it is on the row the exchange
already loads, so nothing has to carry it across the hop — and nothing may,
because a value carried in a hand-off artifact is a value an attacker who obtains
the artifact chooses. ``bind_request_enterprise_context`` refuses a token without
this claim outright (no fallback to the user row), so a mint that drops it
strands the session on its first API call.

**Billing** (``organization_id``) still has to travel with the code. The ``users``
row carries no organization — membership lives in ``organization_members`` — so
the authorize leg, which runs under a session that knows who pays, is the only
place the value exists. Absence is a legitimate answer under ADR-017 D2: an
account in no organization mints no claim at all, and that is not a failure.

These tests assert at the **surface that renders the claims** — the decoded JWT —
rather than on the user object handed to the generator. Attaching the billing org
to the user is the mechanism; the claims in the token are the guarantee.

The chain is exercised leg by leg, and then end to end: authorize → exchange →
refresh → refresh again, because a claim that survives the first mint but not
rotation still strands the extension a few minutes later.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import jwt
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
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

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

SECRET = "test-secret-key-for-hs256-signing-only"
#: The BILLING organization the authorize leg captures.
BILLING_ORG = "org_acme_7f3c"
OTHER_ORG = "org_globex_9b1d"
#: The ISOLATION enterprise, which lives on the user row rather than the code.
ENTERPRISE = "ent_acme_11a2"
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
    """A user store that returns an ANCHORED but organization-less user.

    Both halves are deliberate, and they are the two routes under test:

    * ``enterprise_id`` IS on the row, because ``users.enterprise_id`` is NOT
      NULL (ADR-017 D3) and is where the isolation claim is minted from.
    * there is no ``organization_id``, as the real row has none — which is
      precisely why the billing claim has to travel with the code. A fixture
      that pre-stamped it would make the #872 fix untestable: the test would
      pass without it.
    """
    repo = AsyncMock()
    user = Mock(
        spec=["user_id", "username", "email", "roles", "is_active", "enterprise_id"]
    )
    user.user_id = "user_123"
    user.username = "testuser"
    user.email = "testuser@acme.example"
    user.roles = ["user"]
    user.is_active = True
    user.enterprise_id = ENTERPRISE
    repo.get = AsyncMock(return_value=user)
    return repo


@pytest.fixture
def unanchored_user_repository(user_repository):
    """The same store, for an account carrying no enterprise at all.

    ``users.enterprise_id`` is NOT NULL, so this shape does not survive a write
    — but the mint path must still fail closed on it rather than invent an
    anchor, because the alternative (defaulting to the Standalone sentinel under
    multi) pools every unanchored account into the deployment's global-KB
    tenant.
    """
    user = Mock(spec=["user_id", "username", "email", "roles", "is_active"])
    user.user_id = "user_123"
    user.username = "testuser"
    user.email = "testuser@acme.example"
    user.roles = ["user"]
    user.is_active = True
    user_repository.get = AsyncMock(return_value=user)
    return user_repository


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
async def test_both_claims_survive_authorize_exchange_and_repeated_rotation(
    oauth_service, pkce_pair
):
    """Every token the chain mints carries the isolation AND the billing claim.

    Rotation is exercised twice, not once: the first refresh reads the claims
    from the token the exchange minted, the second reads them from a token the
    *refresh* minted. A fix that attaches them at exchange but drops one when
    re-minting would pass a single-rotation test and still strand the extension
    on its second hour.

    The two claims are asserted together on every leg because they fail
    independently — the enterprise is re-derived from the row each time, the
    organization is re-attached from the presented token each time, and neither
    mechanism protects the other.
    """
    verifier, challenge = pkce_pair

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=BILLING_ORG
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

        for token in (tokens.access_token, tokens.refresh_token):
            assert _claims(token)["enterprise_id"] == ENTERPRISE
            assert _claims(token)["organization_id"] == BILLING_ORG

        for _ in range(2):
            tokens = await oauth_service.refresh_access_token(
                refresh_token=tokens.refresh_token, client_id="faultmaven-copilot"
            )
            for token in (tokens.access_token, tokens.refresh_token):
                assert _claims(token)["enterprise_id"] == ENTERPRISE
                assert _claims(token)["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_code_carrying_no_organization_still_isolates(oauth_service, pkce_pair):
    """No billing claim, and the isolation claim is untouched by its absence.

    Under ADR-017 D2 "this account is in no organization" is an ordinary steady
    state, so the correct rendering is the ABSENCE of the claim — not an empty
    string, and emphatically not a sentinel some later reader could mistake for
    a tenant. The token is fully usable: it isolates on the enterprise, which
    never travelled with the code in the first place.

    Both halves are asserted because they fail independently. A mint that emitted
    ``organization_id: ""`` would pass a test that only checked the enterprise,
    and a mint that derived isolation from the captured organization would pass
    a test that only checked the absence.
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
            claims = _claims(token)
            assert "organization_id" not in claims
            assert claims["enterprise_id"] == ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unanchored_account_mints_an_unusable_isolation_claim_under_multi(
    unanchored_user_repository, token_generator, pkce_pair
):
    """The fail-closed control, and it asserts BOTH halves on purpose.

    ``resolve_enterprise_claim`` (which emits the claim) and ``usable_tenant_id``
    (which decides whether a claim is a tenant) are two independent copies of the
    same sentinel-rejection rule. So an assertion routed only through
    ``usable_tenant_id`` cannot detect ``resolve_enterprise_claim`` breaking:
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
    service = OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=unanchored_user_repository,
        token_generator=token_generator,
        settings=AuthSettings(
            oauth_allowed_clients=["faultmaven-copilot"],
            oauth_redirect_uri_patterns=[
                r"^chrome-extension://[a-z0-9]+/callback\.html$"
            ],
        ),
    )

    with _MULTI:
        code = await service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=BILLING_ORG
        )
        tokens = await service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

        for token in (tokens.access_token, tokens.refresh_token):
            claim = _claims(token)["enterprise_id"]
            # What was emitted — catches a mint-side default to the sentinel.
            # ``== ""`` already excludes the sentinel, which is non-empty.
            assert claim == ""
            # What it means downstream — catches the predicate going permissive.
            assert usable_tenant_id(claim) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_exchange_claims_the_standalone_enterprise(
    unanchored_user_repository, token_generator, pkce_pair
):
    """Standalone: an unanchored account IS the deployment's one enterprise.

    The same shape that fails closed under multi is the correct answer here, and
    the billing claim stays absent — the Standalone deployment has no
    organization row at all (ADR-017 D8), so there is nothing for the authorize
    leg to capture and nothing to mint.
    """
    verifier, challenge = pkce_pair
    service = OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=unanchored_user_repository,
        token_generator=token_generator,
        settings=AuthSettings(
            oauth_allowed_clients=["faultmaven-copilot"],
            oauth_redirect_uri_patterns=[
                r"^chrome-extension://[a-z0-9]+/callback\.html$"
            ],
        ),
    )

    with _SINGLE:
        code = await service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=None
        )
        tokens = await service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

    claims = _claims(tokens.access_token)
    assert claims["enterprise_id"] == STANDALONE_ENTERPRISE_ID
    assert "organization_id" not in claims


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
            "user_123",
            _authorization_request(challenge),
            organization_id=BILLING_ORG,
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
async def test_exchange_ignores_any_organization_on_the_stored_user(
    oauth_service, user_repository, pkce_pair
):
    """The code's billing organization wins over whatever the store hands back.

    The user object arriving at mint time can carry a stale ``organization_id``
    — the repository may return its own model, and nothing keeps that column in
    step with ``organization_members``. The authorization code, issued under a
    verified session that read the live membership, is the authority.

    Note the scope of the claim being made: this is BILLING precedence. It does
    not pool tenants either way under ADR-017, because isolation is the
    enterprise and the enterprise is never read from the code — which the
    assertion below pins alongside it, so a mutation that started deriving
    isolation from the captured value has somewhere to go red.
    """
    verifier, challenge = pkce_pair
    user_repository.get.return_value.organization_id = OTHER_ORG

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123",
            _authorization_request(challenge),
            organization_id=BILLING_ORG,
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

    claims = _claims(tokens.access_token)
    assert claims["organization_id"] == BILLING_ORG
    assert claims["enterprise_id"] == ENTERPRISE


# =============================================================================
# Each storage hop must carry the field
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_memory_repository_keeps_the_organization_across_mark_used():
    """``claim_code`` must not rebuild the DTO into a lossy subset."""
    repo = InMemoryOAuthCodeRepository()
    await repo.save_code(
        OAuthCodeDTO(
            code="code_1",
            user_id="user_123",
            redirect_uri=REDIRECT,
            code_challenge="challenge",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            organization_id=BILLING_ORG,
        )
    )

    assert await repo.claim_code("code_1") is True

    stored = await repo.get_code("code_1")
    assert stored.used is True
    assert stored.organization_id == BILLING_ORG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_repository_round_trips_the_organization():
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
            organization_id=BILLING_ORG,
        )
    )

    assert (await repo.get_code("code_2")).organization_id == BILLING_ORG

    # …and across the used-marking rewrite, which re-serializes the payload.
    assert await repo.claim_code("code_2") is True
    stored = await repo.get_code("code_2")
    assert stored.used is True
    assert stored.organization_id == BILLING_ORG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_repository_reads_a_payload_written_before_the_column():
    """A rolling deploy must not 500 on a code the user legitimately holds.

    Mid-rollout this store holds payloads written by the previous version, which
    have no ``organization_id`` key at all. Those must decode to "no
    organization captured" — which mints no billing claim — rather than raising.
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
                "organization_id": BILLING_ORG,
                "a_field_a_later_version_removed": "zzz",
            }
        ),
    )

    stored = await repo.get_code("retired")
    assert stored is not None
    assert stored.organization_id == BILLING_ORG


# =============================================================================
# The mint-time mutation, against the real user types
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_claims_attach_to_the_real_user_types_not_just_a_mock(pkce_pair):
    """The exchange's ``setattr`` must work on what the store really returns.

    The suite above uses a ``Mock`` for the user, which cannot establish this:
    the divergence that matters is that a real ``DevUser`` arrives already
    carrying the Standalone ENTERPRISE (stamped by ``__post_init__``) where the
    mock carries whatever the fixture set, so only the real type exercises the
    mint reading an anchor it did not choose.

    Note what this does *not* prove: ``DevUser`` is a plain mutable dataclass, so
    the ``setattr`` itself is trivially satisfied and a frozen-dataclass or
    ``validate_assignment`` hazard is not exercised here — no such type is on
    this path.

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
    Under multi the captured organization reaches the billing claim and the
    account's own enterprise reaches the isolation claim; under single the
    account carries the Standalone enterprise and no organization at all, so the
    billing claim is absent — which is the "no organization row exists there"
    D8 rule, checked against the type that actually stamps the sentinel.
    """
    from faultmaven.modules.auth.domain.models.auth import DevUser

    verifier, challenge = pkce_pair
    fakeredis = pytest.importorskip("fakeredis")

    def _fresh_service(generator, enterprise_id):
        """A service over a FRESH real DevUser, so no leg inherits another's."""
        real_user = DevUser(
            user_id="user_123",
            username="testuser",
            email="testuser@acme.example",
            display_name="Test User",
            created_at=datetime.now(timezone.utc),
            enterprise_id=enterprise_id,
        )
        # The premise this test exists for: a real DevUser arrives carrying an
        # anchor of its own and NO organization, so the mint reads isolation
        # off the row and billing off the code — never one from the other.
        assert real_user.organization_id is None
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

        def claims_of(token):
            return jwt.decode(
                token,
                audience=AUDIENCE,
                issuer=ISSUER,
                **decode_with,
            )

        with _MULTI:
            service = _fresh_service(generator, ENTERPRISE)
            code = await service.create_authorization_code(
                "user_123",
                _authorization_request(challenge),
                organization_id=BILLING_ORG,
            )
            tokens = await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )
        for token in (tokens.access_token, tokens.refresh_token):
            assert claims_of(token)["organization_id"] == BILLING_ORG, algorithm
            assert claims_of(token)["enterprise_id"] == ENTERPRISE, algorithm

        with _SINGLE:
            service = _fresh_service(generator, None)
            code = await service.create_authorization_code(
                "user_123", _authorization_request(challenge), organization_id=None
            )
            tokens = await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )
        claims = claims_of(tokens.access_token)
        assert claims["enterprise_id"] == STANDALONE_ENTERPRISE_ID, algorithm
        assert "organization_id" not in claims, algorithm
