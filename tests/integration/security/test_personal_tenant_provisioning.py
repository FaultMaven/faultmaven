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
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_enterprise_id,
    set_current_enterprise_id,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)
from tests.integration.security.conftest import (
    create_limited_role,
    drop_limited_role,
    limited_url,
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

# The limited-role setup is shared with ``test_tenant_turn_cap`` via
# ``conftest.py``: both modules need a role RLS actually applies to, and two
# byte copies of the grant list would let one module's idea of the deployed
# posture drift from the other's without failing anything. The role NAME stays
# per-module — both create and drop roles inside one ``-m postgres`` session.
_LIMITED_ROLE = f"fm_pt_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_pt_probe_pw"


def _limited_url(superuser_url: str) -> str:
    return limited_url(superuser_url, _LIMITED_ROLE, _LIMITED_PW)


async def _create_limited_role(superuser_url: str) -> None:
    await create_limited_role(superuser_url, _LIMITED_ROLE, _LIMITED_PW)


async def _drop_limited_role(superuser_url: str) -> None:
    await drop_limited_role(superuser_url, _LIMITED_ROLE)


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
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        SessionlessSSOPersonalEnterpriseRepository,
    )

    return SessionlessSSOPersonalEnterpriseRepository()


@pytest.fixture(autouse=True)
def restore_tenant_context():
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


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


async def _as_owner_write(superuser_url: str, sql: str, **params):
    """Mutate as the OWNER, for the states only a privileged actor can create.

    Used to stage a hard-deleted organization — the shape that strands an
    enterprise. RLS is not the thing under test in that setup, and the limited
    role could not reach the row anyway.
    """
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _provision(repository, subject, *, provider_org_id=None, slug=None):
    """Drive the repository exactly as the login service does.

    Note what the caller does NOT do: it never generates or binds an
    organization id. The repository owns both, because binding is an
    implementation detail of writing rows under the RLS-scoped role — a caller
    that had to know about it could also forget, or leave a nonexistent
    organization bound after a failure.
    """
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=provider_org_id or f"org_{uuid.uuid4().hex[:12]}",
        name=PERSONAL_ORG_NAME,
        slug=slug or personal_org_slug(personal_tenant_key(PROVIDER, subject)),
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


async def test_the_repository_binds_the_tenant_it_is_writing(repository, subject):
    """The bind is inside :meth:`provision`, and it is load-bearing.

    ``organizations`` is RLS-tenanted and migration 018 creates its policy with
    no ``FOR`` clause, so ``USING`` doubles as ``WITH CHECK``: an INSERT whose
    ``organization_id`` is not the session's ``app.current_org_id`` is
    *rejected*. Calling with an unrelated tenant bound therefore proves two
    things at once — that the repository binds its own scope (or this write
    could not succeed), and that the policy really would refuse an unbound one.
    """
    unrelated = str(uuid.uuid4())
    set_current_enterprise_id(unrelated)

    organization_id = await _provision(repository, subject)

    assert organization_id != unrelated
    # And the caller's scope is handed back untouched: a failed or successful
    # provision must never leave someone else's — or a nonexistent — org bound.
    assert get_current_enterprise_id() == unrelated


async def test_a_failed_provision_restores_the_callers_scope(
    repository, subject, limited_role_env
):
    """The failure direction of the same contract."""
    first_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=first_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    unrelated = str(uuid.uuid4())
    set_current_enterprise_id(unrelated)
    with pytest.raises(Exception):
        await _provision(repository, intruder, provider_org_id=first_idp_org)

    assert get_current_enterprise_id() == unrelated


# =============================================================================
# Invariant 3 — exactly one tenant, all its rows, idempotent and race-safe
# =============================================================================


