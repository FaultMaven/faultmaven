"""POST /cases/sessions/{session_id}/resume/{case_id} — the route's half (#1390, #1393, #1398).

This route names two resources and needs a check for each;
`faultmaven/api/routes/sessions.py` states the rule: the case gate "covers the
case named in the path and nothing else … Both halves are needed; neither is
sufficient". It originally had neither.

They now live in different places, deliberately:

* **The case** is gated inside `CaseService.link_session_to_case`, so every
  caller of that `ICaseService` member inherits it rather than just this route
  — see `TestTheLinkGatesTheCase` in the service tests. What is asserted HERE
  is that the route hands the member the caller, and maps its two answers
  correctly: `NotFoundError` -> 404, a falsy return -> 500.
* **The session** is gated here, because the session is the route's own
  argument and the service never sees it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import NotFoundError, ServiceException

# Both marks at module level. The vacuity control is not optional coverage —
# under `pytest -m security` a lone refusal assertion passes for a route that
# is not mounted, a trailing-slash redirect, or a dependency that raised.
pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SESSION_ID = "sess-resume-0001"
CASE_ID = "case-123"
CALLER = "user-1"
OWNER = "user-owner"
PATH = f"/api/v1/cases/sessions/{SESSION_ID}/resume/{CASE_ID}"


def _case_service(*, reachable=True, resume=True):
    """Both members the route touches, mirroring the real signatures.

    `get_case` HONOURS `owner_only` rather than omitting it, so narrowing the
    route's early gate asserts "the teammate was refused" rather than surfacing
    as an incidental TypeError -> 500. `resume_case_in_session` takes three
    arguments: a stand-in accepting `**kwargs` would let the route stop
    forwarding the caller and still pass, which is the omission that left the
    member ungated in the first place.
    """
    service = SimpleNamespace()

    async def get_case(case_id, user_id=None, *, owner_only=False):
        if case_id != CASE_ID or not reachable:
            return None
        # A teammate holding a share: visible on the read arm, gone under
        # `owner_only`. The route must use the read arm.
        if owner_only and user_id != OWNER:
            return None
        return SimpleNamespace(case_id=CASE_ID, user_id=OWNER)

    service.get_case = get_case
    if isinstance(resume, Exception):
        service.resume_case_in_session = AsyncMock(side_effect=resume)
    else:
        service.resume_case_in_session = AsyncMock(return_value=resume)
    return service


def _session(user_id=CALLER):
    return SimpleNamespace(session_id=SESSION_ID, user_id=user_id)


async def test_forwards_the_caller_to_the_gate(build_app, call_api):
    """The route's whole contribution to the case half.

    Dropping `current_user.user_id` here resolves the case unscoped, which is
    how the member came to accept any case from any caller (#1393).
    """
    service = _case_service()
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    service.resume_case_in_session.assert_awaited_once_with(CASE_ID, SESSION_ID, CALLER)


async def test_a_case_the_caller_cannot_reach_is_a_404(build_app, call_api):
    """The route's own early gate, which exists for ORDERING — see the handler.

    It also spares a session lookup for a caller who was never going to pass.
    """
    service = _case_service(reachable=False)
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Case not found or resume not permitted"
    service.resume_case_in_session.assert_not_awaited()


async def test_the_services_access_verdict_is_a_404_not_a_500(build_app, call_api):
    """The authoritative gate is in `link_session_to_case`, and it RAISES.

    The handler's bare `except` would otherwise turn that into a 500 and blame
    the server for a request the caller was simply not allowed to make.
    """
    service = _case_service(resume=NotFoundError("Case", CASE_ID))
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text


async def test_an_unevaluable_session_gate_is_a_503(build_app, call_api):
    """`get_session` RAISES rather than returning None when it cannot answer.

    Treating it as a nullable return let `ServiceException("Session store not
    configured")` reach the bare handler and answer 500 on a request the server
    could not evaluate. `sessions.py` states the rule: an unevaluable gate is a
    503, "so nothing is served".
    """
    service = _case_service()
    app = build_app(
        session=ServiceException("Session store not configured"),
        case_service=service,
    )

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 503, response.text
    service.resume_case_in_session.assert_not_awaited()


async def test_refuses_a_session_that_is_not_the_callers(build_app, call_api):
    """The half the service cannot see.

    Naming another user's session retargets their current-case pointer; their
    next turn then lands in the caller's case or abandons their own.
    """
    service = _case_service()
    app = build_app(session=_session(user_id="somebody-else"), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Session not found or resume not permitted"
    service.resume_case_in_session.assert_not_awaited()


async def test_refuses_an_unknown_session_the_same_way(build_app, call_api):
    """Same answer as "not yours": naming another user's session must not be
    distinguishable from naming one that does not exist."""
    service = _case_service()
    app = build_app(session=None, case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Session not found or resume not permitted"
    service.resume_case_in_session.assert_not_awaited()


async def test_a_failed_link_is_a_server_error_not_an_absence(build_app, call_api):
    """A case the caller cannot reach raised above and never got here, so a
    falsy result is the link failing. Answering 404 would repeat the shape this
    endpoint was fixed for — the client abandons a case that is fine."""
    service = _case_service(resume=False)
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 500, response.text


async def test_resumes_a_case_the_caller_can_reach(build_app, call_api):
    """Vacuity control: the refusals above are the gates, not routing."""
    service = _case_service()
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["case_id"] == CASE_ID
