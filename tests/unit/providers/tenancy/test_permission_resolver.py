"""The org-permission resolver behind the tenancy seam (#1163, Phase 0 Step 2).

The multi-tenant resolver runs against a **real** in-memory SQLite engine with
the real ORM schema, the real ``PostgreSQLOrganizationRepository`` and the real
``ROLE_PERMISSIONS`` map — the layer under test is never substituted. The one
place a double appears is ``_RecordingOrgRepository``, and it is there to
observe whether a *dependency* was reached at all, which a real repository
cannot report.

Test ↔ guard mapping (each guard, and the fail-open mutation that breaks it):

===============================================================  =========================================================
Test                                                             Guard it pins
===============================================================  =========================================================
``test_missing_ids_resolve_to_nothing``                          ``if not user_id or not organization_id`` — the
``test_missing_ids_do_not_reach_the_repository``                 unidentified caller. Dropping the guard makes the
                                                                 second fail (a query is issued on ``None``).
``test_non_member_resolves_to_nothing``                          ``if role_id is None: return frozenset()`` — mutate the
                                                                 return to ``STANDALONE_PERMISSIONS`` and it fails.
``test_unseeded_role_id_resolves_to_nothing``                    ``ROLE_BY_ID.get(role_id)`` + the ``None`` check —
                                                                 mutate to ``.get(role_id, Role.MEMBER)`` and it fails.
``test_member_resolves_to_its_role_tier``                        the expansion is the role's own set, per role, not one
``test_each_seeded_role_resolves_to_its_own_set``                tier for everyone.
``test_standalone_resolves_the_admin_tier``                      ``STANDALONE_PERMISSIONS`` is the admin set — mutate it
                                                                 to ``Role.VIEWER``'s and it fails.
``test_standalone_ignores_the_ids``                              standalone answers without a table it does not populate.
``test_factory_follows_the_tenant_provider``                     ``create_permission_resolver`` reads the mode off the
                                                                 built provider — invert the branch and it fails.
``test_has_fails_closed_for_a_non_member``                       ``PermissionResolver.has`` is membership, not truthiness.
===============================================================  =========================================================

**Phase 0 enforces nothing**: this resolver has no caller. That is pinned
separately by ``tests/unit/modules/auth/test_permission_enforcement_is_unwired.py``.
"""

