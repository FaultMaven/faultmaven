"""What a retired subject's next login does (#1045 D8, ADR-016 D5/D8 amended).

The operator command decides; the login is where that decision becomes
behaviour, so this module drives the **login service** rather than the rows.
Every case goes through the real ``complete_callback``.

The decision is carried by **typed columns**, not by a marker: ``users
.enterprise_id`` is nullable since migration 052, so "released" is an absent
anchor and nothing else, and ``enterprises.deleted_at`` +
``enterprises.personal_tenant_retirement`` say a soft-deleted enterprise was
somebody's retired personal tenant. What is pinned here:

* exactly one anchor state releases provisioning, and every other state — live,
  retired, deleted, dangling — refuses, so an unreadable or unexpected value can
  never produce the permissive answer;
* the refusals are told apart in the log, because their remedies are opposite;
* **no login moves an already-anchored account onto a personal enterprise.**
  That is the reverse move the previous design allowed: a partial state left by
  an interrupted re-anchor could be turned, by a user action, into a demotion
  back into the tenant the account had left.
"""

from __future__ import annotations

import pytest

from faultmaven.config.settings import TenantProvider
from faultmaven.infrastructure.persistence.account_anchor import (
    AnchorKind,
    AnchorState,
    move_is_permitted,
)
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
)
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_FAILED,
    ERROR_ORG_UNMAPPED,
)
from tests.unit.modules.auth.test_sso_personal_tenant import (  # noqa: F401
    COMPANY_IDENTITY_SAME_SUBJECT,
    INDIVIDUAL,
    MAPPED_FM_ORG,
    PERSONAL_ENTERPRISE,
    SUBJECT,
    FakeMappingRepository,
    FakeOrgRepository,
    FakePersonalOrgRepository,
    FakeProvider,
    FakeUserRepository,
    as_tenant_provider,
    build_service,
    isolate_settings,
    make_organization,
    make_returning_user,
    redirect_params,
    run_callback,
    store,
    switch,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.usefixtures("restore_tenant_context", "anchor_db"),
]

#: The enterprise an operator retired. The account stays anchored to it under
#: ``--next-login refuse``; under ``fresh-tenant`` the retirement clears it.
RETIRED_ENTERPRISE = "77777777-7777-7777-7777-777777777777"
COMPANY_ENTERPRISE = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture
def logged(monkeypatch):
    """Every event the login service logged, as ``(event, kwargs)``.

    Asserted on the **structured fields** rather than on captured stdout:
    structlog's sink is process-wide configuration other modules change, so a
    substring test over stdout passes or fails on collection order. The real
    logger still runs underneath.
    """
    from faultmaven.modules.auth.domain.services import sso_login_service

    records: list[tuple[str, dict]] = []
    real = sso_login_service.logger

    class _Recorder:
        def __getattr__(self, name):
            def _log(event, **kwargs):
                records.append((event, kwargs))
                return getattr(real, name)(event, **kwargs)

            return _log

    monkeypatch.setattr(sso_login_service, "logger", _Recorder())
    return records


def _reasons(records) -> list[str]:
    return [kwargs["reason"] for _event, kwargs in records if "reason" in kwargs]


def _service(store, *, anchor, users=None, personal=None, mappings=None, orgs=None):
    user = make_returning_user()
    user.enterprise_id = anchor
    users = users or FakeUserRepository({("workos", SUBJECT): user})
    personal = personal if personal is not None else FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        mappings=mappings,
        orgs=orgs if orgs is not None else FakeOrgRepository(answer_any=True),
    )
    return service, provider, personal, users, user


# =============================================================================
# The verdict is a column, and only one value releases
# =============================================================================


