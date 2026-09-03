"""The WorkOS half of a personal tenant, against the real SDK classes (#1045).

Every double here is an ``autospec`` of the **actual** ``workos`` service class,
with ``spec_set=True``. That is the point of the module: a hand-written fake
accepts any signature, so it keeps passing after the SDK renames ``external_id``
or moves ``create_organization_membership``, and the first sign of trouble is a
failed sign-up in production. Autospec binds these tests to the installed SDK's
signatures, so a drift is a red test here.

What the adapter has to get right, and why each part is load-bearing:

* **Idempotent in ``external_id``.** The caller derives that value from the
  subject and calls this before it writes anything of its own, so a retry after
  a failed database commit must find the organization the previous attempt
  created. Read first, create second.
* **A conflict is resolved by re-reading, not by minting a second org.** Two
  concurrent first logins for one subject derive the same external id; the
  loser's create is refused, and the winner's organization is the one it wants.
* **A lookup failure is not "absent".** Treating a provider outage as "no
  organization yet" would answer it by creating a duplicate. Only a genuine
  ``NotFoundError`` means absent.
* **"Already a member" is confirmed, not inferred.** The adapter does not guess
  from an exception type; it lists the memberships. A refusal it cannot confirm
  fails the login closed.
* **No provider detail escapes.** This runs inside an unauthenticated callback,
  which must not become an error oracle.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from workos import ConflictError, NotFoundError, ServerError
from workos.organization_membership import OrganizationMembershipService
from workos.organizations import Organizations

from faultmaven.modules.auth.exceptions import SSOProvisioningError
from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
    WorkOSIdentityProvider,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

SUBJECT = "user_01SUBJECT"
EXTERNAL_ID = "personal-0123456789abcdef0123456789abcdef"
ORG_ID = "org_01PERSONAL"
NAME = "Personal"


def _organization(org_id: str = ORG_ID):
    """A stand-in for ``workos.organizations.Organization``.

    Only ``id`` is read by the adapter. The real model is a frozen msgspec-style
    class that is awkward to construct with dummy timestamps, and constructing
    one would test the SDK's model rather than the adapter — the *methods* are
    what must not drift, and those are autospecced.
    """
    return SimpleNamespace(id=org_id, external_id=EXTERNAL_ID, name=NAME)


def _page(items):
    return SimpleNamespace(data=list(items))


def _api_error(cls):
    """Build a WorkOS API error the way the SDK does (kwargs, not a message)."""
    return cls(status_code=409 if cls is ConflictError else 500)


def build_provider(*, organizations=None, memberships=None):
    """A provider whose two sub-clients are autospecs of the real SDK classes."""
    orgs = organizations or create_autospec(Organizations, spec_set=True, instance=True)
    members = memberships or create_autospec(
        OrganizationMembershipService, spec_set=True, instance=True
    )
    client = SimpleNamespace(
        organizations=orgs,
        organization_membership=members,
        user_management=SimpleNamespace(),
    )
    provider = WorkOSIdentityProvider(
        client=client, redirect_uri="https://api.test/callback"
    )
    return provider, orgs, members


# =============================================================================
# The autospec is real
# =============================================================================


def test_the_doubles_reject_a_signature_the_sdk_does_not_have():
    """If this passes vacuously the rest of the module proves nothing.

    ``spec_set`` autospecs raise on an unknown attribute and on a call the real
    signature would not accept, which is exactly the drift a hand-written fake
    cannot see.
    """
    orgs = create_autospec(Organizations, spec_set=True, instance=True)
    with pytest.raises(AttributeError):
        orgs.create_organisation  # noqa: B018 — British spelling: not a method
    with pytest.raises(TypeError):
        orgs.create_organization(name=NAME, not_a_real_parameter="x")

    members = create_autospec(
        OrganizationMembershipService, spec_set=True, instance=True
    )
    with pytest.raises(TypeError):
        members.create_organization_membership(user_id=SUBJECT)  # organization_id


def test_the_adapter_calls_the_sdk_with_the_parameters_it_declares():
    """The exact keyword names, checked against the installed SDK."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.side_effect = _api_error(NotFoundError)
    orgs.create_organization.return_value = _organization()
    members.create_organization_membership.return_value = SimpleNamespace(id="om_1")

    result = provider.provision_personal_organization(
        provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
    )

    assert result == ORG_ID
    orgs.get_organization_by_external_id.assert_called_once_with(EXTERNAL_ID)
    orgs.create_organization.assert_called_once_with(name=NAME, external_id=EXTERNAL_ID)
    members.create_organization_membership.assert_called_once_with(
        user_id=SUBJECT, organization_id=ORG_ID
    )


# =============================================================================
# Idempotency
# =============================================================================


