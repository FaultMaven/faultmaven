"""``scripts/auth/provision_sso_org.py`` refuses to bind the wrong tenants (#869).

The script resolves an organization by ``(enterprise, slug)`` and the enterprise
itself by slug, so a new customer whose slug collides with an existing one
resolves onto the EXISTING tenant. That is legitimate when an operator means it
(a second IdP organization for the same customer) and a data-isolation incident
when they do not — the new customer's users would land in someone else's tenant
and see their cases.

``_ensure_mapping`` is the last gate before that becomes durable, so it checks
*both* directions of the 1:1 relation and refuses rather than writing:

* the IdP org already points at a different organization (``RemapRefused``);
* the organization is already claimed by a different IdP org
  (``OrgAlreadyClaimed``) — the case the ``UNIQUE (provider, organization_id)``
  constraint would otherwise surface as a raw ``IntegrityError``.

Exercised against a real in-memory SQLite engine built from the ORM metadata,
so the refusals are checked against the schema that actually ships.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "auth" / "provision_sso_org.py"

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
ORG_A = "22222222-2222-2222-2222-222222222222"
ORG_B = "55555555-5555-5555-5555-555555555555"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("provision_sso_org", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now():
    return datetime.now(timezone.utc)


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
        s.add(
            EnterpriseModel(
                enterprise_id=ENTERPRISE_ID,
                name="Acme",
                slug="acme",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        for org_id, slug in ((ORG_A, "acme-a"), (ORG_B, "acme-b")):
            s.add(
                OrganizationModel(
                    organization_id=org_id,
                    enterprise_id=ENTERPRISE_ID,
                    name=f"Acme {slug}",
                    slug=slug,
                    is_active=True,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        await s.commit()
        yield s


async def _seed_mapping(session, provider_org_id, organization_id):
    session.add(
        SSOOrgMappingModel(
            provider="workos",
            provider_org_id=provider_org_id,
            organization_id=organization_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.commit()


async def _mapping_rows(session):
    from sqlalchemy import select

    result = await session.execute(select(SSOOrgMappingModel))
    return [
        (row.provider, row.provider_org_id, row.organization_id)
        for row in result.scalars().all()
    ]


# =============================================================================
# The reverse-claim refusal (F1)
# =============================================================================


async def test_refuses_to_bind_an_org_already_claimed_by_another_idp_org(mod, session):
    """The slug-collision alarm: this tenant already belongs to someone else's
    IdP organization, so binding a second one would pool two customers."""
    await _seed_mapping(session, "org_INCUMBENT", ORG_A)

    with pytest.raises(mod.OrgAlreadyClaimed) as exc:
        await mod._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", organization_id=ORG_A
        )

    assert exc.value.organization_id == ORG_A
    assert exc.value.claimed_by == "org_INCUMBENT"
    assert exc.value.requested_by == "org_NEWCOMER"


async def test_the_reverse_claim_refusal_writes_nothing(mod, session):
    """A refusal must not leave a partial binding behind."""
    await _seed_mapping(session, "org_INCUMBENT", ORG_A)

    with pytest.raises(mod.OrgAlreadyClaimed):
        await mod._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", organization_id=ORG_A
        )

    assert await _mapping_rows(session) == [("workos", "org_INCUMBENT", ORG_A)]


async def test_the_refusal_replaces_a_raw_integrity_error(mod, session):
    """Without the reverse check this same call died on the UNIQUE constraint
    as an unhandled traceback. Pin that it is now a typed refusal."""
    from sqlalchemy.exc import IntegrityError

    await _seed_mapping(session, "org_INCUMBENT", ORG_A)

    with pytest.raises(mod.OrgAlreadyClaimed) as exc:
        await mod._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", organization_id=ORG_A
        )

    assert not isinstance(exc.value, IntegrityError)


# =============================================================================
# The forward remap refusal
# =============================================================================


async def test_refuses_to_repoint_an_idp_org_at_a_different_organization(mod, session):
    await _seed_mapping(session, "org_01H", ORG_A)

    with pytest.raises(mod.RemapRefused) as exc:
        await mod._ensure_mapping(
            session, provider_org_id="org_01H", organization_id=ORG_B
        )

    assert exc.value.provider_org_id == "org_01H"
    assert exc.value.mapped_to == ORG_A
    assert exc.value.requested == ORG_B
    assert await _mapping_rows(session) == [("workos", "org_01H", ORG_A)]


# =============================================================================
# The permitted paths
# =============================================================================


async def test_creates_the_mapping_when_neither_side_is_bound(mod, session):
    created = await mod._ensure_mapping(
        session, provider_org_id="org_01H", organization_id=ORG_A
    )

    assert created is True
    assert await _mapping_rows(session) == [("workos", "org_01H", ORG_A)]


async def test_re_running_the_same_binding_is_a_quiet_no_op(mod, session):
    """Idempotence: same IdP org, same organization, nothing written, no raise."""
    await _seed_mapping(session, "org_01H", ORG_A)

    created = await mod._ensure_mapping(
        session, provider_org_id="org_01H", organization_id=ORG_A
    )

    assert created is False
    assert await _mapping_rows(session) == [("workos", "org_01H", ORG_A)]


async def test_a_second_organization_may_be_bound_to_its_own_idp_org(mod, session):
    """The refusals are per-pair — an unrelated tenant is unaffected."""
    await _seed_mapping(session, "org_01H", ORG_A)

    created = await mod._ensure_mapping(
        session, provider_org_id="org_01J", organization_id=ORG_B
    )

    assert created is True
    assert sorted(await _mapping_rows(session)) == sorted(
        [("workos", "org_01H", ORG_A), ("workos", "org_01J", ORG_B)]
    )


# =============================================================================
# The bind-early invariant (F2)
# =============================================================================


async def test_the_organization_is_bound_as_tenant_before_its_insert(mod, session):
    """`organizations` is RLS-tenanted and its policy doubles as WITH CHECK, so
    the INSERT has to run inside the new organization's own scope."""
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id

    set_current_org_id(STANDALONE_ORG_ID)
    bound_at_insert = []

    original_add = session.add

    def spy_add(instance):
        if isinstance(instance, OrganizationModel):
            bound_at_insert.append(get_current_org_id())
        return original_add(instance)

    session.add = spy_add
    try:
        organization, created = await mod._get_or_create_organization(
            session, enterprise_id=ENTERPRISE_ID, name="Acme New", slug="acme-new"
        )
    finally:
        session.add = original_add
        set_current_org_id(STANDALONE_ORG_ID)

    assert created is True
    # Bound to its OWN id at the moment the row was added, not the sentinel.
    assert bound_at_insert == [organization.organization_id]


async def test_resolving_an_existing_organization_also_binds_it(mod, session):
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id

    set_current_org_id(STANDALONE_ORG_ID)
    try:
        organization, created = await mod._get_or_create_organization(
            session, enterprise_id=ENTERPRISE_ID, name="Acme A", slug="acme-a"
        )
        assert created is False
        assert organization.organization_id == ORG_A
        assert get_current_org_id() == ORG_A
    finally:
        set_current_org_id(STANDALONE_ORG_ID)
