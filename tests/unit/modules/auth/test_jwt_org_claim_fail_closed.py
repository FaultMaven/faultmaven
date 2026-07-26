"""The ``organization_id`` claim never invents a tenant (ADR-010 P2, #629).

``bind_request_org_context`` refuses a verified user that carries no org under
multi-tenant. That guard is only reachable if the *token* says so: both
generators used to substitute ``SingleTenantProvider.DEFAULT_ORG_ID`` for an
org-less user, which under multi silently bound every such user to the
Standalone org — one shared tenant, holding the global-KB write licence that
migration 033 keys on that very id.

The guarantee under test: **under multi-tenant, no token is ever minted
carrying the Standalone sentinel for a user that has no organization** — for
either signing algorithm and for every way "no organization" can be spelled.
Single-tenant keeps the sentinel, where it is the correct answer.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import jwt
import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services import jwt_token_generator
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    resolve_organization_claim,
)
from faultmaven.providers.tenancy import factory as tenancy_factory

REAL_ORG = "22222222-2222-2222-2222-222222222222"

# Every shape an "org-less" user arrives in. The sentinel spellings matter most:
# under multi the Standalone org is not a tenant, and `DevUser.__post_init__`
# stamps it on every user `DatabaseUserStore` loads — which is exactly what the
# `/auth/refresh` and OAuth token-exchange paths hand to the generators.
ORGLESS_USERS = [
    pytest.param(lambda: _user(organization_id=None), id="org-none"),
    pytest.param(lambda: _user(organization_id=""), id="org-empty-string"),
    pytest.param(lambda: _user_without_org_attribute(), id="org-attribute-absent"),
    pytest.param(
        lambda: _user(organization_id=STANDALONE_ORG_ID), id="org-sentinel-valued"
    ),
    pytest.param(lambda: _devuser_from_user_store(), id="org-via-DatabaseUserStore"),
]


def _user(*, organization_id):
    user = MagicMock()
    user.user_id = "user-1"
    user.username = "sso-user"
    user.email = "sso-user@example.com"
    user.roles = ["user"]
    user.organization_id = organization_id
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

    repo_user = MagicMock()
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


def _user_without_org_attribute():
    # spec= keeps MagicMock from auto-creating organization_id on access.
    user = MagicMock(spec=["user_id", "username", "email", "roles"])
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
    settings = MagicMock()
    settings.jwt_access_token_expire_minutes = 15
    return (
        RS256JWTTokenGenerator(
            private_key=private_pem,
            public_key=public_pem,
            revocation_store=MagicMock(),
            settings=settings,
        ),
        {"key": public_pem, "algorithms": ["RS256"]},
    )


def _hs256_generator():
    secret = "unit-test-secret-not-a-real-key-padded-to-32-bytes"
    settings = MagicMock()
    settings.jwt_access_token_expire_minutes = 15
    return (
        HS256JWTTokenGenerator(
            secret_key=secret,
            revocation_store=MagicMock(),
            settings=settings,
        ),
        {"key": secret, "algorithms": ["HS256"]},
    )


GENERATORS = [
    pytest.param(_rs256_generator, id="RS256"),
    pytest.param(_hs256_generator, id="HS256"),
]


def _decode(token, verify_args):
    return jwt.decode(
        token,
        verify_args["key"],
        algorithms=verify_args["algorithms"],
        audience="faultmaven-api",
        issuer="faultmaven",
    )


# --- the guarantee ------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("build_user", ORGLESS_USERS)
async def test_multi_tenant_never_mints_the_sentinel_org_for_an_orgless_user(
    build_generator, build_user, as_tenant_provider
):
    """Under multi, an org-less user's claim is empty — never the Standalone org."""
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify_args = build_generator()

    token = await generator.generate_access_token(build_user())
    claims = _decode(token, verify_args)

    assert claims["organization_id"] != STANDALONE_ORG_ID
    assert claims["organization_id"] == ""


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("build_user", ORGLESS_USERS)
async def test_orgless_multi_tenant_token_is_refused_by_the_binder(
    build_generator, build_user, as_tenant_provider
):
    """Close the loop: the minted claim makes ``bind_request_org_context``'s
    fail-closed 403 reachable, which is the whole point of the empty claim."""
    as_tenant_provider(TenantProvider.MULTI)
    generator, verify_args = build_generator()

    token = await generator.generate_access_token(build_user())
    user = AuthenticatedUser.from_jwt_claims(_decode(token, verify_args))

    # The binder's refusal predicate is `if not user.organization_id`.
    assert not user.organization_id


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("build_user", ORGLESS_USERS)
async def test_single_tenant_still_claims_the_standalone_org(
    build_generator, build_user, as_tenant_provider
):
    """Standalone regression guard: there, the org-less user *is* the sole
    tenant, so the sentinel remains the correct claim."""
    as_tenant_provider(TenantProvider.SINGLE)
    generator, verify_args = build_generator()

    token = await generator.generate_access_token(build_user())

    assert _decode(token, verify_args)["organization_id"] == STANDALONE_ORG_ID


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("build_generator", GENERATORS)
@pytest.mark.parametrize("provider", [TenantProvider.SINGLE, TenantProvider.MULTI])
async def test_a_real_org_is_carried_verbatim_in_both_modes(
    build_generator, provider, as_tenant_provider
):
    """The fix must not disturb the ordinary path: a user with an org keeps it."""
    as_tenant_provider(provider)
    generator, verify_args = build_generator()

    token = await generator.generate_access_token(_user(organization_id=REAL_ORG))

    assert _decode(token, verify_args)["organization_id"] == REAL_ORG


@pytest.mark.unit
@pytest.mark.parametrize("build_user", ORGLESS_USERS)
def test_helper_warns_when_it_declines_to_invent_a_tenant(
    build_user, as_tenant_provider, caplog
):
    """An org-less user under multi is an operator-visible misconfiguration —
    the refusal must not be silent."""
    as_tenant_provider(TenantProvider.MULTI)

    with caplog.at_level("WARNING", logger=jwt_token_generator.__name__):
        assert resolve_organization_claim(build_user()) == ""

    assert any("no organization" in record.message for record in caplog.records)
