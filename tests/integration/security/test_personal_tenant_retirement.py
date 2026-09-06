"""Retiring a personal tenant against a real PostgreSQL (#1045 D8, ADR-017 D3).

The unit tests pin the decision — which steps run, in what order, and what the
login does with the operator's choice. What only a real database can answer is
here, and each case is here because a SQLite unit test would pass while
PostgreSQL failed, or because the fact under test *is* a constraint:

* **The derived slug is genuinely reusable.** A retired enterprise keeps its
  slug, and the subject's next tenant derives exactly the same one. That works
  only because ``ix_enterprises_slug_live`` is partial on ``deleted_at IS NULL``
  — an earlier design renamed the slug instead, and the rename is what produced
  an ambiguous ``LIKE`` lookup once a subject had two retired tenants. Here the
  index is real.
* **The typed retirement state round-trips**, through a real CHECK constraint,
  and the login reads it through the real repositories rather than a fake that
  answers whatever it is asked.
* **A refresh chain cannot outlive the tenant** — the guard reads a real
  soft-deleted enterprise row.
* **Nothing else moved.** A second personal tenant is present throughout and is
  compared row by row.

What a retirement is, now that every account is anchored
--------------------------------------------------------
``users.enterprise_id`` is NOT NULL (ADR-017 D3), so "anchored to nothing" is
not a state and a retirement cannot release an account by clearing it. The
release is a **positive record** instead: ``sso_personal_enterprises`` keeps its
row, stamped with ``retired_at`` and the operator's ``--next-login`` choice, and
that value is what the next org-less sign-in reads. The account stays anchored
to the enterprise the retirement fenced, and that is the whole of what "retired"
means for it. A binding kept and stamped is also the only thing that *can* carry
the choice: ``subject`` is the row's primary key, so a subject has exactly one.

The command runs with the **owner** DSN, as the deployment procedure does: it
reads and writes rows of a tenant it is taking out of service without binding
it, and a preflight refuses an RLS-scoped role before any write. Every
assertion reads the rows back through that same owner connection, so no pass
can come from a row being hidden rather than absent.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.cli import personal_tenant as cli
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
    ISSOIdentityProvider,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ENTERPRISE_NAME,
    domain_enterprise_slug,
    personal_enterprise_slug,
    personal_tenant_key,
)
from tests.conftest import RecordingIdP, RecordingRevoker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.usefixtures("restore_tenant_context"),
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

PROVIDER = "workos"

#: A consumer-mail domain from the shipped ``PERSONAL_EMAIL_DOMAINS`` default,
#: so the identities below take the personal arm of sign-up.
PERSONAL_DOMAIN = "gmail.com"


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    """One engine per test, because there is one event loop per test."""
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


@pytest.fixture
def owner_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture
def subject() -> str:
    """A fresh subject per test, so no run passes on another's leftovers."""
    return f"user_pt_retire_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def repository():
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_enterprise_repository import (  # noqa: E501
        SessionlessSSOPersonalEnterpriseRepository,
    )

    return SessionlessSSOPersonalEnterpriseRepository()


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


async def _as_owner(url: str, sql: str, **params):
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).fetchall()
    finally:
        await engine.dispose()


async def _as_owner_write(url: str, sql: str, **params):
    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _provision(repository, subject: str, *, provider_org_id=None) -> str:
    """Exactly what the login path does, so the rows are the real ones."""
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=provider_org_id or f"org_{uuid.uuid4().hex[:12]}",
        name=PERSONAL_ENTERPRISE_NAME,
        slug=personal_enterprise_slug(personal_tenant_key(PROVIDER, subject)),
    )


async def _idp_org_of(url: str, enterprise_id: str) -> str:
    rows = await _as_owner(
        url,
        "SELECT provider_org_id FROM sso_org_mappings WHERE enterprise_id = :e",
        e=enterprise_id,
    )
    return rows[0].provider_org_id


async def _seed_user(url: str, *, subject: str, enterprise_id: str) -> str:
    """An account anchored to ``enterprise_id`` — the only anchor there is.

    ``users.enterprise_id`` is NOT NULL, so a seeded account names an
    enterprise, and the retirement's revocation step finds it from the
    enterprise id alone.
    """
    user_id = str(uuid.uuid4())
    await _as_owner_write(
        url,
        "INSERT INTO users (user_id, enterprise_id, username, email, display_name, "
        " sso_provider, sso_provider_id, is_active) "
        "VALUES (:u, :e, :n, :m, :n, 'workos', :s, true)",
        u=user_id,
        e=enterprise_id,
        n=subject[:60],
        m=f"{subject}@{PERSONAL_DOMAIN}",
        s=subject,
    )
    return user_id