from typing import List, Optional, Tuple

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    OrganizationMemberModel,
    PermissionModel,
    RoleModel,
    RolePermissionModel,
)
from faultmaven.infrastructure.persistence.organization_repository import (
    PostgreSQLOrganizationRepository,
)
from faultmaven.models.rbac import ROLE_PERMISSIONS, Permission, Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.permissions import (
    STANDALONE_PERMISSIONS,
    MultiTenantPermissionResolver,
    SingleTenantPermissionResolver,
    create_permission_resolver,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

ORG = "org-1"
from tests.utils import DEFAULT_TEST_ENTERPRISE_ID  # noqa: E402

OTHER_ORG = "org-2"
USER = "user-1"
OUTSIDER = "user-2"


@pytest.fixture(scope="function")
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def seeded(session):
    """Roles / permissions / grants, as migration 029 seeds them."""
    for role, role_id in SYSTEM_ROLE_IDS.items():
        session.add(
            RoleModel(
                role_id=role_id,
                name=role.value,
                scope="organization",
                is_system_role=True,
            )
        )
    for perm in Permission:
        resource, action = perm.value.split(":")
        session.add(
            PermissionModel(
                permission_id=f"perm-{perm.name}", resource=resource, action=action
            )
        )
    for role, perms in ROLE_PERMISSIONS.items():
        for perm in perms:
            session.add(
                RolePermissionModel(
                    role_id=SYSTEM_ROLE_IDS[role], permission_id=f"perm-{perm.name}"
                )
            )
    await session.commit()
    return session


@pytest.fixture
def org_repo(seeded):
    return PostgreSQLOrganizationRepository(seeded)


@pytest.fixture
def multi(org_repo):
    return MultiTenantPermissionResolver(org_repo)


async def _join(session, user_id: str, role: Role, organization_id: str = ORG) -> None:
    session.add(
        OrganizationMemberModel(
            user_id=user_id,
            organization_id=organization_id,
            # The roster row is RLS-tenanted on the enterprise (ADR-017 D1), so
            # it carries the one its organization belongs to.
            enterprise_id=DEFAULT_TEST_ENTERPRISE_ID,
            role_id=SYSTEM_ROLE_IDS[role],
        )
    )
    await session.commit()


class _RecordingOrgRepository(PostgreSQLOrganizationRepository):
    """The real repository, with the one lookup the resolver uses instrumented.

    Not a stand-in for anything: it answers exactly as the repository does and
    only records that it was asked. "Was a query issued?" is not observable
    from the answer — a missing id and a non-member both resolve to nothing —
    and that distinction is what the missing-id guard is for.
    """

    def __init__(self, db_session) -> None:
        super().__init__(db_session)
        self.role_lookups: List[Tuple[Optional[str], Optional[str]]] = []

    async def get_member_role(
        self, organization_id: str, user_id: str
    ) -> Optional[str]:
        self.role_lookups.append((organization_id, user_id))
        return await super().get_member_role(organization_id, user_id)


# =============================================================================
# Multi-tenant: the caller's org role, expanded live
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_member_resolves_to_its_role_tier(multi, seeded):
    await _join(seeded, USER, Role.MEMBER)

    assert await multi.resolve(USER, ORG) == ROLE_PERMISSIONS[Role.MEMBER]


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("role", list(Role))
async def test_each_seeded_role_resolves_to_its_own_set(multi, seeded, role):
    """The property, not one instance — every seeded role, its own set."""
    await _join(seeded, USER, role)

    assert await multi.resolve(USER, ORG) == ROLE_PERMISSIONS[role]


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_non_member_resolves_to_nothing(multi, seeded):
    """Including a member of a *different* org — membership is per organization."""
    await _join(seeded, USER, Role.ADMIN)

    assert await multi.resolve(OUTSIDER, ORG) == frozenset()
    assert await multi.resolve(USER, OTHER_ORG) == frozenset()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_unseeded_role_id_resolves_to_nothing(multi, seeded):
    """A role_id outside the seeded set grants nothing, not a default tier."""
    seeded.add(
        RoleModel(
            role_id="custom-role",
            name="custom",
            scope="organization",
            is_system_role=False,
        )
    )
    await seeded.commit()
    seeded.add(
        OrganizationMemberModel(
            user_id=USER,
            organization_id=ORG,
            enterprise_id=DEFAULT_TEST_ENTERPRISE_ID,
            role_id="custom-role",
        )
    )
    await seeded.commit()

    assert await multi.resolve(USER, ORG) == frozenset()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    ("user_id", "organization_id"),
    [(None, ORG), (USER, None), (None, None), ("", ORG), (USER, "")],
)
async def test_missing_ids_resolve_to_nothing(multi, seeded, user_id, organization_id):
    await _join(seeded, USER, Role.ADMIN)

    assert await multi.resolve(user_id, organization_id) == frozenset()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    ("user_id", "organization_id"),
    [(None, ORG), (USER, None), (None, None), ("", ORG), (USER, "")],
)
async def test_missing_ids_do_not_reach_the_repository(
    seeded, user_id, organization_id
):
    """An unidentified caller is answered, not looked up."""
    recorder = _RecordingOrgRepository(seeded)
    await _join(seeded, USER, Role.ADMIN)

    result = await MultiTenantPermissionResolver(recorder).resolve(
        user_id, organization_id
    )

    assert result == frozenset()
    assert recorder.role_lookups == []
    # The instrument works: a resolvable caller does reach the lookup.
    assert await MultiTenantPermissionResolver(recorder).resolve(USER, ORG)
    assert recorder.role_lookups == [(ORG, USER)]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resolve_returns_a_set_never_none(multi, seeded):
    """``None`` would read as 'no permissions' to ``if result:`` and as an
    unresolved answer to a reader that checks for it — the ambiguity this
    project keeps paying for."""
    assert isinstance(await multi.resolve(OUTSIDER, ORG), frozenset)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_has_fails_closed_for_a_non_member(multi, seeded):
    await _join(seeded, USER, Role.VIEWER)

    assert await multi.has(USER, ORG, Permission.CASES_READ)
    assert not await multi.has(USER, ORG, Permission.CASES_DELETE)
    assert not await multi.has(OUTSIDER, ORG, Permission.CASES_READ)
    assert not await multi.has(None, ORG, Permission.CASES_READ)


# =============================================================================
# Standalone: the fixed set, no table read
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_standalone_resolves_the_admin_tier():
    """The single account is legitimately its organization's admin.

    Pinned by content, not by re-deriving the same expression the module uses:
    ``org:manage_users`` is what separates admin from every other tier, and a
    resolver that quietly dropped to ``member`` or ``viewer`` would still look
    plausible.
    """
    resolver = SingleTenantPermissionResolver()

    resolved = await resolver.resolve(USER, ORG)

    assert Permission.ORG_MANAGE_USERS in resolved
    assert Permission.ORG_MANAGE_SETTINGS in resolved
    assert Permission.CASES_DELETE in resolved
    assert resolved == STANDALONE_PERMISSIONS


@pytest.mark.asyncio
@pytest.mark.unit
async def test_standalone_ignores_the_ids():
    """No membership row is required — standalone does not populate that table."""
    resolver = SingleTenantPermissionResolver()

    assert await resolver.resolve(None, None) == STANDALONE_PERMISSIONS
    assert await resolver.resolve("anyone", "any-org") == STANDALONE_PERMISSIONS


# =============================================================================
# The seam
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_factory_follows_the_tenant_provider(org_repo):
    """The mode is read off the built provider, so there is one decision."""
    single = SingleTenantProvider(enterprise_repository=org_repo)
    multi_provider = MultiTenantProvider(enterprise_repository=org_repo)

    assert isinstance(
        await create_permission_resolver(single, org_repo),
        SingleTenantPermissionResolver,
    )
    assert isinstance(
        await create_permission_resolver(multi_provider, org_repo),
        MultiTenantPermissionResolver,
    )