async def test_a_refuse_retirement_refuses_with_its_own_reason(
    store, as_tenant_provider, switch, anchor_db, logged
):
    """Not ``personal_account_already_anchored``: the remedies are opposite.

    That slug's documented remedy is "scope the session in WorkOS", which points
    an operator away from the cause when the truth is that they retired this
    subject themselves.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(RETIRED_ENTERPRISE, retired=True, policy=RETIREMENT_POLICY_REFUSE)
    service, provider, personal, _users, _user = _service(
        store, anchor=RETIRED_ENTERPRISE
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "personal_tenant_retired" in _reasons(logged)
    assert "personal_account_already_anchored" not in _reasons(logged)
    assert provider.provision_calls == []
    assert personal.provisioned == []


async def test_a_fresh_tenant_retirement_provisions_a_new_one(
    store, as_tenant_provider, switch, anchor_db
):
    """The whole point of the flag: the subject starts over.

    A ``fresh-tenant`` retirement leaves the anchor NULL, which is the one state
    that releases provisioning. Driven through the callback so it also covers
    the half that is easy to miss: the account is anchored to nothing when the
    new tenant is written, and the membership write has to set that anchor
    rather than refuse the login.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, users, user = _service(store, anchor=None)

    params = redirect_params(await run_callback(service))

    assert "code" in params, params
    assert len(personal.provisioned) == 1
    assert provider.provision_calls
    # The anchor was SET to the tenant this login resolved, and persisted.
    assert user.enterprise_id == PERSONAL_ENTERPRISE
    assert users.updated and users.updated[-1] is user
    # The new binding was not retired on the way through.
    assert personal.retired == []


@pytest.mark.parametrize(
    "kind,policy,expected_reason",
    [
        (AnchorKind.LIVE, None, "personal_account_already_anchored"),
        (
            AnchorKind.RETIRED_PERSONAL,
            RETIREMENT_POLICY_REFUSE,
            "personal_tenant_retired",
        ),
        (AnchorKind.DELETED, None, "personal_anchor_enterprise_deleted"),
    ],
)
async def test_every_anchor_state_but_absent_refuses(
    store, as_tenant_provider, switch, anchor_db, logged, kind, policy, expected_reason
):
    """One state releases; the rest refuse, each with its own reason.

    Fail-closed by construction — the permissive answer is reachable from
    exactly one column value, not from the absence of a marker or from a parse
    that returned nothing.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(
        RETIRED_ENTERPRISE,
        retired=kind is not AnchorKind.LIVE,
        policy=policy,
    )
    service, provider, personal, _users, _user = _service(
        store, anchor=RETIRED_ENTERPRISE
    )

    assert redirect_params(await run_callback(service)) == {"error": ERROR_ORG_UNMAPPED}
    assert expected_reason in _reasons(logged)
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_an_anchor_naming_a_missing_enterprise_refuses(
    store, as_tenant_provider, switch, anchor_db, logged
):
    """A dangling anchor is a data fault, not an absent one.

    Reading it as "no anchor" would hand a broken account a fresh tenant, which
    is the permissive direction on the evidence that something is wrong.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store, anchor="99999999-9999-9999-9999-999999999999"
    )

    assert redirect_params(await run_callback(service)) == {"error": ERROR_ORG_UNMAPPED}
    assert "personal_anchor_enterprise_missing" in _reasons(logged)
    assert personal.provisioned == []


# =============================================================================
# R2 — one mover, and it never moves an anchor toward a personal tenant
# =============================================================================


