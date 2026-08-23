"""The operator role must stay disjoint from the org-scoped role vocabulary.

ADR-012 D9 splits one ambiguous ``admin`` role string into two axes:
``platform_admin`` (deployment operator, cross-tenant) and ``Role.ADMIN``
(organization-scoped, tenant-bounded). The separation is enforced by absence —
``platform_admin`` is simply not a member of the ``Role`` enum — which is
exactly the kind of invariant that a well-meaning future edit erases without
anything failing. These tests make that edit fail loudly.

Absence alone only stops the role API from *minting* an operator. It said
nothing about *destroying* one, and for a long time the API did exactly that:
``assign_role`` replaced the whole role list, so pointing it at an operator
silently dropped ``platform_admin`` and reported a successful promotion. The
``TestOperatorRoleSurvivesOrgRoleManagement`` cases below pin the other half of
the invariant (#706).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import ConflictError, NotFoundError
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.models import rbac as models_rbac
from faultmaven.modules.auth.contracts import (
    PLATFORM_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE_SET,
)
from faultmaven.modules.auth.domain.models import rbac as auth_rbac
from faultmaven.modules.auth.domain.services.user_service import UserService

# Both import paths for the role vocabulary. There used to be two separate
# definitions here and this parametrisation was the tripwire keeping them
# honest; since #1040 item 4 `modules.auth...rbac` re-exports
# `models.rbac`, so these are the same object and the parametrisation asserts
# the invariant once per door rather than once per copy. Kept because both doors
# are live — `UserService.assign_role` validates against the first and
# `contracts.py` re-exports the second — and a future fork would silently make
# it mean two things again, which
# `test_the_role_vocabulary_has_exactly_one_definition` below catches.
ROLE_ENUMS = [models_rbac.Role, auth_rbac.Role]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("role_enum", ROLE_ENUMS)
def test_platform_admin_is_not_an_org_role(role_enum):
    """`platform_admin` must not be assignable as an organization role.

    `UserService.assign_role` accepts any value in this enum, so membership
    here would let the user-management API mint cross-tenant operators.
    """
    assert PLATFORM_ADMIN_ROLE not in [r.value for r in role_enum]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("role_enum", ROLE_ENUMS)
def test_org_admin_is_not_the_operator_role(role_enum):
    """The org admin role must keep its own distinct string."""
    assert role_enum.ADMIN.value == "admin"
    assert role_enum.ADMIN.value != PLATFORM_ADMIN_ROLE


@pytest.mark.unit
@pytest.mark.security
def test_platform_admin_grants_no_org_permissions_by_itself():
    """Holding only the operator role confers no organization permissions.

    Cross-tenant reach and in-org authority are separate grants; an operator's
    org permissions come from the `admin` role it is provisioned alongside.
    """
    assert auth_rbac.get_permissions_for_roles([PLATFORM_ADMIN_ROLE]) == set()
    assert models_rbac.get_permissions_for_roles([PLATFORM_ADMIN_ROLE]) == set()


@pytest.mark.unit
def test_operator_role_set_carries_both_axes():
    """The provisioned operator holds the operator role AND the org admin role.

    Every provisioning path (bootstrap seed, `create_user.py`,
    `fm-promote-platform-admin`) consumes this one list, so they cannot
    produce operators with unequal in-org authority.
    """
    assert PLATFORM_ADMIN_ROLE in PLATFORM_ADMIN_ROLE_SET
    assert "admin" in PLATFORM_ADMIN_ROLE_SET
    assert "user" in PLATFORM_ADMIN_ROLE_SET
    # And that set does grant the full org permission set, via `admin`.
    assert models_rbac.get_permissions_for_roles(PLATFORM_ADMIN_ROLE_SET) == set(
        models_rbac.ROLE_PERMISSIONS[models_rbac.Role.ADMIN]
    )


# ============================================================
# Role management must not cross the axis boundary (#706)
# ============================================================


def _operator_user(user_id: str = "op-target") -> RepositoryUser:
    """An account holding the full operator set, as every provisioning path grants it."""
    now = datetime.now(timezone.utc)
    return RepositoryUser(
        user_id=user_id,
        username="operator@example.com",
        email="operator@example.com",
        display_name="Operator",
        hashed_password=None,
        created_at=now,
        updated_at=now,
        is_active=True,
        is_email_verified=True,
        roles=list(PLATFORM_ADMIN_ROLE_SET),
    )


def _service_for(user: RepositoryUser) -> tuple[UserService, MagicMock]:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    repo.get_user = repo.get
    repo.save = AsyncMock(return_value=user)
    repo.update_user = repo.save

    auth_service = MagicMock()
    auth_service.revoke_user_tokens = AsyncMock(
        return_value=datetime(2026, 8, 12, tzinfo=timezone.utc)
    )
    service = UserService(
        user_repo=repo,
        auth_service=auth_service,
        token_generator=MagicMock(),
    )
    return service, repo


def _saved_roles(repo: MagicMock) -> list[str]:
    return list(repo.save.call_args[0][0].roles)


@pytest.mark.unit
@pytest.mark.security
class TestOperatorRoleSurvivesOrgRoleManagement:
    """`POST/DELETE /admin/users/{id}/roles` must not touch the operator axis.

    The org-scoped role API is reachable by any platform admin and takes another
    user as its target, so a clobbering implementation lets one operator revoke
    another's cross-tenant reach through an endpoint whose response says
    "assigned successfully". Revoking `platform_admin` is
    `fm-demote-platform-admin`'s job, and only its job.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "role",
        # An operator already holds org `admin` (PLATFORM_ADMIN_ROLE_SET), so
        # re-assigning it is the no-op conflict covered separately below. These
        # are the assignments that actually rewrite the org axis.
        [models_rbac.Role.MEMBER.value, models_rbac.Role.VIEWER.value],
    )
    async def test_assign_role_preserves_platform_admin(self, role):
        """Reassigning an operator's org role leaves the operator role intact."""
        user = _operator_user()
        service, repo = _service_for(user)

        await service.assign_role(
            user_id=user.user_id,
            role=role,
            organization_id="org-1",
            admin_user_id="other-operator",
        )

        saved = _saved_roles(repo)
        assert PLATFORM_ADMIN_ROLE in saved
        # The org axis is replaced by exactly the assigned role...
        assert [r for r in saved if r in {x.value for x in models_rbac.Role}] == [role]
        # ...and the base marker is not collateral damage either.
        assert "user" in saved

    @pytest.mark.asyncio
    async def test_conflicting_assignment_persists_nothing(self):
        """A no-op assignment must 409 without touching the account.

        The conflict check is what decides "this would change nothing", so it
        has to run before any write — otherwise the cheapest way to strip an
        operator would be the request that claims to do nothing at all.
        """
        user = _operator_user()
        service, repo = _service_for(user)

        with pytest.raises(ConflictError):
            await service.assign_role(
                user_id=user.user_id,
                role=models_rbac.Role.ADMIN.value,
                organization_id="org-1",
                admin_user_id="other-operator",
            )

        repo.save.assert_not_called()
        assert list(user.roles) == list(PLATFORM_ADMIN_ROLE_SET)

    @pytest.mark.asyncio
    async def test_remove_role_preserves_platform_admin(self):
        """Removing an operator's org admin role does not revoke cross-tenant reach."""
        user = _operator_user()
        service, repo = _service_for(user)

        await service.remove_role(
            user_id=user.user_id,
            role=models_rbac.Role.ADMIN.value,
            organization_id="org-1",
            admin_user_id="other-operator",
        )

        saved = _saved_roles(repo)
        assert PLATFORM_ADMIN_ROLE in saved
        assert "user" in saved
        # Org axis emptied by the removal, so minimum privilege applies there.
        assert models_rbac.Role.ADMIN.value not in saved
        assert models_rbac.Role.VIEWER.value in saved

    @pytest.mark.asyncio
    async def test_assign_role_cannot_introduce_the_operator_role(self):
        """A validated org role is the only thing written — no echo of caller input."""
        user = _operator_user()
        service, _ = _service_for(user)

        with pytest.raises(Exception) as exc_info:
            await service.assign_role(
                user_id=user.user_id,
                role=PLATFORM_ADMIN_ROLE,
                organization_id="org-1",
                admin_user_id="other-operator",
            )
        assert "Invalid role" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_account_with_no_roles_is_reported_honestly(self):
        """An account with no org role is treated as having none.

        ``dev_roles`` is nullable and stored NULL whenever the list is empty,
        so ``roles == []`` is reachable. The pre-#706 code substituted a
        phantom ``["member"]`` default here, which made *assigning* member a
        spurious 409 and *removing* it a silent success. Both now answer from
        the real state: the role genuinely is not held.
        """
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        user = RepositoryUser(
            user_id="u-empty",
            username="empty@example.com",
            email="empty@example.com",
            display_name="No Roles",
            hashed_password=None,
            created_at=now,
            updated_at=now,
            is_active=True,
            is_email_verified=True,
            roles=[],
        )

        service, repo = _service_for(user)
        with pytest.raises(NotFoundError):
            await service.remove_role(
                user_id=user.user_id,
                role=models_rbac.Role.MEMBER.value,
                organization_id="org-1",
                admin_user_id="operator",
            )
        repo.save.assert_not_called()
        # Pin the premise of the second half rather than inferring it: the
        # rejected removal must not have mutated the in-memory account either,
        # or the assignment below would be starting from a state this test
        # never established.
        assert user.roles == []

        service, repo = _service_for(user)
        await service.assign_role(
            user_id=user.user_id,
            role=models_rbac.Role.MEMBER.value,
            organization_id="org-1",
            admin_user_id="operator",
        )
        assert _saved_roles(repo) == [models_rbac.Role.MEMBER.value]

    @pytest.mark.asyncio
    async def test_unrecognised_roles_are_preserved_not_dropped(self):
        """Role management must not decide that a role it cannot name is disposable."""
        user = _operator_user()
        user.roles = ["user", "member", "some_future_grant"]
        service, repo = _service_for(user)

        await service.assign_role(
            user_id=user.user_id,
            role=models_rbac.Role.ADMIN.value,
            organization_id="org-1",
            admin_user_id="other-operator",
        )

        saved = _saved_roles(repo)
        assert "some_future_grant" in saved
        assert "user" in saved
        assert "member" not in saved  # org axis replaced
        assert "admin" in saved


