"""Tenancy survives token rotation (#869, #873).

An access token lives minutes; a session lives days. The only thing that
carries the organization across that gap is the refresh token, because the user
store's model has **no** organization column — a refresh reloads the user, and a
reloaded user is org-less. Before this, the first refresh after a mapped SSO
login minted an org-less pair and every subsequent request failed closed at
``bind_request_enterprise_context`` (the #850 trap).

The guarantee under test: **a refresh token carries the organization claim, and
every refresh path puts it back on the user before minting the next pair** — for
both signing algorithms. FaultMaven has two such paths and both are covered
here: ``POST /auth/refresh`` (dashboard) and the oauth refresh grant
``POST /auth/oauth/token`` with ``grant_type=refresh_token`` (Copilot, and the
D10 service-account credential). The #850 backstop is unchanged on both: a token
that genuinely carries no organization still mints an empty claim under
multi-tenant.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import AuthMode, AuthSettings, TenantProvider
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import TokenRefreshRequest
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    resolve_enterprise_claim,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.utils import InMemoryRevocationStore

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

pytestmark = pytest.mark.asyncio

REAL_ENTERPRISE = "22222222-2222-2222-2222-222222222222"


# =============================================================================
# Generators (real crypto, both algorithms)
# =============================================================================


# Token lifetimes are explicit generator arguments (#888), not a settings read.
ACCESS_MINUTES = 15
REFRESH_DAYS = 7


def _rs256_generator(revocation_store=None):
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
        revocation_store=revocation_store or InMemoryRevocationStore(),
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    return generator, {"key": public_pem, "algorithms": ["RS256"]}


def _hs256_generator(revocation_store=None):
    secret = "unit-test-secret-not-a-real-key-padded-to-32-bytes"
    generator = HS256JWTTokenGenerator(
        secret_key=secret,
        revocation_store=revocation_store or InMemoryRevocationStore(),
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    return generator, {"key": secret, "algorithms": ["HS256"]}


GENERATORS = [
    pytest.param(_rs256_generator, id="RS256"),
    pytest.param(_hs256_generator, id="HS256"),
]


def _decode(token, verify_args):
    return jwt.decode(
        token,
        verify_args["key"],
        algorithms=verify_args["algorithms"],
        audience=AUDIENCE,
        issuer=ISSUER,
    )


@pytest.fixture
def as_tenant_provider(monkeypatch):
    """Drive the real ``TenantProvider`` enum through the real coercion path."""

    def _apply(provider: TenantProvider):
        settings = MagicMock()
        settings.providers.tenant_provider = provider
        monkeypatch.setattr(tenancy_factory, "get_settings", lambda: settings)

    return _apply


def _user(*, enterprise_id):
    user = MagicMock(
        spec=[
            "user_id",
            "username",
            "email",
            "roles",
            "is_active",
            "enterprise_id",
            "organization_id",
        ]
    )
    user.is_active = True
    user.enterprise_id = enterprise_id
    user.organization_id = None
    user.user_id = "user-1"
    user.username = "sso-user"
    user.email = "sso-user@example.com"
    user.roles = ["user"]
    return user


# =============================================================================
# The refresh token carries the claim
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_refresh_token_carries_the_organization_claim(
    build_generator, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify_args = build_generator()

    token = await generator.generate_refresh_token(
        _user(enterprise_id=REAL_ENTERPRISE), state_read_at=datetime.now(timezone.utc)
    )

    assert _decode(token, verify_args)["enterprise_id"] == REAL_ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_refresh_token_of_an_orgless_user_carries_the_empty_claim(
    build_generator, as_tenant_provider
):
    """The #850 backstop is unchanged on the refresh token: no invented tenant."""
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify_args = build_generator()

    token = await generator.generate_refresh_token(
        _user(enterprise_id=None), state_read_at=datetime.now(timezone.utc)
    )

    assert _decode(token, verify_args)["enterprise_id"] == ""


@pytest.mark.unit
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_single_tenant_refresh_token_claims_the_standalone_org(
    build_generator, as_tenant_provider
):
    as_tenant_provider(TenantProvider.SINGLE)
    generator, verify_args = build_generator()

    token = await generator.generate_refresh_token(
        _user(enterprise_id=None), state_read_at=datetime.now(timezone.utc)
    )

    assert _decode(token, verify_args)["enterprise_id"] == STANDALONE_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_validate_refresh_token_returns_the_organization_claim(
    build_generator, as_tenant_provider
):
    """`/auth/refresh` reads the claim off the validated payload, so validation
    must not filter it out."""
    as_tenant_provider(TenantProvider.MULTI)
    generator, _ = build_generator()

    token = await generator.generate_refresh_token(
        _user(enterprise_id=REAL_ENTERPRISE), state_read_at=datetime.now(timezone.utc)
    )
    claims = await generator.validate_refresh_token(token)

    assert claims is not None
    assert claims["enterprise_id"] == REAL_ENTERPRISE


