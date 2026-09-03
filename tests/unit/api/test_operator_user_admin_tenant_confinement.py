"""Operator user administration resolves its target inside the caller's tenant (#1318).

``/api/v1/admin/users*`` and the two ``/api/v1/auth/users*`` operator routes are
gated on ``platform_admin``. That role says what an operator may *do*, never
*whose* accounts they may do it to: without a tenant predicate every one of
these reached a bare user lookup, and five of the seven are writes — deactivate,
re-role, remove a role, revoke every token, delete the account. The measured
behaviour is recorded in #1318 and in the two-tenant surface probe.

These pin the predicate on all of them, in both directions, and pin the shape of
the refusal: an out-of-tenant id answers **404 with the body an absent id
answers**, so the status code and the text are both free of existence
inference. Nothing is written behind the 404 — asserted against the collaborator
rather than the status, because a route that answers 404 *after* mutating is a
different and much worse bug than one that refuses.

Two things are deliberately NOT asserted here:

* an audit row. Cross-tenant user administration is refused rather than granted,
  so there is nothing to audit; the break-glass-with-audit model for this
  surface (ADR-012 D9 option A) is a later change and half of it would be worse
  than none.
* single-tenant behaviour changing. It does not: under ``single`` the deployment
  is the organization, ``organization_members`` is not populated there at all,
  and the predicate consults nothing. That is asserted too — a confinement that
  broke every standalone install would be a worse outcome than the finding.

Companion rule: ``docs/architecture/security/rbac.md`` — "Tenant-Scoped
Resolution". Companion policy: ``faultmaven/api/operator_user_scope.py``.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import (
    require_platform_admin as require_platform_admin_authenticated,
)
from faultmaven.api.routes.admin import get_user_service
from faultmaven.api.routes.admin import router as admin_router
from faultmaven.api.v1.auth_dependencies import (
    UNSCOPED_REQUEST_MSG,
)
from faultmaven.api.v1.auth_dependencies import (
    require_platform_admin as require_platform_admin_dev,
)
from faultmaven.models.interfaces_user import OrganizationMember
from faultmaven.modules.auth.api.auth import router as auth_router
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser, DevUser
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

ORG_A = "org-alpha-11111111"
ORG_B = "org-beta-22222222"

OPERATOR_A = "user-operator-a"
MEMBER_A = "user-member-a"
MEMBER_B = "user-member-b"
ABSENT = "user-nobody-at-all"

#: Who is in which organization. The ONLY difference between ``MEMBER_A`` and
#: ``MEMBER_B`` — same shape of account, same roles, same activity — so a 404 can
#: only have come from the tenant predicate and not from some other guard.
MEMBERSHIP = {
    ORG_A: [OPERATOR_A, MEMBER_A],
    ORG_B: [MEMBER_B],
}


@contextmanager
def _tenancy(name: str):
    """Run the block under one tenant provider — at BOTH of its read sites.

    ``operator_user_scope`` imports ``requested_tenant_provider`` directly;
    ``config.tenant_context.usable_tenant_id``, which decides whether the
    Standalone sentinel counts as a tenant, imports it from the factory inside
    the call. In a deployment both read the same settings and cannot disagree.
    Patching only one of them here would let this file assert a state the
    application can never be in — and, as it happens, would turn the sentinel
    case below from the 403 it must be into a 404.
    """
    with (
        patch(
            "faultmaven.api.operator_user_scope.requested_tenant_provider",
            return_value=name,
        ),
        patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value=name,
        ),
    ):
        yield


def _MULTI():
    return _tenancy(BUILTIN_MULTI)


def _SINGLE():
    return _tenancy(BUILTIN_SINGLE)


# =============================================================================
# The world: two organizations, one user each, and an operator bound to A
# =============================================================================


class FakeOrganizations:
    """Just enough ``IOrganizationRepository`` for the predicate to resolve.

    Not a ``Mock``: the predicate's whole content is *which* organization it
    asks about, and a Mock answers the same thing however it is called — so a
    predicate that passed the wrong org would still look confined here.
    """

    def __init__(self, membership: dict[str, list[str]]):
        self._membership = membership
        self.calls: list[tuple[str, str]] = []

    async def get_member_role(self, organization_id: str, user_id: str):
        self.calls.append((organization_id, user_id))
        if user_id in self._membership.get(organization_id, []):
            return "role-org-admin"
        return None

    async def list_organization_members(self, organization_id: str):
        return [
            OrganizationMember(
                user_id=user_id,
                organization_id=organization_id,
                role_id="role-org-admin",
                joined_at=datetime.now(timezone.utc),
            )
            for user_id in self._membership.get(organization_id, [])
        ]


def _repository_user(user_id: str, organization_id: str):
    """A ``user_repository.User``-shaped row, as the service layer returns it."""
    from faultmaven.infrastructure.persistence.user_repository import User

    return User(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        roles=["user"],
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        organization_id=organization_id,
    )


def _dev_user(user_id: str, organization_id: str) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
        roles=["user"],
        organization_id=organization_id,
    )


def _operator(organization_id: str | None = ORG_A) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=OPERATOR_A,
        organization_id=organization_id,
        email=f"{OPERATOR_A}@example.com",
        roles=["user", "platform_admin"],
        permissions=[],
    )


def _operator_dev(organization_id: str | None = ORG_A) -> DevUser:
    return DevUser(
        user_id=OPERATOR_A,
        username=OPERATOR_A,
        email=f"{OPERATOR_A}@example.com",
        display_name=OPERATOR_A,
        created_at=datetime.now(timezone.utc),
        roles=["user", "platform_admin"],
        organization_id=organization_id,
    )


class _World:
    def __init__(self, client, user_service, user_store, auth_service, organizations):
        self.client = client
        self.user_service = user_service
        self.user_store = user_store
        self.auth_service = auth_service
        self.organizations = organizations


@pytest.fixture
def world():
    """Both operator user-administration routers, over one fake membership store."""
    users = {
        OPERATOR_A: _repository_user(OPERATOR_A, ORG_A),
        MEMBER_A: _repository_user(MEMBER_A, ORG_A),
        MEMBER_B: _repository_user(MEMBER_B, ORG_B),
    }

    user_service = AsyncMock()
    user_service.list_users = AsyncMock(
        return_value=([users[MEMBER_A], users[MEMBER_B]], 2)
    )
    user_service.get_user_with_metadata = AsyncMock(
        return_value={
            "user_id": MEMBER_B,
            "email": f"{MEMBER_B}@example.com",
            "full_name": MEMBER_B,
            "roles": ["user"],
            "permissions": [],
            "is_active": True,
            "is_verified": True,
            "last_login_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "metadata": {},
        }
    )
    user_service.deactivate_user_admin = AsyncMock(return_value=users[MEMBER_B])
    user_service.activate_user_admin = AsyncMock(return_value=users[MEMBER_B])
    user_service.assign_role = AsyncMock(return_value=users[MEMBER_B])
    user_service.remove_role = AsyncMock(return_value=users[MEMBER_B])

    user_store = AsyncMock()
    user_store.list_users = AsyncMock(
        return_value=[
            _dev_user(OPERATOR_A, ORG_A),
            _dev_user(MEMBER_A, ORG_A),
            _dev_user(MEMBER_B, ORG_B),
        ]
    )
    user_store.count_users = AsyncMock(return_value=3)
    user_store.get_user = AsyncMock(side_effect=lambda uid: _dev_user(uid, ORG_B))
    user_store.get_user_by_username = AsyncMock(
        side_effect=lambda name: _dev_user(name, ORG_B)
    )
    user_store.delete_user = AsyncMock(return_value=True)

    auth_service = AsyncMock()
    auth_service.revoke_user_tokens = AsyncMock(return_value=datetime.now(timezone.utc))

    organizations = FakeOrganizations(MEMBERSHIP)

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[require_platform_admin_authenticated] = _operator
    app.dependency_overrides[require_platform_admin_dev] = _operator_dev
    app.dependency_overrides[get_user_service] = lambda: user_service
    app.state.user_service = user_service
    app.state.user_store = user_store
    app.state.auth_service = auth_service
    app.state.organization_repository = organizations

    return _World(
        TestClient(app), user_service, user_store, auth_service, organizations
    )


# =============================================================================
# The seven operations, and how each one is reached
# =============================================================================
#
# One table so the sweeps below cannot cover six of seven and look complete. The
# ``mutates`` entry names the collaborator call that must NOT have happened
# behind a refusal — the row-level check, in the only form a unit test has.

#: (name, method, path template, request body, the service call a refusal must
#: NOT have made). ``{user_id}`` / ``{username}`` are the OpenAPI templates, so
#: this table is directly comparable with the live spec.
ID_ADDRESSED_OPERATIONS = (
    ("read", "GET", "/api/v1/admin/users/{user_id}", None, "get_user_with_metadata"),
    (
        "deactivate",
        "POST",
        "/api/v1/admin/users/{user_id}/deactivate",
        None,
        "deactivate_user_admin",
    ),
    (
        "activate",
        "POST",
        "/api/v1/admin/users/{user_id}/activate",
        None,
        "activate_user_admin",
    ),
    (
        "assign_role",
        "POST",
        "/api/v1/admin/users/{user_id}/roles",
        {"role": "member"},
        "assign_role",
    ),
    (
        "remove_role",
        "DELETE",
        "/api/v1/admin/users/{user_id}/roles/{role}",
        None,
        "remove_role",
    ),
    ("revoke_tokens", "POST", "/api/v1/auth/users/{user_id}/revoke-tokens", None, None),
    ("delete", "DELETE", "/api/v1/auth/users/{username}", None, None),
)

#: The two listings. Confined by an allowlist rather than by a per-id refusal,
#: so they are swept separately — but they belong to the same surface.
LISTING_OPERATIONS = (
    ("GET", "/api/v1/admin/users"),
    ("GET", "/api/v1/auth/users"),
)

#: Every operation of the operator user-administration surface, as the live
#: OpenAPI document spells it. ``tests/integration/api/
#: test_operator_user_routes_are_confined.py`` reads this and fails when the app
#: exposes one that is not here — so a route added tomorrow cannot slip past the
#: predicate by simply not being swept above.
CONFINED_OPERATIONS = frozenset(
    [(method, path) for _, method, path, _, _ in ID_ADDRESSED_OPERATIONS]
) | frozenset(LISTING_OPERATIONS)


def _operations(user_id: str, username: str):
    return [
        (
            name,
            method,
            template.format(user_id=user_id, username=username, role="member"),
            body,
            mutator,
        )
        for name, method, template, body, mutator in ID_ADDRESSED_OPERATIONS
    ]


def _call(client, method: str, path: str, body):
    return client.request(method, path, json=body)


# =============================================================================
# Invariant 1 — the operator bound to A reaches none of B's users
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
def test_no_operation_reaches_another_tenants_user(world):
    """Every id-addressed operation refuses, and nothing is written."""
    with _MULTI():
        for name, method, path, body, mutator in _operations(MEMBER_B, MEMBER_B):
            response = _call(world.client, method, path, body)

            assert response.status_code == 404, (
                f"{name}: an operator bound to {ORG_A} reached {ORG_B}'s user "
                f"({response.status_code}): {response.text[:300]}"
            )
            if mutator is not None:
                getattr(world.user_service, mutator).assert_not_awaited()

    world.auth_service.revoke_user_tokens.assert_not_awaited()
    world.user_store.delete_user.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.security
def test_the_refusal_is_the_answer_an_absent_id_gets(world):
    """404 is not enough: the BODY has to match, or the code is an oracle.

    A distinguishable refusal confirms that the id names a real account in
    another tenant, which is exactly the inference the 404 exists to deny. The
    id the caller sent is echoed back by both answers and is normalised out
    before the comparison — the caller already knows what they asked for; what
    must not differ is everything else.
    """
    with _MULTI():
        for (name, method, path, body, _), (_, _, absent_path, _, _) in zip(
            _operations(MEMBER_B, MEMBER_B), _operations(ABSENT, ABSENT)
        ):
            foreign = _call(world.client, method, path, body)
            absent = _call(world.client, method, absent_path, body)

            assert absent.status_code == foreign.status_code, name
            assert foreign.text.replace(MEMBER_B, "<id>") == absent.text.replace(
                ABSENT, "<id>"
            ), (
                f"{name}: the out-of-tenant refusal is distinguishable from the "
                f"absent-id one — {foreign.text[:200]} vs {absent.text[:200]}"
            )


@pytest.mark.unit
@pytest.mark.security
def test_no_body_names_the_other_tenants_user(world):
    """Not even the email or the organization leaks through a refusal."""
    with _MULTI():
        for name, method, path, body, _ in _operations(MEMBER_B, MEMBER_B):
            response = _call(world.client, method, path, body)
            assert f"{MEMBER_B}@example.com" not in response.text, name
            assert ORG_B not in response.text, name


@pytest.mark.unit
@pytest.mark.security
def test_the_auth_listing_neither_names_nor_counts_another_tenant(world):
    """``GET /auth/users``: no foreign row, and no deployment-wide total.

    ``total`` is asserted as well as the rows. A count is an inference about
    tenants the caller cannot see — #1318 measured ``{"total": 83, "truncated":
    true}`` on a deployment where the caller's organization held a handful — and
    it is the half a "filter the page" fix leaves behind.
    """
    with _MULTI():
        listing = world.client.get("/api/v1/auth/users")

    assert listing.status_code == 200, listing.text[:300]
    body = listing.json()

    assert {user["user_id"] for user in body["users"]} == set(MEMBERSHIP[ORG_A])
    assert body["total"] == len(MEMBERSHIP[ORG_A]), body
    assert body["truncated"] is False, body
    assert MEMBER_B not in listing.text


@pytest.mark.unit
@pytest.mark.security
def test_the_admin_listing_passes_the_predicate_to_the_service(world):
    """``GET /admin/users`` hands the allowlist down rather than post-filtering.

    The route's half of the confinement. The service's half — that the allowlist
    is applied BEFORE pagination, so ``total`` counts the tenant — is asserted
    against the real service below; splitting them is what keeps either from
    being satisfied by the other's double.
    """
    with _MULTI():
        listing = world.client.get("/api/v1/admin/users")

    assert listing.status_code == 200, listing.text[:300]
    kwargs = world.user_service.list_users.await_args.kwargs
    assert kwargs["restrict_to_user_ids"] == frozenset(MEMBERSHIP[ORG_A])


@pytest.mark.unit
@pytest.mark.security
async def test_the_service_applies_the_allowlist_before_paginating():
    """The real ``UserService``: filtered first, counted second.

    Filtering a page after the fact leaves ``total`` deployment-wide — the
    disclosure above — and makes "is there a next page?" wrong as well.
    """
    from faultmaven.infrastructure.persistence.user_repository import (
        InMemoryUserRepository,
    )
    from faultmaven.modules.auth.domain.services.user_service import UserService

    repository = InMemoryUserRepository()
    for user_id, organization_id in (
        (OPERATOR_A, ORG_A),
        (MEMBER_A, ORG_A),
        (MEMBER_B, ORG_B),
    ):
        await repository.create(_repository_user(user_id, organization_id))

    service = UserService(user_repo=repository, auth_service=AsyncMock())

    users, total = await service.list_users(
        restrict_to_user_ids=frozenset(MEMBERSHIP[ORG_A])
    )
    assert {user.user_id for user in users} == set(MEMBERSHIP[ORG_A])
    assert total == len(MEMBERSHIP[ORG_A])

    # An EMPTY allowlist returns nothing. Read as "no restriction" it would
    # return the deployment, which is the fail-open inversion of the predicate.
    users, total = await service.list_users(restrict_to_user_ids=frozenset())
    assert users == []
    assert total == 0

    # And `None` still means unconfined, for the single-tenant caller.
    _, total = await service.list_users(restrict_to_user_ids=None)
    assert total == 3


@pytest.mark.unit
@pytest.mark.security
def test_the_predicate_asks_about_the_operators_own_organization(world):
    """Which org is asked about is the whole content of the check."""
    with _MULTI():
        world.client.get(f"/api/v1/admin/users/{MEMBER_B}")

    assert world.organizations.calls == [(ORG_A, MEMBER_B)]


# =============================================================================
# Invariant 2 — the positive control: A's own users still work
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
def test_the_operators_own_tenant_is_unaffected(world):
    """Without this, refusing everything would pass every case above."""
    with _MULTI():
        for name, method, path, body, mutator in _operations(MEMBER_A, MEMBER_A):
            response = _call(world.client, method, path, body)
            assert response.status_code == 200, (
                f"{name}: the operator was refused a user of their OWN "
                f"organization ({response.status_code}): {response.text[:300]}"
            )
            if mutator is not None:
                getattr(world.user_service, mutator).assert_awaited()

    world.auth_service.revoke_user_tokens.assert_awaited()
    world.user_store.delete_user.assert_awaited()


# =============================================================================
# The single-tenant deployment is not confined into uselessness
# =============================================================================


@pytest.mark.unit
def test_single_tenant_administration_consults_no_membership_row(world):
    """Standalone does not populate ``organization_members``.

    A predicate that read it there would deny the only accounts the deployment
    has — the failure ``SingleTenantPermissionResolver`` already exists to
    avoid. So under ``single`` every operation succeeds and the store is never
    asked.
    """
    with _SINGLE():
        for name, method, path, body, _ in _operations(MEMBER_B, MEMBER_B):
            response = _call(world.client, method, path, body)
            assert response.status_code == 200, f"{name}: {response.text[:300]}"

        listing = world.client.get("/api/v1/admin/users")

    assert world.organizations.calls == []
    assert (
        world.user_service.list_users.await_args.kwargs["restrict_to_user_ids"] is None
    )
    assert listing.status_code == 200


# =============================================================================
# An operator with no tenant is refused, and says so without naming an id
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
def test_an_operator_carrying_no_organization_is_refused(world):
    """403, and the same 403 for every id — so it is not an oracle either.

    Holding the deployment-wide role does not exempt a caller from acting inside
    the organization their request is bound to; ``require_actor_organization``
    refuses rather than handing back ``None`` for the route to degrade into an
    unscoped query.
    """
    world.client.app.dependency_overrides[require_platform_admin_authenticated] = (
        lambda: _operator(organization_id=None)
    )
    world.client.app.dependency_overrides[require_platform_admin_dev] = (
        lambda: _operator_dev(organization_id=None)
    )

    with _MULTI():
        for name, method, path, body, _ in _operations(MEMBER_A, MEMBER_A):
            response = _call(world.client, method, path, body)
            assert response.status_code == 403, f"{name}: {response.text[:300]}"
            assert UNSCOPED_REQUEST_MSG in response.text, name

        listing = world.client.get("/api/v1/admin/users")

    assert listing.status_code == 403
    world.user_service.deactivate_user_admin.assert_not_awaited()
    world.auth_service.revoke_user_tokens.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.security
def test_a_missing_membership_store_refuses_rather_than_serving_unconfined(world):
    """503, not a pass.

    Without the store there is no way to establish that the target is the
    operator's to administer, and "serve it anyway" is the exact failure the
    predicate was added for.
    """
    del world.client.app.state.organization_repository

    with _MULTI():
        response = world.client.get(f"/api/v1/admin/users/{MEMBER_B}")
        listing = world.client.get("/api/v1/admin/users")

    assert response.status_code == 503, response.text[:300]
    assert listing.status_code == 503, listing.text[:300]
    world.user_service.get_user_with_metadata.assert_not_awaited()
