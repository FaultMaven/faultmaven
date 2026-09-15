"""The degraded case service hides no case the real one would have returned.

``MinimalCaseService`` is what the container falls back to when no case
repository is available, so every line of it runs in production the moment the
repository is missing — and its own docstring claims it "mirrors
CaseService.list_user_cases".

It did not. Two blocks sat at the top of its filtering::

    if not getattr(filters, "include_deleted", False):
        ...drop CLOSED...
    if not getattr(filters, "include_terminal", False):
        ...drop RESOLVED and CLOSED...

``CaseListFilter`` declares neither field, so neither ``getattr`` could ever
see anything but its ``False`` default: the stand-in dropped every RESOLVED and
CLOSED case **unconditionally**, whatever the caller asked for. The real
``CaseService.list_user_cases`` applies ``state``, ``source``, ``team_id``,
``include_empty`` and the creation-date window, and excludes no terminal state
at all.

That is #1431's second half, and the opposite sign of the defect this file's
neighbours guard: not a filter declared and never applied, but a filter applied
that nobody requested. Same silence either way — 200, a plausible list, and a
resolved case that has vanished from it.

The no-filters branch said the same thing without a filter object to blame it
on: it narrowed to ``INQUIRY``/``INVESTIGATING`` and dropped empty cases, where
the real service passes ``state=None`` and ``include_empty=True``.

NOTE ON SCOPE: only ``list_user_cases`` is asserted here. ``count_user_cases``
carries the same filtering and was corrected with it, but it selects on
``case.owner_id`` — a field ``Case`` does not have — so it raises
``AttributeError`` for any user with at least one case. That is a separate,
pre-existing defect; fixing it does not belong in a contract removal, and a
test written around it would pin the wrong thing.
"""

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.models.api_models import CaseListFilter
from faultmaven.modules.case.domain.models import CaseState

pytestmark = [pytest.mark.unit]

OWNER = "user_owner"


@pytest.fixture
def service():
    # Via the package facade, not ``_container_impl`` directly: that module
    # re-enters ``faultmaven.container``, and importing it first is a cycle.
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_case_service()


@pytest.fixture
async def one_of_each_state(service):
    """One case in each of the four states, all owned by OWNER."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)

    cases = {}
    for state in (
        CaseState.INQUIRY,
        CaseState.INVESTIGATING,
        CaseState.RESOLVED,
        CaseState.CLOSED,
    ):
        case = await service.create_case(title=state.value, owner_id=OWNER)
        # ``Case`` sets ``validate_assignment`` and guards terminal transitions,
        # so the state is planted rather than transitioned into — this test is
        # about what the LISTING does with a state, not about how it was
        # reached.
        object.__setattr__(case, "state", state)
        cases[state] = case
    return cases


async def _titles(service, filters):
    page, total = await service.list_user_cases(OWNER, filters)
    titles = {c.title for c in page}
    assert total == len(titles), "the page and its total must describe one set"
    return titles


@pytest.mark.asyncio
async def test_terminal_cases_are_listed_with_a_default_filter(
    service, one_of_each_state
):
    """A plain ``CaseListFilter()`` hides nothing — RESOLVED and CLOSED included."""
    assert await _titles(service, CaseListFilter()) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }


@pytest.mark.asyncio
async def test_terminal_cases_are_listed_with_no_filter_at_all(
    service, one_of_each_state
):
    """``filters=None`` means "narrow on nothing", as it does on the real service."""
    assert await _titles(service, None) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }


@pytest.mark.asyncio
async def test_a_declared_filter_still_narrows(service, one_of_each_state):
    """The fix removes phantom predicates, not the real one.

    ``state`` IS a field on ``CaseListFilter`` and IS read by
    ``CaseService.list_user_cases``, so the stand-in must keep applying it —
    otherwise this change would have traded one half of the defect class for
    the other.
    """
    assert await _titles(service, CaseListFilter(state=CaseState.RESOLVED)) == {
        "resolved"
    }
    assert await _titles(service, CaseListFilter(state=CaseState.CLOSED)) == {"closed"}


@pytest.mark.asyncio
async def test_an_undeclared_filter_field_changes_nothing(service, one_of_each_state):
    """Setting the removed names on a filter object must not resurrect them.

    ``CaseListFilter`` ignores extras, so this is what a caller that still
    believes in ``include_terminal`` actually sends. Before the fix the answer
    was the same either way — which was the bug. After it, it is the same
    because nothing reads them.
    """
    believer = CaseListFilter(**{"include_terminal": False, "include_deleted": False})
    assert not hasattr(believer, "include_terminal")
    assert await _titles(service, believer) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }
