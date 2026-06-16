"""Standalone tenant-isolation guard (ADR-006, refinement #5).

PERMANENT SECURITY GUARD — do not weaken. FaultMaven Standalone is
strictly single-user / single-tenant: the local operator is the sole owner of
one instance. The open core must therefore NEVER honor a client-supplied
tenant context (an injected ``X-Organization-ID`` header or a multi-tenant JWT
claim) — every request is forced to the one standalone organization.

These tests pin that property at the org-resolution **seam**
(``SingleTenantProvider`` — the single place Standalone resolves the current org) and
structurally forbid re-introducing request-header tenant extraction in the core.
Multi-tenancy is a cloud-only capability (ADR-006); if you are tempted to make
any of these pass by honoring an external org, you are re-opening the boundary
leak this guard exists to prevent — stop.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import faultmaven
from faultmaven.models.interfaces_user import Organization, OrgPlanTier
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

# A plausible "attacker / other tenant" organization id.
FOREIGN_ORG_ID = "deadbeef-dead-dead-dead-deaddeafbeef"


@pytest.fixture
def default_org() -> Organization:
    return Organization(
        organization_id=SingleTenantProvider.DEFAULT_ORG_ID,
        slug=SingleTenantProvider.DEFAULT_ORG_SLUG,
        name=SingleTenantProvider.DEFAULT_ORG_NAME,
        description="Default organization",
        plan_tier=OrgPlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def provider(default_org):
    repo = AsyncMock()
    repo.get_organization.return_value = default_org
    return SingleTenantProvider(organization_repository=repo)


@pytest.fixture
def user() -> User:
    return User(
        user_id="user_123",
        email="owner@example.com",
        hashed_password="x",
        full_name="Local Owner",
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "injected",
    [FOREIGN_ORG_ID, "", "   ", "another-org", SingleTenantProvider.DEFAULT_ORG_ID],
)
async def test_injected_organization_id_is_ignored(provider, user, injected):
    """Any client-supplied organization_id is ignored -> forced to the default org."""
    org = await provider.get_current_organization(
        current_user=user, organization_id=injected
    )
    assert org.organization_id == SingleTenantProvider.DEFAULT_ORG_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_foreign_org_id_never_reaches_the_repository(provider, user):
    """The injected org id must never even be used to query the database."""
    await provider.get_current_organization(
        current_user=user, organization_id=FOREIGN_ORG_ID
    )
    called_ids = [
        (c.args[0] if c.args else c.kwargs.get("organization_id"))
        for c in provider.organization_repository.get_organization.call_args_list
    ]
    assert called_ids == [SingleTenantProvider.DEFAULT_ORG_ID]
    assert FOREIGN_ORG_ID not in called_ids


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_single_tenant_mode_is_not_multi_tenant(provider):
    assert await provider.is_multi_tenant() is False


@pytest.mark.unit
@pytest.mark.security
def test_standalone_seed_ids_are_pinned():
    """The seeded UUIDs are load-bearing substrate (ADR-006 implicit seed) —
    changing them orphans existing standalone data, so they are pinned here."""
    assert SingleTenantProvider.DEFAULT_ORG_ID == "00000000-0000-0000-0000-000000000001"
    assert (
        SingleTenantProvider.DEFAULT_ENTERPRISE_ID
        == "00000000-0000-0000-0000-000000000002"
    )


@pytest.mark.unit
@pytest.mark.security
def test_core_has_no_request_header_tenant_extraction():
    """Structural guard: the OSS core must not read an org/tenant from a request
    header. Re-adding ``X-Organization-ID`` (or similar) header extraction would
    let single-tenant CE honor an external tenant context — the leak this
    boundary prevents. Docstring/prose mentions are fine; active reads are not.
    """
    core = Path(faultmaven.__file__).resolve().parent
    bad = re.compile(
        r"""(headers\.get\(\s*['"]|alias\s*=\s*['"])x-(organization|org|tenant)[-_]?id""",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for py in core.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for m in bad.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{py.relative_to(core.parent)}:{line}")
    assert not offenders, (
        "Core reads a tenant id from a request header — single-tenant CE must not "
        "honor client-supplied tenant context (ADR-006). Offenders:\n  "
        + "\n  ".join(offenders)
    )
