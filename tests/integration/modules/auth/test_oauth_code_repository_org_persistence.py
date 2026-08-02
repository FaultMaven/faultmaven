"""The ORM authorization-code repository persists the tenant (#872).

`PostgresOAuthCodeRepository` is not wired — `create_oauth_code_repository`
returns only the Redis or in-memory implementation, because authorization codes
are ephemeral and live in the cache layer. So why test it?

Because migration 039's stated justification for adding the column is that it
"keeps the ORM-backed implementation of `IOAuthCodeRepository` capable of
honoring the same contract, so wiring it later cannot silently reintroduce
#872". Adversarial review pointed out that nothing enforced that claim: deleting
`organization_id` from either the write or the read left the whole suite green,
so wiring it later *could* silently reintroduce #872 — the exact failure the
column exists to prevent, in the exact code the column exists for.

A claim in a migration docstring that no test defends is a claim that decays.
This is the cheapest way to make it true rather than aspirational.

SQLite stands in for PostgreSQL here: the assertion is about the repository's
own column mapping, which is dialect-independent, and `oauth_authorization_codes`
carries no PostgreSQL-only construct (the column is a plain nullable VARCHAR(36),
deliberately no FK and no RLS — see migration 039).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.contracts import OAuthCodeDTO
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    PostgresOAuthCodeRepository,
)

TENANT = "org_acme_7f3c"
REDIRECT = "chrome-extension://abc123/callback.html"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _code(code: str, organization_id, **overrides) -> OAuthCodeDTO:
    return OAuthCodeDTO(
        code=code,
        user_id="user_123",
        redirect_uri=REDIRECT,
        code_challenge="challenge",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        organization_id=organization_id,
        **overrides,
    )


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_orm_repository_round_trips_the_organization(session_factory):
    """Write then read must preserve the tenant, or wiring this reintroduces #872."""
    repo = PostgresOAuthCodeRepository(session_factory)

    await repo.save_code(_code("code_1", TENANT))

    stored = await repo.get_code("code_1")
    assert stored is not None
    assert stored.organization_id == TENANT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_orm_repository_keeps_the_organization_across_mark_used(session_factory):
    """The exchange reads the code before marking it, but order must not matter.

    `mark_code_used` issues an UPDATE that touches only `used`; if it were ever
    rewritten as a row replacement it could drop the tenant the way the
    in-memory implementation's field-by-field rebuild once did.
    """
    repo = PostgresOAuthCodeRepository(session_factory)
    await repo.save_code(_code("code_2", TENANT))

    await repo.mark_code_used("code_2")

    stored = await repo.get_code("code_2")
    assert stored is not None
    assert stored.used is True
    assert stored.organization_id == TENANT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_orm_repository_tolerates_a_code_with_no_organization(session_factory):
    """Nullable means nullable: a single-tenant or pre-039 code must round-trip.

    It must come back as `None` rather than raising or inventing a tenant — the
    exchange turns `None` into an unusable claim, which is the fail-closed path.
    """
    repo = PostgresOAuthCodeRepository(session_factory)
    await repo.save_code(_code("code_3", None))

    stored = await repo.get_code("code_3")
    assert stored is not None
    assert stored.organization_id is None