def test_an_existing_organization_is_reused_and_never_recreated():
    """The retry case: a previous attempt made it, this one must find it."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization()
    members.create_organization_membership.return_value = SimpleNamespace(id="om_1")

    assert (
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
        == ORG_ID
    )
    orgs.create_organization.assert_not_called()


def test_a_create_conflict_is_resolved_by_reading_the_winner_back():
    """Two concurrent first logins derive one external id; one org results."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.side_effect = [
        _api_error(NotFoundError),  # nothing there when we looked
        _organization("org_01WINNER"),  # the winner created it meanwhile
    ]
    orgs.create_organization.side_effect = _api_error(ConflictError)
    members.create_organization_membership.return_value = SimpleNamespace(id="om_1")

    assert (
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
        == "org_01WINNER"
    )
    # The member is added to the WINNER's organization, not a phantom one.
    members.create_organization_membership.assert_called_once_with(
        user_id=SUBJECT, organization_id="org_01WINNER"
    )


def test_an_unresolvable_conflict_refuses_rather_than_guessing():
    """A conflict whose winner cannot be read back is a refusal, not a retry."""
    provider, orgs, _ = build_provider()
    orgs.get_organization_by_external_id.side_effect = [
        _api_error(NotFoundError),
        _api_error(NotFoundError),
    ]
    orgs.create_organization.side_effect = _api_error(ConflictError)

    with pytest.raises(SSOProvisioningError):
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )


def test_a_lookup_outage_is_not_read_as_absent():
    """Otherwise a provider blip answers itself by creating a duplicate org."""
    provider, orgs, _ = build_provider()
    orgs.get_organization_by_external_id.side_effect = _api_error(ServerError)

    with pytest.raises(SSOProvisioningError):
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
    orgs.create_organization.assert_not_called()


def test_a_create_returning_no_id_is_refused():
    """A falsy id must never reach the mapping row."""
    provider, orgs, _ = build_provider()
    orgs.get_organization_by_external_id.side_effect = _api_error(NotFoundError)
    orgs.create_organization.return_value = SimpleNamespace(id="")

    with pytest.raises(SSOProvisioningError):
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )


# =============================================================================
# Membership
# =============================================================================


def test_an_already_member_refusal_is_confirmed_by_listing():
    """The retry case for the second call: confirmed, not inferred."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization()
    members.create_organization_membership.side_effect = _api_error(ConflictError)
    members.list_organization_memberships.return_value = _page(
        [SimpleNamespace(id="om_1")]
    )

    assert (
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
        == ORG_ID
    )
    members.list_organization_memberships.assert_called_once_with(
        organization_id=ORG_ID, user_id=SUBJECT, limit=1
    )


def test_an_unconfirmed_membership_refusal_fails_the_login_closed():
    """A refused membership that listing does not confirm is a refusal.

    An organization the subject is not in is not a tenant they may land in.
    """
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization()
    members.create_organization_membership.side_effect = _api_error(ServerError)
    members.list_organization_memberships.return_value = _page([])

    with pytest.raises(SSOProvisioningError):
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )


def test_a_membership_listing_outage_fails_closed():
    """The confirmation is fail-closed: unknown is not 'already a member'."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization()
    members.create_organization_membership.side_effect = _api_error(ConflictError)
    members.list_organization_memberships.side_effect = _api_error(ServerError)

    with pytest.raises(SSOProvisioningError):
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )


# =============================================================================
# No error oracle
# =============================================================================


@pytest.mark.parametrize(
    "wire",
    [
        pytest.param("lookup", id="lookup-failure"),
        pytest.param("create", id="create-failure"),
        pytest.param("membership", id="membership-failure"),
    ],
)
def test_no_provider_detail_reaches_the_caller(wire):
    """The raised message names nothing the IdP said."""
    secret = "workos-internal-detail-do-not-echo"
    provider, orgs, members = build_provider()
    if wire == "lookup":
        orgs.get_organization_by_external_id.side_effect = ServerError(
            secret, status_code=500
        )
    elif wire == "create":
        orgs.get_organization_by_external_id.side_effect = _api_error(NotFoundError)
        orgs.create_organization.side_effect = ServerError(secret, status_code=500)
    else:
        orgs.get_organization_by_external_id.return_value = _organization()
        members.create_organization_membership.side_effect = ServerError(
            secret, status_code=500
        )
        members.list_organization_memberships.return_value = _page([])

    with pytest.raises(SSOProvisioningError) as excinfo:
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
    assert secret not in str(excinfo.value)


# =============================================================================
# The port's default
# =============================================================================


def test_a_provider_that_does_not_implement_it_fails_closed():
    """A provider is opted in by implementing the method, never by default.

    The base implementation must raise rather than return something falsy that a
    caller could mistake for a provisioned organization.
    """
    from faultmaven.modules.auth.contracts import ISSOIdentityProvider

    class MinimalProvider(ISSOIdentityProvider):
        @property
        def provider_name(self) -> str:
            return "minimal"

        def build_authorization_url(self, *, state: str) -> str:
            return "https://idp.test"

        def exchange_code(self, code: str):
            raise NotImplementedError

    with pytest.raises(SSOProvisioningError):
        MinimalProvider().provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
