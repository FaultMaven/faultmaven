"""Personal tenants against a real PostgreSQL under RLS (#1045, ADR-016 D5).

The unit tests pin the *decision* — which branch runs, what it resolves to, what
it refuses. This module pins the thing only a real database can answer: that the
five rows a personal tenant is made of can actually be written **by the
application role**, that they are written atomically, and that two concurrent
first logins for one subject produce exactly one organization and no orphans.

Why it must be PostgreSQL, and why as a limited role
----------------------------------------------------
``organizations``, ``teams`` and ``organization_members`` are RLS-tenanted
(migration 018), and the policies are created with no ``FOR`` clause — so
``USING`` doubles as ``WITH CHECK`` and an INSERT whose ``organization_id`` is
not the session's ``app.current_org_id`` is *rejected*, not merely hidden. That
rejection is the whole reason ``fm-provision-sso-org`` demands the RLS-exempt
owner DSN. This path deliberately does not: it generates the organization id and
binds it before opening the transaction, so the policy accepts the row.

That claim is only testable as a **non-superuser, non-owner** role, because
PostgreSQL exempts both from RLS. Run as the migration role, every assertion
below would pass whether or not the binding happened — the test would be of a
system nobody deploys. So the module creates a limited role with exactly the
grants ``02-create-rls-app-role.sql`` gives ``faultmaven_app`` (DML on all
tables and sequences, no ownership) and drives the real repository through it.

SQLite proves nothing here: it has no RLS, so the half of the boundary this
module exists for would simply be absent.

What is asserted, and how a false green is prevented
----------------------------------------------------
Every "it worked" is checked by reading the rows back **as the owner**, so a
pass cannot come from RLS hiding a failure, and every "nothing was written" is
checked the same way, so a pass cannot come from RLS hiding residue. The
concurrency case asserts a count of one organization *and* a count of zero
orphaned enterprises — a rolled-back loser that left its enterprise behind would
be invisible to the first assertion and caught by the second.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

PROVIDER = "workos"

_LIMITED_ROLE = f"fm_pt_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_pt_probe_pw"
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""


def _limited_url(superuser_url: str) -> str:
    from sqlalchemy.engine import make_url

    # ``render_as_string(hide_password=False)``: ``URL.__str__`` masks the
    # password as ``***``, which fails authentication with a message naming the
    # role rather than the mask.
    return (
        make_url(superuser_url)
        .set(username=_LIMITED_ROLE, password=_LIMITED_PW)
        .render_as_string(hide_password=False)
    )


async def _create_limited_role(superuser_url: str) -> None:
    """A role with the deployed ``faultmaven_app`` grants and no ownership."""
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
            await conn.exec_driver_sql(
                f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' "
                "NOSUPERUSER NOBYPASSRLS"
            )
            await conn.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
            )
            await conn.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}"
            )
            await conn.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {_LIMITED_ROLE}"
            )
            await conn.exec_driver_sql(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                f"TO {_LIMITED_ROLE}"
            )
    finally:
        await engine.dispose()


async def _drop_limited_role(superuser_url: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at the limited role for this module only.

    Restored wholesale in teardown: the ``-m postgres`` lane runs sibling
    modules that read ``DATABASE_URL`` expecting the SUPERUSER url, and leaking
    the limited one would make them measure RLS as the wrong role and quietly
    stop proving anything.
    """
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }

    asyncio.run(_create_limited_role(superuser_url))

    os.environ["DATABASE_URL"] = _limited_url(superuser_url)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"

    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.utils import reset_settings_singleton

    reset_settings_singleton()
    reset_engine()

    yield superuser_url

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_singleton()
    reset_engine()
    asyncio.run(_drop_limited_role(superuser_url))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    """One engine per test, because there is one event loop per test.

    ``get_engine`` memoises a module-global engine whose connection pool binds
    to whatever loop first used it. pytest-asyncio gives each test its own loop,
    so a pooled connection carried into the next test belongs to a loop that is
    already closed — which surfaces as "attached to a different loop", not as a
    tenancy failure, and would make this module's real assertions unreachable.

    Disposed at teardown *inside the test's own loop*, where the connections can
    actually be closed; the setup reset is the backstop for a test that died
    before teardown.
    """
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


@pytest.fixture
def repository(limited_role_env):
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    return SessionlessSSOPersonalOrgRepository()


@pytest.fixture(autouse=True)
def restore_tenant_context():
    yield
    set_current_org_id(STANDALONE_ORG_ID)


@pytest.fixture
def subject():
    """A fresh subject per test, so no run can pass on another's leftovers."""
    return f"user_pt_{uuid.uuid4().hex[:12]}"


