"""Sign-up against a real PostgreSQL under RLS (#1045, ADR-017 D3/D5/D9).

The unit tests pin the *decision* — which arm runs, what it resolves to, what it
refuses. This module pins what only a real database can answer: that the rows a
sign-up is made of can actually be written **by the application role**, that
they are written atomically, that the domain arm's get-or-create is arbitrated
by a real partial unique index rather than by a Python ``if``, and that two
concurrent first logins for one subject produce exactly one enterprise and no
orphans.

Sign-up derives one fact and writes one tenant
----------------------------------------------
The enterprise isolates; the organization bills; the team shares. A sign-up
knows who is signing in and nothing about who pays or who has agreed to share,
so it creates **an enterprise and nothing else**. Which enterprise is decided by
the domain of the IdP-verified email:

* a **personal domain** yields a private enterprise per account (plus the IdP
  organization that holds the single member, its ``sso_org_mappings`` row and
  the subject binding);
* **every other domain** yields the enterprise for that domain, created by the
  first sign-up from it and joined by every later one — with no IdP
  organization, no mapping row and no subject row, because the domain is
  re-derived from the verified email on every login and needs nothing stored.

Why it must be PostgreSQL, and why as a limited role
----------------------------------------------------
Two of this module's claims are constraints rather than code. "The domain has
exactly one enterprise" is the partial unique index ``ix_enterprises_domain_live``
(unique among rows with ``deleted_at IS NULL``), and "one subject, one tenant"
is ``sso_personal_enterprises``' primary key — the row that arbitrates the race
between two concurrent first logins. SQLite would not exercise either the way
the deployment does, and neither would a mock.

The role matters for the opposite reason. The sign-up path writes only tables
RLS does **not** cover, which is exactly why it binds no tenant; run as a
superuser or a table owner that claim is untestable, because PostgreSQL exempts
both from RLS and every write would succeed whether or not a policy applied. So
the module creates a role with exactly the grants
``02-create-rls-app-role.sql`` gives ``faultmaven_app`` (DML on all tables and
sequences, no ownership) and drives the real code through it — and proves the
policy machinery genuinely bites that role before relying on it.

What is asserted, and how a false green is prevented
----------------------------------------------------
Every "it worked" is checked by reading the rows back **as the owner**, so a
pass cannot come from RLS hiding a failure, and every "nothing was written" is
checked the same way, so a pass cannot come from RLS hiding residue. Each
absence assertion has a sibling that shows the same write happening when it
should — an absence with no positive control passes for a system that writes
nothing at all.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_enterprise_id,
    set_current_enterprise_id,
)
from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
from faultmaven.modules.auth.domain.personal_tenant import (
    DOMAIN_SLUG_PREFIX,
    PERSONAL_ENTERPRISE_NAME,
    PERSONAL_SLUG_PREFIX,
    domain_enterprise_slug,
    personal_enterprise_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
    PersonalTenantCollision,
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

#: A consumer-mail domain from the shipped ``PERSONAL_EMAIL_DOMAINS`` default.
#: Spelled once so the exactness cases below can be built from it — the
#: near-misses have to be near-misses of a domain the setting really holds, or
#: they prove nothing about the rule.
PERSONAL_DOMAIN = "gmail.com"

# The limited-role setup is shared with ``test_tenant_turn_cap`` via
# ``conftest.py``: both modules need a role RLS actually applies to, and two
# byte copies of the grant list would let one module's idea of the deployed
# posture drift from the other's without failing anything. The role NAME stays
# per-module — both create and drop roles inside one ``-m postgres`` session.
_LIMITED_ROLE = f"fm_pt_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_pt_probe_pw"


def _limited_url(superuser_url: str) -> str:
    return limited_url(superuser_url, _LIMITED_ROLE, _LIMITED_PW)


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

    asyncio.run(create_limited_role(superuser_url, _LIMITED_ROLE, _LIMITED_PW))

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
    asyncio.run(drop_limited_role(superuser_url, _LIMITED_ROLE))


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


@pytest.fixture
def domain():
    """A fresh company domain per test, for the same reason.

    ``.example`` is reserved and appears in no consumer-mail list, so a domain
    built from it is unambiguously the non-personal arm.
    """
    return f"acme{uuid.uuid4().hex[:10]}.example"


def _emitted_logs(capsys, caplog) -> str:
    """Everything this test's logging could have reached, as one string.

    structlog's sink is decided by whatever configured it first in the pytest
    session: a console renderer writing to stdout under one collection order,
    the stdlib-logging bridge — stderr plus pytest's own log capture — under
    another. Asserting on a single stream would make a real property of the code
    depend on which sibling modules happened to be imported, which is a flake
    rather than a finding. All three are read, and the assertion is about the
    reason slug either way.
    """
    captured = capsys.readouterr()
    return caplog.text + captured.out + captured.err


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

    Used to stage the states an operator or a partly-finished run leaves behind
    — a soft-deleted enterprise, a seeded account. RLS is not the thing under
    test in those setups, and the limited role could not reach some of the rows
    anyway.
    """
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _as_limited(superuser_url: str, bound: str, sql: str, **params):
    """Run one statement as the limited role with ``bound`` as the tenant.

    The GUC is set explicitly rather than through the engine's ``begin``
    listener because this helper exists to probe the *policy*, and a probe that
    depended on the application's own binding could not tell a policy that does
    not fire from a binding that did not happen.
    """
    engine = create_async_engine(_limited_url(superuser_url), future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_enterprise_id', :e, false)"),
                {"e": bound},
            )
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _seed_enterprise(superuser_url: str, *, name="Probe", slug=None) -> str:
    """A live enterprise to bind, for probes that need one that is not a tenant."""
    enterprise_id = str(uuid.uuid4())
    await _as_owner_write(
        superuser_url,
        "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e, :n, :s)",
        e=enterprise_id,
        n=name,
        s=slug or f"probe-{uuid.uuid4().hex[:16]}",
    )
    return enterprise_id