# =============================================================================
# One vocabulary, two doors (#1040 item 4)
# =============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["Role", "Permission", "ROLE_PERMISSIONS"],
)
def test_the_role_vocabulary_has_exactly_one_definition(name):
    """The two modules must expose the *same* objects, not equal ones.

    Identity rather than equality is the point. Two separately-defined enums
    with identical members compare unequal member-by-member and pass most
    plausible assertions anyway, so an equality check would keep passing right
    through a re-fork — while `Role.ADMIN` from one import stopped matching
    `Role.ADMIN` from the other in every `in` test and dict lookup in the
    codebase.

    Before #1040 item 4 these were genuinely two copies, and the failure this
    guards is a role granting different permissions depending on which import
    the caller reached for.
    """
    assert getattr(models_rbac, name) is getattr(auth_rbac, name)


@pytest.mark.unit
def test_the_operator_constants_stay_out_of_the_shared_vocabulary():
    """`platform_admin` belongs to the auth module, not to the org role model.

    The convergence pulled the org vocabulary down into `models.rbac`; it must
    not have dragged the operator role with it. `models.rbac.Role` is what
    `UserService.assign_role` validates against, so an operator constant landing
    there is how the user-management API starts minting deployment operators —
    the exact thing `test_platform_admin_is_not_an_org_role` forbids, arriving
    by a different route.
    """
    assert not hasattr(models_rbac, "PLATFORM_ADMIN_ROLE")
    assert not hasattr(models_rbac, "PLATFORM_ADMIN_ROLE_SET")
    assert auth_rbac.PLATFORM_ADMIN_ROLE == PLATFORM_ADMIN_ROLE