# =============================================================================
# /auth/refresh re-attaches it before minting
# =============================================================================


class _FakeUserStore:
    """Returns the org-less shape the real store returns: a DevUser whose
    ``__post_init__`` stamps the Standalone sentinel (the repository model has
    no organization column)."""

    def __init__(self, user):
        self._user = user

    async def get_user(self, user_id: str):
        if self._user and self._user.user_id == user_id:
            return self._user
        return None


def _dev_user(enterprise_id=None):
    return DevUser(
        user_id="user-1",
        username="sso-user",
        email="sso-user@example.com",
        display_name="SSO User",
        created_at=datetime.now(timezone.utc),
        enterprise_id=enterprise_id,
    )


def _fake_request(user_store, revocation_store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store, token_revocation_store=revocation_store
            )
        )
    )


def _route_patches(generator):
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL,
            jwt_access_token_expire_minutes=15,
        )
    )
    return (
        patch.object(auth_routes, "_build_local_jwt_generator", return_value=generator),
        patch.object(auth_routes, "get_settings", return_value=settings_stub),
    )


@pytest.mark.unit
@pytest.mark.security
async def test_refresh_mints_the_tenant_from_the_account_row(as_tenant_provider):
    """The rotated pair carries the enterprise the ACCOUNT is anchored to.

    Not the one the presented token claims — that is the ADR-017 change, and it
    is the sharper property: a session whose anchor was moved (a retirement, a
    personal→company switch) follows the move at its next rotation instead of
    being pinned by a token that predates it.
    """
    as_tenant_provider(TenantProvider.MULTI)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    # A refresh token minted while the account was anchored SOMEWHERE ELSE.
    old_refresh = await generator.generate_refresh_token(
        _user(enterprise_id="ent-the-account-has-since-left"),
        state_read_at=datetime.now(timezone.utc),
    )

    # The account row is the authority, and it says REAL_ENTERPRISE.
    dev_user = _dev_user(REAL_ENTERPRISE)
    request = _fake_request(_FakeUserStore(dev_user), revocation_store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _route_patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

    assert _decode(result.access_token, verify_args)["enterprise_id"] == REAL_ENTERPRISE
    assert (
        _decode(result.refresh_token, verify_args)["enterprise_id"] == REAL_ENTERPRISE
    )


@pytest.mark.unit
@pytest.mark.security
async def test_refresh_of_a_pre_mapping_token_still_fails_closed(as_tenant_provider):
    """A refresh token minted before org mapping existed carries no claim. It
    must NOT be healed into the Standalone sentinel — the empty claim is what
    makes ``bind_request_enterprise_context`` refuse."""
    as_tenant_provider(TenantProvider.MULTI)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    # Mint a refresh token WITHOUT the claim, the pre-#869 payload shape.
    legacy_payload_token = jwt.encode(
        {
            "sub": "user-1",
            "exp": datetime.now(timezone.utc).timestamp() + 3600,
            "iat": datetime.now(timezone.utc),
            "iss": ISSUER,
            "aud": AUDIENCE,
            "jti": "legacy-jti",
            "type": "refresh",
        },
        verify_args["key"],
        algorithm="HS256",
    )
    assert "enterprise_id" not in jwt.decode(
        legacy_payload_token,
        verify_args["key"],
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
    )

    dev_user = _dev_user()
    request = _fake_request(_FakeUserStore(dev_user), revocation_store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _route_patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=legacy_payload_token), request, response
        )

    # Empty claim, not the sentinel — the request will be refused at bind time.
    assert _decode(result.access_token, verify_args)["enterprise_id"] == ""
    assert _decode(result.refresh_token, verify_args)["enterprise_id"] == ""
    # And the helper agrees about what the user now carries.
    assert resolve_enterprise_claim(dev_user) == ""


@pytest.mark.unit
async def test_single_tenant_refresh_is_unaffected(as_tenant_provider):
    """Standalone regression guard: the sentinel keeps flowing through refresh."""
    as_tenant_provider(TenantProvider.SINGLE)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    old_refresh = await generator.generate_refresh_token(
        _dev_user(), state_read_at=datetime.now(timezone.utc)
    )
    request = _fake_request(_FakeUserStore(_dev_user()), revocation_store)
    response = SimpleNamespace(headers={})

    gen_patch, settings_patch = _route_patches(generator)
    with gen_patch, settings_patch:
        result = await auth_routes.refresh_tokens(
            TokenRefreshRequest(refresh_token=old_refresh), request, response
        )

    assert (
        _decode(result.access_token, verify_args)["enterprise_id"]
        == STANDALONE_ENTERPRISE_ID
    )
    assert (
        _decode(result.refresh_token, verify_args)["enterprise_id"]
        == STANDALONE_ENTERPRISE_ID
    )


