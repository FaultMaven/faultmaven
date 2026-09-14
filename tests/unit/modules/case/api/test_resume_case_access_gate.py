"""POST /cases/sessions/{session_id}/resume/{case_id} checks case access (#1390).

The route had no gate at all. `resume_case_in_session` delegates to
`link_session_to_case`, which resolves the case with a bare
`repository.get(case_id)` — no caller, no ownership, no share — so any
authenticated user could attach their session to any case their tenant could
see, and the subsequent turns would land there.

It was not exploitable only by accident: the method always failed on a
malformed event row and returned False, which the route reported as 404. Fixing
that bug is what makes the missing gate reachable, so the gate lands with it.

`tests/integration/security/test_two_enterprise_surface_probe.py` covers the
same route end to end under PostgreSQL/RLS; this module pins the gate's SHAPE
without needing a database.
"""

from types import SimpleNamespace

import pytest

SESSION_ID = "sess-resume-0001"
CASE_ID = "case-123"
PATH = f"/api/v1/cases/sessions/{SESSION_ID}/resume/{CASE_ID}"


def _service(case, *, calls):
    """A case service whose `get_case` mirrors the REAL signature.

    `CaseService.get_case` takes `owner_only` as a keyword-only argument, and
    this fake deliberately does not: the route must resolve through
    owner ∪ shared-to-my-teams, matching `submit_turn`, so a future edit that
    "hardens" this to `owner_only=True` raises TypeError here rather than
    silently refusing every teammate holding a share. See the conftest note on
    signature-faithful fakes.
    """

    async def get_case(case_id, user_id=None):
        calls.append((case_id, user_id))
        return case

    async def resume_case_in_session(case_id, session_id):
        return True

    return SimpleNamespace(
        get_case=get_case, resume_case_in_session=resume_case_in_session
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_refuses_a_case_the_caller_cannot_reach(build_app, call_api):
    """`get_case` answering None is the whole of the refusal."""
    calls = []
    app = build_app(case_service=_service(None, calls=calls))

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Case not found or resume not permitted"
    # The gate ran, and it ran against the CALLER — not an unscoped lookup.
    assert calls == [(CASE_ID, "user-1")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resumes_a_case_the_caller_can_reach(build_app, call_api):
    """Vacuity control: the 404 above is the gate, not routing or a redirect."""
    calls = []
    case = SimpleNamespace(case_id=CASE_ID, user_id="user-1")
    app = build_app(case_service=_service(case, calls=calls))

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["case_id"] == CASE_ID
    assert calls == [(CASE_ID, "user-1")]
