"""What a retired subject's next login does (#1045 D8, ADR-016 D5/D8, ADR-017 D3).

The operator command decides; the login is where that decision becomes
behaviour, so this module drives the **login service** rather than the rows.
Every case goes through the real ``complete_callback``.

The decision is carried by **typed columns**, and ADR-017 changed which ones.
``users.enterprise_id`` is NOT NULL now — every account is anchored to exactly
one enterprise — so "released" can no longer be an absent anchor. It is the
operator's recorded choice instead: ``enterprises.deleted_at`` says the tenant
was fenced, and ``sso_personal_enterprises.retired_at`` /
``retirement_state`` say whose it was and what the next sign-in gets. A positive
value is the stronger spelling in any case: an absence can be produced by a
half-finished retirement, and a recorded ``fresh_tenant`` cannot.

What is pinned here:

* exactly one column value releases provisioning, and every other state — live,
  retired-with-``refuse``, deleted, dangling — refuses, so an unreadable or
  unexpected value can never produce the permissive answer;
* the refusals are told apart in the log, because their remedies are opposite;
* **no login moves an already-anchored account onto a personal enterprise**,
  with one authorised exception: the subject whose own tenant was retired with
  ``fresh-tenant``, moving onto the replacement that retirement authorised.
  Everything else is the reverse move the previous design allowed, where a
  partial state left by an interrupted re-anchor could be turned, by a user
  action, into a demotion back into the tenant the account had left.
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
    PersonalEnterpriseRecord,
)
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_FAILED,
    ERROR_ORG_UNMAPPED,
)
from tests.unit.modules.auth.test_sso_personal_tenant import (  # noqa: F401
    COMPANY_ENTERPRISE_ID,
    COMPANY_IDENTITY_SAME_SUBJECT,
    IDP_ORG,
    INDIVIDUAL,
    PERSONAL_ENTERPRISE,
    SUBJECT,
    FakeEnterpriseRepository,
    FakeMappingRepository,
    FakePersonalEnterpriseRepository,
    FakeProvider,
    FakeUserRepository,
    as_tenant_provider,
    build_service,
    isolate_settings,
    make_enterprise,
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
#: BOTH policies; what differs is whether the next sign-in may move off it.
RETIRED_ENTERPRISE = "77777777-7777-7777-7777-777777777777"


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


def _service(
    store, *, anchor, users=None, personal=None, mappings=None, enterprises=None
):
    user = make_returning_user()
    user.enterprise_id = anchor
    users = users or FakeUserRepository({("workos", SUBJECT): user})
    personal = personal if personal is not None else FakePersonalEnterpriseRepository()
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        mappings=mappings,
        enterprises=(
            enterprises
            if enterprises is not None
            else FakeEnterpriseRepository(answer_any=True)
        ),
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

    Under ADR-017 D3 the account is NOT unanchored while it waits — it stays on
    the enterprise the retirement fenced, and the recorded ``fresh_tenant``
    policy is what releases the next sign-in. Driven through the callback so it
    also covers the half that is easy to miss: the anchor has to MOVE, from a
    retired personal enterprise onto the new one, which is the single
    authorised move toward a personal tenant.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(
        RETIRED_ENTERPRISE, retired=True, policy=RETIREMENT_POLICY_FRESH_TENANT
    )
    service, provider, personal, users, user = _service(
        store, anchor=RETIRED_ENTERPRISE
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params, params
    assert len(personal.provisioned) == 1
    assert provider.provision_calls
    # The anchor MOVED off the retired enterprise onto the new one, and the move
    # was persisted.
    minted = personal.provisioned[0]["enterprise_id"]
    assert user.enterprise_id == minted
    assert user.enterprise_id != RETIRED_ENTERPRISE
    assert users.updated and users.updated[-1] is user
    # The new binding was not retired on the way through.
    assert personal.retired == []


async def test_a_fresh_tenant_retirement_repoints_the_one_subject_row(
    store, as_tenant_provider, switch, anchor_db
):
    """``subject`` is the primary key, so there is one row and it moves.

    Inserting beside the retired row is impossible, and leaving the retired one
    in place would tell the next anchor read that the tenant this sign-in just
    created is itself retired.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(
        RETIRED_ENTERPRISE, retired=True, policy=RETIREMENT_POLICY_FRESH_TENANT
    )
    personal = FakePersonalEnterpriseRepository(
        retired_rows={
            ("workos", SUBJECT): PersonalEnterpriseRecord(
                enterprise_id=RETIRED_ENTERPRISE,
                provider_org_id="org_01OLD",
                membership_confirmed=True,
            )
        }
    )
    service, _provider, personal, _users, _user = _service(
        store, anchor=RETIRED_ENTERPRISE, personal=personal
    )

    assert "code" in redirect_params(await run_callback(service))

    assert list(personal.rows) == [("workos", SUBJECT)]
    assert personal.retired_rows == {}
    assert personal.rows[("workos", SUBJECT)].enterprise_id != RETIRED_ENTERPRISE


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
async def test_every_anchor_state_but_a_fresh_tenant_retirement_refuses(
    store, as_tenant_provider, switch, anchor_db, logged, kind, policy, expected_reason
):
    """One recorded value releases; the rest refuse, each with its own reason.

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
    """A dangling anchor is a data fault, not a released one.

    Reading it as "released" would hand a broken account a fresh tenant, which
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