# =============================================================================
# The oauth refresh grant re-attaches it too (#873)
# =============================================================================
#
# `POST /auth/oauth/token` with grant_type=refresh_token is the *other* refresh
# path — Copilot's, and the one the D10 service-account credential rotates
# through. It reloads the user from the repository exactly as `/auth/refresh`
# does, so it needs the same re-attachment or a claim-stamped credential loses
# its tenant on the first rotation.


class _FakeUserRepository:
    """The OAuth service's ``user_repository``, keyed the way it calls it.

    In production the composition root passes the *user store*, so ``get`` here
    returns the same org-less DevUser shape (sentinel-stamped) the real store
    returns.
    """

    def __init__(self, user):
        self._user = user

    async def get(self, user_id: str):
        if self._user and self._user.user_id == user_id:
            return self._user
        return None


def _oauth_service(user, generator) -> OAuthServiceImpl:
    return OAuthServiceImpl(
        code_repository=None,
        user_repository=_FakeUserRepository(user),
        token_generator=generator,
        # Real AuthSettings, not a Mock: a Mock settings object previously hid a
        # live AttributeError on this exact call path.
        settings=AuthSettings(auth_mode="oauth", oauth_enabled=True),
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_oauth_refresh_grant_mints_the_tenant_from_the_account_row(
    build_generator, as_tenant_provider
):
    """Without this, the service credential would come back tenant-less from its
    first rotation and then be refused on every request. The authority is the
    ACCOUNT ROW, not the presented token — same rule as ``/auth/refresh``."""
    as_tenant_provider(TenantProvider.MULTI)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = build_generator(revocation_store)

    presented = await generator.generate_refresh_token(
        _user(enterprise_id="ent-the-account-has-since-left"),
        state_read_at=datetime.now(timezone.utc),
    )

    service = _oauth_service(_dev_user(REAL_ENTERPRISE), generator)

    tokens = await service.refresh_access_token(
        refresh_token=presented, client_id="faultmaven-copilot"
    )

    assert _decode(tokens.access_token, verify_args)["enterprise_id"] == REAL_ENTERPRISE
    assert (
        _decode(tokens.refresh_token, verify_args)["enterprise_id"] == REAL_ENTERPRISE
    )


@pytest.mark.unit
@pytest.mark.security
async def test_oauth_refresh_grant_survives_repeated_rotation(as_tenant_provider):
    """The agent rotates continuously; the tenant must not decay after one hop."""
    as_tenant_provider(TenantProvider.MULTI)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    token = await generator.generate_refresh_token(
        _user(enterprise_id=REAL_ENTERPRISE), state_read_at=datetime.now(timezone.utc)
    )
    service = _oauth_service(_dev_user(REAL_ENTERPRISE), generator)

    for _ in range(3):
        tokens = await service.refresh_access_token(
            refresh_token=token, client_id="faultmaven-copilot"
        )
        assert (
            _decode(tokens.access_token, verify_args)["enterprise_id"]
            == REAL_ENTERPRISE
        )
        assert (
            _decode(tokens.refresh_token, verify_args)["enterprise_id"]
            == REAL_ENTERPRISE
        )
        token = tokens.refresh_token


@pytest.mark.unit
@pytest.mark.security
async def test_oauth_refresh_grant_of_an_orgless_token_still_fails_closed(
    as_tenant_provider,
):
    """The #850 backstop on this path too: an org-less presented token mints an
    empty claim — it still mints (rotation is not the place to break), and the
    request it is used for is refused at bind time."""
    as_tenant_provider(TenantProvider.MULTI)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    presented = await generator.generate_refresh_token(
        _user(enterprise_id=None), state_read_at=datetime.now(timezone.utc)
    )
    assert _decode(presented, verify_args)["enterprise_id"] == ""

    service = _oauth_service(_dev_user(), generator)
    tokens = await service.refresh_access_token(
        refresh_token=presented, client_id="faultmaven-copilot"
    )

    assert _decode(tokens.access_token, verify_args)["enterprise_id"] == ""
    assert _decode(tokens.refresh_token, verify_args)["enterprise_id"] == ""


@pytest.mark.unit
async def test_single_tenant_oauth_refresh_grant_is_unaffected(as_tenant_provider):
    """Standalone regression guard: the sentinel keeps flowing through this path."""
    as_tenant_provider(TenantProvider.SINGLE)
    revocation_store = InMemoryRevocationStore()
    generator, verify_args = _hs256_generator(revocation_store)

    presented = await generator.generate_refresh_token(
        _dev_user(), state_read_at=datetime.now(timezone.utc)
    )
    service = _oauth_service(_dev_user(), generator)

    tokens = await service.refresh_access_token(
        refresh_token=presented, client_id="faultmaven-copilot"
    )

    assert (
        _decode(tokens.access_token, verify_args)["enterprise_id"]
        == STANDALONE_ENTERPRISE_ID
    )
    assert (
        _decode(tokens.refresh_token, verify_args)["enterprise_id"]
        == STANDALONE_ENTERPRISE_ID
    )
