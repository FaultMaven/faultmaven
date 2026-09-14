"""The degraded case service honours the creation-date window, like the real one.

``MinimalCaseService`` is what the container falls back to when no case
repository is available, so every line of it runs in production the moment the
repository is missing — and its own docstring claims it "mirrors
CaseService.list_user_cases".

When the date bounds were first added they went into the route, the service, the
contract and all four repositories, and NOT into this stand-in. A degraded
server would then have answered `GET /cases?created_after=...` with 200 and the
UNFILTERED list: no error, no log line, nothing to notice — which is the exact
silence the bounds were added to end, reproduced in the one code path nobody
looks at.

The window is `[created_after, created_before)`, and `list_user_cases` and
`count_user_cases` must apply the same one, or the page and its count describe
different sets.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.models.api_models import CaseListFilter

pytestmark = [pytest.mark.unit]

OWNER = "user_owner"


@pytest.fixture
def service():
    # Via the package facade, not ``_container_impl`` directly: that module
    # re-enters ``faultmaven.container``, and importing it first is a cycle.
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_case_service()


@pytest.fixture
async def three_days(service):
    """Three cases, created on 10, 11 and 12 September."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)

    created = []
    for day in (10, 11, 12):
        case = await service.create_case(title=f"Day {day}", owner_id=OWNER)
        object.__setattr__(
            case, "created_at", datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc)
        )
        created.append(case)
    return created


async def _titles(service, **bounds):
    cases, _ = await service.list_user_cases(OWNER, CaseListFilter(**bounds))
    return {getattr(c, "title", None) for c in cases}


@pytest.mark.asyncio
async def test_the_window_actually_filters(service, three_days):
    """A bound narrows the result rather than being ignored."""
    assert await _titles(service) == {"Day 10", "Day 11", "Day 12"}

    narrowed = await _titles(
        service, created_after=datetime(2026, 9, 11, tzinfo=timezone.utc)
    )
    assert narrowed == {"Day 11", "Day 12"}


@pytest.mark.asyncio
async def test_the_upper_bound_is_exclusive(service, three_days):
    """`[after, before)` — a case created exactly on the upper bound is out."""
    window = await _titles(
        service,
        created_after=datetime(2026, 9, 10, tzinfo=timezone.utc),
        created_before=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert window == {"Day 10", "Day 11"}


@pytest.mark.asyncio
async def test_a_bound_carrying_an_offset_answers_like_its_utc_spelling(
    service, three_days
):
    """One instant, two spellings, one answer — here too."""
    ist = timezone(timedelta(hours=5, minutes=30))
    via_utc = await _titles(
        service, created_after=datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)
    )
    via_ist = await _titles(
        service, created_after=datetime(2026, 9, 11, 12, 0, tzinfo=ist)
    )
    assert via_utc == via_ist == {"Day 11", "Day 12"}


@pytest.mark.asyncio
async def test_no_bounds_still_lists_everything(service, three_days):
    """The stand-in must not invent a default window."""
    assert len(await _titles(service, limit=50)) == 3
