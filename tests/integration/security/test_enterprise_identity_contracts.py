"""Four schema-and-writer contracts the review round restored or introduced.

Each one is a claim some other file already makes — a CLI's help text, a
repository's own filter, a uniqueness index — that nothing was holding true.

* **A named ``--enterprise-id`` refuses when it names no LIVE enterprise.**
  ``provision_sso_org`` documents this refusal and catches ``LookupError`` for
  it; the writer had stopped raising, so an unknown or retired id fell through to
  the slug arm and joined — or created — an enterprise the operator did not name,
  with the "REUSING AN EXISTING TENANT" warning suppressed. Accounts land under a
  tenant nobody chose and every login then fails closed with
  ``enterprise_mismatch``.
* **``sso_personal_enterprises`` is keyed on ``(provider, subject)``.** A subject
  handle is unique only within an IdP, and ``find_live_binding`` already filtered
  on the provider — a question the key could not answer.
* **``enterprises.domain`` is case-folded on every write.** Uniqueness is on the
  raw column and only the get-or-create writer folded, so ``Acme.com`` and
  ``acme.com`` were two enterprises for one company, each invisible to the
  other's sign-ups.
* **The audit writer refuses an unscoped context** rather than stamping the empty
  non-tenant sentinel, which passes ``NOT NULL`` and then dies on the foreign key
  several frames later, inside whatever transaction the caller had open.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from tests.integration.security.conftest import DEFAULT_ENTERPRISE_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def session_factory():
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


# ---------------------------------------------------------------------------
# A8 — a named enterprise id is a claim that it exists
# ---------------------------------------------------------------------------


async def test_an_unknown_enterprise_id_is_refused(session_factory):
    """The operator typed an id. It has to name something."""
    from faultmaven.infrastructure.persistence.tenant_bootstrap import (
        get_or_create_enterprise,
    )

    absent = f"ent_absent_{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        with pytest.raises(LookupError, match=absent):
            await get_or_create_enterprise(
                session, enterprise_id=absent, name="Acme", slug="acme-probe"
            )
        await session.rollback()

    # And nothing was written under the id, nor under the slug it would have
    # fallen through to.
    async with session_factory() as session:
        found = (
            await session.execute(
                text(
                    "SELECT count(*) FROM enterprises "
                    "WHERE enterprise_id = :e OR slug = :s"
                ),
                {"e": absent, "s": "acme-probe"},
            )
        ).scalar()
    assert found == 0, "the refused call created the tenant it refused to find"


async def test_a_retired_enterprise_id_is_refused(session_factory):
    """A soft-deleted tenant is exactly as unusable as an absent one here.

    Adopting it would put a new organization inside a fenced tenant, which is
    the state a retirement exists to prevent.
    """
    from faultmaven.infrastructure.persistence.tenant_bootstrap import (
        get_or_create_enterprise,
    )

    retired = f"ent_retired_{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug, deleted_at) "
                "VALUES (:e, :n, :s, now())"
            ),
            {"e": retired, "n": "Retired", "s": f"retired-{retired[-8:]}"},
        )
        await session.commit()

    try:
        async with session_factory() as session:
            with pytest.raises(LookupError, match="retired"):
                await get_or_create_enterprise(
                    session,
                    enterprise_id=retired,
                    name="Retired",
                    slug=f"retired-{retired[-8:]}",
                )
            await session.rollback()
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"), {"e": retired}
            )
            await session.commit()


async def test_a_live_enterprise_id_still_resolves(session_factory):
    """The control: the refusal is about absence, not about naming an id."""
    from faultmaven.infrastructure.persistence.tenant_bootstrap import (
        get_or_create_enterprise,
    )

    async with session_factory() as session:
        enterprise, created = await get_or_create_enterprise(
            session,
            enterprise_id=DEFAULT_ENTERPRISE_ID,
            name="unused",
            slug="unused",
        )
        assert created is False
        assert enterprise.enterprise_id == DEFAULT_ENTERPRISE_ID


# ---------------------------------------------------------------------------
# A16 — a subject handle is unique only within an IdP
# ---------------------------------------------------------------------------


async def test_one_subject_may_own_a_binding_at_each_provider(session_factory):
    """Two providers, one spelling. Keyed on the subject alone, they collide.

    And a collision here is not a duplicate row — it is the second person
    resolving to the FIRST person's tenant, at a different IdP.
    """
    subject = f"user_{uuid.uuid4().hex[:12]}"
    enterprises = [f"ent_a16_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    try:
        async with session_factory() as session:
            for enterprise_id in enterprises:
                await session.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, :n, :s)"
                    ),
                    {
                        "e": enterprise_id,
                        "n": "A16 probe",
                        "s": f"a16-{enterprise_id[-8:]}",
                    },
                )
            for provider, enterprise_id in zip(("workos", "okta"), enterprises):
                await session.execute(
                    text(
                        "INSERT INTO sso_personal_enterprises "
                        "(subject, provider, enterprise_id, provider_org_id) "
                        "VALUES (:s, :p, :e, :o)"
                    ),
                    {
                        "s": subject,
                        "p": provider,
                        "e": enterprise_id,
                        "o": f"org_{provider}",
                    },
                )
            await session.commit()

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT provider, enterprise_id FROM sso_personal_enterprises "
                        "WHERE subject = :s ORDER BY provider"
                    ),
                    {"s": subject},
                )
            ).all()
        assert [row[0] for row in rows] == ["okta", "workos"]
        assert len({row[1] for row in rows}) == 2, (
            "the two providers' subjects resolved to one enterprise — which is "
            "one person's tenant answering for another's"
        )
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM sso_personal_enterprises WHERE subject = :s"),
                {"s": subject},
            )
            for enterprise_id in enterprises:
                await session.execute(
                    text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                    {"e": enterprise_id},
                )
            await session.commit()


async def test_the_repository_resolves_a_binding_by_its_whole_key(session_factory):
    """The read half: ``get`` must not answer with another provider's row."""
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        SessionlessSSOPersonalEnterpriseRepository,
    )

    subject = f"user_{uuid.uuid4().hex[:12]}"
    enterprise_id = f"ent_a16r_{uuid.uuid4().hex[:8]}"
    try:
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:e, :n, :s)"
                ),
                {
                    "e": enterprise_id,
                    "n": "A16 read",
                    "s": f"a16r-{enterprise_id[-8:]}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO sso_personal_enterprises "
                    "(subject, provider, enterprise_id, provider_org_id) "
                    "VALUES (:s, 'workos', :e, 'org_x')"
                ),
                {"s": subject, "e": enterprise_id},
            )
            await session.commit()

        repository = SessionlessSSOPersonalEnterpriseRepository()
        assert (await repository.get("workos", subject)) is not None
        assert (await repository.get("okta", subject)) is None, (
            "a binding minted at one IdP answered for the same spelling at " "another"
        )
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM sso_personal_enterprises WHERE subject = :s"),
                {"s": subject},
            )
            await session.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise_id},
            )
            await session.commit()


