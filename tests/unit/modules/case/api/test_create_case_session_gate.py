"""POST /api/v1/cases — the session named in the BODY is an argument, not a licence.

`resume_case_in_session` names its session in the PATH and gates it (#1390,
#1393, #1398). This route names one in the body — `CaseCreateRequest.session_id`
— and had the identical defect: it checked only that the session *existed*
(`if not session: 401`) and then handed the id to `CaseService.create_case`,
which writes `session:{session_id}:current_case_id`.

So naming somebody else's session id retargeted their current-case pointer at a
case of the caller's choosing. Their next turn then either lands in the caller's
case or silently abandons the one they were working — the same outcome the
resume route was fixed for, reached through a different parameter.

The write is inside the service, BEFORE `repository.save`, so it lands ahead of
every failure path this route has. That is why each refusal below asserts
`create_case` was never awaited rather than only asserting the status: a gate
that refuses *after* the service call would leave the retarget done and still
answer 401.

The answer is this route's EXISTING 401 `SESSION_EXPIRED`, not the resume
route's 404, because "no such session" already answers that here and the two
must stay indistinguishable — naming another user's session must not be
distinguishable from naming one that does not exist.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import ServiceException, SessionStoreException
from faultmaven.modules.case.domain.models import Case

# Both marks at module level. The vacuity controls are not optional coverage —
# under `pytest -m security` a lone refusal assertion passes for a route that
# is not mounted, a trailing-slash redirect, or a dependency that raised.
pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SESSION_ID = "sess-create-0001"
CALLER = "user-1"  # what the conftest's `require_authentication` override mints
PATH = "/api/v1/cases"


def _case_service():
    """A case service whose `create_case` mirrors the real signature.

    Signature-faithful on purpose (the house rule in this directory's
    conftest): a stand-in taking `**kwargs` would let the route stop forwarding
    `session_id` — the very argument under test — and still pass.
    """
    created = AsyncMock(
        return_value=Case(enterprise_id="ent-1", title="t", user_id=CALLER)
    )

    async def create_case(
        title=None,
        description=None,
        owner_id=None,
        session_id=None,
        initial_message=None,
        source="copilot",
    ):
        return await created(
            title=title,
            description=description,
            owner_id=owner_id,
            session_id=session_id,
            initial_message=initial_message,
            source=source,
        )

    return SimpleNamespace(create_case=create_case, created=created)


def _session(user_id=CALLER):
    return SimpleNamespace(session_id=SESSION_ID, user_id=user_id)


def _body(session_id=SESSION_ID):
    return {"title": "t", "description": "d", "session_id": session_id}


async def test_refuses_a_session_that_is_not_the_callers(build_app, call_api):
    """The gate this route never had.

    The pre-fix route created the case — and wrote the pointer — because the
    session resolved. Whose it was was never asked.
    """
    service = _case_service()
    app = build_app(session=_session(user_id="somebody-else"), case_service=service)

    response = await call_api(app, "POST", PATH, json=_body())

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["error"]["code"] == "SESSION_EXPIRED"
    service.created.assert_not_awaited()


async def test_refuses_an_unknown_session_the_same_way(build_app, call_api):
    """Byte-for-byte the same refusal, so the two cannot be told apart.

    Asserted against the other test's body rather than restated, because an
    answer that merely shares a status code still distinguishes them.
    """
    service = _case_service()
    app = build_app(session=None, case_service=service)

    response = await call_api(app, "POST", PATH, json=_body())

    assert response.status_code == 401, response.text

    other = build_app(session=_session(user_id="somebody-else"), case_service=service)
    not_mine = await call_api(other, "POST", PATH, json=_body())

    assert response.json() == not_mine.json()
    service.created.assert_not_awaited()


async def test_an_unevaluable_session_gate_is_a_503(build_app, call_api):
    """`get_session` RAISES rather than returning None when it cannot answer.

    `ServiceException("Session store not configured")` is the shipped case.
    Treating it as a nullable return let it reach this handler's own
    `except ServiceException` arm and answer 500 "Failed to create case" on a
    request the server could not evaluate. An unevaluable gate is a 503 —
    `faultmaven/api/routes/sessions.py` states the rule for its own surface.
    """
    service = _case_service()
    app = build_app(
        session=ServiceException("Session store not configured"),
        case_service=service,
    )

    response = await call_api(app, "POST", PATH, json=_body())

    assert response.status_code == 503, response.text
    service.created.assert_not_awaited()


async def test_an_unreachable_session_store_is_also_a_503(build_app, call_api):
    """The second spelling of "the gate could not be evaluated".

    A store that IS configured and unreachable raises `SessionStoreException`,
    which descends from `SessionException` and NOT from `ServiceException` —
    so catching one family answers 503 and 500 for the same condition.
    """
    service = _case_service()
    app = build_app(
        session=SessionStoreException("redis: connection refused"),
        case_service=service,
    )

    response = await call_api(app, "POST", PATH, json=_body())

    assert response.status_code == 503, response.text
    service.created.assert_not_awaited()


async def test_creates_on_the_callers_own_session(build_app, call_api):
    """Vacuity control: the refusals above are the gate, not routing.

    Also pins that the session id still REACHES the service — the pointer write
    is the legitimate half of this parameter, and a gate that silently dropped
    `session_id` would make every refusal above pass for the wrong reason.
    """
    service = _case_service()
    app = build_app(session=_session(), case_service=service)

    response = await call_api(app, "POST", PATH, json=_body())

    assert response.status_code == 201, response.text
    service.created.assert_awaited_once()
    assert service.created.await_args.kwargs["session_id"] == SESSION_ID
    assert service.created.await_args.kwargs["owner_id"] == CALLER


async def test_creates_with_no_session_at_all(build_app, call_api):
    """The parameter is optional, and its absence asks the store nothing.

    `session_id` is not how this route identifies its caller — the bearer is —
    so a request without one is ordinary, not anonymous.
    """
    service = _case_service()
    app = build_app(
        session=ServiceException("must not be consulted"), case_service=service
    )

    response = await call_api(app, "POST", PATH, json={"title": "t"})

    assert response.status_code == 201, response.text
    service.created.assert_awaited_once()
    assert service.created.await_args.kwargs["session_id"] is None