async def _as_owner(superuser_url: str, sql: str, **params):
    """Read back as the OWNER — RLS-exempt, so it sees residue and successes alike.

    Every assertion in this module goes through here on purpose. Reading back as
    the limited role would make "nothing was written" indistinguishable from
    "written into a tenant I cannot see", which is the exact confusion the
    module exists to rule out.
    """
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall()
    finally:
        await engine.dispose()


async def _provision(
    repository, subject, *, organization_id=None, provider_org_id=None
):
    """Drive the repository exactly as the login service does."""
    key = personal_tenant_key(PROVIDER, subject)
    organization_id = organization_id or str(uuid.uuid4())
    # The service binds the tenant BEFORE the transaction opens; the engine's
    # `begin` listener samples the contextvar once per transaction, so binding
    # after would leave the INSERT scoped to the previous tenant and the policy
    # would refuse it.
    set_current_org_id(organization_id)
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=provider_org_id or f"org_{uuid.uuid4().hex[:12]}",
        organization_id=organization_id,
        name=PERSONAL_ORG_NAME,
        slug=personal_org_slug(key),
    )


# =============================================================================
# The posture is what we think it is
# =============================================================================


async def test_the_role_under_test_is_actually_subject_to_rls(limited_role_env):
    """If RLS were bypassed, every assertion below would be vacuous."""
    limited = _limited_url(limited_role_env)
    engine = create_async_engine(limited, future=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert row.rolsuper is False
            assert row.rolbypassrls is False
            owns = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                        "AND tableowner = current_user"
                    )
                )
            ).scalar()
            assert owns == 0, "the role owns tables, so RLS would not apply"
            # And the policy really is enabled on the table the write targets.
            enabled = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = 'organizations'"
                    )
                )
            ).scalar()
            assert enabled is True
    finally:
        await engine.dispose()


async def test_an_unbound_write_is_refused_by_the_policy(repository, subject):
    """The binding is load-bearing, not decorative.

    Binding a DIFFERENT organization than the row carries must be refused by the
    ``WITH CHECK`` arm. Without this, "we bind before the transaction" would be
    an unfalsifiable claim.
    """
    set_current_org_id(str(uuid.uuid4()))  # some other tenant
    key = personal_tenant_key(PROVIDER, subject)
    with pytest.raises(Exception) as excinfo:
        await repository.provision(
            provider=PROVIDER,
            provider_user_id=subject,
            provider_org_id=f"org_{uuid.uuid4().hex[:12]}",
            organization_id=str(uuid.uuid4()),  # NOT the bound one
            name=PERSONAL_ORG_NAME,
            slug=personal_org_slug(key),
        )
    assert "row-level security" in str(excinfo.value).lower()


# =============================================================================
# Invariant 3 — exactly one tenant, all its rows, idempotent and race-safe
# =============================================================================


async def test_first_provisioning_writes_the_whole_tenant(
    repository, subject, limited_role_env
):
    """Enterprise, organization, default team, mapping and subject row."""
    tenant = await _provision(repository, subject)
    assert tenant.created is True
    org_id = tenant.organization_id
    assert org_id != STANDALONE_ORG_ID

    rows = await _as_owner(
        limited_role_env,
        """
        SELECT o.organization_id, o.name, o.slug, o.is_active, o.enterprise_id,
               e.slug AS enterprise_slug,
               (SELECT count(*) FROM teams t
                  WHERE t.organization_id = o.organization_id) AS teams,
               (SELECT count(*) FROM sso_org_mappings m
                  WHERE m.organization_id = o.organization_id) AS mappings,
               (SELECT count(*) FROM sso_personal_orgs p
                  WHERE p.organization_id = o.organization_id) AS personal
          FROM organizations o
          JOIN enterprises e ON e.enterprise_id = o.enterprise_id
         WHERE o.organization_id = :org
        """,
        org=org_id,
    )
    assert len(rows) == 1
    row = rows[0]
    key = personal_tenant_key(PROVIDER, subject)
    assert row.name == PERSONAL_ORG_NAME
    assert row.slug == personal_org_slug(key)
    assert row.enterprise_slug == personal_org_slug(key)
    assert row.is_active is True
    assert row.teams == 1
    assert row.mappings == 1
    assert row.personal == 1

    # The default team is the one the operator path and the standalone
    # bootstrap create, by name.
    from faultmaven.config.constants import STANDALONE_TEAM_NAME

    team = await _as_owner(
        limited_role_env,
        "SELECT name FROM teams WHERE organization_id = :org",
        org=org_id,
    )
    assert team[0].name == STANDALONE_TEAM_NAME

    # No membership yet: the user row does not exist at tenant-resolution time.
    # `_ensure_org_affiliation` writes it, with the member role.
    members = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM organization_members WHERE organization_id = :org",
        org=org_id,
    )
    assert members[0].n == 0