async def test_first_provisioning_writes_the_whole_tenant(
    repository, subject, limited_role_env
):
    """Enterprise, organization, default team, mapping and subject row."""
    org_id = await _provision(repository, subject)
    assert org_id != STANDALONE_ENTERPRISE_ID

    rows = await _as_owner(
        limited_role_env,
        """
        SELECT o.organization_id, o.name, o.slug, o.is_active, o.enterprise_id,
               e.slug AS enterprise_slug,
               (SELECT count(*) FROM teams t
                  WHERE t.organization_id = o.organization_id) AS teams,
               (SELECT count(*) FROM sso_org_mappings m
                  WHERE m.organization_id = o.organization_id) AS mappings,
               (SELECT count(*) FROM sso_personal_enterprises p
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

    from faultmaven.config.constants import STANDALONE_TEAM_NAME

    team = await _as_owner(
        limited_role_env,
        "SELECT name FROM teams WHERE organization_id = :org",
        org=org_id,
    )
    assert team[0].name == STANDALONE_TEAM_NAME

    # The subject row denormalises the enterprise (read on the MAPPED branch,
    # where this organization is invisible under RLS) and starts with the IdP
    # membership unconfirmed — it is established only after this commit.
    record = await repository.get(PROVIDER, subject)
    assert record.organization_id == org_id
    assert record.enterprise_id == row.enterprise_id
    assert record.membership_confirmed is False

    # No membership yet: the user row does not exist at tenant-resolution time.
    members = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM organization_members WHERE organization_id = :org",
        org=org_id,
    )
    assert members[0].n == 0


async def test_confirm_membership_marks_only_that_subject(
    repository, subject, limited_role_env
):
    """The recovery marker is per-subject and durable."""
    other = f"user_pt_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject)
    await _provision(repository, other)

    await repository.confirm_membership(PROVIDER, subject)

    assert (await repository.get(PROVIDER, subject)).membership_confirmed is True
    assert (await repository.get(PROVIDER, other)).membership_confirmed is False


async def test_find_by_enterprise_answers_from_inside_another_tenant(
    repository, subject
):
    """The switching check runs bound to the COMPANY tenant.

    ``organizations`` is invisible there under RLS, which is exactly why the
    enterprise is denormalised onto the untenanted subject row. Bound to an
    unrelated organization, the probe must still answer.
    """
    org_id = await _provision(repository, subject)
    record = await repository.get(PROVIDER, subject)

    set_current_enterprise_id(str(uuid.uuid4()))  # a company tenant, not this one
    assert await repository.find_by_enterprise(PROVIDER, subject, record.enterprise_id)
    assert not await repository.find_by_enterprise(PROVIDER, subject, str(uuid.uuid4()))
    assert org_id == record.organization_id


async def test_retire_drops_the_binding_and_leaves_the_tenant(
    repository, subject, limited_role_env
):
    """Retiring is not a migration: the organization and its rows stay put."""
    org_id = await _provision(repository, subject)

    assert await repository.retire(PROVIDER, subject) is True
    assert await repository.get(PROVIDER, subject) is None
    assert await repository.retire(PROVIDER, subject) is False  # idempotent

    surviving = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations
                  WHERE organization_id = :org) AS orgs,
               (SELECT count(*) FROM teams WHERE organization_id = :org) AS teams,
               (SELECT count(*) FROM sso_org_mappings
                  WHERE organization_id = :org) AS mappings
        """,
        org=org_id,
    )
    assert surviving[0].orgs == 1
    assert surviving[0].teams == 1
    assert surviving[0].mappings == 1


async def test_count_created_since_bounds_the_window(repository, limited_role_env):
    """What the provisioning ceiling reads."""
    from datetime import timedelta

    before = datetime.now(UTC) - timedelta(hours=1)
    baseline = await repository.count_created_since(PROVIDER, before)

    await _provision(repository, f"user_pt_{uuid.uuid4().hex[:12]}")
    await _provision(repository, f"user_pt_{uuid.uuid4().hex[:12]}")

    assert await repository.count_created_since(PROVIDER, before) == baseline + 2
    # A window that starts in the future counts nothing — the bound is real.
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await repository.count_created_since(PROVIDER, future) == 0


