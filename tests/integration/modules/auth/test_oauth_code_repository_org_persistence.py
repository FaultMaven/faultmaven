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
    fields = dict(
        code=code,
        user_id="user_123",
        redirect_uri=REDIRECT,
        code_challenge="challenge",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        organization_id=organization_id,
    )
    fields.update(overrides)
    return OAuthCodeDTO(**fields)


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

    `claim_code` issues an UPDATE that touches only `used`; if it were ever
    rewritten as a row replacement it could drop the tenant the way the
    in-memory implementation's field-by-field rebuild once did.
    """
    repo = PostgresOAuthCodeRepository(session_factory)
    await repo.save_code(_code("code_2", TENANT))

    assert await repo.claim_code("code_2") is True

    stored = await repo.get_code("code_2")
    assert stored is not None
    assert stored.used is True
    assert stored.organization_id == TENANT


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_orm_repository_claim_is_a_compare_and_swap(session_factory):
    """The claim must be won once, and the database must be the arbiter.

    `claim_code` reports success from `rowcount == 1` on an UPDATE predicated on
    `used = false`. Drop that predicate and the UPDATE matches the row every
    time, so every concurrent redeemer is told it won and every one of them
    mints — the exact replay the atomic claim exists to prevent.

    Mutation testing put this test here: removing the predicate left the rest of
    this file green, because org round-tripping does not depend on it. A
    correctness-critical primitive in an unwired implementation is still a
    primitive someone will one day wire.
    """
    repo = PostgresOAuthCodeRepository(session_factory)
    await repo.save_code(_code("code_cas", TENANT))

    assert await repo.claim_code("code_cas") is True
    # Every subsequent claim loses, however many times it is attempted.
    assert await repo.claim_code("code_cas") is False
    assert await repo.claim_code("code_cas") is False


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_orm_repository_refuses_to_claim_an_expired_code(session_factory):
    """Expiry must be in the claim predicate, not left to a prior SELECT.

    Rows here are removed by a sweep, not by a TTL, so an expired row lingers
    and stays claimable unless the UPDATE says otherwise. Redis gets this free
    because the key is gone, and the in-memory store checks it explicitly — a
    backend that disagreed would make single-use depend on the deployment,
    which is the divergence this contract exists to remove.
    """
    repo = PostgresOAuthCodeRepository(session_factory)
    await repo.save_code(
        _code(
            "code_expired",
            TENANT,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )

    assert await repo.claim_code("code_expired") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_orm_repository_refuses_to_claim_an_unknown_code(session_factory):
    """Nothing to claim must read as "you did not win", never as success."""
    repo = PostgresOAuthCodeRepository(session_factory)
    assert await repo.claim_code("never-existed") is False


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
