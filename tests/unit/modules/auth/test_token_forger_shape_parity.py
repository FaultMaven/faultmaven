"""The test forgers must emit the claim shape the live mint emits (#853).

`tests.utils.forge_access_token` / `forge_refresh_token` became the canonical
token factory for the ~35 `AuthService` verification, revocation and identity
tests when #853 removed the parallel mint those tests had been using as a
factory. A forger is a stand-in for production, and a stand-in that emits a
shape production never emits makes every test built on it agree about a token no
deployment will ever see.

The concrete trap, and why this file exists: the removed mint emitted a
``permissions`` claim, and `AuthenticatedUser.from_jwt_claims` reads
``permissions`` — but both live generators emit ``scopes`` and no ``permissions``
at all. So in production `AuthenticatedUser.permissions` is **always** empty,
while every forged-token test would have seen it populated.
`require_permission` is defined and exported (`api/middleware/auth.py`) and is
wired to no route today; the first route to adopt it would have passed the whole
suite and returned 403 to every real caller.

These tests pin the two shapes equal, in both directions:

* the forged claim key set equals the live generator's, so the forger cannot
  drift from the mint;
* `AuthenticatedUser` built from a *live* token has no permissions, so the fact
  above is recorded where the next reader of `require_permission` will meet it.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest

from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from tests.utils import (
    InMemoryRevocationStore,
    forge_access_token,
    forge_refresh_token,
)

SECRET = "test-secret-key-0123456789abcdef"  # 32+ bytes: HS256 minimum
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

USER_ID = "11111111-1111-1111-1111-111111111111"
ORG_ID = "22222222-2222-2222-2222-222222222222"
USERNAME = "forge.parity"
EMAIL = "forge.parity@example.com"
ROLES = ["member"]


def _settings():
    settings = MagicMock()
    settings.auth.auth_mode = "local"
    settings.security.jwt_algorithm = "HS256"
    settings.auth.jwt_access_token_expire_minutes = 15
    settings.auth.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = ISSUER
    settings.security.jwt_audience = AUDIENCE
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = None
    settings.security.jwt_public_key = None
    settings.security.jwt_private_key_path = None
    settings.security.jwt_public_key_path = None
    settings.security.jwt_secret_key = MagicMock()
    settings.security.jwt_secret_key.get_secret_value.return_value = SECRET
    return settings


@pytest.fixture
def auth_service():
    """The verify-side service the forgers sign for."""
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_settings(),
    ):
        yield AuthService(revocation_store=InMemoryRevocationStore())


@pytest.fixture
def generator():
    """The live local mint, configured to match `auth_service`."""
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=InMemoryRevocationStore(),
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _user():
    return DevUser(
        user_id=USER_ID,
        username=USERNAME,
        email=EMAIL,
        display_name="Forge Parity",
        organization_id=ORG_ID,
        roles=ROLES,
        created_at=datetime.now(timezone.utc),
    )


def _claims(token):
    return pyjwt.decode(token, options={"verify_signature": False, "verify_exp": False})


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_forged_access_token_has_the_live_mint_claim_set(auth_service, generator):
    """Key set equality, both directions, for the same user.

    Compared as sets rather than by spot-checking names: a subset assertion
    would let the forger keep emitting an extra claim (which is exactly the
    `permissions` defect), and a superset assertion would let it drop one.
    """
    live = _claims(await generator.generate_access_token(_user()))
    forged = _claims(
        forge_access_token(
            auth_service,
            user_id=USER_ID,
            organization_id=ORG_ID,
            email=EMAIL,
            roles=ROLES,
            username=USERNAME,
        )
    )

    assert set(forged) == set(live), (
        "forged access claims have drifted from the live mint: "
        f"only forged={sorted(set(forged) - set(live))}, "
        f"only live={sorted(set(live) - set(forged))}"
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_forged_refresh_token_has_the_live_mint_claim_set(
    auth_service, generator
):
    """Same rule for the refresh payload."""
    live = _claims(await generator.generate_refresh_token(_user()))
    forged = _claims(
        forge_refresh_token(auth_service, user_id=USER_ID, organization_id=ORG_ID)
    )

    assert set(forged) == set(live), (
        "forged refresh claims have drifted from the live mint: "
        f"only forged={sorted(set(forged) - set(live))}, "
        f"only live={sorted(set(live) - set(forged))}"
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_no_mint_emits_a_permissions_claim(generator):
    """The specific drift this file was written for, named.

    Set equality above already fails if the forger reintroduces `permissions`,
    but only while the live mint does not emit it either. Assert the live side
    directly so the property survives someone "fixing" the parity test by adding
    the claim to both.
    """
    access = _claims(await generator.generate_access_token(_user()))
    refresh = _claims(await generator.generate_refresh_token(_user()))

    assert "permissions" not in access
    assert "permissions" not in refresh
    assert "scopes" in access


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_identity_from_a_live_token_carries_no_permissions(
    auth_service, generator
):
    """Production truth, recorded: `AuthenticatedUser.permissions` is empty.

    `from_jwt_claims` reads `claims.get("permissions", [])` and no mint emits
    that claim, so `has_permission` is False for every permission on every real
    request. `require_permission` is wired to no route today — the first route
    that adopts it 403s in production, and this test is where that is written
    down. Roles, which the mint *does* emit, survive the trip.
    """
    token = await generator.generate_access_token(_user())

    user = auth_service.extract_user_from_token(token)

    assert user.permissions == []
    assert not user.has_permission("cases:write")
    assert user.roles == ROLES
