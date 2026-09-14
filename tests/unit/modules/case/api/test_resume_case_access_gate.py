"""POST /cases/sessions/{session_id}/resume/{case_id} gates BOTH names (#1390, #1393).

The route had no authorization at all. It names two resources and needs a check
for each — `faultmaven/api/routes/sessions.py` states the rule for its own
surface: the case gate "covers the case named in the path and nothing else …
Both halves are needed; neither is sufficient".

* **The case.** `resume_case_in_session` -> `link_session_to_case` resolved it
  with a bare `repository.get(case_id)` — no caller, no ownership, no share.
* **The session.** Nothing looked at it, so naming someone else's session id
  retargeted their `session:{id}:current_case_id` pointer.

`tests/integration/security/test_two_enterprise_surface_probe.py` drives the
same route end to end under PostgreSQL/RLS; this module pins the gates' SHAPE
without needing a database.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Both marks at module level. The vacuity control is not optional coverage —
# under `pytest -m security` a lone refusal assertion passes for a route that
# is not mounted, a trailing-slash redirect, or a dependency that raised.
pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SESSION_ID = "sess-resume-0001"
CASE_ID = "case-123"
CALLER = "user-1"
OWNER = "user-owner"
PATH = f"/api/v1/cases/sessions/{SESSION_ID}/resume/{CASE_ID}"


def _case_service(*, shared_to_caller: bool = True, resume: bool = True):
    """A stand-in for the real resolver's two answers.

    `get_case` HONOURS `owner_only` rather than omitting it, so this module can
    assert which resolver the route asked for. A fake that simply lacked the
    parameter would turn an `owner_only=True` regression into a TypeError the
    route's own `except Exception` converts to a 500 — the assertion would then
    fire on "not 200" rather than on "the teammate was refused", and widening
    the fake later would silently disarm it. Shape borrowed from
    `test_case_write_owner_gate.py`.
    """
    calls = []

    async def get_case(case_id, user_id=None, *, owner_only=False):
        calls.append({"case_id": case_id, "user_id": user_id, "owner_only": owner_only})
        if case_id != CASE_ID:
            return None
        if user_id != OWNER:
            # The caller is a teammate holding a read share: visible on the
            # read arm, invisible once `owner_only` drops it.
            if owner_only or not shared_to_caller:
                return None
        return SimpleNamespace(case_id=CASE_ID, user_id=OWNER)

    service = SimpleNamespace(
        get_case=get_case,
        resume_case_in_session=AsyncMock(return_value=resume),
    )
    service.calls = calls
    return service


def _session(user_id=CALLER):
    return SimpleNamespace(session_id=SESSION_ID, user_id=user_id)


async def test_refuses_a_case_the_caller_cannot_reach(build_app, call_api):
    service = _case_service(shared_to_caller=False)
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Case not found or resume not permitted"
    # The gate ran, against the CALLER, on the read arm.
    assert service.calls == [
        {"case_id": CASE_ID, "user_id": CALLER, "owner_only": False}
    ]
    service.resume_case_in_session.assert_not_awaited()


async def test_admits_a_teammate_holding_a_share(build_app, call_api):
    """Owner ∪ shared, deliberately — see the route's comment.

    A teammate who may POST a turn into a shared case must be able to attach a
    session to it. Narrowing this to `owner_only=True` fails here rather than
    silently refusing every teammate.
    """
    service = _case_service(shared_to_caller=True)
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


async def test_refuses_a_session_that_is_not_the_callers(build_app, call_api):
    """The half that was missing entirely.

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


async def test_a_failed_link_is_a_server_error_not_an_absence(build_app, call_api):
    """Both gates have passed, so a failure here is the link failing.

    Answering 404 would repeat the shape this endpoint was fixed for — the
    client abandons a case that is fine instead of retrying.
    """
    service = _case_service(resume=False)
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 500, response.text


async def test_resumes_a_case_the_caller_owns(build_app, call_api):
    """Vacuity control: the refusals above are the gates, not routing."""
    service = _case_service()
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["case_id"] == CASE_ID
    service.resume_case_in_session.assert_awaited_once_with(CASE_ID, SESSION_ID)


def _run(call_api, app):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(call_api(app, "POST", PATH))
