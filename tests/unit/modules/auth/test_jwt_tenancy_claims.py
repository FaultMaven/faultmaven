"""The token's two tenancy claims (ADR-010 P2, #629, re-keyed by ADR-017 D9).

A token carries an ``enterprise_id`` (isolation) and, when somebody pays for the
account, an ``organization_id`` (billing). They come from different fields, mean
different things, and fail in opposite directions — which is what this module
pins.

``bind_request_enterprise_context`` refuses a token with no usable enterprise
claim. That guard is only reachable if the *token* says so: a generator that
substituted the Standalone sentinel for an unanchored account would silently
bind every such account to one shared tenant — the one holding the global-KB
write licence the policy keys on that very id.

The guarantees under test:

* under multi-tenant, **no token is ever minted carrying the Standalone
  sentinel** for an account with no enterprise — for either signing algorithm,
  for the access AND the refresh token, and for every way "no enterprise" can be
  spelled;
* single-tenant keeps the sentinel, where it is the correct answer;
* the organization claim is **omitted** when there is none, because absence is
  the answer for an account nobody pays for, and a sentinel there is what a
  later reader would mistake for a tenant.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import jwt
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services import jwt_token_generator
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    resolve_billing_organization,
    resolve_enterprise_claim,
)
from faultmaven.providers.tenancy import factory as tenancy_factory

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

REAL_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
BILLING_ORG = "44444444-4444-4444-4444-444444444444"

# Every shape an "unanchored" account arrives in. The sentinel spellings matter
# most: under multi the Standalone enterprise is not a tenant, and
# `DevUser.__post_init__` stamps it on every user `DatabaseUserStore` loads —
# which is exactly what the `/auth/refresh` and OAuth token-exchange paths hand
# to the generators.
ANCHORLESS_USERS = [
    pytest.param(lambda: _user(enterprise_id=None), id="enterprise-none"),
    pytest.param(lambda: _user(enterprise_id=""), id="enterprise-empty-string"),
    pytest.param(
        lambda: _user_without_enterprise_attribute(), id="enterprise-attribute-absent"
    ),
    pytest.param(
        lambda: _user(enterprise_id=STANDALONE_ENTERPRISE_ID),
        id="enterprise-sentinel-valued",
    ),
    pytest.param(
        lambda: _devuser_from_user_store(), id="enterprise-via-DatabaseUserStore"
    ),
]


def _user(*, enterprise_id):
    """A mint-path user. ``spec`` keeps MagicMock from inventing attributes.

    Without it every ``getattr(user, ...)`` in the resolvers answers a Mock, and
    an assertion about a *missing* field could never fail.
    """
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
    user.user_id = "user-1"
    user.username = "sso-user"
    user.email = "sso-user@example.com"
    user.roles = ["user"]
    user.is_active = True
    user.enterprise_id = enterprise_id
    user.organization_id = None
    return user


def _devuser_from_user_store():
    """A DevUser built by the *real* ``DatabaseUserStore`` conversion.

    This is the live `/auth/refresh` and OAuth token-exchange shape: the
    repository model carries no organization at all, so `DevUser.__post_init__`
    invents the Standalone sentinel. Constructing it through the real converter
    (rather than asserting the sentinel by hand) means this case keeps tracking
    that path if the conversion changes.
    """
    from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore

    repo_user = MagicMock(
        spec=[
            "user_id",
            "username",
            "email",
            "display_name",
            "created_at",
            "is_active",
            "roles",
            "account_kind",
            "enterprise_id",
        ]
    )
    repo_user.enterprise_id = None
    repo_user.user_id = "user-1"
    repo_user.username = "sso-user"
    repo_user.email = "sso-user@example.com"
    repo_user.display_name = "SSO User"
    repo_user.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    repo_user.is_active = True
    repo_user.roles = ["user"]
    repo_user.account_kind = "individual"

    store = DatabaseUserStore.__new__(DatabaseUserStore)
    return store._user_to_devuser(repo_user)


def _user_without_enterprise_attribute():
    # spec= keeps MagicMock from auto-creating enterprise_id on access.
    # `is_active` is in the spec because this fixture varies the TENANT
    # attribute, not account liveness — and the mint gate refuses a user with no
    # liveness flag, so omitting it here would fail for an unrelated reason.
    user = MagicMock(spec=["user_id", "username", "email", "roles", "is_active"])
    user.is_active = True
    user.user_id = "user-1"
    user.username = "sso-user"
    user.email = "sso-user@example.com"
    user.roles = ["user"]
    return user


@pytest.fixture
def as_tenant_provider(monkeypatch):
    """Drive the real ``TenantProvider`` enum through the real coercion path.

    Patching ``get_settings`` (rather than ``requested_tenant_provider``) keeps
    ``coerce_provider_name`` in the loop, so a rename of the enum member breaks
    this test instead of silently passing a dead gate.
    """

    def _apply(provider: TenantProvider):
        settings = MagicMock()
        settings.providers.tenant_provider = provider
        monkeypatch.setattr(tenancy_factory, "get_settings", lambda: settings)

    return _apply


def _rs256_generator():
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
    return (
        RS256JWTTokenGenerator(
            private_key=private_pem,
            public_key=public_pem,
            revocation_store=MagicMock(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        ),
        {"key": public_pem, "algorithms": ["RS256"]},
    )


def _hs256_generator():
    secret = "unit-test-secret-not-a-real-key-padded-to-32-bytes"
    return (
        HS256JWTTokenGenerator(
            secret_key=secret,
            revocation_store=MagicMock(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        ),
        {"key": secret, "algorithms": ["HS256"]},
    )


GENERATORS = [
    pytest.param(_rs256_generator, id="RS256"),
    pytest.param(_hs256_generator, id="HS256"),
]

MINTERS = [
    pytest.param("generate_access_token", id="access"),
    pytest.param("generate_refresh_token", id="refresh"),
]


async def _mint(generator, minter, user):
    token = await getattr(generator, minter)(
        user, state_read_at=datetime.now(timezone.utc)
    )
    return token


# =============================================================================
# The enterprise claim — the token's only isolation input
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_the_enterprise_claim_is_minted_from_the_account_row(
    as_tenant_provider, build_generator, minter
):
    """``users.enterprise_id``, on BOTH tokens, for BOTH algorithms.

    The refresh token matters as much as the access token: rotation is the only
    thing that carries tenancy across an access token's lifetime, and a refresh
    pair minted without the claim is a dead credential at the next request.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator()

    token = await _mint(generator, minter, _user(enterprise_id=REAL_ENTERPRISE))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["enterprise_id"] == REAL_ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