async def _seed_user(
    superuser_url: str, *, subject: str, enterprise_id: str, active: bool = True
) -> str:
    """An account anchored to ``enterprise_id``.

    ``users.enterprise_id`` is NOT NULL (ADR-017 D3): every account is anchored
    to exactly one enterprise, so a seeded account must name one.
    """
    user_id = str(uuid.uuid4())
    await _as_owner_write(
        superuser_url,
        "INSERT INTO users (user_id, enterprise_id, username, email, display_name, "
        " sso_provider, sso_provider_id, is_active) "
        "VALUES (:u, :e, :n, :m, :n, 'workos', :s, :a)",
        u=user_id,
        e=enterprise_id,
        n=subject[:60],
        m=f"{subject}@{PERSONAL_DOMAIN}",
        s=subject,
        a=active,
    )
    return user_id


async def _provision(repository, subject, *, provider_org_id=None, slug=None):
    """Drive the repository exactly as the login service does.

    Note what the caller does NOT do: it binds no tenant. Everything the
    sign-up path writes is outside RLS — the enterprise IS the tenant, and the
    two SSO tables are read on the unauthenticated callback before one exists —
    so there is nothing for a bind to authorise.
    """
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=provider_org_id or f"org_{uuid.uuid4().hex[:12]}",
        name=PERSONAL_ENTERPRISE_NAME,
        slug=slug or personal_enterprise_slug(personal_tenant_key(PROVIDER, subject)),
    )


async def _counts_for(superuser_url: str, enterprise_id: str) -> dict:
    """Every row a sign-up could have written, counted as the owner.

    One query, so "the personal arm wrote three rows" and "it wrote no
    organization and no team" are answered from the same read of the same
    tenant rather than from two that could disagree.
    """
    row = (
        await _as_owner(
            superuser_url,
            """
            SELECT (SELECT count(*) FROM enterprises
                      WHERE enterprise_id = :e) AS enterprises,
                   (SELECT count(*) FROM sso_org_mappings
                      WHERE enterprise_id = :e) AS mappings,
                   (SELECT count(*) FROM sso_personal_enterprises
                      WHERE enterprise_id = :e) AS bindings,
                   (SELECT count(*) FROM organizations
                      WHERE enterprise_id = :e) AS organizations,
                   (SELECT count(*) FROM teams WHERE enterprise_id = :e) AS teams
            """,
            e=enterprise_id,
        )
    )[0]
    return {
        "enterprises": row.enterprises,
        "mappings": row.mappings,
        "bindings": row.bindings,
        "organizations": row.organizations,
        "teams": row.teams,
    }


# =============================================================================
# The sign-up path, end to end, through the real login service
# =============================================================================


class _RecordingProvider(ISSOIdentityProvider):
    """An IdP whose personal-organization mint is idempotent in ``external_id``.

    That idempotency is not a convenience: the real provider is required to have
    it, and the whole retry story depends on it (a first sign-in that minted the
    organization and then failed to commit must find *that* organization on the
    next attempt rather than mint a second). A fake that handed out a fresh id
    per call would make the adoption paths below untestable, and would let code
    that mints duplicates pass.
    """

    def __init__(self) -> None:
        self.minted: dict[str, str] = {}
        self.calls: list[str] = []

    @property
    def provider_name(self) -> str:
        return PROVIDER

    def build_authorization_url(self, *, state: str) -> str:
        return f"https://authkit.test/authorize?state={state}"

    def exchange_code(self, code: str) -> SSOIdentity:  # pragma: no cover - unused
        raise AssertionError("this module drives resolution, not the code exchange")

    def provision_personal_organization(
        self, *, provider_user_id: str, external_id: str, name: str
    ) -> str:
        self.calls.append(external_id)
        return self.minted.setdefault(external_id, f"org_{uuid.uuid4().hex[:12]}")


@pytest.fixture
def idp():
    return _RecordingProvider()


@pytest.fixture
def signup_enabled(monkeypatch):
    """The real org-less sign-up switch, through the real settings singleton.

    The hourly ceiling is raised alongside it, and deliberately not silently:
    ``count_created_since`` is a deployment-wide count, so on a scratch database
    that accumulates rows every provisioning case here would eventually refuse
    ``personal_provisioning_ceiling`` — a property of the fixture, not of
    anything under test. The ceiling itself is pinned where it belongs, in the
    unit module.
    """
    from tests.utils import get_live_settings, reset_settings_singleton

    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_ENABLED", "true")
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "100000")
    reset_settings_singleton()
    assert get_live_settings().auth.sso_jit_personal_tenant_enabled is True
    yield
    monkeypatch.delenv("SSO_JIT_PERSONAL_TENANT_ENABLED", raising=False)
    monkeypatch.delenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", raising=False)
    reset_settings_singleton()


@pytest.fixture
def login_service(repository, idp, signup_enabled):
    """The sign-up resolver over the REAL enterprise, user and binding stores.

    Only the IdP and the transport-shaped collaborators are doubles: everything
    that decides which enterprise an address lands in reads and writes the real
    schema, because that decision is the subject of this module.
    """
    import fakeredis.aioredis as fakeredis

    from faultmaven.infrastructure.persistence.sessionless_enterprise_repository import (  # noqa: E501
        SessionlessEnterpriseRepository,
    )
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.modules.auth.domain.services.sso_login_service import (
        SSOLoginService,
    )
    from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
        SSOEphemeralStore,
    )

    return SSOLoginService(
        identity_provider=idp,
        ephemeral_store=SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True)),
        user_repository=SessionlessUserRepository(),
        token_generator=object(),
        session_service=object(),
        dashboard_url="https://app.faultmaven.test",
        access_token_expires_in=3600,
        enterprise_repository=SessionlessEnterpriseRepository(),
        personal_enterprise_repository=repository,
    )


