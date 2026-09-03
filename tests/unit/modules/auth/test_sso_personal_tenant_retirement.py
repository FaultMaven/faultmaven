"""What a retired subject's next org-less login does (#1045 D8).

The operator command decides; the login is where that decision becomes
behaviour, so this module drives the **login service** rather than the rows.
Every case here goes through the real ``complete_callback``.

The decision cannot be expressed by clearing the account's anchor —
``users.enterprise_id`` is NOT NULL (migration 006) — so a retirement records a
**marker** on the retired enterprise and the login reads it. That makes the
marker a permission, which is why most of what follows is about the ways it must
NOT be honoured:

* a marker written for a different subject releases nobody;
* a marker naming a policy this version does not implement releases nobody;
* a lookup that FAILS releases nobody — "the table did not answer" must never
  read as "the anchor was retired";
* an enterprise with no marker behaves exactly as it did before any of this
  existed.

Releasing an anchor is the permissive outcome, so every one of those answers the
refusal, and the one path that releases requires a key this login re-derives
from its own identity and matches.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.config.settings import TenantProvider
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
    PersonalTenantRetirement,
)
from faultmaven.modules.auth.domain.personal_tenant import personal_tenant_key
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_ORG_UNMAPPED,
)
from tests.unit.modules.auth.test_sso_personal_tenant import (  # noqa: F401
    INDIVIDUAL,
    PERSONAL_ENTERPRISE,
    PERSONAL_FM_ORG,
    SUBJECT,
    FakeOrgRepository,
    FakePersonalOrgRepository,
    FakeProvider,
    FakeUserRepository,
    as_tenant_provider,
    build_service,
    isolate_settings,
    make_returning_user,
    redirect_params,
    restore_tenant_context,
    run_callback,
    store,
    switch,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

#: The enterprise an operator retired. The account is still anchored to it,
#: because a retirement cannot clear an anchor.
RETIRED_ENTERPRISE = "77777777-7777-7777-7777-777777777777"

KEY = personal_tenant_key("workos", SUBJECT)


@pytest.fixture
def logged(monkeypatch):
    """Every event the login service logged, as ``(event, kwargs)``.

    Asserted on the **structured fields** rather than on captured stdout.
    structlog's sink is process-wide configuration that other modules in the
    same suite change, so a substring test over stdout passes or fails on
    collection order — which it did, green alone and red in the full selection.
    The real logger still runs underneath, so nothing about the log itself is
    suppressed.
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


def _events(records) -> list[str]:
    return [event for event, _kwargs in records]


def _marker(
    *,
    policy: str = RETIREMENT_POLICY_FRESH_TENANT,
    key: str = KEY,
    provider: str = "workos",
) -> PersonalTenantRetirement:
    return PersonalTenantRetirement(
        provider=provider,
        key=key,
        policy=policy,
        organization_id=PERSONAL_FM_ORG,
        retired_at=datetime(2026, 9, 3, tzinfo=UTC).isoformat(),
    )


def _retired_user():
    """The account as a retirement leaves it: alive, anchored to the retired
    enterprise, with no subject binding."""
    user = make_returning_user()
    user.enterprise_id = RETIRED_ENTERPRISE
    return user


def _service(store, *, retirements=None, retirement_error=None, orgs=None):
    user = _retired_user()
    users = FakeUserRepository({("workos", SUBJECT): user})
    personal = FakePersonalOrgRepository(
        retirements=retirements, retirement_error=retirement_error
    )
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        orgs=orgs if orgs is not None else FakeOrgRepository(answer_any=True),
    )
    return service, provider, personal, users, user


# =============================================================================
# The flag decides, and the login is where it decides
# =============================================================================