async def _seed_content(url: str, enterprise_id: str, tag: str) -> None:
    """A case and a knowledge item, so "nothing was deleted" has something to say."""
    await _as_owner_write(
        url,
        "INSERT INTO cases (case_id, enterprise_id, title) VALUES (:c, :e, :t)",
        c=f"case_{tag}",
        e=enterprise_id,
        t="disk full",
    )
    await _as_owner_write(
        url,
        "INSERT INTO knowledge_items (item_id, enterprise_id, title, content, "
        "item_type, scope) VALUES (:i, :e, 'runbook', 'body', 'runbook', 'personal')",
        i=f"kb_{tag}",
        e=enterprise_id,
    )


async def _tenant_rows(url: str, enterprise_id: str) -> dict:
    """Every row of one tenant an operation could touch, read as the owner."""
    return {
        "enterprise": await _as_owner(
            url,
            "SELECT enterprise_id, name, slug, domain, deleted_at FROM enterprises "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
        "mapping": await _as_owner(
            url,
            "SELECT provider, provider_org_id FROM sso_org_mappings "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
        "binding": await _as_owner(
            url,
            "SELECT subject, provider, provider_org_id, membership_confirmed, "
            "retired_at, retirement_state FROM sso_personal_enterprises "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
        "accounts": await _as_owner(
            url,
            "SELECT user_id, enterprise_id FROM users WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
        "cases": await _as_owner(
            url,
            "SELECT case_id, title FROM cases WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
        "knowledge": await _as_owner(
            url,
            "SELECT item_id, title FROM knowledge_items WHERE enterprise_id = :e",
            e=enterprise_id,
        ),
    }


async def _retire(owner_url, enterprise_id, *, policy="refuse", idp=None, revoker=None):
    """Run the operator command against the real schema, addressed by enterprise id.

    Addressed by id rather than by subject because that is the address a
    part-retired tenant has: the retirement stamps the binding as retired early,
    and the subject lookup reads live bindings only.
    """
    return await cli.retire(
        subject=None,
        enterprise_id=enterprise_id,
        next_login=policy,
        apply=True,
        idp=idp or RecordingIdP([await _idp_org_of(owner_url, enterprise_id)]),
        auth_service=revoker or RecordingRevoker(),
    )


# =============================================================================
# The typed state round-trips, and the constraints are what make it work
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT),
    ],
)
async def test_the_retirement_stamps_the_binding_and_keeps_it(
    owner_url, repository, subject, flag, policy
):
    """The whole retirement state is typed columns on two rows, and both survive.

    ``deleted_at`` on the enterprise is the fence; ``retired_at`` and
    ``retirement_state`` on the subject's binding are the operator's decision
    about the next sign-in. The binding is *kept* — it is the only place that
    decision can live, because ``subject`` is its primary key and a subject has
    exactly one. The account stays anchored to the fenced enterprise, which is
    what "anchored to exactly one enterprise" costs and what replaced the anchor
    clearing that a NOT NULL column can no longer express.
    """
    enterprise_id = await _provision(repository, subject)
    user_id = await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)

    assert await _retire(owner_url, enterprise_id, policy=flag) == 0

    enterprise = (
        await _as_owner(
            owner_url,
            "SELECT deleted_at, slug FROM enterprises WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    )[0]
    assert enterprise.deleted_at is not None
    # No rename: the retired enterprise keeps the slug derived from the subject.
    assert enterprise.slug == personal_enterprise_slug(
        personal_tenant_key(PROVIDER, subject)
    )

    binding = await _as_owner(
        owner_url,
        "SELECT retired_at, retirement_state, enterprise_id "
        "FROM sso_personal_enterprises WHERE subject = :s",
        s=subject,
    )
    assert len(binding) == 1, "the retirement deleted the row that carries its policy"
    assert binding[0].retired_at is not None
    assert binding[0].retirement_state == policy
    assert binding[0].enterprise_id == enterprise_id

    anchored = await _as_owner(
        owner_url,
        "SELECT enterprise_id FROM users WHERE user_id = :u",
        u=user_id,
    )
    assert anchored[0].enterprise_id == enterprise_id


async def test_every_step_of_a_retirement_lands(owner_url, repository, subject):
    """The five side-effects, each asserted by its own effect rather than its name.

    A retirement is reported as a list of steps and then executed from that same
    list, so what a dry run prints and what an ``--apply`` run does cannot drift.
    What that list must actually achieve is here: the enterprise is fenced, the
    anchored account's tokens are revoked (a live refresh chain outlives the
    callback, and the enterprise row alone would not stop it), the binding is
    stamped, the IdP organization is deleted **by the id its own mapping row
    records**, and the mapping row goes last so that id is still readable when
    the delete needs it.
    """
    enterprise_id = await _provision(repository, subject)
    idp_org = await _idp_org_of(owner_url, enterprise_id)
    user_id = await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    idp = RecordingIdP([idp_org])
    revoker = RecordingRevoker()

    assert await _retire(owner_url, enterprise_id, idp=idp, revoker=revoker) == 0

    assert revoker.revoked == [user_id]
    assert idp.calls == [idp_org], "the IdP was addressed by some other id"
    assert idp_org not in idp.present, "the IdP organization was not removed"
    rows = await _tenant_rows(owner_url, enterprise_id)
    assert rows["enterprise"][0].deleted_at is not None
    assert rows["mapping"] == []
    assert len(rows["binding"]) == 1
    assert rows["binding"][0].retired_at is not None


async def test_a_second_retirement_of_the_same_tenant_has_nothing_to_do(
    owner_url, repository, subject
):
    """Re-running a finished retirement is a genuine no-op, not a perpetual plan.

    The revocation has no observable end state — a watermark cannot be read back
    as "already bumped" — so it is included only when some other step is
    outstanding. Without that gating a finished retirement would report one
    pending step forever, and a scripted sweep could not tell "this run did the
    work" from "somebody already had". The first run's exit code is the positive
    control: 0 means work happened, 3 means it had already happened.
    """
    enterprise_id = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)

    assert await _retire(owner_url, enterprise_id, policy="refuse") == 0

    second_revoker = RecordingRevoker()
    code = await cli.retire(
        subject=None,
        enterprise_id=enterprise_id,
        next_login="refuse",
        apply=True,
        idp=RecordingIdP([]),
        auth_service=second_revoker,
    )

    assert code == cli.EXIT_NOTHING_TO_DO
    assert second_revoker.revoked == []


async def test_the_policy_column_refuses_a_value_the_code_has_no_branch_for(
    owner_url, repository, subject
):
    """A CHECK constraint, not a convention.

    The next sign-in branches on this value, so a hand-written UPDATE must not
    be able to invent a third policy the login cannot interpret — an
    unrecognised value would fall through to the refusing side of
    ``releases_provisioning``, which is the safe direction but not one anybody
    chose. The two values the code implements are accepted in the same test, so
    a constraint that rejected everything would not pass for correctness.
    """
    await _provision(repository, subject)

    with pytest.raises(IntegrityError) as exc:
        await _as_owner_write(
            owner_url,
            "UPDATE sso_personal_enterprises SET retirement_state = "
            "'wipe_everything' WHERE subject = :s",
            s=subject,
        )
    # The CHECK is named, not merely "something refused it". The invented value
    # is deliberately short enough to fit ``varchar(16)``: a longer one is
    # rejected for its LENGTH, and a test that accepted that refusal would pass
    # on a database with no CHECK at all.
    assert "sso_personal_enterprises_retirement_state_check" in str(exc.value)

    for policy in (RETIREMENT_POLICY_REFUSE, RETIREMENT_POLICY_FRESH_TENANT):
        await _as_owner_write(
            owner_url,
            "UPDATE sso_personal_enterprises SET retirement_state = :p "
            "WHERE subject = :s",
            p=policy,
            s=subject,
        )
    stored = await _as_owner(
        owner_url,
        "SELECT retirement_state FROM sso_personal_enterprises WHERE subject = :s",
        s=subject,
    )
    assert stored[0].retirement_state == RETIREMENT_POLICY_FRESH_TENANT


async def test_a_fresh_tenant_reuses_the_derived_slug_the_retired_one_keeps(
    owner_url, repository, subject
):
    """Only a real database can answer this: it is a uniqueness constraint.

    ``ix_enterprises_slug_live`` is unique deployment-wide but partial on
    ``deleted_at IS NULL``. Without the partial predicate the second insert
    fails and the previous design's slug rename is forced back — and with it the
    ``LIKE``-and-``.first()`` lookup that could not tell one retired tenant of a
    subject from another.
    """
    first = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=first)

    assert await _retire(owner_url, first, policy="fresh-tenant") == 0

    second = await _provision(repository, subject)

    assert second != first
    slug = personal_enterprise_slug(personal_tenant_key(PROVIDER, subject))
    live = await _as_owner(
        owner_url,
        "SELECT enterprise_id FROM enterprises WHERE slug = :s AND deleted_at IS NULL",
        s=slug,
    )
    assert [row.enterprise_id for row in live] == [second]
    # Both tenants carry the same slug; only their liveness differs.
    both = await _as_owner(
        owner_url,
        "SELECT enterprise_id, slug FROM enterprises WHERE enterprise_id IN (:a, :b)",
        a=first,
        b=second,
    )
    assert {row.slug for row in both} == {slug}


async def test_a_fresh_tenant_repoints_the_binding_rather_than_adding_one(
    owner_url, repository, subject
):
    """``subject`` is the primary key, so there is one binding and this is it.

    A second row beside the retired one is not merely untidy — it is impossible,
    and an implementation that tried would fail the login instead of provisioning
    it. Re-pointing is also what clears the retirement: the policy has by then
    been honoured, and a stamp left in place would tell the next anchor read that
    the tenant this call just created is retired.
    """
    first = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=first)
    assert await _retire(owner_url, first, policy="fresh-tenant") == 0

    stamped = await _as_owner(
        owner_url,
        "SELECT enterprise_id, retired_at, retirement_state "
        "FROM sso_personal_enterprises WHERE subject = :s",
        s=subject,
    )
    assert len(stamped) == 1
    assert stamped[0].enterprise_id == first
    assert stamped[0].retired_at is not None

    second = await _provision(repository, subject)

    rows = await _as_owner(
        owner_url,
        "SELECT enterprise_id, retired_at, retirement_state, membership_confirmed "
        "FROM sso_personal_enterprises WHERE subject = :s",
        s=subject,
    )
    assert len(rows) == 1, "a second binding was inserted beside the retired one"
    assert rows[0].enterprise_id == second
    assert rows[0].retired_at is None
    assert rows[0].retirement_state is None
    assert rows[0].membership_confirmed is False


