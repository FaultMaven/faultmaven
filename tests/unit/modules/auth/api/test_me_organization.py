"""`GET /auth/me` names the tenant the session is bound to (account UI).

The field is a display label, so the tests that matter are the ones about what
happens when it *cannot* be produced: a profile endpoint must not fail because
an organization row was unreadable, and a client must never be able to read the
absent field as a permission signal.
"""

from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.modules.auth.api.auth import _resolve_organization_summary
from faultmaven.modules.auth.domain.models.api_auth import UserInfoResponse
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

pytestmark = [pytest.mark.unit]

_ORG_ID = "9f2b1c7e-1f3a-4c5d-8e91-0a2b3c4d5e6f"

#: Where the real tenancy predicate reads its configuration. Patching *here*
#: rather than at ``usable_tenant_id`` keeps the sentinel rule under test.
_PROVIDER = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


class _User:
    """Minimal stand-in for the resolved actor.

    Only ``organization_id`` is read, and it is the request-scoped tenant the
    auth dependency already resolved — not a raw JWT claim.
    """

    def __init__(self, organization_id):
        self.organization_id = organization_id


class _Org:
    def __init__(self, name):
        self.name = name


def _repo_returning(org):
    repo = AsyncMock()
    repo.get_organization = AsyncMock(return_value=org)
    return repo


class TestResolveOrganizationSummary:
    async def test_names_the_bound_tenant(self):
        repo = _repo_returning(_Org("Northwind Engineering"))

        summary = await _resolve_organization_summary(_User(_ORG_ID), repo)

        assert summary is not None
        assert summary.organization_id == _ORG_ID
        assert summary.name == "Northwind Engineering"
        # The id looked up is the one usable_tenant_id returned, not anything
        # re-derived — a second derivation is how the label and the tenant the
        # request actually wrote to would drift apart.
        repo.get_organization.assert_awaited_once_with(_ORG_ID)

    async def test_unreadable_row_degrades_to_none_rather_than_raising(self):
        """A profile is still worth returning without its label.

        Raising here would turn an unreadable organization row into a failed
        `/auth/me`, and a client that treats that as "not authenticated" would
        sign the user out over a display string.
        """
        repo = AsyncMock()
        repo.get_organization = AsyncMock(side_effect=RuntimeError("db down"))

        assert await _resolve_organization_summary(_User(_ORG_ID), repo) is None

    async def test_an_unwired_repository_degrades_too(self):
        """The composition root may not have wired one; that is not an error here.

        ``get_organization_repository`` returns None rather than raising 503,
        because a profile is worth returning without its label. Asserting it
        keeps a future 503 from being introduced quietly at the dependency.
        """
        assert await _resolve_organization_summary(_User(_ORG_ID), None) is None

    async def test_a_broken_tenancy_config_degrades_too(self):
        """The "never raises" guarantee has to cover the predicate itself.

        ``usable_tenant_id`` reaches settings through a deferred import, so it
        is a failure site like any other. If it were outside the guard, a
        misconfigured deployment would 500 `/auth/me` — signing users out of
        the UI over a display string, the exact failure the degrade exists to
        prevent. The sentinel is the actor here because that is the branch on
        which the predicate actually reads the tenancy configuration.
        """
        repo = _repo_returning(_Org("never read"))

        with patch(_PROVIDER, side_effect=RuntimeError("settings unreadable")):
            assert (
                await _resolve_organization_summary(
                    _User(STANDALONE_ENTERPRISE_ID), repo
                )
                is None
            )

        repo.get_organization.assert_not_awaited()

    async def test_missing_row_is_none(self):
        repo = _repo_returning(None)

        assert await _resolve_organization_summary(_User(_ORG_ID), repo) is None

    async def test_nameless_row_is_absence_not_an_empty_label(self):
        """A blank name must not reach the client as a present-but-empty chip.

        ``Organization.name`` is non-empty by validation, so this state is not
        reachable today. It is pinned because the alternative fallback — an
        empty string — is indistinguishable from a real org whose name failed
        to load, and a UI that hides absent orgs would render a nameless box
        instead of nothing.
        """
        repo = _repo_returning(_Org(""))

        assert await _resolve_organization_summary(_User(_ORG_ID), repo) is None

    async def test_the_standalone_sentinel_is_not_a_tenant_under_multi(self):
        """The reachable "no tenant bound" case, against the real predicate.

        A ``DevUser`` can never hold ``organization_id = None`` — ``__post_init__``
        substitutes the Standalone sentinel — so this, not a null org id, is how
        "nothing to name" actually arrives. Only the tenancy *configuration* is
        stubbed; ``usable_tenant_id`` runs for real, so if its sentinel rule ever
        changes this endpoint changes with it instead of silently disagreeing.
        """
        repo = _repo_returning(_Org("Default Organization"))

        with patch(_PROVIDER, return_value=BUILTIN_MULTI):
            assert (
                await _resolve_organization_summary(
                    _User(STANDALONE_ENTERPRISE_ID), repo
                )
                is None
            )

        repo.get_organization.assert_not_awaited()

    async def test_the_standalone_sentinel_is_the_tenant_under_single(self):
        """The same id is the deployment's one organization in standalone.

        Paired with the multi-tenant case so the delegation is pinned in both
        directions: the endpoint owns no copy of the sentinel rule, and a label
        appears exactly where the predicate says there is a tenant to name.
        """
        repo = _repo_returning(_Org("Default Organization"))

        with patch(_PROVIDER, return_value=BUILTIN_SINGLE):
            summary = await _resolve_organization_summary(
                _User(STANDALONE_ENTERPRISE_ID), repo
            )

        assert summary is not None
        assert summary.name == "Default Organization"
        repo.get_organization.assert_awaited_once_with(STANDALONE_ENTERPRISE_ID)


class TestResponseContract:
    def test_organization_is_optional_and_absent_by_default(self):
        """Absence must be representable, and must be the default.

        Every existing caller constructs this model without an organization;
        a required field would have made this a breaking change to a response
        model rather than an addition.
        """
        assert "organization" in UserInfoResponse.model_fields
        assert UserInfoResponse.model_fields["organization"].default is None

    def test_absence_is_not_expressible_as_a_permission_denial(self):
        """The schema offers exactly two states: a named org, or nothing.

        There is no error/denied variant, which is deliberate — a client cannot
        infer authorization from this field, so it cannot grow a guard on it.
        """
        model = UserInfoResponse(
            user_id="u1",
            username="john.doe",
            email="john.doe@faultmaven.local",
            display_name="John Doe",
            created_at="2026-01-15T10:00:00Z",
            is_dev_user=False,
            roles=["user"],
        )
        assert model.organization is None
        assert model.model_dump()["organization"] is None