async def test_a_refuse_retirement_refuses_with_its_own_reason(
    store, as_tenant_provider, switch, logged
):
    """Not ``personal_account_already_anchored``: an operator reading the logs
    has to be able to tell "an employee arrived unscoped" from "I retired
    this"."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store,
        retirements={RETIRED_ENTERPRISE: _marker(policy=RETIREMENT_POLICY_REFUSE)},
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "personal_tenant_retired" in _reasons(logged)
    assert "personal_account_already_anchored" not in _reasons(logged)
    # And nothing was provisioned on either side.
    assert provider.provision_calls == []
    assert personal.provisioned == []


async def test_a_fresh_tenant_retirement_provisions_a_new_one(
    store, as_tenant_provider, switch
):
    """The whole point of the flag: the subject starts over.

    Driven through the callback, so it also covers the half that is easy to
    miss — the account is still anchored to the RETIRED enterprise when the new
    tenant is written, and the membership write would refuse it
    ``enterprise_mismatch`` unless the stale anchor is adopted.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, users, user = _service(
        store, retirements={RETIRED_ENTERPRISE: _marker()}
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params, params
    assert len(personal.provisioned) == 1
    minted = personal.provisioned[0]["organization_id"]
    assert minted != PERSONAL_FM_ORG
    assert provider.provision_calls  # the IdP organization was minted too
    # The anchor moved onto the tenant this login resolved, and it was
    # PERSISTED — an in-memory move would meet the same mismatch next login.
    assert user.enterprise_id == PERSONAL_ENTERPRISE
    assert users.updated and users.updated[-1] is user
    # The NEW binding was not retired on the way through: retiring it would
    # strand the tenant this login just created.
    assert personal.retired == []


# =============================================================================
# Every way the marker must NOT be honoured
# =============================================================================


async def test_a_marker_for_a_different_subject_releases_nobody(
    store, as_tenant_provider, switch, logged
):
    """The key is what binds a marker to one person. An operator who stamps the
    wrong enterprise must not hand its occupants a self-service tenant."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store,
        retirements={
            RETIRED_ENTERPRISE: _marker(key=personal_tenant_key("workos", "user_OTHER"))
        },
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "personal_account_already_anchored" in _reasons(logged)
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_a_marker_from_another_provider_releases_nobody(
    store, as_tenant_provider, switch
):
    """Two providers' identically-spelled subjects are different people."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store, retirements={RETIRED_ENTERPRISE: _marker(provider="okta")}
    )

    assert redirect_params(await run_callback(service)) == {"error": ERROR_ORG_UNMAPPED}
    assert personal.provisioned == []


async def test_an_unknown_policy_releases_nobody(store, as_tenant_provider, switch):
    """Fail closed on a value this version does not implement — a marker that
    was hand-edited, or written by a later version, must not be guessed at."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store, retirements={RETIRED_ENTERPRISE: _marker(policy="delete_everything")}
    )

    assert redirect_params(await run_callback(service)) == {"error": ERROR_ORG_UNMAPPED}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_a_failed_retirement_lookup_releases_nobody(
    store, as_tenant_provider, switch, logged
):
    """ "The enterprises table did not answer" must never read as "retired"."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(
        store, retirement_error=RuntimeError("enterprises unavailable")
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "sso_personal_retirement_lookup_failed" in _events(logged)
    assert personal.provisioned == []


async def test_an_unmarked_anchor_behaves_exactly_as_before(
    store, as_tenant_provider, switch, logged
):
    """The employee-arriving-unscoped case, unchanged by any of this."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    service, provider, personal, _users, _user = _service(store, retirements={})

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "personal_account_already_anchored" in _reasons(logged)
    assert personal.provisioned == []
    assert provider.provision_calls == []
    # The lookup was made against the account's own anchor and nothing else.
    assert personal.retirement_lookups == [RETIRED_ENTERPRISE]


async def test_an_unanchored_account_never_reaches_the_retirement_lookup(
    store, as_tenant_provider, switch
):
    """No anchor, nothing to release: the marker read is not on the hot path."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    user = make_returning_user()
    user.enterprise_id = None
    users = FakeUserRepository({("workos", SUBJECT): user})
    personal = FakePersonalOrgRepository()
    service = build_service(
        store,
        provider=FakeProvider(INDIVIDUAL),
        users=users,
        personal=personal,
        orgs=FakeOrgRepository(answer_any=True),
    )

    assert "code" in redirect_params(await run_callback(service))
    assert personal.retirement_lookups == []
