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
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO, OAuthCodeDTO
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
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
        audience="faultmaven-api",
        issuer="faultmaven",
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
    """A code carrying no tenant mints an EMPTY claim, never a defaulted one.

    This is the negative control, and it is the half that matters for isolation:
    the danger is not only losing the claim but silently substituting the
    Standalone sentinel, which under multi-tenant is not a tenant at all — it is
    the identity migration 033 keys the global-KB write policy on. An empty claim
    is refused at ``bind_request_org_context``; a sentinel claim would not be.
    """
    verifier, challenge = pkce_pair

    with _MULTI:
        code = await oauth_service.create_authorization_code(
            "user_123", _authorization_request(challenge), organization_id=None
        )
        tokens = await oauth_service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )

    claims = _claims(tokens.access_token)
    assert claims["organization_id"] == ""
    assert claims["organization_id"] != SingleTenantProvider.DEFAULT_ORG_ID


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
    """``mark_code_used`` must not rebuild the DTO into a lossy subset."""
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

    await repo.mark_code_used("code_1")

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
    await repo.mark_code_used("code_2")
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