# ---------------------------------------------------------------------------
# A18 — a domain is case-insensitive, so the column has to be
# ---------------------------------------------------------------------------


async def test_two_spellings_of_one_domain_are_one_enterprise(session_factory):
    """``Acme.com`` and ``acme.com`` are the same company.

    Written through the repository, which is the writer an admin surface uses —
    the get-or-create path folded already, and this is the one that did not.
    """
    from faultmaven.infrastructure.persistence.enterprise_repository import (
        PostgreSQLEnterpriseRepository,
    )
    from faultmaven.models.interfaces_user import Enterprise

    domain = f"Acme{uuid.uuid4().hex[:8]}.com"
    enterprise_id = f"ent_a18_{uuid.uuid4().hex[:8]}"
    try:
        async with session_factory() as session:
            repository = PostgreSQLEnterpriseRepository(session)
            now = datetime.now(UTC)
            await repository.create_enterprise(
                Enterprise(
                    enterprise_id=enterprise_id,
                    name=domain,
                    slug=f"a18-{enterprise_id[-8:]}",
                    domain=domain,
                    created_at=now,
                    updated_at=now,
                )
            )

        async with session_factory() as session:
            stored = (
                await session.execute(
                    text("SELECT domain FROM enterprises WHERE enterprise_id = :e"),
                    {"e": enterprise_id},
                )
            ).scalar()
        assert stored == domain.casefold(), (
            "the domain was stored as typed, so the unique index — which is on "
            "the raw column — would admit the other spelling as a second "
            "enterprise for the same company"
        )

        # And the lookup finds it however the caller spells it, which is the
        # property the sign-up path depends on.
        async with session_factory() as session:
            repository = PostgreSQLEnterpriseRepository(session)
            assert (await repository.find_live_by_domain(domain)) is not None
            assert (await repository.find_live_by_domain(domain.upper())) is not None
            assert (await repository.find_live_by_domain(domain.casefold())) is not None
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise_id},
            )
            await session.commit()


# ---------------------------------------------------------------------------
# A14 — the audit writer refuses an unscoped context
# ---------------------------------------------------------------------------


async def test_an_unscoped_audit_write_is_refused_by_name(session_factory):
    """``""`` is what the front door binds for an unauthenticated request.

    It passes ``NOT NULL``, and then dies on the ``enterprises`` foreign key as
    an opaque ``IntegrityError`` several frames later, inside whatever
    transaction the caller had open. The record was lost either way; what
    changes is that the reason is now stated where it becomes knowable.
    """
    from faultmaven.infrastructure.persistence.audit_repository import (
        PostgreSQLAuditRepository,
    )
    from faultmaven.models.interfaces_user import AuditCategory, AuditEventType

    set_current_enterprise_id("")

    async with session_factory() as session:
        repository = PostgreSQLAuditRepository(session)
        with pytest.raises(ValueError, match="not scoped to an enterprise"):
            await repository.log_event(
                user_id=None,
                event_type=AuditEventType.LOGIN_FAILED,
                event_category=AuditCategory.AUTHENTICATION,
            )


async def test_a_scoped_audit_write_still_lands(session_factory):
    """The control: the refusal is about the unscoped context, not about writing."""
    from faultmaven.infrastructure.persistence.audit_repository import (
        PostgreSQLAuditRepository,
    )
    from faultmaven.models.interfaces_user import AuditCategory, AuditEventType

    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)
    marker = f"probe_{uuid.uuid4().hex[:8]}"

    try:
        async with session_factory() as session:
            repository = PostgreSQLAuditRepository(session)
            assert await repository.log_event(
                user_id=None,
                event_type=AuditEventType.LOGIN_FAILED,
                event_category=AuditCategory.AUTHENTICATION,
                resource_id=marker,
            )

        async with session_factory() as session:
            stored = (
                await session.execute(
                    text(
                        "SELECT enterprise_id FROM user_audit_log "
                        "WHERE resource_id = :r"
                    ),
                    {"r": marker},
                )
            ).scalar()
        assert stored == DEFAULT_ENTERPRISE_ID
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM user_audit_log WHERE resource_id = :r"), {"r": marker}
            )
            await session.commit()
