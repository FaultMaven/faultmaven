"""The filters that were never applied are GONE from the surface (#1413, #1416).

Three published things did nothing, and were removed rather than implemented:

* ``include_archived`` on ``GET /api/v1/cases`` — declared as a ``Query``,
  handed to ``CaseListFilter``, and dropped there by Pydantic's default
  ``extra='ignore'`` because the model declared no such field. No repository
  carried the predicate either, and there is no archived column to carry it
  against.
* ``CaseSearchRequest.user_id`` and ``CaseSearchRequest.organization_id`` —
  both published in ``docs/reference/api/openapi.json``, both read by nothing.

Two internal fields went with them, ``CaseListFilter.user_id`` and
``CaseListFilter.organization_id``, which cost no contract surface at all —
asserted below, because "internal" is a claim about the published document and
not a thing to take on trust.

**What these tests are and are not.** The removal's effect is on the DOCUMENT:
a client reading the contract is no longer told a filter exists. It is not on
the wire, and pretending otherwise would be its own small lie — FastAPI drops
an unknown query parameter silently and ``extra='ignore'`` drops an unknown
body key, so both can still be SENT. So the assertions here are: gone from the
route's resolved parameters, gone from the published document, gone from the
models, and — the half that matters at runtime — no longer reaching the filter
object the handler builds, which is the seam where the old silence lived.

The filter is CAPTURED from the handler rather than reconstructed: a fake
service records the object it was handed. A test that only checked status codes
would have passed throughout the entire life of the bug.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.dependencies.utils import get_flat_params
from fastapi.params import ParamTypes

from faultmaven.models.api_models import CaseListFilter, CaseSearchRequest
from faultmaven.modules.case.api.routes import router as case_router

_SPEC = json.loads(
    (
        Path(__file__).resolve().parents[5]
        / "docs"
        / "reference"
        / "api"
        / "openapi.json"
    ).read_text()
)


def _capturing_case_service(captured: list):
    """Records the filter (or search request) the route hands the service."""

    async def list_user_cases(user_id, filters=None):
        captured.append((user_id, filters))
        return [], 0

    async def search_cases(search_request, user_id=None):
        captured.append((user_id, search_request))
        return []

    return SimpleNamespace(list_user_cases=list_user_cases, search_cases=search_cases)


def _list_route_query_params() -> set[str]:
    """Every query parameter ``GET /api/v1/cases`` PUBLISHES.

    ``get_flat_params`` is what ``fastapi.openapi.utils.get_openapi_path`` calls
    to build an operation's parameter list, so this returns exactly what lands
    in ``openapi.json``. ``route.dependant.query_params`` — which an earlier
    version of this helper used — does NOT: it is the top-level dependant's own
    list, and ``get_flat_params`` flattens sub-dependants first. A dead filter
    reintroduced behind a ``Depends()`` would be published and accepted while a
    pin built on the top-level list stayed green, which is this file's own
    defect wearing a different hat.
    ``tests/unit/modules/case/test_declared_filters_reach_the_query.py`` reads
    it the same way, for the same reason.
    """
    for route in case_router.routes:
        if getattr(route, "name", None) == "list_cases" and "GET" in getattr(
            route, "methods", set()
        ):
            return {
                param.name
                for param in get_flat_params(route.dependant)
                if param.field_info.in_ == ParamTypes.query
            }
    raise AssertionError("GET /cases not found on the case router")


# ============================================================
# Gone from the route
# ============================================================


@pytest.mark.unit
def test_include_archived_is_not_a_query_parameter():
    assert "include_archived" not in _list_route_query_params()


@pytest.mark.unit
def test_the_surviving_query_parameters_are_exactly_these():
    """A pin, so the removal cannot quietly take a working filter with it.

    Every name here is one that reaches the repository query; #1413 is what
    happens when that stops being true and nothing notices.
    """
    assert _list_route_query_params() == {
        "state",
        "source",
        "team_id",
        "created_after",
        "created_before",
        "limit",
        "offset",
        "include_empty",
    }


@pytest.mark.unit
def test_include_archived_is_not_in_the_published_document():
    params = _SPEC["paths"]["/api/v1/cases"]["get"]["parameters"]
    assert "include_archived" not in {p["name"] for p in params}


# ============================================================
# Gone from the request models
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize("field", ["user_id", "organization_id"])
def test_case_search_request_no_longer_declares(field):
    assert field not in CaseSearchRequest.model_fields


@pytest.mark.unit
@pytest.mark.parametrize("field", ["user_id", "organization_id"])
def test_the_published_search_schema_no_longer_declares(field):
    schema = _SPEC["components"]["schemas"]["CaseSearchRequest"]
    assert field not in schema["properties"]


@pytest.mark.unit
def test_the_search_schema_still_declares_what_it_applies():
    """The other half of the pin: the removal took only the dead fields."""
    schema = _SPEC["components"]["schemas"]["CaseSearchRequest"]
    assert set(schema["properties"]) == {"query", "state", "team_id", "limit"}


@pytest.mark.unit
@pytest.mark.parametrize("field", ["user_id", "organization_id"])
def test_case_list_filter_no_longer_declares(field):
    assert field not in CaseListFilter.model_fields


@pytest.mark.unit
def test_case_list_filter_costs_no_contract_surface():
    """The claim that the two internal removals are free, checked.

    ``CaseListFilter`` is a service-layer model and is not a request body, so
    it never reaches ``components.schemas`` — which is why removing fields from
    it is internal cleanup riding along rather than part of the MAJOR bump. If
    it ever DOES become published, this fails and that judgement gets revisited.
    """
    assert "CaseListFilter" not in _SPEC["components"]["schemas"]


# ============================================================
# The list route still works without the removed constructor argument
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_list_route_still_serves(build_app, call_api):
    """The route no longer passes ``user_id=`` into ``CaseListFilter(...)``.

    That argument was redundant — ``list_user_cases`` takes the principal as
    its own argument and never read it off the filter — but "redundant" and
    "removable" are different claims, and this is the one that checks the
    second.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(app, "GET", "/api/v1/cases")

    assert response.status_code == 200
    assert len(captured) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_principal_still_reaches_the_service(build_app, call_api):
    """It arrives as the service's own argument, from the authenticated user.

    This is the whole reason the filter field was removable rather than a
    scoping control: the scope never came from the filter.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    await call_api(app, "GET", "/api/v1/cases")

    user_id, filters = captured[0]
    assert user_id == "user-1"
    assert not hasattr(filters, "user_id")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_surviving_filters_still_reach_the_filter(build_app, call_api):
    """The removal did not take a working control with it."""
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    await call_api(
        app,
        "GET",
        "/api/v1/cases",
        params={
            "state": "resolved",
            "source": "slack",
            "team_id": "team-7",
            "limit": 25,
            "offset": 50,
            "include_empty": "false",
        },
    )

    _, filters = captured[0]
    assert filters.state.value == "resolved"
    assert filters.source == "slack"
    assert filters.team_id == "team-7"
    assert filters.limit == 25
    assert filters.offset == 50
    assert filters.include_empty is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_client_still_sending_include_archived_is_not_carrying_a_filter(
    build_app, call_api
):
    """``?include_archived=true`` no longer becomes anything.

    It is still ACCEPTED — FastAPI drops an unknown query parameter silently and
    there is no way to make it refuse one, so the removal buys a truthful
    document rather than a rejection. What it must not do is arrive as a
    settable field on the object the handler builds, which is where it used to
    look like a filter to every reader of this code.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(
        app, "GET", "/api/v1/cases", params={"include_archived": "true"}
    )

    assert response.status_code == 200
    _, filters = captured[0]
    assert not hasattr(filters, "include_archived")
    assert "include_archived" not in filters.model_dump()