def _identity(subject: str, email: str) -> SSOIdentity:
    """An org-less identity: the branch a self-service sign-up arrives on."""
    return SSOIdentity(
        provider=PROVIDER,
        provider_user_id=subject,
        email=email,
        email_verified=True,
        display_name="Sam Individual",
        organization_id=None,
    )


async def _sign_up(login_service, subject: str, email: str):
    """Resolve the enterprise for an org-less identity. ``(enterprise, error)``."""
    return await login_service._resolve_login_enterprise(_identity(subject, email))


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
    finally:
        await engine.dispose()


async def test_the_signup_path_writes_only_tables_rls_does_not_cover(limited_role_env):
    """Why sign-up binds no tenant — and the claim is structural, not a comment.

    ``enterprises`` is the tenant itself, and the two SSO tables are read on the
    unauthenticated callback before a tenant exists, so none of the three is
    RLS-enrolled and there is nothing a bind could authorise. The tables the old
    five-row sign-up also wrote **are** enrolled, which is what made the bind
    load-bearing then; asserting both halves is what stops "no bind needed"
    quietly becoming true for the wrong reason — an enrolment silently dropped
    from a tenant-scoped table.
    """
    rows = await _as_owner(
        limited_role_env,
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
        "AND relname = ANY(:names)",
        names=[
            "enterprises",
            "sso_org_mappings",
            "sso_personal_enterprises",
            "organizations",
            "teams",
            "organization_members",
        ],
    )
    enrolled = {row.relname: row.relrowsecurity for row in rows}
    assert enrolled["enterprises"] is False
    assert enrolled["sso_org_mappings"] is False
    assert enrolled["sso_personal_enterprises"] is False
    assert enrolled["organizations"] is True
    assert enrolled["teams"] is True
    assert enrolled["organization_members"] is True


async def test_the_policy_really_refuses_this_role_a_foreign_tenants_row(
    limited_role_env,
):
    """The positive control for the posture: the policy fires, and it fires here.

    The tenant-isolation policies carry no ``FOR`` clause, so ``USING`` doubles
    as ``WITH CHECK`` and an INSERT stamped with an enterprise other than the
    bound one is *rejected*, not merely hidden. Both directions are exercised in
    one test on purpose: a refusal with no accepted sibling would also be
    produced by a role that simply cannot write, and an acceptance with no
    refused sibling would be produced by a policy that does nothing.
    """
    mine = await _seed_enterprise(limited_role_env)
    theirs = await _seed_enterprise(limited_role_env)

    await _as_limited(
        limited_role_env,
        mine,
        "INSERT INTO organizations (organization_id, enterprise_id, name, slug) "
        "VALUES (:o, :e, 'Mine', :s)",
        o=str(uuid.uuid4()),
        e=mine,
        s=f"probe-{uuid.uuid4().hex[:12]}",
    )

    with pytest.raises(DBAPIError) as excinfo:
        await _as_limited(
            limited_role_env,
            mine,
            "INSERT INTO organizations (organization_id, enterprise_id, name, slug) "
            "VALUES (:o, :e, 'Theirs', :s)",
            o=str(uuid.uuid4()),
            e=theirs,
            s=f"probe-{uuid.uuid4().hex[:12]}",
        )
    # The POLICY refused it, not some other constraint the row happened to
    # violate — which is the whole claim this control is making.
    assert "row-level security" in str(excinfo.value)

    assert (await _counts_for(limited_role_env, mine))["organizations"] == 1
    assert (await _counts_for(limited_role_env, theirs))["organizations"] == 0


# =============================================================================
# The personal arm — one enterprise, three rows, and nothing else
# =============================================================================


