"""``faultmaven.cli.provision_sso_org`` refuses to bind the wrong tenants (#869).

The script resolves the enterprise by slug, so a new customer whose slug
collides with an existing one resolves onto the EXISTING tenant. Under ADR-017
D1 that tenant is the isolation boundary, so the mistake is a data-isolation
incident: the new customer's accounts land inside somebody else's wall and
become eligible for its teams. It is legitimate only when an operator means it
and says so with ``--enterprise-id``.

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
)

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
OTHER_ENTERPRISE_ID = "77777777-7777-7777-7777-777777777777"
ORG_A = "22222222-2222-2222-2222-222222222222"
ORG_B = "55555555-5555-5555-5555-555555555555"


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
        for enterprise_id, slug in (
            (ENTERPRISE_ID, "acme"),
            (OTHER_ENTERPRISE_ID, "globex"),
        ):
            s.add(
                EnterpriseModel(
                    enterprise_id=enterprise_id,
                    name=slug.title(),
                    slug=slug,
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


async def test_the_default_team_is_created_once_then_resolved(session):
    """One default team per ENTERPRISE (ADR-017 D4): a team belongs to the
    isolation boundary, not to a billing group."""
    team, created = await provision_sso_org._get_or_create_default_team(
        session, enterprise_id=ENTERPRISE_ID
    )
    assert created is True
    assert team.enterprise_id == ENTERPRISE_ID

    again, created_again = await provision_sso_org._get_or_create_default_team(
        session, enterprise_id=ENTERPRISE_ID
    )
    assert created_again is False
    assert again.team_id == team.team_id


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
        name="Acme", slug="acme", workos_org_id="org_01H", enterprise_id=None
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
            name="Acme", slug="acme", workos_org_id="org_01H", enterprise_id=None
        )

    assert reached == [True]


# =============================================================================
# The REUSING AN EXISTING TENANT alarm
# =============================================================================


@pytest.fixture
def provision_against(monkeypatch, session):
    """Run the real `provision()` against the in-memory session.

    The preflight is a no-op here (SQLite has no RLS, and the role posture is
    covered above); what this exercises is the alarm condition, which nothing
    reached before because both preflight tests stop at the session boundary.

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
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    monkeypatch.setattr(
        provision_sso_org, "assert_provisioning_db_role_bypasses_rls", allow
    )
    monkeypatch.setattr(provision_sso_org, "get_db_session", lend_session)
    return provision_sso_org.provision


async def test_binding_a_new_idp_org_onto_an_enterprise_matched_by_slug_warns(
    provision_against, capsys
):
    """The slug collision this whole module exists to catch: --slug resolves onto
    an enterprise somebody else already owns.

    Under ADR-017 D1 that enterprise is the isolation boundary, so the new
    customer's accounts land inside somebody else's wall and become eligible for
    its teams. Paired with the two tests below — the same enterprise, and only
    how the operator named it decides between "you are about to join someone
    else's tenant" and "you said so".
    """
    ok = await provision_against(
        name="Not Acme",
        slug="acme",
        workos_org_id="org_NEW",
        enterprise_id=None,
    )

    assert ok is True
    out = capsys.readouterr().out
    assert "REUSING AN EXISTING TENANT" in out
    assert ENTERPRISE_ID in out


async def test_an_empty_enterprise_id_still_warns(provision_against, capsys):
    """``--enterprise-id ""`` — an unset shell variable in the documented kubectl
    recipe. ``_get_or_create_enterprise`` tests truthiness, so it takes the slug
    path; the warning must read it the same way rather than as a named parent."""
    ok = await provision_against(
        name="Acme Reseller", slug="acme", workos_org_id="org_RES2", enterprise_id=""
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
        enterprise_id=None,
    )

    assert ok is True
    assert "⚠️" not in capsys.readouterr().out


async def test_a_refusal_leaves_no_half_provisioned_tenant(
    provision_against, session, capsys
):
    """`provision` creates the enterprise, organization and team *before* it
    learns the mapping is refused. Every refusal therefore raises out of the
    session block rather than returning from inside it — returning would let
    `get_db_session` commit a tenant with no mapping, the state that makes the
    next run's slug lookup dangerous."""
    from sqlalchemy import select

    await _seed_mapping(session, "org_TAKEN", OTHER_ENTERPRISE_ID)

    ok = await provision_against(
        name="Brand New",
        slug="brand-new",
        workos_org_id="org_TAKEN",
        enterprise_id=None,
    )

    assert ok is False
    assert (
        "already mapped to a different FaultMaven enterprise" in capsys.readouterr().out
    )
    # The enterprise/organization/team this run got as far as creating are gone.
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
    last time. The organization is reused, but the binding is not new, so the
    alarm must not cry wolf on the script's documented no-op."""
    args = dict(
        name="Fresh Co", slug="fresh", workos_org_id="org_FRESH", enterprise_id=None
    )

    ok = await provision_against(**args)
    assert ok is True
    assert "⚠️" not in capsys.readouterr().out

    ok_again = await provision_against(**args)

    assert ok_again is True
    # Neither warning: the tenant is reused, but this run created it and the
    # binding already points here.
    assert "⚠️" not in capsys.readouterr().out