@pytest.mark.parametrize("make_user", ANCHORLESS_USERS)
async def test_no_token_invents_the_sentinel_for_an_unanchored_account(
    as_tenant_provider, build_generator, minter, make_user
):
    """Under multi the Standalone sentinel is not a tenant.

    Every spelling of "no enterprise" is swept, including the one
    ``DevUser.__post_init__`` invents on every user the store loads — which is
    exactly what the refresh and OAuth-exchange paths hand these generators.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator()

    token = await _mint(generator, minter, make_user())

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["enterprise_id"] == ""
    assert claims["enterprise_id"] != STANDALONE_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_single_tenant_keeps_the_sentinel(
    as_tenant_provider, build_generator, minter
):
    """Where the sentinel IS the deployment's one tenant, it is the right claim."""
    as_tenant_provider(TenantProvider.SINGLE)
    generator, verify = build_generator()

    token = await _mint(generator, minter, _user(enterprise_id=None))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["enterprise_id"] == STANDALONE_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_the_claim_is_always_present_even_when_empty(
    as_tenant_provider, build_generator, minter
):
    """Present-and-empty, not absent.

    Both spellings are refused by the binder, so this is not a security
    difference — it is a diagnosability one: an empty claim says "this mint
    could not resolve a tenant", while a missing key is indistinguishable from
    a token minted by something that never heard of the field.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator()

    token = await _mint(generator, minter, _user(enterprise_id=None))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "enterprise_id" in claims


# =============================================================================
# The organization claim — billing, and absent when there is none
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_the_billing_organization_rides_when_there_is_one(
    as_tenant_provider, build_generator, minter
):
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator()
    user = _user(enterprise_id=REAL_ENTERPRISE)
    user.organization_id = BILLING_ORG

    token = await _mint(generator, minter, user)

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
@pytest.mark.parametrize("no_org", [None, ""])
async def test_an_account_in_no_organization_gets_no_organization_claim(
    as_tenant_provider, build_generator, minter, no_org
):
    """OMITTED, not empty, and never a sentinel.

    Absence *is* the answer for an account nobody pays for (ADR-017 D5), and it
    is the opposite convention from the enterprise claim on purpose: an empty
    enterprise is a failed resolution the binder must refuse, while an empty
    organization would be a value some reader could mistake for a tenant.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator()
    user = _user(enterprise_id=REAL_ENTERPRISE)
    user.organization_id = no_org

    token = await _mint(generator, minter, user)

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_single_tenant_mints_no_organization_claim_either(
    as_tenant_provider, build_generator
):
    """A standalone deployment has no organization at all (ADR-017 D8).

    The sentinel arm applies to the ENTERPRISE and to nothing else: an
    organization sentinel would be a billing subject nobody agreed to.
    """
    as_tenant_provider(TenantProvider.SINGLE)
    generator, verify = build_generator()

    token = await _mint(generator, "generate_access_token", _user(enterprise_id=None))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims


# =============================================================================
# The resolvers, directly
# =============================================================================


@pytest.mark.unit
def test_resolve_billing_organization_invents_nothing():
    assert resolve_billing_organization(_user(enterprise_id="e")) is None

    user = _user(enterprise_id="e")
    user.organization_id = BILLING_ORG
    assert resolve_billing_organization(user) == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
def test_the_two_resolvers_read_two_different_fields(as_tenant_provider):
    """The whole of ADR-017 D1/D2, as one assertion.

    A user anchored to enterprise E and billed to organization O must produce
    E for isolation and O for billing. A resolver that read the wrong field
    would still return *a* string, which is why this pins them against each
    other rather than each alone.
    """
    as_tenant_provider(TenantProvider.MULTI)
    user = _user(enterprise_id=REAL_ENTERPRISE)
    user.organization_id = BILLING_ORG

    assert resolve_enterprise_claim(user) == REAL_ENTERPRISE
    assert resolve_billing_organization(user) == BILLING_ORG