async def test_first_provisioning_writes_the_enterprise_the_mapping_and_the_binding(
    repository, subject, limited_role_env
):
    """A sign-up creates an enterprise and nothing else (ADR-017 D3/D5/D4).

    Three rows, not five. The organization is a billing target created by
    payment and the team is formed by consent, and a sign-in knows neither — so
    their absence is the design, and it is asserted directly rather than
    inferred from the three that are present.
    """
    enterprise_id = await _provision(repository, subject)
    assert enterprise_id != STANDALONE_ENTERPRISE_ID

    key = personal_tenant_key(PROVIDER, subject)
    row = (
        await _as_owner(
            limited_role_env,
            "SELECT name, slug, domain, deleted_at FROM enterprises "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    )[0]
    assert row.name == PERSONAL_ENTERPRISE_NAME
    assert row.slug == personal_enterprise_slug(key)
    assert row.deleted_at is None
    # NULL, not the account's mail domain: a personal enterprise is claimed by
    # no domain, which is what makes "a personal account can never share" true
    # by construction — nobody else's sign-up can be routed into it.
    assert row.domain is None

    assert await _counts_for(limited_role_env, enterprise_id) == {
        "enterprises": 1,
        "mappings": 1,
        "bindings": 1,
        "organizations": 0,
        "teams": 0,
    }

    # The binding starts with the IdP membership unconfirmed and unretired: the
    # membership is established only after this transaction commits, and a
    # retirement is something an operator does later.
    binding = (
        await _as_owner(
            limited_role_env,
            "SELECT provider, provider_org_id, membership_confirmed, retired_at, "
            "retirement_state FROM sso_personal_enterprises WHERE subject = :s",
            s=subject,
        )
    )[0]
    assert binding.provider == PROVIDER
    assert binding.membership_confirmed is False
    assert binding.retired_at is None
    assert binding.retirement_state is None

    record = await repository.get(PROVIDER, subject)
    assert record.enterprise_id == enterprise_id
    assert record.provider_org_id == binding.provider_org_id
    assert record.membership_confirmed is False


async def test_the_repository_writes_without_binding_a_tenant(
    repository, subject, limited_role_env
):
    """Nothing sign-up writes is RLS-enrolled, so it neither binds nor restores.

    Called with an unrelated — and deliberately nonexistent — enterprise bound,
    the write still lands and the caller's scope comes back untouched. Both
    halves matter: a repository that bound its own scope and forgot to restore
    it would leave someone else's, or a nonexistent, tenant current for
    everything the callback does next.
    """
    unrelated = str(uuid.uuid4())
    set_current_enterprise_id(unrelated)

    enterprise_id = await _provision(repository, subject)

    assert enterprise_id != unrelated
    assert get_current_enterprise_id() == unrelated
    assert (await _counts_for(limited_role_env, enterprise_id))["enterprises"] == 1


async def test_a_failed_provision_leaves_the_callers_scope_alone(
    repository, subject, limited_role_env
):
    """The failure direction of the same contract."""
    first_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=first_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    unrelated = str(uuid.uuid4())
    set_current_enterprise_id(unrelated)
    with pytest.raises(PersonalTenantCollision):
        await _provision(repository, intruder, provider_org_id=first_idp_org)

    assert get_current_enterprise_id() == unrelated


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

    Every membership table is RLS-tenanted and invisible from another
    enterprise, which is exactly why this question is answered from the
    untenanted subject binding. Bound to an unrelated enterprise, the probe must
    still answer — and must still say no to an enterprise this subject does not
    own, or the login's personal→company switch would fire for a stranger.
    """
    enterprise_id = await _provision(repository, subject)

    set_current_enterprise_id(str(uuid.uuid4()))  # a company tenant, not this one
    assert await repository.find_by_enterprise(PROVIDER, subject, enterprise_id)
    assert not await repository.find_by_enterprise(PROVIDER, subject, str(uuid.uuid4()))


async def test_dropping_the_binding_leaves_the_enterprise_and_its_mapping(
    repository, subject, limited_role_env
):
    """``retire`` on the repository is the personal→company switch, not a retirement.

    It deletes the binding outright, because the account has moved onto a
    company enterprise and there is no next-login policy to record — a stamped
    row would tell the anchor check the opposite of the truth. The tenant it
    leaves behind is untouched: its cases stay where they are.
    """
    enterprise_id = await _provision(repository, subject)

    assert await repository.retire(PROVIDER, subject) is True
    assert await repository.get(PROVIDER, subject) is None
    assert await repository.retire(PROVIDER, subject) is False  # idempotent

    counts = await _counts_for(limited_role_env, enterprise_id)
    assert counts["enterprises"] == 1
    assert counts["mappings"] == 1
    assert counts["bindings"] == 0
    surviving = (
        await _as_owner(
            limited_role_env,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    )[0]
    assert surviving.deleted_at is None, "the switch is not a retirement"


async def test_count_created_since_bounds_the_window(repository, limited_role_env):
    """What the provisioning ceiling reads."""
    before = datetime.now(UTC) - timedelta(hours=1)
    baseline = await repository.count_created_since(PROVIDER, before)

    await _provision(repository, f"user_pt_{uuid.uuid4().hex[:12]}")
    await _provision(repository, f"user_pt_{uuid.uuid4().hex[:12]}")

    assert await repository.count_created_since(PROVIDER, before) == baseline + 2
    # A window that starts in the future counts nothing — the bound is real.
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await repository.count_created_since(PROVIDER, future) == 0


async def test_provisioning_twice_yields_one_tenant(
    repository, subject, limited_role_env
):
    """A replayed callback adopts rather than duplicates."""
    first = await _provision(repository, subject)
    second = await _provision(repository, subject)

    assert second == first

    slug = personal_enterprise_slug(personal_tenant_key(PROVIDER, subject))
    row = (
        await _as_owner(
            limited_role_env,
            "SELECT (SELECT count(*) FROM sso_personal_enterprises "
            "          WHERE subject = :s) AS bindings, "
            "       (SELECT count(*) FROM enterprises WHERE slug = :slug) AS ents",
            s=subject,
            slug=slug,
        )
    )[0]
    assert row.bindings == 1
    assert row.ents == 1


async def test_concurrent_first_logins_yield_one_tenant_and_no_orphans(
    repository, subject, limited_role_env
):
    """The race, run for real: two provisions in flight at once.

    Both derive the same slug and are handed the same IdP organization, so one
    of the constraints refuses the loser and its whole transaction rolls back.
    The orphan count is the assertion that matters — a loser that committed its
    enterprise before failing on a later row would be invisible to a binding
    count alone.
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

    slug = personal_enterprise_slug(personal_tenant_key(PROVIDER, subject))
    row = (
        await _as_owner(
            limited_role_env,
            """
            SELECT (SELECT count(*) FROM enterprises WHERE slug = :slug) AS ents,
                   (SELECT count(*) FROM sso_personal_enterprises
                      WHERE subject = :s) AS bindings,
                   (SELECT count(*) FROM sso_org_mappings
                      WHERE provider = :p AND provider_org_id = :idp) AS mappings
            """,
            slug=slug,
            s=subject,
            p=PROVIDER,
            idp=shared_idp_org,
        )
    )[0]
    assert row.ents == 1, "the loser left an orphaned enterprise behind"
    assert row.bindings == 1
    assert row.mappings == 1
    assert (await _counts_for(limited_role_env, succeeded[0]))["organizations"] == 0


async def test_a_write_beaten_to_a_key_adopts_the_winners_tenant(
    limited_role_env, subject
):
    """The conflict branch, deterministically — not left to the scheduler.

    ``asyncio.gather`` above proves the *outcome* under real concurrency, but
    which key refused its loser is up to the event loop. Here the winner's
    tenant is committed *between* the loser's derivation and its write, which is
    the interleaving no pre-check can cover; everything else — the transaction,
    the rollback, the re-read — is the real code.

    The winner is committed under a **different** IdP organization, so the key
    that refuses the loser is the one-IdP-org-per-enterprise claim. That is the
    conflict this staging can produce: two attempts that reach the writer one
    after the other, rather than at once, agree about the enterprise (the shared
    writer adopts a live row carrying the derived slug) and so never race for
    the slug at all. The slug is what arbitrates genuine concurrency, and the
    ``gather`` case above is where it is exercised.

    What matters either way is the recovery: a conflict a lost race explains is
    resolved by re-reading the subject's own untenanted row and adopting the
    enterprise it names, never by reporting a collision the operator cannot act
    on.
    """
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        SessionlessSSOPersonalEnterpriseRepository,
    )

    winner_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    loser_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    slug = personal_enterprise_slug(personal_tenant_key(PROVIDER, subject))
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
                    provider_org_id=winner_idp_org,
                    name=PERSONAL_ENTERPRISE_NAME,
                    slug=slug,
                )
            try:
                return await super()._write(**kwargs)
            except Exception:
                self.conflicts += 1
                raise

    repository = BeatenRepository()
    enterprise_id = await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=loser_idp_org,
        name=PERSONAL_ENTERPRISE_NAME,
        slug=slug,
    )

    # A key really refused it — this is what the gather test cannot promise.
    assert repository.conflicts == 1
    assert enterprise_id == winner_holder["id"]

    row = (
        await _as_owner(
            limited_role_env,
            "SELECT (SELECT count(*) FROM enterprises WHERE slug = :slug) AS ents, "
            "       (SELECT count(*) FROM sso_personal_enterprises "
            "          WHERE subject = :s) AS bindings, "
            "       (SELECT count(*) FROM sso_org_mappings "
            "          WHERE provider_org_id = :loser) AS loser_mappings",
            slug=slug,
            s=subject,
            loser=loser_idp_org,
        )
    )[0]
    assert row.ents == 1, "the loser's enterprise survived its rollback"
    assert row.bindings == 1
    assert row.loser_mappings == 0, "the refused write left a mapping row behind"