async def test_provisioning_twice_yields_one_organization(
    repository, subject, limited_role_env
):
    """A replayed callback adopts rather than duplicates."""
    first = await _provision(repository, subject)
    second = await _provision(repository, subject)

    assert second == first

    rows = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM sso_personal_enterprises "
        "WHERE provider = :p AND provider_user_id = :s",
        p=PROVIDER,
        s=subject,
    )
    assert rows[0].n == 1
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

    Both derive the same slug and the same IdP organization, so one of the
    constraints refuses the loser and its whole transaction rolls back. The
    orphan count is the assertion that matters — a loser that committed its
    enterprise before failing on a later row would be invisible to an
    organization count alone.
    """
    shared_idp_org = f"org_{uuid.uuid4().hex[:12]}"

    async def attempt():
        return await _provision(repository, subject, provider_org_id=shared_idp_org)

    results = await asyncio.gather(
        asyncio.create_task(attempt()),
        asyncio.create_task(attempt()),
        return_exceptions=True,
    )

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    assert len(succeeded) == 2, f"a login was refused outright: {results}"
    assert len(set(succeeded)) == 1, "the two logins landed in different tenants"

    winner = succeeded[0]
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations WHERE slug = :slug) AS orgs,
               (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM teams
                  WHERE organization_id = :org) AS teams,
               (SELECT count(*) FROM sso_personal_enterprises
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
    which mechanism refused its loser is up to the event loop. Here the winner's
    tenant is committed *between* the loser's derivation and its INSERT, which
    is the interleaving no pre-check can cover. Everything else — the
    transaction, the rollback, the re-read — is the real code.
    """
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        SessionlessSSOPersonalEnterpriseRepository,
    )

    idp_org = f"org_{uuid.uuid4().hex[:12]}"
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    winner_holder: dict[str, str] = {}

    class BeatenRepository(SessionlessSSOPersonalEnterpriseRepository):
        def __init__(self):
            self.conflicts = 0

        async def _write(self, **kwargs):
            if not getattr(self, "_seeded", False):
                self._seeded = True
                winner_holder[
                    "id"
                ] = await SessionlessSSOPersonalEnterpriseRepository().provision(
                    provider=PROVIDER,
                    provider_user_id=subject,
                    provider_org_id=idp_org,
                    name=PERSONAL_ORG_NAME,
                    slug=slug,
                )
            try:
                return await super()._write(**kwargs)
            except Exception:
                self.conflicts += 1
                raise

    repository = BeatenRepository()
    organization_id = await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=idp_org,
        name=PERSONAL_ORG_NAME,
        slug=slug,
    )

    # The constraint really fired — this is what the gather test cannot promise.
    assert repository.conflicts == 1
    assert organization_id == winner_holder["id"]

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM organizations WHERE slug = :slug) AS orgs,
               (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM sso_personal_enterprises
                  WHERE provider = :p AND provider_user_id = :s) AS personal
        """,
        slug=slug,
        p=PROVIDER,
        s=subject,
    )
    row = counts[0]
    assert row.orgs == 1
    assert row.enterprises == 1, "the loser's enterprise survived its rollback"
    assert row.personal == 1


async def test_a_failed_transaction_leaves_nothing_to_adopt(
    repository, subject, limited_role_env
):
    """Invariant 8, at the database: a refused write leaves no partial tenant.

    A second subject is pointed at an IdP organization the first already claimed
    — ``sso_org_mappings``'s primary key refuses it. Because the whole tenant is
    one transaction, the enterprise, organization and team added before it must
    be gone too.
    """
    first_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=first_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    with pytest.raises(Exception):
        await _provision(repository, intruder, provider_org_id=first_idp_org)

    counts = await _as_owner(
        limited_role_env,
        """
        SELECT (SELECT count(*) FROM enterprises WHERE slug = :slug) AS enterprises,
               (SELECT count(*) FROM organizations WHERE slug = :slug) AS orgs,
               (SELECT count(*) FROM sso_personal_enterprises
                  WHERE provider = :p AND provider_user_id = :s) AS personal
        """,
        slug=personal_org_slug(personal_tenant_key(PROVIDER, intruder)),
        p=PROVIDER,
        s=intruder,
    )
    row = counts[0]
    assert row.orgs == 0
    assert row.enterprises == 0
    assert row.personal == 0


# =============================================================================
# Review item 4 — a collision that is not a race says which key collided
# =============================================================================


async def test_a_mapping_collision_names_the_idp_org_not_the_invented_id(
    repository, subject, limited_role_env
):
    """Conflating a collision with a lost race produced a useless log.

    The organization id in the old message was one this attempt invented and
    never committed — an operator could not look it up anywhere. What they need
    is the key that actually collided.
    """
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        PersonalTenantCollision,
    )

    taken_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=taken_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    with pytest.raises(PersonalTenantCollision) as excinfo:
        await _provision(repository, intruder, provider_org_id=taken_idp_org)

    assert excinfo.value.colliding_key == "sso_org_mappings.provider_org_id"
    assert excinfo.value.colliding_value == taken_idp_org


async def test_an_orphaned_enterprise_is_adopted_rather_than_a_permanent_lockout(
    repository, subject, limited_role_env
):
    """``organizations`` has no CASCADE to ``enterprises`` (#1045 review, 4a).

    Hard-deleting a personal organization cascades the mapping and subject rows
    but strands the enterprise. A writer that always INSERTed would then collide
    forever on ``enterprises.slug`` and every login would be refused as
    "belongs to somebody else". Adopting the orphan is safe precisely because
    the slug is derived from the subject: nobody else can produce it.
    """
    first_org = await _provision(repository, subject)
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    enterprise_before = (
        await _as_owner(
            limited_role_env,
            "SELECT enterprise_id FROM enterprises WHERE slug = :slug",
            slug=slug,
        )
    )[0].enterprise_id

    # Hard-delete the organization exactly as the database would: the FK
    # cascades take the mapping and subject rows, the enterprise stays.
    await _as_owner_write(
        limited_role_env,
        "DELETE FROM organizations WHERE organization_id = :org",
        org=first_org,
    )
    assert await repository.get(PROVIDER, subject) is None
    orphan = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM enterprises WHERE slug = :slug",
        slug=slug,
    )
    assert orphan[0].n == 1, "the premise: the enterprise really is stranded"

    # The next login must work, and must reuse the stranded enterprise.
    second_org = await _provision(repository, subject)
    assert second_org != first_org
    after = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id, "
        "(SELECT count(*) FROM enterprises WHERE slug = :slug) AS n "
        "FROM organizations WHERE organization_id = :org",
        slug=slug,
        org=second_org,
    )
    assert after[0].n == 1, "a second enterprise was created for the same slug"
    assert after[0].enterprise_id == enterprise_before


async def test_the_lookup_answers_without_any_tenant_bound(repository, subject):
    """The subject-keyed read is untenanted — that is its whole reason to exist.

    Bound to the Standalone sentinel (what an unbound execution context holds),
    the row for a tenant that is emphatically not the sentinel must still be
    readable. A table enrolled in RLS would answer ``None`` here, and the login
    would provision a second tenant on every visit.
    """
    org_id = await _provision(repository, subject)

    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    assert (await repository.get(PROVIDER, subject)).organization_id == org_id

    set_current_enterprise_id(str(uuid.uuid4()))
    assert (await repository.get(PROVIDER, subject)).organization_id == org_id


async def test_a_distinct_subject_gets_a_distinct_tenant(repository, limited_role_env):
    """Two individuals are two tenants — the slug cannot collide across users."""
    a = f"user_pt_{uuid.uuid4().hex[:12]}"
    b = f"user_pt_{uuid.uuid4().hex[:12]}"
    org_a = await _provision(repository, a)
    org_b = await _provision(repository, b)

    assert org_a != org_b
    rows = await _as_owner(
        limited_role_env,
        "SELECT organization_id, slug, enterprise_id FROM organizations "
        "WHERE organization_id IN (:a, :b)",
        a=org_a,
        b=org_b,
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
        "SELECT count(*) AS n FROM sso_personal_enterprises WHERE organization_id = :org",
        org=STANDALONE_ENTERPRISE_ID,
    )
    assert rows[0].n == 0
