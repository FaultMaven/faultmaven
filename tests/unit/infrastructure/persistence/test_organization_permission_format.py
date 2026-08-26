"""``user_has_permission`` speaks the ``Permission`` enum's own spelling.

The method used to parse ``"resource.action"`` (a **dot**) while the
:class:`~faultmaven.models.rbac.Permission` enum spells the same permission with
a **colon** — and migration 029 seeds the ``permissions`` rows by splitting the
enum value on that colon. Passing the enum's own value therefore returned
``False``: a denial with no relation to the user's permissions, which fails
closed and so surfaces only as "the RBAC wiring doesn't work" (#1163, Step 1).

These tests run the **real** repository against a real in-memory SQLite engine
with the real ORM schema and the real ``ROLE_PERMISSIONS`` map (pattern:
``test_team_repository``). Nothing about the layer under test is mocked — only
the database is local.

Test ↔ guard mapping (what each test pins, and the mutation that must break it):

===========================================================  ==========================================================
Test                                                         Guard it pins
===========================================================  ==========================================================
``test_enum_member_is_accepted``                             ``raw = permission.value if isinstance(...)`` +
``test_every_permission_of_the_role_resolves``               ``raw.split(":")`` — restore ``permission.split(".")``
``test_colon_string_is_accepted``                            and all four fail.
``test_dot_form_is_no_longer_accepted``                      The dot form is *dropped*, not kept: restoring a
                                                             dot-tolerant parse makes this one fail.
``test_unparseable_permission_denies``                       ``len(parts) != 2`` — widening it to ``< 2`` makes the
                                                             three-part cases fail (an unpack error, out of an
                                                             authorization check).
``test_half_empty_spelling_denies_even_against_a``           ``not all(parts)`` — the half that is invisible against
``_degenerate_row``                                          the seeded table, so this one seeds the degenerate row
                                                             that makes it visible. Drop the clause and it fails.
``test_non_string_permission_denies``                        ``isinstance(raw, str)`` — without it a ``None`` raises
                                                             ``AttributeError`` out of an authorization check.
``test_permission_outside_the_role_denies``                  the join itself still discriminates: the normalisation
``test_non_member_denies``                                   did not turn the check into a constant ``True``.
===========================================================  ==========================================================
"""

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

ORG = "org-1"
USER = "user-1"
OUTSIDER = "user-2"


@pytest.fixture(scope="function")
async def engine():
    """In-memory SQLite engine with the full ORM schema."""
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
    """Seed roles/permissions/grants exactly as migration 029 does.

    Permissions are stored as the enum value split on ``":"`` — that split is
    the migration's, restated here so the test would notice if the storage
    convention and the lookup convention ever diverged again.
    """
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
def repo(seeded):
    return PostgreSQLOrganizationRepository(seeded)


async def _join(session, user_id: str, role: Role) -> None:
    session.add(
        OrganizationMemberModel(
            user_id=user_id, organization_id=ORG, role_id=SYSTEM_ROLE_IDS[role]
        )
    )
    await session.commit()


# =============================================================================
# The enum's own spelling is what the check accepts
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_enum_member_is_accepted(repo, seeded):
    """A ``Permission`` member passed straight through resolves to a grant.

    This is the test the issue asks for by name: a string literal cannot
    observe the bug, because the caller would have written the dot form that
    the old parse happened to accept.
    """
    await _join(seeded, USER, Role.ADMIN)

    assert await repo.user_has_permission(USER, ORG, Permission.ORG_MANAGE_USERS)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_every_permission_of_the_role_resolves(repo, seeded):
    """The property, not one instance: an admin gets *all* of admin's grants."""
    await _join(seeded, USER, Role.ADMIN)

    for perm in ROLE_PERMISSIONS[Role.ADMIN]:
        assert await repo.user_has_permission(USER, ORG, perm), perm


@pytest.mark.asyncio
@pytest.mark.unit
async def test_colon_string_is_accepted(repo, seeded):
    """The enum's *value* — what a caller that cannot import the enum passes."""
    await _join(seeded, USER, Role.ADMIN)

    assert await repo.user_has_permission(USER, ORG, "org:manage_users")


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_dot_form_is_no_longer_accepted(repo, seeded):
    """The dot form is dropped, not kept beside the colon form.

    Keeping it would leave two spellings for one permission and the next
    caller free to pick the one nothing else understands.
    """
    await _join(seeded, USER, Role.ADMIN)

    assert not await repo.user_has_permission(USER, ORG, "org.manage_users")


# =============================================================================
# Fails closed
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "org",
        "org:",
        ":manage_users",
        "org:manage:users",
        "org::manage_users",
        "org manage_users",
    ],
)
async def test_unparseable_permission_denies(repo, seeded, bad):
    """Anything that is not exactly one ``resource:action`` pair denies."""
    await _join(seeded, USER, Role.ADMIN)

    assert not await repo.user_has_permission(USER, ORG, bad)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_half_empty_spelling_denies_even_against_a_degenerate_row(repo, seeded):
    """The rejection does not lean on the ``permissions`` table's contents.

    ``permissions.resource`` / ``.action`` are NOT NULL but not non-empty, so a
    row with an empty half is schema-legal. Without the explicit rejection,
    ``"org:"`` would *match* such a row and grant — so this seeds one and pins
    that it still denies.
    """
    seeded.add(
        PermissionModel(permission_id="perm-degenerate", resource="org", action="")
    )
    seeded.add(
        RolePermissionModel(
            role_id=SYSTEM_ROLE_IDS[Role.ADMIN], permission_id="perm-degenerate"
        )
    )
    await seeded.commit()
    await _join(seeded, USER, Role.ADMIN)

    assert not await repo.user_has_permission(USER, ORG, "org:")


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("bad", [None, 7, ["org:manage_users"]])
async def test_non_string_permission_denies(repo, seeded, bad):
    """A non-string denies rather than raising out of an authorization check."""
    await _join(seeded, USER, Role.ADMIN)

    assert not await repo.user_has_permission(USER, ORG, bad)


# =============================================================================
# The check still discriminates
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_permission_outside_the_role_denies(repo, seeded):
    """A viewer does not hold a permission its role was never granted."""
    await _join(seeded, USER, Role.VIEWER)

    assert await repo.user_has_permission(USER, ORG, Permission.CASES_READ)
    assert not await repo.user_has_permission(USER, ORG, Permission.CASES_DELETE)
    assert not await repo.user_has_permission(USER, ORG, Permission.ORG_MANAGE_USERS)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.security
async def test_non_member_denies(repo, seeded):
    """Membership of *this* organization is what the join requires."""
    await _join(seeded, USER, Role.ADMIN)

    assert not await repo.user_has_permission(OUTSIDER, ORG, Permission.CASES_READ)
    assert not await repo.user_has_permission(USER, "org-other", Permission.CASES_READ)