async def test_two_retired_tenants_of_one_subject_are_both_addressable(
    owner_url, repository, subject
):
    """The ambiguity the rename produced, made impossible by addressing by id.

    Retire, provision again, retire again: two retired enterprises share the
    derived slug, and each is still reachable by its own enterprise id — which
    is the address the command prints on every run, so an interrupted one can be
    finished.
    """
    first = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=first)
    assert await _retire(owner_url, first, policy="fresh-tenant") == 0
    second = await _provision(repository, subject)
    assert await _retire(owner_url, second, policy="fresh-tenant") == 0

    for enterprise_id in (first, second):
        rows = await _as_owner(
            owner_url,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            e=enterprise_id,
        )
        assert rows[0].deleted_at is not None


# =============================================================================
# Blast radius and survival
# =============================================================================


async def test_the_other_tenants_rows_are_byte_identical(owner_url, repository):
    """A retirement is one tenant's, and the bystander proves the blast radius.

    Comparing the bystander row by row rather than counting is what catches a
    predicate that reached too far — an UPDATE missing its ``WHERE`` stamps
    every binding, and a count of one would not notice.
    """
    victim = f"user_pt_v_{uuid.uuid4().hex[:10]}"
    bystander = f"user_pt_b_{uuid.uuid4().hex[:10]}"
    victim_enterprise = await _provision(repository, victim)
    bystander_enterprise = await _provision(repository, bystander)
    await _seed_content(owner_url, victim_enterprise, victim[-10:])
    await _seed_content(owner_url, bystander_enterprise, bystander[-10:])
    await _seed_user(owner_url, subject=victim, enterprise_id=victim_enterprise)
    await _seed_user(owner_url, subject=bystander, enterprise_id=bystander_enterprise)

    before = await _tenant_rows(owner_url, bystander_enterprise)

    assert await _retire(owner_url, victim_enterprise, policy="fresh-tenant") == 0

    assert await _tenant_rows(owner_url, bystander_enterprise) == before

    # And the victim keeps everything a retirement is not allowed to remove:
    # what a retired tenant holds, and for how long, is a separate decision.
    victim_rows = await _tenant_rows(owner_url, victim_enterprise)
    assert len(victim_rows["cases"]) == 1
    assert len(victim_rows["knowledge"]) == 1
    assert len(victim_rows["accounts"]) == 1