# ============================================================
# The search route no longer carries a second principal
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_search_body_carrying_user_id_carries_no_second_principal(
    build_app, call_api
):
    """The dangerous one.

    ``search_cases(search_request, user_id=None)`` already receives the
    AUTHENTICATED caller from the route, so ``search_request.user_id`` was a
    second, client-supplied user id beside it — inert, and a cross-tenant read
    the day anyone wired it up. After the removal the request the service is
    handed cannot carry one at all, and the only principal in the call is the
    authenticated one.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(
        app,
        "POST",
        "/api/v1/cases/search",
        json={
            "query": "db",
            "user_id": "somebody-else",
            "organization_id": "org_alpha",
        },
    )

    assert response.status_code == 200
    user_id, search_request = captured[0]
    assert user_id == "user-1"
    assert not hasattr(search_request, "user_id")
    assert not hasattr(search_request, "organization_id")
    assert set(search_request.model_dump()) == {"query", "state", "team_id", "limit"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_still_carries_what_it_applies(build_app, call_api):
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    await call_api(
        app,
        "POST",
        "/api/v1/cases/search",
        json={"query": "db", "state": "resolved", "team_id": "team-7", "limit": 5},
    )

    _, search_request = captured[0]
    assert search_request.query == "db"
    assert search_request.state.value == "resolved"
    assert search_request.team_id == "team-7"
    assert search_request.limit == 5
