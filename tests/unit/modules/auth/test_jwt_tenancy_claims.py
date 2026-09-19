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
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    SUBJECT_ACCOUNT,
    SUBJECT_ORGANIZATION,
    billing_subject_for,
)
from faultmaven.models.interfaces_user import Organization
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services import jwt_token_generator
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    resolve_billing_organization,
    resolve_enterprise_claim,
)
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.utils import InMemoryRevocationStore

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
            "service_channel",
            "enterprise_id",
        ]
    )
    repo_user.enterprise_id = None
    repo_user.service_channel = None
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


def _rs256_generator(resolve_organizations=None):
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
            revocation_store=InMemoryRevocationStore(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
            resolve_organizations=resolve_organizations,
        ),
        {"key": public_pem, "algorithms": ["RS256"]},
    )


def _hs256_generator(resolve_organizations=None):
    secret = "unit-test-secret-not-a-real-key-padded-to-32-bytes"
    return (
        HS256JWTTokenGenerator(
            secret_key=secret,
            revocation_store=InMemoryRevocationStore(),
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
            resolve_organizations=resolve_organizations,
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
    assert (
        resolve_billing_organization(_user(enterprise_id="e"), enterprise_claim="e")
        is None
    )

    user = _user(enterprise_id="e")
    user.organization_id = BILLING_ORG
    assert resolve_billing_organization(user, enterprise_claim="e") == BILLING_ORG


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
    assert (
        resolve_billing_organization(user, enterprise_claim=REAL_ENTERPRISE)
        == BILLING_ORG
    )


# =============================================================================
# The organization claim, resolved from `organization_members`
#
# The claim used to be read off `user.organization_id` — a field the `users`
# table has no column for, and which nothing ever originated: the OAuth token
# exchange, `/auth/refresh` and the SSO exchange each re-attach what a previous
# token carried, so the chain had no source and the claim was never minted.
# These pin the resolution that gives it one, and the three ways it refuses.
# =============================================================================


def _organization(
    *, organization_id=BILLING_ORG, enterprise_id=REAL_ENTERPRISE, is_active=True
):
    """A real ``Organization``, not a stand-in.

    The resolution reads two of its fields and compares one against the
    enterprise claim; a mock would answer both from thin air and could not fail
    the enterprise check.
    """
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Organization(
        organization_id=organization_id,
        enterprise_id=enterprise_id,
        name="Acme",
        slug="acme",
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )


def _resolver(*organizations, records=None):
    """A membership lookup returning ``organizations``, recording its arguments."""

    async def resolve(user_id, enterprise_id):
        if records is not None:
            records.append((user_id, enterprise_id))
        return list(organizations)

    return resolve


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_one_membership_reaches_the_claim(
    as_tenant_provider, build_generator, minter
):
    """The whole point: a row in ``organization_members`` reaches the token.

    On BOTH tokens, because the refresh token is what carries the claim across
    a rotation — an access-only claim would be minted once and then lost.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator(
        resolve_organizations=_resolver(_organization())
    )

    token = await _mint(generator, minter, _user(enterprise_id=REAL_ENTERPRISE))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_no_membership_omits_the_key(as_tenant_provider, build_generator, minter):
    """Absence is the answer, and the KEY is what is absent.

    Asserting on the key rather than on a falsy value is the point: an empty
    string would satisfy `not claims["organization_id"]` and would still be read
    downstream as an organization named "".
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = build_generator(resolve_organizations=_resolver())

    token = await _mint(generator, minter, _user(enterprise_id=REAL_ENTERPRISE))

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_two_memberships_omit_the_claim_and_say_so(as_tenant_provider, caplog):
    """ADR-017 D5 allows one organization. Two is a data defect, not a choice.

    Picking either would attribute an account's spend to an organization no
    operator chose, and the wrong choice is indistinguishable from the right one
    downstream — so the mint refuses and leaves a record an operator can act on.
    """
    as_tenant_provider(TenantProvider.MULTI)
    second = _organization(organization_id="55555555-5555-5555-5555-555555555555")
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(_organization(), second)
    )

    with caplog.at_level("ERROR"):
        token = await _mint(
            generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
        )

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims
    assert "2 memberships" in caplog.text


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_an_organization_of_another_enterprise_is_refused(
    as_tenant_provider, caplog
):
    """The claim may not name an organization outside the enterprise it names.

    ``tenant_scope._validated_billing_organization`` re-checks this when the
    token is presented, so minting one would show up as nothing at all — a claim
    dropped at bind time looks exactly like a claim that was never minted.
    """
    as_tenant_provider(TenantProvider.MULTI)
    foreign = _organization(enterprise_id="99999999-9999-9999-9999-999999999999")
    generator, verify = _rs256_generator(resolve_organizations=_resolver(foreign))

    with caplog.at_level("ERROR"):
        token = await _mint(
            generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
        )

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims
    assert "another enterprise" in caplog.text


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_lookup_is_scoped_to_the_enterprise_being_minted(as_tenant_provider):
    """The resolver is asked about the enterprise going into the SAME token.

    Every mint path is unauthenticated, so the request context holds the
    non-tenant sentinel by the time the mint runs. The enterprise has to travel
    with the question or the RLS-scoped read matches nothing — this pins that it
    does, and that it is the account's own enterprise rather than the ambient one.
    """
    as_tenant_provider(TenantProvider.MULTI)
    asked = []
    generator, _ = _rs256_generator(
        resolve_organizations=_resolver(_organization(), records=asked)
    )

    await _mint(
        generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
    )

    assert asked == [("user-1", REAL_ENTERPRISE)]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_failed_lookup_omits_the_claim_instead_of_raising(as_tenant_provider):
    """A membership lookup that raises must not take sign-in down.

    This runs on every login and every refresh. Two failure directions were
    available and only one is safe: omitting meters the account to itself, which
    is restrictive and visible; falling back to whatever rode in on the user
    object would let a removed member keep drawing on a pool.
    """
    as_tenant_provider(TenantProvider.MULTI)

    async def explode(user_id, enterprise_id):
        raise RuntimeError("database is having a day")

    generator, verify = _rs256_generator(resolve_organizations=explode)
    user = _user(enterprise_id=REAL_ENTERPRISE)
    user.organization_id = BILLING_ORG

    token = await _mint(generator, "generate_access_token", user)

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("minter", MINTERS)
async def test_without_a_resolver_the_mint_is_unchanged(
    as_tenant_provider, build_generator, minter
):
    """The standalone path, and every construction site that wires no resolver.

    Nothing was consulted, so the attached value still answers exactly as it did
    before the resolution existed. This is what makes the change a no-op
    everywhere it has not been wired, rather than a silent claim removal.
    """
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
@pytest.mark.parametrize(
    "organizations,expected_kind,expected_id",
    [
        pytest.param((), SUBJECT_ACCOUNT, "user-1", id="no-membership-meters-itself"),
        pytest.param(
            (_organization(),),
            SUBJECT_ORGANIZATION,
            BILLING_ORG,
            id="one-membership-meters-the-organization",
        ),
    ],
)
async def test_the_metering_subject_follows_the_claim(
    as_tenant_provider, organizations, expected_kind, expected_id
):
    """What the claim is FOR, asserted end to end at the seam that reads it.

    ``InvestigationService`` charges ``billing_subject_for(<the claim>, user_id)``
    on every turn, so the claim is the whole of what decides whether an account
    meters to itself or draws on its organization's pooled bucket. Asserted on
    the subject rather than on a cap number: the cap is a policy that can change,
    the subject is the thing this claim determines.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(*organizations)
    )

    token = await _mint(
        generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
    )
    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)

    subject = billing_subject_for(claims.get("organization_id"), claims["sub"])

    assert subject.kind == expected_kind
    assert subject.subject_id == expected_id


# =============================================================================
# Review fixes (fm#1517) — each of these fails without its fix
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_deactivated_organization_is_refused(as_tenant_provider, caplog):
    """Deactivating an organization must not UNCAP its members (A2).

    An organization with no ``daily_turn_cap`` override is uncapped
    (``SOURCE_COMPANY_UNCAPPED``), so minting the claim for a deactivated
    organization would remove its members' turn cap rather than stop them —
    deactivation would raise the ceiling it exists to lower. The repository's
    list filters ``deleted_at`` only, so the gate has to be at the mint.
    """
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(_organization(is_active=False))
    )

    with caplog.at_level("WARNING"):
        token = await _mint(
            generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
        )

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims
    assert "not active" in caplog.text


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_stale_foreign_membership_does_not_suppress_the_real_one(
    as_tenant_provider,
):
    """Scope to the enterprise, THEN count (A3).

    Counting the raw result first meant one in-scope membership plus one stale
    row in another enterprise was logged as a D5 data defect and the legitimate
    claim suppressed — refusing the innocent account. D5 bounds how many
    organizations bill an account *within its own enterprise*, so that is what
    must be counted.
    """
    as_tenant_provider(TenantProvider.MULTI)
    stale = _organization(
        organization_id="66666666-6666-6666-6666-666666666666",
        enterprise_id="99999999-9999-9999-9999-999999999999",
    )
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(stale, _organization())
    )

    token = await _mint(
        generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
    )

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert claims["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_two_in_scope_memberships_still_refuse(as_tenant_provider, caplog):
    """Scoping first must not weaken D5 for the case it is actually about."""
    as_tenant_provider(TenantProvider.MULTI)
    second = _organization(organization_id="55555555-5555-5555-5555-555555555555")
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(_organization(), second)
    )

    with caplog.at_level("ERROR"):
        token = await _mint(
            generator, "generate_access_token", _user(enterprise_id=REAL_ENTERPRISE)
        )

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert "organization_id" not in claims
    assert "2 memberships" in caplog.text


@pytest.mark.unit
@pytest.mark.security
def test_an_unanchored_account_gets_no_organization_from_the_attached_value():
    """The unanchored rule outranks BOTH inputs (A4).

    Below the attached-value arm this was unreachable: a falsy enterprise claim
    also means nothing was read, so ``organizations is None`` won and the value
    re-attached by ``/auth/refresh`` minted an ``organization_id`` beside an
    empty ``enterprise_id`` — the exact token the contract forbids.
    """
    user = _user(enterprise_id=None)
    user.organization_id = BILLING_ORG

    assert resolve_billing_organization(user, enterprise_claim="") is None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unanchored_multi_tenant_mint_carries_no_organization(
    as_tenant_provider,
):
    """The same rule, end to end on the path that produced the bad token."""
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify = _rs256_generator(
        resolve_organizations=_resolver(_organization())
    )
    user = _user(enterprise_id=None)
    user.organization_id = BILLING_ORG

    token = await _mint(generator, "generate_refresh_token", user)

    claims = jwt.decode(
        token, audience=AUDIENCE, issuer=ISSUER, options={"verify_exp": False}, **verify
    )
    assert claims["enterprise_id"] == ""
    assert "organization_id" not in claims


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unidentifiable_user_does_not_override_the_attached_value(
    as_tenant_provider,
):
    """``None`` means not consulted; ``()`` means consulted and empty (A5).

    With no user id the resolver is never called, so the honest answer is "not
    consulted" — returning the authoritative-empty answer instead would let a
    read that never happened silently discard an attached value.
    """
    as_tenant_provider(TenantProvider.MULTI)
    called = []

    async def resolve(user_id, enterprise_id):
        called.append(user_id)
        return []

    generator, verify = _rs256_generator(resolve_organizations=resolve)
    user = _user(enterprise_id=REAL_ENTERPRISE)
    user.user_id = ""
    user.organization_id = BILLING_ORG

    token = await _mint(generator, "generate_access_token", user)

    claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert called == []
    assert claims["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
async def test_a_pair_is_minted_from_one_resolution(
    as_tenant_provider, build_generator
):
    """One resolution per pair, and both halves carry it (A6).

    Resolving per token opened two transactions at two instants, so a
    membership written between them — or a failure hitting only one — split the
    pair into an access token naming the organization and a refresh token
    without it. The refresh token is the claim's only carrier across rotation,
    so that split never heals. Asserting the call COUNT is the point: asserting
    only that both tokens agree would pass on two reads that happened to agree.
    """
    as_tenant_provider(TenantProvider.MULTI)
    asked = []
    generator, verify = build_generator(
        resolve_organizations=_resolver(_organization(), records=asked)
    )

    access, refresh = await generator.generate_token_pair(
        _user(enterprise_id=REAL_ENTERPRISE),
        state_read_at=datetime.now(timezone.utc),
    )

    assert len(asked) == 1
    for token in (access, refresh):
        claims = jwt.decode(token, audience=AUDIENCE, issuer=ISSUER, **verify)
        assert claims["organization_id"] == BILLING_ORG


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_pair_cannot_split_when_the_membership_changes_mid_mint(
    as_tenant_provider,
):
    """The failure the single resolution exists to prevent.

    The resolver answers differently on its second call. Under the old
    per-token resolution that produced an access token with the organization
    and a refresh token without it; sharing one resolution makes the second
    answer unreachable within a pair.
    """
    as_tenant_provider(TenantProvider.MULTI)
    answers = [[_organization()], []]

    async def resolve(user_id, enterprise_id):
        return answers.pop(0) if answers else []

    generator, verify = _rs256_generator(resolve_organizations=resolve)

    access, refresh = await generator.generate_token_pair(
        _user(enterprise_id=REAL_ENTERPRISE),
        state_read_at=datetime.now(timezone.utc),
    )

    access_claims = jwt.decode(access, audience=AUDIENCE, issuer=ISSUER, **verify)
    refresh_claims = jwt.decode(refresh, audience=AUDIENCE, issuer=ISSUER, **verify)
    assert (
        access_claims.get("organization_id")
        == refresh_claims.get("organization_id")
        == BILLING_ORG
    )
    assert answers == [[]]