async def test_a_dry_run_writes_nothing_against_the_real_schema(
    owner_url, repository, subject
):
    """Dry run is the default, and it must be inert on both sides.

    "Both sides" is the point: a command that reported a plan while already
    having asked the IdP to delete an organization would be unrecoverable, and
    the provider double records every call so that cannot pass unnoticed. The
    ``--apply`` runs above are the positive control that the same plan does
    write when it is asked to.
    """
    enterprise_id = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    await _seed_content(owner_url, enterprise_id, subject[-10:])
    before = await _tenant_rows(owner_url, enterprise_id)
    idp = RecordingIdP([await _idp_org_of(owner_url, enterprise_id)])
    revoker = RecordingRevoker()

    code = await cli.retire(
        subject=None,
        enterprise_id=enterprise_id,
        next_login="fresh-tenant",
        apply=False,
        idp=idp,
        auth_service=revoker,
    )

    assert code == 0
    assert idp.calls == []
    assert revoker.revoked == []
    assert await _tenant_rows(owner_url, enterprise_id) == before


async def test_retiring_a_domain_enterprise_is_refused_and_writes_nothing(owner_url):
    """This command retires personal tenants only, and the slug is what says so.

    A domain enterprise is shared by every account at that domain, so retiring
    one as though it were somebody's private tenant would fence a whole company
    out of the product and stamp it with a policy naming a subject that does not
    own it. The slug is the discriminator — a personal one is
    ``personal-<32 hex>``, derived from an IdP subject, and a domain one is
    deliberately prefixed differently so it can never answer that test.
    """
    domain = f"acme{uuid.uuid4().hex[:10]}.example"
    enterprise_id = str(uuid.uuid4())
    await _as_owner_write(
        owner_url,
        "INSERT INTO enterprises (enterprise_id, name, slug, domain) "
        "VALUES (:e, :n, :s, :d)",
        e=enterprise_id,
        n=domain,
        s=domain_enterprise_slug(domain),
        d=domain,
    )
    idp = RecordingIdP([])

    code = await cli.retire(
        subject=None,
        enterprise_id=enterprise_id,
        next_login="refuse",
        apply=True,
        idp=idp,
        auth_service=RecordingRevoker(),
    )

    assert code == cli.EXIT_REFUSED
    assert idp.calls == []
    row = (
        await _as_owner(
            owner_url,
            "SELECT deleted_at, domain FROM enterprises WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    )[0]
    assert row.deleted_at is None, "a refused command fenced the company anyway"
    assert row.domain == domain


# =============================================================================
# The login, through the real repositories and real rows
# =============================================================================


class _FakeProvider(ISSOIdentityProvider):
    """The IdP port for the login side, minting one organization per external id.

    Local rather than imported: what the login needs from the provider on this
    path is a deterministic personal-organization mint, and a double defined
    beside the tests that use it cannot be changed out from under them by a
    module they do not otherwise depend on.
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


def _identity(subject: str, organization_id: str | None = None) -> SSOIdentity:
    return SSOIdentity(
        provider=PROVIDER,
        provider_user_id=subject,
        email=f"{subject}@{PERSONAL_DOMAIN}",
        email_verified=True,
        display_name="Retired Individual",
        organization_id=organization_id,
    )


async def _login_service(repository, *, mappings=None):
    """The real binding, enterprise and user stores; only the IdP is a double."""
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
        identity_provider=_FakeProvider(),
        ephemeral_store=SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True)),
        user_repository=SessionlessUserRepository(),
        token_generator=object(),
        session_service=object(),
        dashboard_url="https://app.faultmaven.test",
        access_token_expires_in=3600,
        org_mapping_repository=mappings,
        enterprise_repository=SessionlessEnterpriseRepository(),
        personal_enterprise_repository=repository,
    )


@pytest.fixture
def switch_on(monkeypatch):
    """The real switch, through the real settings singleton.

    The hourly ceiling is raised alongside it, and deliberately not silently:
    ``count_created_since`` is a deployment-wide count, so on a scratch database
    that accumulates rows every provisioning case here would refuse
    ``personal_provisioning_ceiling`` — a property of the fixture, not of
    anything under test. The ceiling is pinned where it belongs, in the unit
    module.
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


async def test_a_refuse_retirement_refuses_the_next_orgless_login(
    owner_url, repository, subject, switch_on, capsys, caplog
):
    """``--next-login refuse`` is read off the binding by the next sign-in.

    The account is still anchored to the fenced enterprise — that is the state
    a retirement leaves — so what refuses here is the recorded policy rather
    than any absence. The reason slug matters as much as the refusal: an
    employee arriving unscoped, a retired subject and a broken anchor have
    opposite remedies, and only the slug tells an operator which they are
    looking at.
    """
    enterprise_id = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(owner_url, enterprise_id, policy="refuse") == 0

    service = await _login_service(repository)
    enterprise, error = await service._resolve_login_enterprise(_identity(subject))

    assert enterprise is None
    assert error == "sso_org_unmapped"
    assert "personal_tenant_retired" in _emitted_logs(capsys, caplog)


async def test_a_fresh_tenant_retirement_lets_the_next_orgless_login_provision(
    owner_url, repository, subject, switch_on
):
    """The positive control for the refusal above: the other policy releases.

    Both runs reach the same code with the same anchored account and the same
    fenced enterprise; only the recorded value differs. That is what makes this
    pair evidence that the policy is what decides, rather than something else
    the two situations happened not to share.
    """
    enterprise_id = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(owner_url, enterprise_id, policy="fresh-tenant") == 0

    # The account is still anchored to the retired enterprise: NOT NULL leaves
    # no absence to mean "released", so the release is the recorded policy.
    anchored = await _as_owner(
        owner_url,
        "SELECT enterprise_id FROM users WHERE sso_provider_id = :s",
        s=subject,
    )
    assert anchored[0].enterprise_id == enterprise_id

    service = await _login_service(repository)
    enterprise, error = await service._resolve_login_enterprise(_identity(subject))

    assert error is None
    assert enterprise is not None
    assert enterprise.enterprise_id != enterprise_id
    assert enterprise.deleted_at is None
    rows = await _as_owner(
        owner_url,
        "SELECT enterprise_id, retired_at FROM sso_personal_enterprises "
        "WHERE subject = :s",
        s=subject,
    )
    assert len(rows) == 1
    assert rows[0].enterprise_id == enterprise.enterprise_id
    assert rows[0].retired_at is None


async def test_the_retired_tenant_is_unreachable_even_by_its_own_idp_org(
    owner_url, repository, subject, switch_on
):
    """The mapping row is gone, so a still-echoing IdP organization meets the
    operator-fixable unmapped refusal rather than binding a dead tenant."""
    enterprise_id = await _provision(repository, subject)
    idp_org = await _idp_org_of(owner_url, enterprise_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(owner_url, enterprise_id) == 0

    from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (  # noqa: E501
        SessionlessSSOOrgMappingRepository,
    )

    service = await _login_service(
        repository, mappings=SessionlessSSOOrgMappingRepository()
    )

    enterprise, error = await service._resolve_login_enterprise(
        _identity(subject, idp_org)
    )

    assert enterprise is None
    assert error == "sso_org_unmapped"


# =============================================================================
# R5 — a refresh chain cannot outlive the tenant
# =============================================================================


async def test_a_refresh_is_refused_for_a_retired_tenant(
    owner_url, repository, subject
):
    """The second leg of the fence, read off a real soft-deleted row.

    The revocation watermark stops the chain that exists at retirement time;
    this stops any chain that presents the retired tenant's enterprise claim
    afterwards. The **request** path is out of scope here — this pins the
    predicate both refresh surfaces call.
    """
    from faultmaven.infrastructure.persistence.enterprise_liveness import (
        enterprise_id_is_usable,
    )

    enterprise_id = await _provision(repository, subject)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await enterprise_id_is_usable(enterprise_id) is True

    assert await _retire(owner_url, enterprise_id) == 0

    assert await enterprise_id_is_usable(enterprise_id) is False
    # An absent claim is a different condition with its own handling, and must
    # not be turned into a refusal here.
    assert await enterprise_id_is_usable(None) is True


def test_the_module_is_not_silently_skipping():
    """CI greps this lane for "skipped"; the skipif above must not be firing."""
    assert os.environ.get("DATABASE_URL", "").startswith("postgresql")
