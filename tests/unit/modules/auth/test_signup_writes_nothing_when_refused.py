"""Sign-up derives the enterprise from a VERIFIED domain, and refuses before writing.

Three findings from the fm#1353 review, all on the ``SSO_JIT_PERSONAL_TENANT_ENABLED``
sign-up path and all about the order in which a login decides things.

**The domain must be verified (D3).** ADR-017 D3 derives the enterprise from "the
domain of the IdP-verified email". Neither arm read ``identity.email_verified``,
so with the switch on an identity carrying an unverified ``anyone@acme.com``
joined Acme's enterprise — the one thing that makes joining a domain enterprise
safe is that the IdP vouched for the address.

**A refused login writes nothing — on BOTH arms.** The personal arm evaluates the
anchor rule before provisioning. The domain arm did not: it created the
``enterprises(domain=…)`` row and only then reached the anchor check, so an
employee already anchored elsewhere who signed in org-less left a live enterprise
for their company's domain behind. That row then captures every later org-less
sign-up from the domain, because it is exactly the row those logins look for.

**A fresh-tenant retirement must survive its own provisioning.** The
``fresh_tenant`` release is recorded on the subject row; provisioning the
replacement re-points that same row and clears the retirement. The later anchor
move then reads the OLD enterprise, finds no subject row naming it, classifies it
DELETED (a removed company) and refuses — permanently, because every retry
repeats it. The account the operator authorised to start over could never sign in
again.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.persistence.account_anchor import AnchorKind, AnchorState
from faultmaven.modules.auth.domain.services import sso_login_service as svc
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_FAILED,
    SSOLoginService,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

SUBJECT = "user_01HZZZ"
PROVIDER = "workos"


def _identity(**overrides):
    identity = SimpleNamespace(
        provider=PROVIDER,
        provider_user_id=SUBJECT,
        organization_id=None,
        email="ada@acme.com",
        email_verified=True,
        display_name="Ada",
        provider_session_id=None,
    )
    for key, value in overrides.items():
        setattr(identity, key, value)
    return identity


def _service(*, user=None, enterprises=None, personal=None):
    service = object.__new__(SSOLoginService)
    service._users = MagicMock()
    service._users.get_by_sso = AsyncMock(return_value=user)
    service._users.get_by_email = AsyncMock(return_value=None)
    service._enterprises = enterprises if enterprises is not None else MagicMock()
    service._personal_enterprises = personal if personal is not None else MagicMock()
    return service


# ---------------------------------------------------------------------------
# A10 — the domain comes from the IdP-VERIFIED email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verified", [False, None])
async def test_an_unverified_address_never_yields_an_enterprise(verified, monkeypatch):
    """Both arms, one check: the derivation's whole premise is the vouching."""
    enterprises = MagicMock()
    enterprises.get_or_create_for_domain = AsyncMock()
    personal = MagicMock()
    personal.get = AsyncMock(return_value=None)
    service = _service(enterprises=enterprises, personal=personal)

    enterprise, error = await service._resolve_signup_enterprise(
        _identity(email_verified=verified)
    )

    assert enterprise is None
    assert error == ERROR_FAILED
    enterprises.get_or_create_for_domain.assert_not_awaited()


async def test_a_verified_address_still_resolves(monkeypatch):
    """The control: the check must refuse the unverified one, not everyone."""
    resolved = SimpleNamespace(enterprise_id="ent_acme")
    enterprises = MagicMock()
    enterprises.find_live_by_domain = AsyncMock(return_value=resolved)
    enterprises.get_or_create_for_domain = AsyncMock(return_value=resolved)
    service = _service(enterprises=enterprises)
    monkeypatch.setattr(
        service, "_bind_and_verify_enterprise", AsyncMock(return_value=(resolved, None))
    )

    enterprise, error = await service._resolve_signup_enterprise(_identity())

    assert error is None
    assert enterprise is resolved


# ---------------------------------------------------------------------------
# A9 — the domain arm evaluates the anchor rule before it writes
# ---------------------------------------------------------------------------


async def test_an_employee_anchored_elsewhere_leaves_no_domain_enterprise(monkeypatch):
    """The stray row is the finding, not the refusal.

    The refusal already happened, one step later, in ``_ensure_enterprise_anchor``.
    What did not happen was the write being skipped, and the row it left is the
    one every later org-less sign-up from that domain resolves to.
    """
    anchored_elsewhere = SimpleNamespace(
        user_id="user_1",
        enterprise_id="ent_other_company",
        deleted_at=None,
        is_active=True,
        sso_provider=PROVIDER,
        sso_provider_id=SUBJECT,
    )
    enterprises = MagicMock()
    enterprises.get_or_create_for_domain = AsyncMock()
    enterprises.find_live_by_domain = AsyncMock(return_value=None)
    service = _service(user=anchored_elsewhere, enterprises=enterprises)

    monkeypatch.setattr(
        svc,
        "read_anchor",
        AsyncMock(return_value=AnchorState(AnchorKind.LIVE, "ent_other_company", None)),
    )
    service._personal_enterprises.find_by_enterprise = AsyncMock(return_value=False)

    enterprise, error = await service._resolve_signup_enterprise(_identity())

    assert enterprise is None
    assert error is not None
    enterprises.get_or_create_for_domain.assert_not_awaited(), (
        "a login that could never be admitted created its company's enterprise "
        "anyway; that row captures every later org-less sign-up for the domain"
    )


async def test_an_employee_of_an_existing_domain_enterprise_is_not_refused(monkeypatch):
    """The control, and the reason the check is conditional on the write.

    An account already anchored to the domain's enterprise is a returning
    employee. Nothing is created for them, so there is nothing to refuse before —
    and refusing a LIVE anchor unconditionally would lock every one of them out.
    """
    resolved = SimpleNamespace(enterprise_id="ent_acme")
    already_here = SimpleNamespace(
        user_id="user_1",
        enterprise_id="ent_acme",
        deleted_at=None,
        is_active=True,
        sso_provider=PROVIDER,
        sso_provider_id=SUBJECT,
    )
    enterprises = MagicMock()
    enterprises.find_live_by_domain = AsyncMock(return_value=resolved)
    enterprises.get_or_create_for_domain = AsyncMock(return_value=resolved)
    service = _service(user=already_here, enterprises=enterprises)
    monkeypatch.setattr(
        service, "_bind_and_verify_enterprise", AsyncMock(return_value=(resolved, None))
    )

    enterprise, error = await service._resolve_signup_enterprise(_identity())

    assert error is None
    assert enterprise is resolved
