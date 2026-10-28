"""GET /cases/{case_id}/uploaded-files with no case service composed (#2358).

The route took the bare ``get_case_service``, which returns ``None`` when
``app.state.case_service`` is unset, and never refused it: ``None.get_case``
raised ``AttributeError`` and the handler's catch-all answered 500 where every
sibling answers 503. The refusal sits in the handler body, so the
authentication gate still answers an anonymous caller first.
"""

import pytest

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from tests.utils import uncomposed_auth_service

PATH = "/api/v1/cases/case-1/uploaded-files"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_authenticated_caller_gets_503_not_500(build_app, call_api):
    app = build_app()
    # The real provider, over an ``app.state`` that holds no case service.
    del app.dependency_overrides[_di_get_case_service_dependency]

    response = await call_api(app, "GET", PATH)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Case service unavailable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused_before_the_case_service_resolves(
    build_app, call_api
):
    """The real gate over a case-service provider that raises.

    A 401 is reachable only if the gate resolved first; solved after the
    provider, the caller would get the provider's failure instead.
    """
    app = build_app()
    del app.dependency_overrides[require_authentication]
    app.state.auth_service = uncomposed_auth_service()

    async def _resolved():
        raise RuntimeError("the case service resolved ahead of the auth gate")

    app.dependency_overrides[_di_get_case_service_dependency] = _resolved

    response = await call_api(app, "GET", PATH)

    assert response.status_code == 401, response.text
