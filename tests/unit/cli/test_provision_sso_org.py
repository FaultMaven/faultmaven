"""``faultmaven.cli.provision_sso_org`` refuses to bind the wrong tenants (#869).

The script resolves the enterprise by its ``--domain``, so a customer whose
domain already has an enterprise — a colleague signed up first, or the operator
mistyped it — resolves onto the EXISTING tenant. Under ADR-017 D1 that tenant is
the isolation boundary, so the second reading is a data-isolation incident: the
new customer's accounts land inside somebody else's wall and become eligible for
its teams. It is legitimate only when an operator means it and says so with
``--enterprise-id``.

The domain is also the whole reason the argument is required. ``enterprises
.domain`` is what a colleague's sign-in finds this tenant by and what the team
invitation rule is decided on, so an enterprise provisioned without one invites
nobody and is duplicated by the next sign-up — silently, days later.

``_ensure_mapping`` is the last gate before that becomes durable, so it checks
*both* directions of the 1:1 relation and refuses rather than writing:

* the IdP org already points at a different enterprise (``RemapRefused``);
* the enterprise is already claimed by a different IdP org
  (``OrgAlreadyClaimed``) — the case the ``UNIQUE (provider, enterprise_id)``
  constraint would otherwise surface as a raw ``IntegrityError``.

Exercised against a real in-memory SQLite engine built from the ORM metadata,
so the refusals are checked against the schema that actually ships.
"""

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.cli import provision_sso_org
from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    TeamModel,
)

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
OTHER_ENTERPRISE_ID = "77777777-7777-7777-7777-777777777777"
ORG_A = "22222222-2222-2222-2222-222222222222"
ORG_B = "55555555-5555-5555-5555-555555555555"
DOMAINLESS_ENTERPRISE_ID = "99999999-9999-9999-9999-999999999999"


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
async def empty_session(engine):
    """A database holding nothing — what a freshly wiped deployment looks like."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for enterprise_id, slug, domain in (
            (ENTERPRISE_ID, "acme", "acme.example"),
            (OTHER_ENTERPRISE_ID, "globex", "globex.example"),
            (DOMAINLESS_ENTERPRISE_ID, "legacy", None),
        ):
            s.add(
                EnterpriseModel(
                    enterprise_id=enterprise_id,
                    name=slug.title(),
                    slug=slug,
                    domain=domain,
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


async def _seed_mapping(session, provider_org_id, enterprise_id):
    session.add(
        SSOOrgMappingModel(
            provider="workos",
            provider_org_id=provider_org_id,
            enterprise_id=enterprise_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.commit()


async def _mapping_rows(session):
    from sqlalchemy import select

    result = await session.execute(select(SSOOrgMappingModel))
    return [
        (row.provider, row.provider_org_id, row.enterprise_id)
        for row in result.scalars().all()
    ]


# =============================================================================
# The reverse-claim refusal (F1)
# =============================================================================


async def test_refuses_to_bind_an_enterprise_already_claimed_by_another_idp_org(
    session,
):
    """The slug-collision alarm: this tenant already belongs to someone else's
    IdP organization, so binding a second one would pool two customers inside
    one isolation boundary."""
    await _seed_mapping(session, "org_INCUMBENT", ENTERPRISE_ID)

    with pytest.raises(provision_sso_org.OrgAlreadyClaimed) as exc:
        await provision_sso_org._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", enterprise_id=ENTERPRISE_ID
        )

    assert exc.value.enterprise_id == ENTERPRISE_ID
    assert exc.value.claimed_by == "org_INCUMBENT"
    assert exc.value.requested_by == "org_NEWCOMER"


async def test_the_reverse_claim_refusal_writes_nothing(session):
    """A refusal must not leave a partial binding behind."""
    await _seed_mapping(session, "org_INCUMBENT", ENTERPRISE_ID)

    with pytest.raises(provision_sso_org.OrgAlreadyClaimed):
        await provision_sso_org._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", enterprise_id=ENTERPRISE_ID
        )

    assert await _mapping_rows(session) == [("workos", "org_INCUMBENT", ENTERPRISE_ID)]


async def test_the_refusal_replaces_a_raw_integrity_error(session):
    """Without the reverse check this same call died on the UNIQUE constraint
    as an unhandled traceback. Pin that it is now a typed refusal."""
    from sqlalchemy.exc import IntegrityError

    await _seed_mapping(session, "org_INCUMBENT", ENTERPRISE_ID)

    with pytest.raises(provision_sso_org.OrgAlreadyClaimed) as exc:
        await provision_sso_org._ensure_mapping(
            session, provider_org_id="org_NEWCOMER", enterprise_id=ENTERPRISE_ID
        )

    assert not isinstance(exc.value, IntegrityError)


# =============================================================================
# The forward remap refusal
# =============================================================================


async def test_refuses_to_repoint_an_idp_org_at_a_different_enterprise(session):
    await _seed_mapping(session, "org_01H", ENTERPRISE_ID)

    with pytest.raises(provision_sso_org.RemapRefused) as exc:
        await provision_sso_org._ensure_mapping(
            session, provider_org_id="org_01H", enterprise_id=OTHER_ENTERPRISE_ID
        )

    assert exc.value.provider_org_id == "org_01H"
    assert exc.value.mapped_to == ENTERPRISE_ID
    assert exc.value.requested == OTHER_ENTERPRISE_ID
    assert await _mapping_rows(session) == [("workos", "org_01H", ENTERPRISE_ID)]


# =============================================================================
# The permitted paths
# =============================================================================


async def test_creates_the_mapping_when_neither_side_is_bound(session):
    created = await provision_sso_org._ensure_mapping(
        session, provider_org_id="org_01H", enterprise_id=ENTERPRISE_ID
    )

    assert created is True
    assert await _mapping_rows(session) == [("workos", "org_01H", ENTERPRISE_ID)]


async def test_re_running_the_same_binding_is_a_quiet_no_op(session):
    """Idempotence: same IdP org, same enterprise, nothing written, no raise."""
    await _seed_mapping(session, "org_01H", ENTERPRISE_ID)

    created = await provision_sso_org._ensure_mapping(
        session, provider_org_id="org_01H", enterprise_id=ENTERPRISE_ID
    )

    assert created is False
    assert await _mapping_rows(session) == [("workos", "org_01H", ENTERPRISE_ID)]


async def test_a_second_enterprise_may_be_bound_to_its_own_idp_org(session):
    """The refusals are per-pair — an unrelated tenant is unaffected.

    The positive control for the two refusals above: without it they would also
    hold if ``_ensure_mapping`` refused every second row for any reason.
    """
    await _seed_mapping(session, "org_01H", ENTERPRISE_ID)

    created = await provision_sso_org._ensure_mapping(
        session, provider_org_id="org_01J", enterprise_id=OTHER_ENTERPRISE_ID
    )

    assert created is True
    assert sorted(await _mapping_rows(session)) == sorted(
        [
            ("workos", "org_01H", ENTERPRISE_ID),
            ("workos", "org_01J", OTHER_ENTERPRISE_ID),
        ]
    )


# =============================================================================
# Slug-keyed resolution — what makes a re-run a no-op
# =============================================================================
#
# These used to assert that `_get_or_create_organization` bound the resolved
# organization as the current tenant "so the writes stay inside policy even
# where RLS is forced". That claim was false and is gone (#935): the whole run
# shares one transaction, and the engine's `begin` listener samples the tenant
# contextvar at BEGIN — before the first SELECT in `_get_or_create_enterprise`,
# and never again. The old assertions read the Python contextvar at
# `session.add` time, a proxy that passed while PostgreSQL still held the
# Standalone sentinel, so they would have gone on passing had the listener
# never fired at all. What actually keeps provisioning legal is the RLS-exempt
# role, checked by the preflight below.


async def test_a_new_slug_creates_an_organization_under_the_enterprise(session):
    organization, created = await provision_sso_org._get_or_create_organization(
        session, enterprise_id=ENTERPRISE_ID, name="Acme New", slug="acme-new"
    )

    assert created is True
    assert organization.enterprise_id == ENTERPRISE_ID
    assert organization.slug == "acme-new"
    assert organization.organization_id not in (ORG_A, ORG_B)


async def test_an_existing_slug_resolves_onto_the_existing_organization(session):
    """Identity is (enterprise_id, slug), so re-running is a no-op rather than a
    second tenant — the property the script's idempotency claim rests on."""
    organization, created = await provision_sso_org._get_or_create_organization(
        session, enterprise_id=ENTERPRISE_ID, name="Acme A", slug="acme-a"
    )

    assert created is False
    assert organization.organization_id == ORG_A