async def test_a_failed_transaction_leaves_nothing_to_adopt(
    repository, subject, limited_role_env
):
    """A refused write leaves no partial tenant.

    A second subject is pointed at an IdP organization the first already claimed
    — ``sso_org_mappings``' primary key refuses it. Because the whole sign-up is
    one transaction, the enterprise added before it must be gone too.
    """
    first_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=first_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    with pytest.raises(PersonalTenantCollision):
        await _provision(repository, intruder, provider_org_id=first_idp_org)

    row = (
        await _as_owner(
            limited_role_env,
            "SELECT (SELECT count(*) FROM enterprises WHERE slug = :slug) AS ents, "
            "       (SELECT count(*) FROM sso_personal_enterprises "
            "          WHERE subject = :s) AS bindings",
            slug=personal_enterprise_slug(personal_tenant_key(PROVIDER, intruder)),
            s=intruder,
        )
    )[0]
    assert row.ents == 0
    assert row.bindings == 0


async def test_a_mapping_collision_names_the_idp_org_not_the_invented_id(
    repository, subject, limited_role_env
):
    """Conflating a collision with a lost race produced a useless log.

    The enterprise id in the old message was one this attempt invented and never
    committed — an operator could not look it up anywhere. What they need is the
    key that actually collided.
    """
    taken_idp_org = f"org_{uuid.uuid4().hex[:12]}"
    await _provision(repository, subject, provider_org_id=taken_idp_org)

    intruder = f"user_pt_{uuid.uuid4().hex[:12]}"
    with pytest.raises(PersonalTenantCollision) as excinfo:
        await _provision(repository, intruder, provider_org_id=taken_idp_org)

    assert excinfo.value.colliding_key == "sso_org_mappings.provider_org_id"
    assert excinfo.value.colliding_value == taken_idp_org


async def test_an_enterprise_left_by_a_dropped_binding_is_adopted(
    repository, subject, limited_role_env
):
    """The slug arm of the shared writer adopts rather than colliding forever.

    The personal→company switch deletes the binding and leaves the enterprise
    and its mapping standing, so a subject who later comes back to a personal
    tenant derives a slug that a live row already holds. A writer that always
    INSERTed would collide on ``ix_enterprises_slug_live`` and refuse every
    later login as somebody else's tenant. Adopting is safe precisely because
    the slug is derived from the subject: nobody else can produce it, and the
    IdP hands back the same organization for the same derived external id.
    """
    first = await _provision(repository, subject)
    slug = personal_enterprise_slug(personal_tenant_key(PROVIDER, subject))
    idp_org = (
        await _as_owner(
            limited_role_env,
            "SELECT provider_org_id FROM sso_personal_enterprises WHERE subject = :s",
            s=subject,
        )
    )[0].provider_org_id

    assert await repository.retire(PROVIDER, subject) is True
    counts = await _counts_for(limited_role_env, first)
    assert (
        counts["enterprises"] == 1 and counts["mappings"] == 1
    ), "the premise: the enterprise and its mapping really are left standing"

    await _provision(repository, subject, provider_org_id=idp_org)

    live = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id FROM enterprises WHERE slug = :slug "
        "AND deleted_at IS NULL",
        slug=slug,
    )
    assert [row.enterprise_id for row in live] == [
        first
    ], "a second enterprise was minted for a slug a live row already holds"
    # The binding — the row every later login resolves the tenant from — names
    # the adopted enterprise, so the subject is back in the tenant that still
    # holds their cases rather than in a fresh empty one.
    assert (await repository.get(PROVIDER, subject)).enterprise_id == first
    assert await _counts_for(limited_role_env, first) == {
        "enterprises": 1,
        "mappings": 1,
        "bindings": 1,
        "organizations": 0,
        "teams": 0,
    }