@pytest.mark.unit
@pytest.mark.security
def test_demotion_removes_exactly_what_promotion_grants():
    """The promote/demote asymmetry (#1040 item 3), pinned at the constant.

    `fm-promote-platform-admin` grants PLATFORM_ADMIN_ROLE_SET; demotion used to
    remove only `platform_admin`, so promote-then-demote left the account
    holding the org-scoped `admin` it never had. Deriving one list from the
    other is what makes them inverses — restating it is how they came apart.

    The base `user` marker is excluded deliberately: a demotion must leave a
    usable account, not an empty role list.
    """
    assert auth_rbac.OPERATOR_GRANTED_ROLES == [
        r for r in PLATFORM_ADMIN_ROLE_SET if r != auth_rbac.BASE_USER_ROLE
    ]
    assert auth_rbac.BASE_USER_ROLE not in auth_rbac.OPERATOR_GRANTED_ROLES
    assert PLATFORM_ADMIN_ROLE in auth_rbac.OPERATOR_GRANTED_ROLES
    # The org role is the half that used to be left behind.
    assert models_rbac.Role.ADMIN.value in auth_rbac.OPERATOR_GRANTED_ROLES


@pytest.mark.unit
def test_the_base_marker_grants_nothing():
    """`user` is a marker, not a role — which is why demotion may leave it.

    It is not a member of `Role`, so it maps to no permissions. If a future edit
    made it grant something, "demotion leaves the base marker" would silently
    become "demotion leaves authority behind", and #1040 item 2 (the
    `user`/`member` vocabulary duplication) is exactly the kind of change that
    would do it.
    """
    assert auth_rbac.get_permissions_for_roles([auth_rbac.BASE_USER_ROLE]) == set()
