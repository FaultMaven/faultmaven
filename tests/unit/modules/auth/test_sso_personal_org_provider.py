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

import importlib.util
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from faultmaven.modules.auth.exceptions import SSOProvisioningError

# ``workos`` is a CLOUD-ONLY dependency: it is pinned in requirements/cloud.txt
# and absent from requirements/test.txt, which is what the Test Standalone lane
# installs. A module-level `from workos import ...` therefore fails COLLECTION
# there — the whole lane red, on a module that has nothing to say about a
# deployment with no SSO adapter.
#
# The skip is a marker rather than ``pytest.importorskip`` deliberately, matching
# the reasoning already recorded in tests/infrastructure/test_storage_backends.py
# for boto3: a marker states the condition where a reader can see it, while an
# importorskip buries it in an import side effect.
#
# A skip is only safe if it cannot silently swallow the lane that SHOULD run it.
# The module-level marker skips EVERY test here, this one included, so the guard
# cannot live in this file — it lives in the sibling
# ``test_sso_personal_tenant.py`` (which needs no SDK and therefore always runs)
# as ``test_the_cloud_lockfile_still_pins_the_sdk_the_provider_tests_need``. That
# turns "these run on Test Cloud" into a checked fact rather than a hope.
_workos_spec = None
try:
    _workos_spec = importlib.util.find_spec("workos")
except (ImportError, ValueError):
    _workos_spec = None
_WORKOS_AVAILABLE = _workos_spec is not None and _workos_spec.origin is not None

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.skipif(
        not _WORKOS_AVAILABLE,
        reason="workos is a cloud-only dependency (requirements/cloud.txt)",
    ),
]

if _WORKOS_AVAILABLE:
    from workos import (
        ConflictError,
        NotFoundError,
        ServerError,
        UnprocessableEntityError,
    )
    from workos.organization_membership import (
        OrganizationMembershipService,
        UserManagementOrganizationMembershipStatuses,
    )
    from workos.organizations import Organizations

    from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
        WorkOSIdentityProvider,
    )
else:  # pragma: no cover - the standalone lane, where every test below skips
    # Collection still evaluates decorators and default arguments, so the names
    # have to EXIST even where nothing will run. Placeholders, not stubs: any
    # test that reached one would fail loudly rather than assert against a fake
    # SDK. (This is the half the marker alone does not buy, and the reason the
    # standalone lane was red on the reviewed head.)
    ConflictError = UnprocessableEntityError = NotFoundError = ServerError = None
    OrganizationMembershipService = Organizations = None
    UserManagementOrganizationMembershipStatuses = None
    WorkOSIdentityProvider = None


SUBJECT = "user_01SUBJECT"
EXTERNAL_ID = "personal-0123456789abcdef0123456789abcdef"
ORG_ID = "org_01PERSONAL"
NAME = "Personal"


def _organization(org_id: str = ORG_ID):
    """A stand-in for ``workos.organizations.Organization``.

    Only ``id`` is read by the adapter. The real model is a frozen class that is
    awkward to construct with dummy timestamps, and constructing one would test
    the SDK's model rather than the adapter — the *methods* are what must not
    drift, and those are autospecced.
    """
    return SimpleNamespace(id=org_id, external_id=EXTERNAL_ID, name=NAME)


def _page(items):
    return SimpleNamespace(data=list(items))


def _api_error(cls):
    """Build a WorkOS API error the way the SDK does (kwargs, not a message)."""
    status = {ConflictError: 409, UnprocessableEntityError: 422, NotFoundError: 404}
    return cls(status_code=status.get(cls, 500))


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
        organization_id=ORG_ID,
        user_id=SUBJECT,
        statuses=[s.value for s in UserManagementOrganizationMembershipStatuses],
        limit=1,
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


def test_a_provider_cannot_be_constructed_without_implementing_it():
    """Abstract, not a raising default.

    A default that raised would be indistinguishable at the call site from a
    provider outage, and would let a provider ship without the capability and
    only discover it on a user's first sign-up. Making it abstract moves that
    from a runtime refusal to a construction-time one.
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

    with pytest.raises(TypeError, match="provision_personal_organization"):
        MinimalProvider()


# =============================================================================
# Review item 5 — the retry must tolerate every membership state
# =============================================================================


def test_the_membership_check_covers_every_state_the_sdk_defines():
    """The SDK's default lists ``active`` only.

    A ``pending`` or ``inactive`` membership left by an earlier attempt would
    then read as "not a member" — and since the create that follows a refusal is
    the same create that was refused, every retry would refuse again, forever.
    The statuses are DERIVED from the enum, so a state added by a future SDK is
    covered automatically instead of silently narrowing the check.
    """
    from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
        _membership_statuses,
    )

    covered = _membership_statuses()
    assert set(covered) == {
        s.value for s in UserManagementOrganizationMembershipStatuses
    }
    assert {"active", "inactive", "pending"} <= set(covered)


@pytest.mark.parametrize("status", ["pending", "inactive"], ids=["pending", "inactive"])
def test_a_non_active_membership_from_a_prior_attempt_is_accepted(status):
    """The retry case that used to refuse permanently."""
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization()
    members.create_organization_membership.side_effect = _api_error(ConflictError)
    members.list_organization_memberships.return_value = _page(
        [SimpleNamespace(id="om_1", status=status)]
    )

    assert (
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
        == ORG_ID
    )
    call = members.list_organization_memberships.call_args
    assert status in call.kwargs["statuses"]


# =============================================================================
# Review item 6 — a duplicate external_id may be 409 OR 422
# =============================================================================


@pytest.mark.parametrize(
    "conflict_cls",
    [ConflictError, UnprocessableEntityError],
    ids=["409-conflict", "422-unprocessable"],
)
def test_both_conflict_classes_are_resolved_by_re_reading(conflict_cls):
    """Which one a duplicate unique field produces is NOT verified against the
    live API (stated in the PR body). Catching only one would turn the common
    retry into a permanent refusal; catching both costs nothing, because the
    recovery is a re-read that either finds the winner or re-raises.
    """
    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.side_effect = [
        _api_error(NotFoundError),
        _organization("org_01WINNER"),
    ]
    orgs.create_organization.side_effect = _api_error(conflict_cls)
    members.create_organization_membership.return_value = SimpleNamespace(id="om_1")

    assert (
        provider.provision_personal_organization(
            provider_user_id=SUBJECT, external_id=EXTERNAL_ID, name=NAME
        )
        == "org_01WINNER"
    )


def test_the_conflict_classes_are_the_sdks_own():
    """Named from the SDK, so a rename is a red test rather than a dead except."""
    from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
        _conflict_errors,
    )

    assert set(_conflict_errors()) == {ConflictError, UnprocessableEntityError}
