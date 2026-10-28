"""GET /cases/{case_id}/uploaded-files with no case service composed (#2358).

The route took the bare ``get_case_service``, which returns ``None`` when
``app.state.case_service`` is unset, and never refused it: ``None.get_case``
raised ``AttributeError`` and the handler's catch-all answered 500 where every
sibling answers 503. The refusal sits in the handler body, so the
authentication gate still answers an anonymous caller first.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.case.api.routes.router import router as case_router
from tests.utils import uncomposed_auth_service

PATH = "/api/v1/cases/case-1/uploaded-files"


def _service_less_app() -> FastAPI:
    """The case router with no ``app.state.case_service`` and no provider overridden."""
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    app.state.auth_service = uncomposed_auth_service()
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_authenticated_caller_gets_503_not_500(call_api):
    app = _service_less_app()

    async def _user():
        return SimpleNamespace(user_id="user-1")

    app.dependency_overrides[require_authentication] = _user

    response = await call_api(app, "GET", PATH)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Case service unavailable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused_by_the_gate_first(call_api):
    response = await call_api(_service_less_app(), "GET", PATH)

    assert response.status_code == 401, response.text
