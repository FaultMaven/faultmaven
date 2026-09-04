"""``/debug/cases/{case_id}/causal-graph`` authenticates and checks case access.

Surfaced by the two-tenant surface probe as an observation rather than a leak:
the route took **no authentication** and performed **no case-access check** — it
loaded the row straight from the repository. Under the deployed cloud posture
PostgreSQL row-level security covered it, so what it served was bounded by a
layer the route never asked for; on a deployment without RLS (standalone on
SQLite, which is the default) it served any case to any caller, with no token at
all. It is registered only outside production, which bounds the blast radius but
is not the same as a check.

These pin both halves, and pin that the refusal is the same envelope an absent
case gets — a distinguishable one would confirm the case id exists.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData

DEBUG_PATH = "/debug/cases/{case_id}/causal-graph"

OWNER = "user-owner"
STRANGER = "user-stranger"
CASE_ID = "case_a1b2c3d4e5f6"  # `case_id` is bounded at 17 characters


class _CaseService:
    """Applies the real service's owner check, and records what it was asked.

    Not an ``AsyncMock``: the whole content of the fix is that ``user_id`` is
    passed at all, and a Mock returns the same case however it is called — so a
    route that dropped the argument would still look gated.
    """

    def __init__(self, owner_id: str):
        self._owner_id = owner_id
        self.calls: list[tuple[str, str | None]] = []

    async def get_case(self, case_id: str, user_id: str | None = None):
        self.calls.append((case_id, user_id))
        if case_id != CASE_ID:
            return None
        if user_id is not None and user_id != self._owner_id:
            return None
        return Case(
            case_id=case_id,
            title="causal graph access",
            state=CaseState.INVESTIGATING,
            user_id=self._owner_id,
            organization_id="org-1",
            description="d",
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="p",
            ),
        )


@pytest.fixture
def app():
    import os

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("ENVIRONMENT")
    # The route only exists outside production; pinned rather than inherited so
    # this module cannot pass by testing a route that was never registered.
    os.environ["ENVIRONMENT"] = "development"
    reset_settings()
    try:
        built = rebuild_app()
    finally:
        if previous is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = previous
        reset_settings()
    return built


@pytest.fixture
def case_service(app):
    service = _CaseService(owner_id=OWNER)
    app.state.case_service = service
    return service


def _as(app, user_id: str) -> TestClient:
    app.dependency_overrides[require_authentication] = lambda: DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
    )
    return TestClient(app)


@pytest.mark.integration
@pytest.mark.security
def test_the_route_is_registered_outside_production(app):
    """The gate must not pass by having no route to check."""
    assert DEBUG_PATH in {getattr(route, "path", None) for route in app.routes}


@pytest.mark.integration
@pytest.mark.security
def test_an_anonymous_caller_is_refused(app, case_service):
    """No token, no graph — and no lookup either."""
    response = TestClient(app).get(f"/debug/cases/{CASE_ID}/causal-graph")

    assert response.status_code in (401, 403), response.text[:300]
    assert case_service.calls == []


@pytest.mark.integration
@pytest.mark.security
def test_the_owner_still_gets_their_graph(app, case_service):
    """The positive control: without it, refusing everything would pass."""
    response = _as(app, OWNER).get(f"/debug/cases/{CASE_ID}/causal-graph")

    assert response.status_code == 200, response.text[:300]
    assert response.json().get("error") is None
    assert "causal_nodes" in response.json()
    assert case_service.calls == [(CASE_ID, OWNER)]


@pytest.mark.integration
@pytest.mark.security
def test_a_stranger_gets_the_answer_an_absent_case_gets(app, case_service):
    """Authenticated is not authorised, and the refusal is not an oracle."""
    client = _as(app, STRANGER)

    denied = client.get(f"/debug/cases/{CASE_ID}/causal-graph")
    absent = client.get("/debug/cases/case_000000000000/causal-graph")

    assert denied.json()["error"] == "case not found"
    assert "causal_nodes" not in denied.json()
    assert denied.status_code == absent.status_code
    assert denied.json()["error"] == absent.json()["error"]

    # And the check was actually delegated rather than re-derived here.
    assert case_service.calls[0] == (CASE_ID, STRANGER)