async def test_an_unscoped_login_cannot_move_a_company_anchor_back(
    store, as_tenant_provider, switch, anchor_db, logged
):
    """The reverse-move defect, constructed exactly as it was reachable.

    The partial state is "anchor already moved to the company, personal binding
    not yet retired" — reachable from an interrupted re-anchor, from an exit-4
    ``re-anchor`` run, or from an IdP echo. Before the redesign an unscoped
    login turned that into a move back onto the personal tenant, which the code
    two lines away said must never happen implicitly.

    The binding is deliberately present and pointing at the personal enterprise,
    so a rule keyed on "is this the subject's own personal tenant?" alone would
    permit the move; only the direction rule refuses it.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(COMPANY_ENTERPRISE, name="Acme")
    personal = FakePersonalOrgRepository()
    # A LIVE binding that still names the personal tenant: the half-finished
    # re-anchor, not a fabricated fake.
    from faultmaven.modules.auth.contracts import PersonalOrgRecord

    personal.rows[("workos", SUBJECT)] = PersonalOrgRecord(
        organization_id="55555555-5555-5555-5555-555555555555",
        enterprise_id=PERSONAL_ENTERPRISE,
        provider_org_id="org_01PERSONAL",
        membership_confirmed=True,
    )
    service, provider, personal, users, user = _service(
        store, anchor=COMPANY_ENTERPRISE, personal=personal
    )

    params = redirect_params(await run_callback(service))

    # The login is refused and — the part that matters — the anchor did not
    # move. Before the redesign this callback returned a completion code with
    # the account quietly demoted back onto the personal tenant.
    assert params == {"error": ERROR_FAILED}
    assert user.enterprise_id == COMPANY_ENTERPRISE
    assert users.updated == []
    assert "enterprise_mismatch" in _reasons(logged)
    assert personal.retired == []


def test_the_direction_rule_is_one_expression():
    """The rule itself, without I/O, over every state it can be asked about."""
    absent = AnchorState(AnchorKind.ABSENT, None, None)
    live = AnchorState(AnchorKind.LIVE, "e", None)
    retired = AnchorState(AnchorKind.RETIRED_PERSONAL, "e", "refuse")
    dangling = AnchorState(AnchorKind.DANGLING, "e", None)

    # A set is always permitted; it takes nothing away.
    assert move_is_permitted(absent, destination_is_personal=True)
    assert move_is_permitted(absent, destination_is_personal=False)
    # Toward a personal enterprise, from an anchor that already exists: never.
    assert not move_is_permitted(live, destination_is_personal=True)
    assert not move_is_permitted(retired, destination_is_personal=True)
    # Toward a company: from a retirement, and from a live anchor only when the
    # caller has established it is the subject's own personal tenant.
    assert move_is_permitted(retired, destination_is_personal=False)
    assert move_is_permitted(live, destination_is_personal=False)
    # A broken anchor is not a licence to move.
    assert not move_is_permitted(dangling, destination_is_personal=False)


# =============================================================================
# R6 — a retired subject a company invites can sign in
# =============================================================================


@pytest.mark.parametrize(
    "anchor_is_retired", [True, False], ids=["retired-refuse", "released"]
)
async def test_a_retired_subject_invited_to_a_company_can_sign_in(
    store, as_tenant_provider, switch, anchor_db, anchor_is_retired
):
    """Both retirement policies leave an account a company can still adopt.

    ``refuse`` leaves the anchor on the retired enterprise and ``fresh-tenant``
    clears it; R2 makes both movable toward a company, so neither locks a person
    out of a job offer. The binding is gone in both cases — retirement deletes
    it — which is exactly the state the old re-anchor path could not handle.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    anchor = RETIRED_ENTERPRISE if anchor_is_retired else None
    if anchor_is_retired:
        await anchor_db(
            RETIRED_ENTERPRISE, retired=True, policy=RETIREMENT_POLICY_REFUSE
        )
    company = make_organization(
        organization_id=MAPPED_FM_ORG, enterprise_id=COMPANY_ENTERPRISE
    )
    user = make_returning_user()
    user.enterprise_id = anchor
    users = FakeUserRepository({("workos", SUBJECT): user})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        personal=FakePersonalOrgRepository(),  # the binding is gone
        mappings=FakeMappingRepository({("workos", "org_01HWORKOS"): MAPPED_FM_ORG}),
        orgs=FakeOrgRepository({MAPPED_FM_ORG: company}),
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params, params
    assert user.enterprise_id == COMPANY_ENTERPRISE
    assert users.updated and users.updated[-1] is user


async def test_the_login_tells_the_mover_when_the_destination_is_personal(
    store, as_tenant_provider, switch, anchor_db, monkeypatch
):
    """A wiring assertion, and it is here because the behaviour it protects has
    no independently reachable path today.

    The direction rule refuses "already anchored → a personal enterprise". On the
    org-less branch the pre-flight already refuses every non-absent anchor, and
    on the mapped branch the destination is a company, so the rule is currently
    defence in depth: mutating the login to pass ``destination_is_personal=False``
    changes no observable outcome. That is exactly the state in which a
    pass-through quietly rots, so what is pinned is the argument itself — the
    rule's own coverage lives in
    ``test_the_direction_rule_is_one_expression``.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    seen: list[dict] = []

    from faultmaven.modules.auth.domain.services import sso_login_service

    real = sso_login_service.move_account_anchor

    async def _spy(users, user, **kwargs):
        seen.append(kwargs)
        return await real(users, user, **kwargs)

    monkeypatch.setattr(sso_login_service, "move_account_anchor", _spy)

    service, _provider, _personal, _users, _user = _service(store, anchor=None)
    assert "code" in redirect_params(await run_callback(service))

    assert seen, "the login did not go through the shared anchor-mover"
    assert seen[-1]["destination_is_personal"] is True