# The `created` flags are not decoration: `org_created` decides whether
# `provision` prints the REUSING AN EXISTING TENANT alarm — the warning that
# stands between a slug collision and a new customer's users landing in someone
# else's tenant. A helper that reported "created" for a row it merely found
# would silence it. The alarm itself is exercised further down.


async def test_an_existing_enterprise_slug_resolves_rather_than_creating(session):
    enterprise, created = await provision_sso_org._get_or_create_enterprise(
        session, enterprise_id=None, name="Acme", slug="acme"
    )

    assert created is False
    assert enterprise.enterprise_id == ENTERPRISE_ID


async def test_a_new_enterprise_slug_creates_one(session):
    enterprise, created = await provision_sso_org._get_or_create_enterprise(
        session, enterprise_id=None, name="Initech", slug="initech"
    )

    assert created is True
    assert enterprise.enterprise_id not in (ENTERPRISE_ID, OTHER_ENTERPRISE_ID)


# =============================================================================
# The --enterprise-id argument boundary
# =============================================================================


def test_an_empty_enterprise_id_is_refused_not_guessed(monkeypatch, capsys):
    """A bogus non-empty id refuses with LookupError, so an empty one must not
    quietly mean something else. Falling through to slug resolution would put
    the tenant under an enterprise the operator never named — recoverable only
    by a manual account migration."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fm-provision-sso-org",
            "--name",
            "Acme",
            "--slug",
            "acme",
            "--workos-org-id",
            "org_01H",
            "--enterprise-id",
            "",
        ],
    )

    def explode(*_a, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("provision ran on an empty --enterprise-id")

    monkeypatch.setattr(provision_sso_org, "provision", explode)

    with pytest.raises(SystemExit) as exit_info:
        provision_sso_org.main()

    assert exit_info.value.code == 2  # argparse usage error, not a provisioning run
    assert "--enterprise-id was given but is empty" in capsys.readouterr().err


# =============================================================================
# The role preflight (#887) — refuses before any write
# =============================================================================


async def test_provision_refuses_when_the_role_is_rls_scoped(monkeypatch, capsys):
    """The documented `kubectl exec` recipe inherits the pod's DATABASE_URL,
    which startup guarantees is the RLS-scoped application role. Provisioning
    must refuse on that role — and refuse *before* opening a session, so a
    half-provisioned tenant cannot be left behind."""

    async def refuse(**_kwargs):
        raise DeploymentCoherenceError(
            "connected as 'faultmaven_app' ... pass DATABASE_URL explicitly"
        )

    def explode(*_a, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("a session was opened after the preflight refused")

    monkeypatch.setattr(
        provision_sso_org, "assert_provisioning_db_role_bypasses_rls", refuse
    )
    monkeypatch.setattr(provision_sso_org, "get_db_session", explode)

    ok = await provision_sso_org.provision(
        name="Acme",
        slug="acme",
        workos_org_id="org_01H",
        domain="acme.example",
        enterprise_id=None,
    )

    assert ok is False
    assert "faultmaven_app" in capsys.readouterr().out


async def test_provision_proceeds_when_the_role_is_rls_exempt(monkeypatch):
    """The gate is not a wall: an owner role passes the preflight and the run
    reaches the session. (Mutation guard — if the gate were inverted, or always
    raised, this test would fail while the refusal test above still passed.)"""
    reached = []

    async def allow(**_kwargs):
        return "faultmaven"

    def record(*_a, **_kw):
        reached.append(True)
        raise RuntimeError("stop after the preflight")

    monkeypatch.setattr(
        provision_sso_org, "assert_provisioning_db_role_bypasses_rls", allow
    )
    monkeypatch.setattr(provision_sso_org, "get_db_session", record)

    with pytest.raises(RuntimeError, match="stop after the preflight"):
        await provision_sso_org.provision(
            name="Acme",
            slug="acme",
            workos_org_id="org_01H",
            domain="acme.example",
            enterprise_id=None,
        )

    assert reached == [True]


# =============================================================================
# Running the real `provision()`
# =============================================================================


def _lend(monkeypatch, target_session):
    """Point `provision` at an in-memory session with `get_db_session`'s contract.

    The preflight is a no-op here (SQLite has no RLS, and the role posture is
    covered above); what this exercises is everything past the session boundary,
    which nothing reached before because both preflight tests stop at it.

    The stub reproduces `get_db_session`'s contract rather than just yielding —
    commit on clean exit, rollback and re-raise on exception. Without that, a
    refusal changed from `raise` to `return False` would pass every test here
    while committing a half-provisioned tenant, which is precisely the hazard
    the comment above the session block claims to prevent. The rollback is a
    real SQLite one; only the RLS the dialect lacks is out of scope.
    """

    async def allow(**_kwargs):
        return "faultmaven"

    @asynccontextmanager
    async def lend_session(*_a, **_kw):
        try:
            yield target_session
            await target_session.commit()
        except Exception:
            await target_session.rollback()
            raise

    monkeypatch.setattr(
        provision_sso_org, "assert_provisioning_db_role_bypasses_rls", allow
    )
    monkeypatch.setattr(provision_sso_org, "get_db_session", lend_session)
    return provision_sso_org.provision


@pytest.fixture
def provision_against(monkeypatch, session):
    """`provision()` against the seeded tenants."""
    return _lend(monkeypatch, session)


@pytest.fixture
def provision_into_empty(monkeypatch, empty_session):
    """`provision()` against an empty database — a freshly wiped deployment."""
    return _lend(monkeypatch, empty_session)


async def _rows(session, model):
    from sqlalchemy import select

    return (await session.execute(select(model))).scalars().all()


# =============================================================================
# --domain: the column the rest of the system keys on
# =============================================================================


async def test_the_domain_is_written_case_folded(provision_into_empty, empty_session):
    """One spelling of a domain is one enterprise.

    The operator types the domain and the sign-up path derives it from a
    verified address; the two only meet if this row is written with the same
    fold the derivation uses. Stored unfolded, the colleague who signs in next
    does not find this tenant and gets a SECOND enterprise — defect 1 of the
    2026-09-10 cutover, in its other direction.
    """
    ok = await provision_into_empty(
        name="Acme Corp",
        slug="acme-corp",
        workos_org_id="org_ACME",
        domain="ACME.Example.",
        enterprise_id=None,
    )

    assert ok is True
    enterprises = await _rows(empty_session, EnterpriseModel)
    assert [e.domain for e in enterprises] == ["acme.example"]


async def test_a_consumer_mail_domain_is_refused(provision_into_empty, empty_session):
    """A company cannot be provisioned onto a consumer domain (D3's island rule).

    Such a domain gives every account its OWN enterprise, so a row stamped
    ``gmail.com`` would claim strangers into one isolation boundary.
    """
    ok = await provision_into_empty(
        name="Not A Company",
        slug="not-a-company",
        workos_org_id="org_PERSONAL",
        domain="GMAIL.com",
        enterprise_id=None,
    )

    assert ok is False
    assert await _rows(empty_session, EnterpriseModel) == []


async def test_a_company_domain_is_not_refused(provision_into_empty, capsys):
    """The positive control: the refusal above is about the consumer list, not
    about `--domain` being present at all."""
    ok = await provision_into_empty(
        name="Acme Corp",
        slug="acme-corp",
        workos_org_id="org_ACME",
        domain="acme.example",
        enterprise_id=None,
    )

    assert ok is True
    assert "consumer mail domain" not in capsys.readouterr().out


async def test_a_named_enterprise_with_a_different_domain_is_refused(
    provision_against, session, capsys
):
    """Naming an enterprise AND a domain asserts they agree.

    Re-domaining moves both which addresses the tenant's teams may invite and
    which sign-ups join it — for accounts already inside it — so a run that
    would do it silently refuses instead.
    """
    ok = await provision_against(
        name="Acme",
        slug="acme-eu",
        workos_org_id="org_EU",
        domain="globex.example",
        enterprise_id=ENTERPRISE_ID,
    )

    assert ok is False
    assert "carries domain acme.example" in capsys.readouterr().out
    enterprise = await session.get(EnterpriseModel, ENTERPRISE_ID)
    assert enterprise.domain == "acme.example"
    assert await _mapping_rows(session) == []


async def test_a_named_enterprise_with_the_matching_domain_proceeds(provision_against):
    """The positive control for the refusal above: the same enterprise, the same
    named id, and only the domain differs."""
    ok = await provision_against(
        name="Acme EU",
        slug="acme-eu",
        workos_org_id="org_EU",
        domain="ACME.Example",
        enterprise_id=ENTERPRISE_ID,
    )

    assert ok is True


async def test_a_slug_held_by_another_enterprise_is_refused(
    provision_against, session, capsys
):
    """The slug is unique among live enterprises, so a new domain's enterprise
    cannot be created under one another tenant holds.

    Without this the domain lookup misses, the INSERT trips
    ``ix_enterprises_slug_live``, and the operator gets a raw IntegrityError
    where this script promises a named refusal.
    """
    ok = await provision_against(
        name="Acme Rival",
        slug="acme",
        workos_org_id="org_RIVAL",
        domain="rival.example",
        enterprise_id=None,
    )

    assert ok is False
    out = capsys.readouterr().out
    assert "already belongs to enterprise" in out
    assert ENTERPRISE_ID in out
    survivors = [
        e for e in await _rows(session, EnterpriseModel) if e.domain == "rival.example"
    ]
    assert survivors == []


async def test_a_named_enterprise_without_a_domain_is_flagged(
    provision_against, capsys
):
    """The 2026-09-10 cutover state, reached the only way that is still open.

    An enterprise with a NULL domain reads as PERSONAL to the invitation rule,
    so its teams can invite nobody, and the next sign-up on the customer's
    domain builds a second enterprise beside it. The run succeeds — the operator
    named the tenant — but it does not go unremarked.
    """
    ok = await provision_against(
        name="Legacy",
        slug="legacy-org",
        workos_org_id="org_LEGACY",
        domain=None,
        enterprise_id=DOMAINLESS_ENTERPRISE_ID,
    )

    assert ok is True
    assert "THIS ENTERPRISE CARRIES NO DOMAIN" in capsys.readouterr().out


# =============================================================================
# No team (ADR-017 D4)
# =============================================================================


async def test_provisioning_creates_no_team(provision_into_empty, empty_session):
    """A team forms by consent, so a team minted here would have no members —
    invisible to every membership-gated read, impossible to administer or
    retire, and holding its name against the enterprise's partial unique index.

    The docstring claimed this before the code did (fm#1372); this is the test
    that makes the claim true.
    """
    ok = await provision_into_empty(
        name="Acme Corp",
        slug="acme-corp",
        workos_org_id="org_ACME",
        domain="acme.example",
        enterprise_id=None,
    )

    assert ok is True
    assert await _rows(empty_session, EnterpriseModel) != []  # the run did write
    assert await _rows(empty_session, TeamModel) == []


# =============================================================================
# (created) / (already present) — the labels the live run was read by
# =============================================================================


async def test_a_first_run_reports_created_and_a_second_already_present(
    provision_into_empty, capsys
):
    """Every line of the 2026-09-10 run printed "(already present)" against what
    the verifier had counted as an empty database.

    Either the labels lie about fresh creations or the command was run twice.
    This decides it: against an empty database every line reports ``created``,
    and only a re-run reports ``already present``.
    """
    args = dict(
        name="Acme Corp",
        slug="acme-corp",
        workos_org_id="org_ACME",
        domain="acme.example",
        enterprise_id=None,
    )

    assert await provision_into_empty(**args) is True
    first = capsys.readouterr().out
    assert "already present" not in first, first
    assert first.count("(created)") == 3  # enterprise, organization, mapping

    assert await provision_into_empty(**args) is True
    second = capsys.readouterr().out
    assert "(created)" not in second, second
    assert second.count("(already present)") == 3


# =============================================================================
# The REUSING AN EXISTING TENANT alarm
# =============================================================================


async def test_binding_a_new_idp_org_onto_an_enterprise_matched_by_domain_warns(
    provision_against, capsys
):
    """The collision this module exists to catch: --domain resolves onto an
    enterprise somebody else already owns.

    Under ADR-017 D1 that enterprise is the isolation boundary, so the new
    customer's accounts land inside somebody else's wall and become eligible for
    its teams. Paired with the two tests below — the same enterprise, and only
    how the operator named it decides between "you are about to join someone
    else's tenant" and "you said so".
    """
    ok = await provision_against(
        name="Not Acme",
        slug="not-acme",
        workos_org_id="org_NEW",
        domain="acme.example",
        enterprise_id=None,
    )

    assert ok is True
    out = capsys.readouterr().out
    assert "REUSING AN EXISTING TENANT" in out
    assert ENTERPRISE_ID in out


async def test_an_empty_enterprise_id_still_warns(provision_against, capsys):
    """``--enterprise-id ""`` — an unset shell variable in the documented kubectl
    recipe. ``provision`` tests truthiness, so it takes the domain path; the
    warning must read it the same way rather than as a named parent."""
    ok = await provision_against(
        name="Acme Reseller",
        slug="acme-reseller",
        workos_org_id="org_RES2",
        domain="acme.example",
        enterprise_id="",
    )

    assert ok is True
    assert "REUSING AN EXISTING TENANT" in capsys.readouterr().out


async def test_a_named_enterprise_does_not_warn(provision_against, capsys):
    """The documented --enterprise-id recipe (a second organization for the same
    customer). The operator named the isolation boundary, which is the whole of
    what the alarm exists to make them confirm — warning here would cry wolf on
    the script's own documented usage.

    The positive control for the two tests above: the same existing enterprise,
    the same new IdP binding, and only the naming differs.
    """
    ok = await provision_against(
        name="Acme EU",
        slug="acme-eu",
        workos_org_id="org_EU",
        domain="acme.example",
        enterprise_id=ENTERPRISE_ID,
    )

    assert ok is True
    assert "⚠️" not in capsys.readouterr().out


async def test_a_brand_new_enterprise_does_not_warn(provision_against, capsys):
    """Nothing was reused, so there is nothing to confirm."""
    ok = await provision_against(
        name="Fresh Co",
        slug="fresh-co",
        workos_org_id="org_FRESHCO",
        domain="fresh-co.example",
        enterprise_id=None,
    )

    assert ok is True
    assert "⚠️" not in capsys.readouterr().out


async def test_a_refusal_leaves_no_half_provisioned_tenant(
    provision_against, session, capsys
):
    """`provision` creates the enterprise and organization *before* it learns the
    mapping is refused. Every refusal therefore raises out of the session block
    rather than returning from inside it — returning would let `get_db_session`
    commit a tenant with no mapping, the state that makes the next run's lookup
    dangerous."""
    from sqlalchemy import select

    await _seed_mapping(session, "org_TAKEN", OTHER_ENTERPRISE_ID)

    ok = await provision_against(
        name="Brand New",
        slug="brand-new",
        workos_org_id="org_TAKEN",
        domain="brand-new.example",
        enterprise_id=None,
    )

    assert ok is False
    assert (
        "already mapped to a different FaultMaven enterprise" in capsys.readouterr().out
    )
    # The enterprise and organization this run got as far as creating are gone.
    survivors = (
        (
            await session.execute(
                select(EnterpriseModel).where(EnterpriseModel.slug == "brand-new")
            )
        )
        .scalars()
        .all()
    )
    assert survivors == []
    assert await _mapping_rows(session) == [
        ("workos", "org_TAKEN", OTHER_ENTERPRISE_ID)
    ]


async def test_an_idempotent_re_run_stays_quiet(provision_against, capsys):
    """Re-running with the same arguments resolves onto the tenant it created
    last time. The enterprise is reused, but the binding is not new, so the
    alarm must not cry wolf on the script's documented no-op."""
    args = dict(
        name="Fresh Co",
        slug="fresh",
        workos_org_id="org_FRESH",
        domain="fresh.example",
        enterprise_id=None,
    )

    ok = await provision_against(**args)
    assert ok is True
    assert "⚠️" not in capsys.readouterr().out

    ok_again = await provision_against(**args)

    assert ok_again is True
    # Neither warning: the tenant is reused, but this run created it and the
    # binding already points here.
    assert "⚠️" not in capsys.readouterr().out


# =============================================================================
# The --domain argument boundary (argparse, before anything runs)
# =============================================================================


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["fm-provision-sso-org", *argv])

    def explode(*_a, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("provision ran on a malformed --domain")

    monkeypatch.setattr(provision_sso_org, "provision", explode)
    with pytest.raises(SystemExit) as exit_info:
        provision_sso_org.main()
    return exit_info.value.code


BASE_ARGV = ["--name", "Acme", "--slug", "acme", "--workos-org-id", "org_01H"]


def test_domain_is_required_without_an_enterprise_id(monkeypatch, capsys):
    """Optional is how the enterprise came out domainless on 2026-09-10 — the
    argument did not exist, and nothing noticed for a day."""
    assert _run_main(monkeypatch, BASE_ARGV) == 2
    assert "--domain is required" in capsys.readouterr().err


def test_an_empty_domain_is_refused_not_read_as_absent(monkeypatch, capsys):
    """An unset shell variable in the documented kubectl recipe. Falling through
    to "no domain" would recreate the very defect --domain closes."""
    assert _run_main(monkeypatch, [*BASE_ARGV, "--domain", "   "]) == 2
    assert "--domain was given but is empty" in capsys.readouterr().err


def test_an_email_address_is_not_a_domain(monkeypatch, capsys):
    """A pasted address would provision cleanly and then match nothing — the
    silent shape this argument exists to prevent."""
    assert _run_main(monkeypatch, [*BASE_ARGV, "--domain", "alice@acme.example"]) == 2
    assert "bare domain" in capsys.readouterr().err


def test_a_named_enterprise_makes_the_domain_optional(monkeypatch):
    """The control: the requirement is "this enterprise must carry a domain",
    and an enterprise named by id already answers that question."""
    calls = []

    async def record(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        sys,
        "argv",
        ["fm-provision-sso-org", *BASE_ARGV, "--enterprise-id", ENTERPRISE_ID],
    )
    monkeypatch.setattr(provision_sso_org, "provision", record)

    with pytest.raises(SystemExit) as exit_info:
        provision_sso_org.main()

    assert exit_info.value.code == 0
    assert calls == [
        dict(
            name="Acme",
            slug="acme",
            workos_org_id="org_01H",
            domain=None,
            enterprise_id=ENTERPRISE_ID,
        )
    ]