async def test_the_lookup_answers_without_any_tenant_bound(repository, subject):
    """The subject-keyed read is untenanted — that is its whole reason to exist.

    Bound to the Standalone sentinel (what an unbound execution context holds),
    the row for a tenant that is emphatically not the sentinel must still be
    readable. A table enrolled in RLS would answer ``None`` here, and the login
    would provision a second tenant on every visit.
    """
    enterprise_id = await _provision(repository, subject)

    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    assert (await repository.get(PROVIDER, subject)).enterprise_id == enterprise_id

    set_current_enterprise_id(str(uuid.uuid4()))
    assert (await repository.get(PROVIDER, subject)).enterprise_id == enterprise_id


async def test_a_distinct_subject_gets_a_distinct_enterprise(
    repository, limited_role_env
):
    """Two individuals are two islands — the slug cannot collide across users."""
    a = f"user_pt_{uuid.uuid4().hex[:12]}"
    b = f"user_pt_{uuid.uuid4().hex[:12]}"
    ent_a = await _provision(repository, a)
    ent_b = await _provision(repository, b)

    assert ent_a != ent_b
    rows = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id, slug, domain FROM enterprises "
        "WHERE enterprise_id IN (:a, :b)",
        a=ent_a,
        b=ent_b,
    )
    assert len(rows) == 2
    assert len({row.slug for row in rows}) == 2
    assert {row.domain for row in rows} == {None}


async def test_the_sentinel_is_never_bound_by_a_personal_tenant(
    repository, subject, limited_role_env
):
    """No binding row may point at the Standalone enterprise (fm#850).

    Under multi-tenant that id identifies the deployment, not a tenant. The
    service refuses it on read (unit-tested); this asserts the deployed database
    holds no such row after the whole module has run. The second half is the
    positive control: the same query, aimed at a tenant this test just
    provisioned, finds it — so the zero above is the absence of a bad row and
    not of the query working.
    """
    enterprise_id = await _provision(repository, subject)

    rows = await _as_owner(
        limited_role_env,
        "SELECT (SELECT count(*) FROM sso_personal_enterprises "
        "          WHERE enterprise_id = :sentinel) AS sentinel_rows, "
        "       (SELECT count(*) FROM sso_personal_enterprises "
        "          WHERE enterprise_id = :mine) AS mine",
        sentinel=STANDALONE_ENTERPRISE_ID,
        mine=enterprise_id,
    )
    assert rows[0].sentinel_rows == 0
    assert rows[0].mine == 1


# =============================================================================
# Which enterprise a domain names (ADR-017 D3)
# =============================================================================


async def test_a_company_domain_gets_a_shared_enterprise_and_no_binding(
    login_service, idp, subject, domain, limited_role_env
):
    """The domain arm writes the enterprise and nothing else at all.

    No IdP organization: a company that brings its own is onboarded
    deliberately, and one minted on behalf of whoever signed in first would be
    that decision made by an accident of ordering. No mapping row and no subject
    binding either — the domain is re-derived from the verified email on every
    login, so there is nothing to fall out of step with, and the binding table
    is 1:1 with the enterprise, which is exactly wrong for a tenant many
    accounts share.
    """
    enterprise, error = await _sign_up(login_service, subject, f"alice@{domain}")

    assert error is None
    row = (
        await _as_owner(
            limited_role_env,
            "SELECT name, slug, domain, deleted_at FROM enterprises "
            "WHERE enterprise_id = :e",
            e=enterprise.enterprise_id,
        )
    )[0]
    assert row.domain == domain
    assert row.name == domain
    assert row.slug == domain_enterprise_slug(domain)
    assert row.slug.startswith(DOMAIN_SLUG_PREFIX)
    assert row.deleted_at is None

    assert await _counts_for(limited_role_env, enterprise.enterprise_id) == {
        "enterprises": 1,
        "mappings": 0,
        "bindings": 0,
        "organizations": 0,
        "teams": 0,
    }
    assert idp.calls == [], "the domain arm asked the IdP for an organization"


async def test_a_second_account_at_one_domain_joins_the_same_enterprise(
    login_service, domain, limited_role_env
):
    """Get-or-create, arbitrated by the live-rows-only unique index.

    The first sign-up creates the domain's enterprise and gains nothing by being
    first; the second joins it. That is the whole of what a domain enterprise
    does — it makes two colleagues *eligible* to share, and a colleague's cases
    stay invisible until they consent to a team.
    """
    first, first_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"alice@{domain}"
    )
    second, second_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"bob@{domain}"
    )

    assert first_error is None and second_error is None
    assert second.enterprise_id == first.enterprise_id
    rows = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id FROM enterprises WHERE domain = :d "
        "AND deleted_at IS NULL",
        d=domain,
    )
    assert [row.enterprise_id for row in rows] == [first.enterprise_id]


async def test_a_different_domain_gets_a_different_enterprise(
    login_service, limited_role_env
):
    """The positive control for the one above: joining is keyed on the domain.

    Without this, "the second account joined the first's enterprise" would also
    be satisfied by a resolver that returned one enterprise for everybody — the
    failure that would put two unrelated companies behind one isolation wall.
    """
    left = f"acme{uuid.uuid4().hex[:10]}.example"
    right = f"initech{uuid.uuid4().hex[:10]}.example"

    a, a_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"alice@{left}"
    )
    b, b_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"bob@{right}"
    )

    assert a_error is None and b_error is None
    assert a.enterprise_id != b.enterprise_id
    rows = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id, domain FROM enterprises "
        "WHERE enterprise_id IN (:a, :b)",
        a=a.enterprise_id,
        b=b.enterprise_id,
    )
    assert {row.domain for row in rows} == {left, right}