async def test_provisioning_twice_yields_one_organization(
    repository, subject, limited_role_env
):
    """A replayed callback adopts rather than duplicates."""
    first = await _provision(repository, subject)
    second = await _provision(repository, subject)

    assert second.organization_id == first.organization_id
    assert second.created is False

    rows = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM sso_personal_orgs "
        "WHERE provider = :p AND provider_user_id = :s",
        p=PROVIDER,
        s=subject,
    )
    assert rows[0].n == 1
    # And the second attempt's generated id left nothing behind.
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    orgs = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM organizations WHERE slug = :slug",
        slug=slug,
    )
    assert orgs[0].n == 1


async def test_concurrent_first_logins_yield_one_tenant_and_no_orphans(
    repository, subject, limited_role_env
):
    """The race, run for real: two provisions in flight at once.

    Both derive the same slug and the same IdP organization, so one of three
    constraints refuses the loser and its whole transaction rolls back. The
    orphan count is the assertion that matters — a loser that committed its
    enterprise before failing on a later row would be invisible to an
    organization count alone.
    """
    shared_idp_org = f"org_{uuid.uuid4().hex[:12]}"

    async def attempt():
        # Each task gets its own contextvar copy (asyncio copies the context at
        # task creation), so the two bind different organization ids exactly as
        # two concurrent callbacks would.
        return await _provision(repository, subject, provider_org_id=shared_idp_org)

    results = await asyncio.gather(
        asyncio.create_task(attempt()),
        asyncio.create_task(attempt()),
        return_exceptions=True,
    )

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    assert len(succeeded) == 2, f"a login was refused outright: {results}"
    assert (
        len({r.organization_id for r in succeeded}) == 1
    ), "the two logins landed in different tenants"
    assert sum(1 for r in succeeded if r.created) == 1, "both claimed to create"

    winner = succeeded[0].organization_id
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations WHERE slug = :slug) AS orgs,
               (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM teams
                  WHERE organization_id = :org) AS teams,
               (SELECT count(*) FROM sso_personal_orgs
                  WHERE provider = :p AND provider_user_id = :s) AS personal,
               (SELECT count(*) FROM sso_org_mappings
                  WHERE provider = :p AND provider_org_id = :idp) AS mappings
        """,
        slug=slug,
        org=winner,
        p=PROVIDER,
        s=subject,
        idp=shared_idp_org,
    )
    row = counts[0]
    assert row.orgs == 1
    assert row.enterprises == 1, "the loser left an orphaned enterprise behind"
    assert row.teams == 1
    assert row.personal == 1
    assert row.mappings == 1


async def test_a_write_beaten_to_the_constraint_adopts_the_winner(
    limited_role_env, subject
):
    """The conflict branch, deterministically — not left to the scheduler.

    ``asyncio.gather`` above proves the *outcome* under real concurrency, but
    whether its loser was refused by the pre-check or by a constraint is up to
    the event loop, so on its own it cannot show the recovery path runs at all.
    Here the winner's tenant is committed *after* the loser's pre-check and
    *before* its INSERT, which is exactly the interleaving that pre-check cannot
    cover. Everything else — the pre-check, the transaction, the rollback, the
    re-read — is the real code.
    """
    from faultmaven.config.tenant_context import get_current_org_id
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    winner_org = str(uuid.uuid4())
    loser_org = str(uuid.uuid4())
    idp_org = f"org_{uuid.uuid4().hex[:12]}"
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))

    class BeatenRepository(SessionlessSSOPersonalOrgRepository):
        def __init__(self):
            self.conflicts = 0

        async def _write(self, **kwargs):
            if not getattr(self, "_seeded", False):
                self._seeded = True
                saved = get_current_org_id()
                set_current_org_id(winner_org)
                try:
                    await super()._write(**{**kwargs, "organization_id": winner_org})
                finally:
                    set_current_org_id(saved)
            try:
                return await super()._write(**kwargs)
            except Exception:
                self.conflicts += 1
                raise

    repository = BeatenRepository()
    set_current_org_id(loser_org)
    tenant = await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=idp_org,
        organization_id=loser_org,
        name=PERSONAL_ORG_NAME,
        slug=slug,
    )

    # The constraint really fired — this is what the gather test cannot promise.
    assert repository.conflicts == 1
    assert tenant.organization_id == winner_org
    assert tenant.created is False

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations WHERE slug = :slug) AS orgs,
               (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM organizations
                  WHERE organization_id = :loser) AS loser_orgs,
               (SELECT count(*) FROM sso_personal_orgs
                  WHERE provider = :p AND provider_user_id = :s) AS personal
        """,
        slug=slug,
        loser=loser_org,
        p=PROVIDER,
        s=subject,
    )
    row = counts[0]
    assert row.orgs == 1
    assert row.enterprises == 1, "the loser's enterprise survived its rollback"
    assert row.loser_orgs == 0
    assert row.personal == 1


