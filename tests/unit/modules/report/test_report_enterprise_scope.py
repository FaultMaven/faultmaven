"""Report-route enterprise scoping (ADR-010 P2c, ADR-017).

``validate_enterprise_access`` refuses a case whose enterprise is not the one the
request is bound to. It used to answer that question by resolving the enterprise
through ``TenantProvider.get_current_enterprise(current_user, enterprise_id=
get_current_enterprise_id())`` and comparing ``enterprise.enterprise_id`` to the
case — a whole ``enterprises`` row read, per request, per report call, to compare
an id to itself. Nothing of the row was ever used, and the provider could be
absent, which made the check conditional on wiring rather than on the tenant.

It is now the comparison it always was. These cases pin both directions and the
absence of the round-trip: the binding is the authority, and no repository is
consulted to reach it.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.report.api import routes
from faultmaven.modules.report.api.routes import validate_enterprise_access

CTX_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
OTHER_ENTERPRISE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _current_user():
    user = MagicMock()
    user.user_id = "user_123"
    user.email = "user@example.com"
    return user


def test_a_case_in_the_bound_enterprise_is_admitted():
    set_current_enterprise_id(CTX_ENTERPRISE)
    # No exception == access granted for a case in the bound enterprise.
    validate_enterprise_access(_current_user(), case_enterprise_id=CTX_ENTERPRISE)


@pytest.mark.security
def test_a_case_from_another_enterprise_is_refused():
    set_current_enterprise_id(CTX_ENTERPRISE)
    with pytest.raises(HTTPException) as exc:
        validate_enterprise_access(_current_user(), case_enterprise_id=OTHER_ENTERPRISE)
    assert exc.value.status_code == 403


@pytest.mark.security
def test_the_binding_is_what_decides_not_the_caller_object():
    """A caller claiming another enterprise changes nothing.

    The old shape read the answer off ``current_user`` (via the provider's anchor
    comparison), which is a value the request front door had already derived from
    the same binding. Here the caller object carries no enterprise at all and the
    verdict is unchanged, which is what "the binding is the authority" means.
    """
    set_current_enterprise_id(CTX_ENTERPRISE)
    liar = _current_user()
    liar.enterprise_id = OTHER_ENTERPRISE

    validate_enterprise_access(liar, case_enterprise_id=CTX_ENTERPRISE)
    with pytest.raises(HTTPException):
        validate_enterprise_access(liar, case_enterprise_id=OTHER_ENTERPRISE)


def test_a_case_naming_no_enterprise_is_not_refused_here():
    """The case gate above already resolved it; this step adds a comparison only."""
    set_current_enterprise_id(CTX_ENTERPRISE)
    validate_enterprise_access(_current_user(), case_enterprise_id=None)


def test_the_check_reads_no_enterprise_row():
    """The efficiency claim, asserted rather than described.

    ``get_tenant_provider`` is gone from this module: there is no dependency left
    that could resolve an ``enterprises`` row on this path, so a future re-add
    fails here rather than quietly costing a query per report call.
    """
    assert not hasattr(routes, "get_tenant_provider")
    assert not hasattr(routes, "check_tenant_provider_available")