async def test_the_domain_match_is_case_folded(login_service, domain, limited_role_env):
    """Two spellings of one domain are one company, not two.

    An IdP-verified address is not normalised for us, and case is the spelling
    difference that actually occurs. Left uncased, ``ACME.example`` and
    ``acme.example`` would be two enterprises — two isolation walls through the
    middle of one company, and colleagues who can never be invited to the same
    team.
    """
    upper, upper_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"Alice@{domain.upper()}"
    )
    lower, lower_error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"bob@{domain}"
    )

    assert upper_error is None and lower_error is None
    assert upper.enterprise_id == lower.enterprise_id
    rows = await _as_owner(
        limited_role_env,
        "SELECT domain FROM enterprises WHERE enterprise_id = :e",
        e=upper.enterprise_id,
    )
    # Stored folded, so the column itself carries one spelling of the domain.
    assert rows[0].domain == domain


@pytest.mark.parametrize(
    "near_miss",
    [
        f"not{PERSONAL_DOMAIN}",
        f"{PERSONAL_DOMAIN}.evil.example",
    ],
)
async def test_a_personal_domain_is_matched_exactly_not_by_substring(
    login_service, limited_role_env, near_miss
):
    """Exactness is the one direction of D3 with a security consequence.

    A suffix or substring rule would fold ``notgmail.com`` — a domain anyone can
    register — into ``gmail.com``, and every account at it would land in the
    private per-account arm or, worse, share a wall with a domain it has nothing
    to do with. The address at the *real* consumer domain is resolved in the
    same test as the positive control: a rule that answered "personal" for
    everything would satisfy the first assertion alone.
    """
    company_subject = f"user_pt_{uuid.uuid4().hex[:12]}"
    company, company_error = await _sign_up(
        login_service, company_subject, f"alice@{near_miss}"
    )

    assert company_error is None
    company_row = (
        await _as_owner(
            limited_role_env,
            "SELECT slug, domain FROM enterprises WHERE enterprise_id = :e",
            e=company.enterprise_id,
        )
    )[0]
    assert company_row.slug.startswith(DOMAIN_SLUG_PREFIX)
    assert company_row.domain == near_miss
    assert (await _counts_for(limited_role_env, company.enterprise_id))["bindings"] == 0

    personal_subject = f"user_pt_{uuid.uuid4().hex[:12]}"
    personal, personal_error = await _sign_up(
        login_service, personal_subject, f"sam@{PERSONAL_DOMAIN}"
    )

    assert personal_error is None
    assert personal.enterprise_id != company.enterprise_id
    personal_row = (
        await _as_owner(
            limited_role_env,
            "SELECT slug, domain FROM enterprises WHERE enterprise_id = :e",
            e=personal.enterprise_id,
        )
    )[0]
    assert personal_row.slug == personal_enterprise_slug(
        personal_tenant_key(PROVIDER, personal_subject)
    )
    assert personal_row.slug.startswith(PERSONAL_SLUG_PREFIX)
    assert personal_row.domain is None
    assert (await _counts_for(limited_role_env, personal.enterprise_id))[
        "bindings"
    ] == 1


async def test_a_retired_domain_enterprise_does_not_capture_the_next_signup(
    login_service, domain, limited_role_env
):
    """The lookup is scoped exactly the way the index is: live rows only.

    ``ix_enterprises_domain_live`` is unique among rows with
    ``deleted_at IS NULL``, so a retired enterprise keeps its domain and the
    index will not stop a second one being created. A resolver that adopted the
    soft-deleted row would hand the next sign-up straight back into the tenant
    an operator took out of service — and it would then be refused by the
    bind-and-verify tail, so the account could never sign in at all. A resolver
    that ignored the index would create a duplicate for a live domain.
    """
    first, _ = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"alice@{domain}"
    )
    await _as_owner_write(
        limited_role_env,
        "UPDATE enterprises SET deleted_at = now() WHERE enterprise_id = :e",
        e=first.enterprise_id,
    )

    second, error = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"bob@{domain}"
    )

    assert error is None
    assert second.enterprise_id != first.enterprise_id
    rows = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id, deleted_at FROM enterprises WHERE domain = :d",
        d=domain,
    )
    assert len(rows) == 2
    live = [row.enterprise_id for row in rows if row.deleted_at is None]
    assert live == [second.enterprise_id]


async def test_the_sentinel_is_refused_even_when_a_domain_names_it(
    login_service, domain, limited_role_env, capsys, caplog
):
    """The Standalone id identifies the deployment, not a tenant (fm#850).

    The invariant used to be enforceable only against the personal arm, because
    nothing else could resolve to an *existing* enterprise. The domain arm can:
    give the sentinel a domain and the get-or-create finds it. The bind-and-verify
    tail every arm ends in is what refuses it, and it refuses *before* the bind,
    so the sentinel is never the request's scope even momentarily.

    The reason slug is asserted, not just the refusal. Every failure on this arm
    answers the same sanitized ``sso_failed``, so a resolver that never adopted
    the sentinel at all — and instead tripped the domain's unique index on the
    way to inserting a duplicate — would look identical from the outside while
    proving nothing about the guard.
    """
    caller_scope = str(uuid.uuid4())
    set_current_enterprise_id(caller_scope)
    await _as_owner_write(
        limited_role_env,
        "UPDATE enterprises SET domain = :d WHERE enterprise_id = :e",
        d=domain,
        e=STANDALONE_ENTERPRISE_ID,
    )
    try:
        enterprise, error = await _sign_up(
            login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"alice@{domain}"
        )
    finally:
        await _as_owner_write(
            limited_role_env,
            "UPDATE enterprises SET domain = NULL WHERE enterprise_id = :e",
            e=STANDALONE_ENTERPRISE_ID,
        )

    assert enterprise is None
    assert error == "sso_failed"
    assert "domain_is_sentinel" in _emitted_logs(capsys, caplog)
    # The guard runs before the bind, so the sentinel was never this request's
    # scope even momentarily — the caller's own is still current.
    assert get_current_enterprise_id() == caller_scope
    # And no second enterprise was invented for the domain on the way out.
    rows = await _as_owner(
        limited_role_env,
        "SELECT enterprise_id FROM enterprises WHERE domain = :d",
        d=domain,
    )
    assert rows == []


