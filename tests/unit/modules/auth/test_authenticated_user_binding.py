"""One source for the isolation key: the request binding (ADR-017 D1/D9).

Two principals disagreed about where a request's enterprise comes from.
``DevUser`` takes the request-bound contextvar; ``AuthenticatedUser`` took the RAW
``enterprise_id`` claim, defaulting to ``""``. Routes that stamp and authorise on
``current_user.enterprise_id`` — the whole session surface, and the admin user
surface — therefore used a value the database session was NOT bound to whenever
the two could differ.

They differ in exactly one shipped configuration, and it is a real one: under
``TENANT_PROVIDER=single`` the request front door FORCES the Standalone
enterprise (a forged claim must never re-scope a standalone deployment), while a
service account provisioned with ``--enterprise-id X`` carries X in its token. Its
cases are written under the binding and its session routes were authorised
against the claim, so every session route answered 403 while every other route
worked.

The fix is that there is one answer and one place it comes from. The claim is
still what the front door reads and refuses on; after that, the binding is the
only thing anyone downstream consults.
"""

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser, DevUser

pytestmark = [pytest.mark.unit, pytest.mark.security]

BOUND = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
CLAIMED = "ffffffff-ffff-ffff-ffff-ffffffffffff"


@pytest.fixture(autouse=True)
def _reset_binding():
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _claims(**overrides) -> dict:
    claims = {
        "sub": "user_service_account",
        "email": "svc@example.com",
        "roles": ["user"],
        "permissions": [],
        "jti": "jti-1",
    }
    claims.update(overrides)
    return claims


def test_the_enterprise_comes_from_the_binding_not_the_raw_claim():
    """The standalone service-account case, which used to 403 on every session route."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)

    user = AuthenticatedUser.from_jwt_claims(_claims(enterprise_id=CLAIMED))

    assert user.enterprise_id == STANDALONE_ENTERPRISE_ID


def test_under_multi_the_binding_is_the_verified_claim_so_nothing_changes():
    """The front door binds from the same verified claim, so the two agree."""
    set_current_enterprise_id(BOUND)

    user = AuthenticatedUser.from_jwt_claims(_claims(enterprise_id=BOUND))

    assert user.enterprise_id == BOUND


def test_a_token_with_no_enterprise_claim_still_gets_the_binding():
    """Refusing a claim-less token is the front door's job, not this object's.

    ``bind_request_enterprise_context`` has already refused such a request under
    multi-tenant before any route dependency runs; under single-tenant there is
    nothing to refuse. Either way this object must not invent a third answer —
    ``""`` was one, and it is a value that passes ``NOT NULL`` and then dies on a
    foreign key several frames later.
    """
    set_current_enterprise_id(BOUND)

    user = AuthenticatedUser.from_jwt_claims(_claims())

    assert user.enterprise_id == BOUND


def test_both_principals_answer_the_same_question_the_same_way():
    """``DevUser`` already read the binding; the two must not disagree."""
    set_current_enterprise_id(BOUND)

    authenticated = AuthenticatedUser.from_jwt_claims(_claims(enterprise_id=CLAIMED))
    dev = DevUser(
        user_id="user_service_account",
        username="svc",
        email="svc@example.com",
        display_name="Service account",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        enterprise_id=BOUND,
    )

    assert authenticated.enterprise_id == dev.enterprise_id == BOUND


def test_the_billing_organization_still_comes_from_the_claim():
    """Only the ISOLATION key moves. Billing attribution is a different fact."""
    set_current_enterprise_id(BOUND)

    user = AuthenticatedUser.from_jwt_claims(
        _claims(enterprise_id=BOUND, organization_id="org_payer")
    )

    assert user.organization_id == "org_payer"
