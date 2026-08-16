"""`GET /auth/me` names the tenant the session is bound to (account UI).

The field is a display label, so the tests that matter are the ones about what
happens when it *cannot* be produced: a profile endpoint must not fail because
an organization row was unreadable, and a client must never be able to read the
absent field as a permission signal.
"""

from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.modules.auth.api.auth import _resolve_organization_summary
from faultmaven.modules.auth.domain.models.api_auth import UserInfoResponse

pytestmark = [pytest.mark.unit]

_ORG_ID = "9f2b1c7e-1f3a-4c5d-8e91-0a2b3c4d5e6f"
_REPO = "faultmaven.modules.auth.api.auth.SessionlessOrganizationRepository"


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
    factory = lambda: repo  # noqa: E731 - the class is called with no args
    return factory, repo


class TestResolveOrganizationSummary:
    async def test_names_the_bound_tenant(self):
        factory, repo = _repo_returning(_Org("Northwind Engineering"))

        with patch(_REPO, factory):
            summary = await _resolve_organization_summary(_User(_ORG_ID))

        assert summary is not None
        assert summary.organization_id == _ORG_ID
        assert summary.name == "Northwind Engineering"
        # The id looked up is the one usable_tenant_id returned, not anything
        # re-derived — a second derivation is how the label and the tenant the
        # request actually wrote to would drift apart.
        repo.get_organization.assert_awaited_once_with(_ORG_ID)

    async def test_no_organization_bound_is_none_without_a_lookup(self):
        factory, repo = _repo_returning(_Org("never read"))

        with patch(_REPO, factory):
            assert await _resolve_organization_summary(_User(None)) is None

        repo.get_organization.assert_not_awaited()

    async def test_unreadable_row_degrades_to_none_rather_than_raising(self):
        """A profile is still worth returning without its label.

        Raising here would turn an unreadable organization row into a failed
        `/auth/me`, and a client that treats that as "not authenticated" would
        sign the user out over a display string.
        """
        repo = AsyncMock()
        repo.get_organization = AsyncMock(side_effect=RuntimeError("db down"))

        with patch(_REPO, lambda: repo):
            assert await _resolve_organization_summary(_User(_ORG_ID)) is None

    async def test_missing_row_is_none(self):
        factory, _ = _repo_returning(None)

        with patch(_REPO, factory):
            assert await _resolve_organization_summary(_User(_ORG_ID)) is None

    async def test_defers_the_is_this_a_tenant_question(self):
        """The sentinel test is `usable_tenant_id`'s, and is not re-implemented.

        Under multi-tenant the Standalone sentinel is not a tenant; under
        single-tenant it is the deployment's one organization. This asserts the
        *delegation* rather than either outcome, so the day that rule changes,
        this endpoint changes with it instead of silently disagreeing.
        """
        factory, repo = _repo_returning(_Org("Default Organization"))

        with patch(
            "faultmaven.modules.auth.api.auth.usable_tenant_id", return_value=None
        ) as usable:
            with patch(_REPO, factory):
                assert (
                    await _resolve_organization_summary(_User(STANDALONE_ORG_ID))
                    is None
                )

        usable.assert_called_once_with(STANDALONE_ORG_ID)
        repo.get_organization.assert_not_awaited()


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