# =============================================================================
# The refusals, and that they refuse before anything is written
# =============================================================================


async def test_an_email_another_account_owns_refuses_the_signup_and_writes_nothing(
    login_service, subject, domain, limited_role_env
):
    """A refused sign-up leaves no stray tenant behind.

    ADR-015 D4 is subject-match-or-create, never email-link, so an address a
    different account already owns fails the login. The point of evaluating that
    *before* the write is that provisioning first would leave this subject a
    permanent stray enterprise and then ``sso_failed`` forever. The second half
    of the test is the positive control: the same identity with an address
    nobody owns provisions, so the zero above is a refusal rather than a path
    that writes nothing in any case.
    """
    squatter_enterprise = await _seed_enterprise(limited_role_env)
    contested = f"alice@{domain}"
    await _as_owner_write(
        limited_role_env,
        "INSERT INTO users (user_id, enterprise_id, username, email, display_name) "
        "VALUES (:u, :e, :n, :m, :n)",
        u=str(uuid.uuid4()),
        e=squatter_enterprise,
        n=f"squatter_{uuid.uuid4().hex[:8]}",
        m=contested,
    )

    enterprise, error = await _sign_up(login_service, subject, contested)

    assert enterprise is None
    assert error == "sso_failed"
    residue = (
        await _as_owner(
            limited_role_env,
            "SELECT (SELECT count(*) FROM enterprises WHERE domain = :d) AS ents, "
            "       (SELECT count(*) FROM sso_personal_enterprises "
            "          WHERE subject = :s) AS bindings",
            d=domain,
            s=subject,
        )
    )[0]
    assert residue.ents == 0
    assert residue.bindings == 0

    unclaimed, ok = await _sign_up(login_service, subject, f"bob@{domain}")
    assert ok is None
    assert unclaimed is not None


async def test_a_deactivated_account_is_refused_before_the_domain_arm_writes(
    login_service, subject, domain, limited_role_env
):
    """The preflight runs on both arms, ahead of the domain enterprise's creation.

    A deactivated account would be refused later anyway, so what this pins is
    the *ordering*: creating the domain's enterprise first would mint a tenant
    for a company on behalf of a login that is about to fail. The live sibling
    at the same domain is the positive control — it shows the arm does create
    the enterprise when the account may sign in.
    """
    anchor = await _seed_enterprise(limited_role_env)
    await _seed_user(
        limited_role_env, subject=subject, enterprise_id=anchor, active=False
    )

    enterprise, error = await _sign_up(login_service, subject, f"alice@{domain}")

    assert enterprise is None
    assert error == "sso_user_inactive"
    rows = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM enterprises WHERE domain = :d",
        d=domain,
    )
    assert rows[0].n == 0, "a refused login created the domain's enterprise"

    live, ok = await _sign_up(
        login_service, f"user_pt_{uuid.uuid4().hex[:12]}", f"bob@{domain}"
    )
    assert ok is None
    assert live is not None


async def test_an_account_anchored_elsewhere_is_not_handed_a_personal_tenant(
    login_service, subject, limited_role_env, idp
):
    """An employee arriving org-less must not be demoted into a private tenant.

    ``users.enterprise_id`` is NOT NULL, so every account is anchored to
    something and "already anchored" is the ordinary state rather than an
    exception. Provisioning here would either strand a tenant the later
    enterprise guard refuses, or — worse — anchor an employee to a personal
    enterprise and lock them out of their company. The refusal is evaluated
    before the IdP is asked for an organization, which is why ``idp.calls`` is
    empty: an organization minted for a refused login is a stray at the provider
    that nothing points at.
    """
    company = await _seed_enterprise(limited_role_env, name="Acme")
    await _seed_user(limited_role_env, subject=subject, enterprise_id=company)

    enterprise, error = await _sign_up(login_service, subject, f"sam@{PERSONAL_DOMAIN}")

    assert enterprise is None
    assert error == "sso_org_unmapped"
    assert idp.calls == []
    residue = (
        await _as_owner(
            limited_role_env,
            "SELECT (SELECT count(*) FROM sso_personal_enterprises "
            "          WHERE subject = :s) AS bindings, "
            "       (SELECT count(*) FROM enterprises WHERE slug = :slug) AS ents",
            s=subject,
            slug=personal_enterprise_slug(personal_tenant_key(PROVIDER, subject)),
        )
    )[0]
    assert residue.bindings == 0
    assert residue.ents == 0


async def test_the_switch_off_refuses_the_orgless_branch_entirely(
    login_service, subject, monkeypatch, limited_role_env
):
    """``SSO_JIT_PERSONAL_TENANT_ENABLED`` is the gate, and it is read live.

    With the switch off the org-less branch behaves exactly as it did before
    self-service sign-up existed: refused, nothing written, on either arm. The
    positive control is the whole rest of this section, which runs with it on.
    """
    from tests.utils import reset_settings_singleton

    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_ENABLED", "false")
    reset_settings_singleton()

    enterprise, error = await _sign_up(login_service, subject, f"sam@{PERSONAL_DOMAIN}")

    assert enterprise is None
    assert error == "sso_org_unmapped"
    rows = await _as_owner(
        limited_role_env,
        "SELECT count(*) AS n FROM sso_personal_enterprises WHERE subject = :s",
        s=subject,
    )
    assert rows[0].n == 0


def test_the_module_is_not_silently_skipping():
    """CI greps this lane for "skipped"; the skipif above must not be firing."""
    assert os.environ.get("DATABASE_URL", "").startswith("postgresql")
