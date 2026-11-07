"""``POST /knowledge/convert`` answers an LLM failure by its status, whatever
its class (#1632).

The route re-raises ``FaultMavenException`` to the global handlers, so an
``LLMException`` there answered 402/429/503 already. The LLM's open breaker and
its configuration error are not ``FaultMavenException``s: they fell to the
route's broad arm, 500 ``LLM_GENERATION_FAILED``. The LLM's deadline is one,
but no handler took it: the raw 500. Any other failure keeps the route's 500.
``TestClient``, because the route takes a multipart upload.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    internal_server_error_handler,
)
from faultmaven.exceptions import LLM_CONFIG_ERROR, QUOTA_EXHAUSTED, LLMException
from faultmaven.infrastructure.base_client import LLM_BREAKER_SERVICE
from faultmaven.models.exceptions import LLMProviderError
from faultmaven.modules.knowledge.api.conversion_routes import (
    _get_conversion_service,
    _require_auth,
)
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from tests.unit.api.test_llm_failure_shapes_1635 import (
    BILLING,
    PROVIDER_ERROR,
    UNAVAILABLE,
    _breaker,
    _deadline,
)
from tests.unit.api.test_service_exception_global_handler import _BILLING_TEXT

pytestmark = pytest.mark.unit


@pytest.fixture
def convert():
    """The service's ``convert_document``; each test sets what it raises."""
    return AsyncMock()


@pytest.fixture
def client(convert) -> TestClient:
    # Registered as `faultmaven/main.py` registers them.
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(500, internal_server_error_handler)
    app.include_router(conversion_router, prefix="/api/v1")

    async def _service():
        return SimpleNamespace(convert_document=convert)

    async def _user():
        return SimpleNamespace(
            user_id="user-1", enterprise_id="ent-1", is_platform_admin=lambda: False
        )

    app.dependency_overrides[_get_conversion_service] = _service
    app.dependency_overrides[_require_auth] = _user
    return TestClient(app, raise_server_exceptions=False)


def _convert(client, convert, failure):
    convert.reset_mock()
    convert.side_effect = failure
    response = client.post(
        "/api/v1/knowledge/convert",
        data={"scope": "personal"},
        files={"file": ("guide.md", b"# A guide\n\nSteps.\n", "text/markdown")},
    )
    convert.assert_awaited_once()
    return response


_LLM_FAILURES = {
    "open-breaker": (_breaker, UNAVAILABLE),
    "breaker-latched-billing": (lambda: _breaker(error_code=QUOTA_EXHAUSTED), BILLING),
    "config-error": (
        lambda: LLMProviderError("no provider", error_code=LLM_CONFIG_ERROR),
        PROVIDER_ERROR,
    ),
    "deadline": (lambda: _deadline(LLM_BREAKER_SERVICE), UNAVAILABLE),
}


@pytest.mark.parametrize("name", sorted(_LLM_FAILURES))
def test_an_llm_failure_answers_the_llm_status(client, convert, name):
    factory, (status, code, retry_after) = _LLM_FAILURES[name]
    response = _convert(client, convert, factory())

    assert response.status_code == status, response.text
    assert response.headers["x-error-code"] == code
    assert response.headers.get("retry-after") == retry_after
    assert "Circuit breaker" not in response.text


def test_a_billing_latched_breaker_answers_as_the_billing_llm_exception(
    client, convert
):
    breaker = _convert(client, convert, _breaker(error_code=QUOTA_EXHAUSTED))
    billing = _convert(client, convert, LLMException(_BILLING_TEXT, status_code=402))

    assert breaker.status_code == billing.status_code == 402
    assert breaker.json() == billing.json()
    assert breaker.headers["x-error-code"] == billing.headers["x-error-code"]


@pytest.mark.parametrize(
    "failure",
    [_breaker(service="ChromaDB"), _breaker(service=None), ValueError("boom")],
    ids=["chromadb-breaker", "unnamed-breaker", "value-error"],
)
def test_any_other_failure_keeps_the_routes_500(client, convert, failure):
    response = _convert(client, convert, failure)

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Document conversion failed. Please try again.",
        "error_code": "LLM_GENERATION_FAILED",
    }