async def test_an_unanchored_account_still_releases(
    store, as_tenant_provider, switch, anchor_db
):
    """The degenerate case, kept because the rule has to answer for it.

    ``users.enterprise_id`` is NOT NULL, so no persisted account is here — but
    an in-memory account that has not been anchored yet reaches the same rule,
    and refusing it would make a first sign-in impossible.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, _provider, personal, _users, user = _service(store, anchor=None)

    assert "code" in redirect_params(await run_callback(service))
    assert len(personal.provisioned) == 1
    assert user.enterprise_id == personal.provisioned[0]["enterprise_id"]


# =============================================================================
# R2 — one mover, and it moves onto a personal tenant only when authorised
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
    permit the move; the direction arm of ``move_is_permitted`` is what refuses
    it.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(COMPANY_ENTERPRISE_ID, name="Acme")
    personal = FakePersonalEnterpriseRepository()
    # A LIVE binding that still names the personal tenant: the half-finished
    # re-anchor, not a fabricated fake.
    personal.rows[("workos", SUBJECT)] = PersonalEnterpriseRecord(
        enterprise_id=PERSONAL_ENTERPRISE,
        provider_org_id="org_01PERSONAL",
        membership_confirmed=True,
    )
    service, provider, personal, users, user = _service(
        store, anchor=COMPANY_ENTERPRISE_ID, personal=personal
    )

    params = redirect_params(await run_callback(service))

    # The login is refused and — the part that matters — the anchor did not
    # move. Before the redesign this callback returned a completion code with
    # the account quietly demoted back onto the personal tenant.
    assert params == {"error": ERROR_FAILED}
    assert user.enterprise_id == COMPANY_ENTERPRISE_ID
    assert users.updated == []
    assert "enterprise_mismatch" in _reasons(logged)
    assert personal.retired == []


def test_the_direction_rule_is_one_expression():
    """The rule itself, without I/O, over every state it can be asked about.

    ``own_live_personal`` is an argument here rather than a second guard in the
    mover. It used to live beside this function, which made "the rule is one
    expression" false: forcing this to True still left the reverse move refused,
    by the other guard, so the reverse-move test was passing for a reason its
    own docstring denied.
    """
    absent = AnchorState(AnchorKind.ABSENT, None, None)
    live = AnchorState(AnchorKind.LIVE, "e", None)
    retired_refuse = AnchorState(
        AnchorKind.RETIRED_PERSONAL, "e", RETIREMENT_POLICY_REFUSE
    )
    retired_fresh = AnchorState(
        AnchorKind.RETIRED_PERSONAL, "e", RETIREMENT_POLICY_FRESH_TENANT
    )
    deleted = AnchorState(AnchorKind.DELETED, "e", None)
    dangling = AnchorState(AnchorKind.DANGLING, "e", None)

    # A set is always permitted; it takes nothing away.
    assert move_is_permitted(absent, destination_is_personal=True)
    assert move_is_permitted(absent, destination_is_personal=False)

    # Toward a personal enterprise, from an anchor that already exists: only the
    # subject whose own tenant an operator retired with ``fresh-tenant``, moving
    # onto the replacement that retirement authorised.
    assert move_is_permitted(retired_fresh, destination_is_personal=True)
    assert not move_is_permitted(retired_refuse, destination_is_personal=True)
    assert not move_is_permitted(live, destination_is_personal=True)
    # And not even when the caller says the current anchor is the subject's own:
    # a LIVE personal anchor is #1320's switch, whose destination is a company.
    assert not move_is_permitted(
        live, destination_is_personal=True, own_live_personal=True
    )
    assert not move_is_permitted(deleted, destination_is_personal=True)
    assert not move_is_permitted(dangling, destination_is_personal=True)

    # Toward a company, from a retirement: always, under either policy. That is
    # R6 — a retired subject a company invites must not be stranded.
    assert move_is_permitted(retired_refuse, destination_is_personal=False)
    assert move_is_permitted(retired_fresh, destination_is_personal=False)

    # Toward a company, from a LIVE anchor: only when the caller has established
    # the anchor is the subject's OWN personal tenant. A live company
    # affiliation stays put, which is what stops an IdP claim moving an account
    # between customers.
    assert move_is_permitted(
        live, destination_is_personal=False, own_live_personal=True
    )
    assert not move_is_permitted(live, destination_is_personal=False)
    assert not move_is_permitted(
        live, destination_is_personal=False, own_live_personal=False
    )

    # Neither a removed company nor a broken row is a licence to move, whatever
    # the caller claims about it.
    for state in (deleted, dangling):
        assert not move_is_permitted(state, destination_is_personal=False)
        assert not move_is_permitted(
            state, destination_is_personal=False, own_live_personal=True
        )


# =============================================================================
# R6 — a retired subject a company invites can sign in
# =============================================================================


@pytest.mark.parametrize(
    "policy",
    [RETIREMENT_POLICY_REFUSE, RETIREMENT_POLICY_FRESH_TENANT],
    ids=["retired-refuse", "retired-fresh-tenant"],
)
async def test_a_retired_subject_invited_to_a_company_can_sign_in(
    store, as_tenant_provider, switch, anchor_db, policy
):
    """Both retirement policies leave an account a company can still adopt.

    Neither locks a person out of a job offer: R2 makes a retired anchor movable
    toward a company whatever the operator chose for the org-less path. The
    binding is retired in both cases, which is exactly the state the old
    re-anchor path could not handle.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    await anchor_db(RETIRED_ENTERPRISE, retired=True, policy=policy)
    company = make_enterprise(
        enterprise_id=COMPANY_ENTERPRISE_ID, name="Acme", slug="acme"
    )
    user = make_returning_user()
    user.enterprise_id = RETIRED_ENTERPRISE
    users = FakeUserRepository({("workos", SUBJECT): user})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        personal=FakePersonalEnterpriseRepository(),  # the binding is retired
        mappings=FakeMappingRepository({("workos", IDP_ORG): COMPANY_ENTERPRISE_ID}),
        enterprises=FakeEnterpriseRepository(
            enterprises={COMPANY_ENTERPRISE_ID: company}
        ),
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params, params
    assert user.enterprise_id == COMPANY_ENTERPRISE_ID
    assert users.updated and users.updated[-1] is user


async def test_the_login_tells_the_mover_when_the_destination_is_personal(
    store, as_tenant_provider, switch, anchor_db, monkeypatch
):
    """The argument the one authorised personal move turns on.

    ``destination_is_personal`` is established from this subject's own binding,
    never from an enterprise's name or slug, and it is what stops the
    ``fresh_tenant`` release from admitting a move onto somebody ELSE's personal
    tenant. Pinning the argument keeps the pass-through from rotting while the
    rule's own coverage lives in ``test_the_direction_rule_is_one_expression``.
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