async def test_a_failed_transaction_leaves_nothing_to_adopt(
    repository, subject, limited_role_env
):
    """Invariant 8, at the database: a refused write leaves no partial tenant.

    A second subject is pointed at an IdP organization the first already claimed
    — ``sso_org_mappings``'s primary key refuses it. Because the whole tenant is
    one transaction, the enterprise, organization and team that were added
    before it must be gone too.
    """
    first_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=first_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    intruder_org = str(uuid.uuid4())
    with pytest.raises(Exception):
        await _provision(
            repository,
            intruder,
            organization_id=intruder_org,
            provider_org_id=first_idp_org,  # already claimed
        )

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations
                  WHERE organization_id = :org) AS orgs,
               (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM sso_personal_orgs
                  WHERE provider = :p AND provider_user_id = :s) AS personal
        """,
        org=intruder_org,
        slug=personal_org_slug(personal_tenant_key(PROVIDER, intruder)),
        p=PROVIDER,
        s=intruder,
    )
    row = counts[0]
    assert row.orgs == 0
    assert row.enterprises == 0
    assert row.personal == 0


async def test_the_lookup_answers_without_any_tenant_bound(repository, subject):
    """The subject-keyed read is untenanted — that is its whole reason to exist.

    Bound to the Standalone sentinel (what an unbound execution context holds),
    the row for a tenant that is emphatically not the sentinel must still be
    readable. A table enrolled in RLS would answer ``None`` here, and the login
    would provision a second tenant on every visit.
    """
    tenant = await _provision(repository, subject)

    set_current_org_id(STANDALONE_ORG_ID)
    found = await repository.get_organization_id(PROVIDER, subject)
    assert found == tenant.organization_id

    # And from inside an unrelated tenant's scope, too.
    set_current_org_id(str(uuid.uuid4()))
    assert await repository.get_organization_id(PROVIDER, subject) == (
        tenant.organization_id
    )


async def test_a_distinct_subject_gets_a_distinct_tenant(repository, limited_role_env):
    """Two individuals are two tenants — the slug cannot collide across users."""
    a = f"user_pt_{uuid.uuid4().hex[:12]}"
    b = f"user_pt_{uuid.uuid4().hex[:12]}"
    tenant_a = await _provision(repository, a)
    tenant_b = await _provision(repository, b)

    assert tenant_a.organization_id != tenant_b.organization_id
    rows = await _as_owner(
        limited_role_env,
        "SELECT organization_id, slug, enterprise_id FROM organizations "
        "WHERE organization_id IN (:a, :b)",
        a=tenant_a.organization_id,
        b=tenant_b.organization_id,
    )
    assert len(rows) == 2
    assert len({r.slug for r in rows}) == 2
    # Separate enterprises, so neither inherits the other's parent.
    assert len({r.enterprise_id for r in rows}) == 2


# =============================================================================
# Invariant 5 — the sentinel never becomes a tenant, at the database
# =============================================================================


async def test_the_sentinel_is_never_written_as_a_personal_tenant(
    repository, limited_role_env
):
    """No subject row may point at the Standalone organization.

    The service refuses it on read (unit-tested); this asserts the deployed
    database holds no such row after the whole suite has run, so a future path
    that writes one is caught here rather than in production.
    """
    rows = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM sso_personal_orgs WHERE organization_id = :org",
        org=STANDALONE_ORG_ID,
    )
    assert rows[0].n == 0

    # And a deliberate attempt to write one is refused by RLS: the sentinel is
    # not the bound tenant, and binding it would be the bug this guards.
    subject = f"user_pt_{uuid.uuid4().hex[:12]}"
    set_current_org_id(str(uuid.uuid4()))
    with pytest.raises(Exception):
        await repository.provision(
            provider=PROVIDER,
            provider_user_id=subject,
            provider_org_id=f"org_{uuid.uuid4().hex[:12]}",
            organization_id=STANDALONE_ORG_ID,
            name=PERSONAL_ORG_NAME,
            slug=personal_org_slug(personal_tenant_key(PROVIDER, subject)),
        )
