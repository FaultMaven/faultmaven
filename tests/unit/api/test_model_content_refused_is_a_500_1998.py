"""A model-content refusal reaching any route answers 500 ``SERVICE_ERROR`` (#1998 P3).

A refusal (``RawModelContentRefused``) means a send site handed the model
boundary content the redaction module did not mint, and nothing was sent: a
FaultMaven programming error. It declares ``retryable=False`` so the router's
transport neither retries it nor counts it against the breaker, and the
``/turns`` mapping used to read that declaration as a terminal PROVIDER
condition: 502 ``LLM_PROVIDER_ERROR``, "The AI provider could not process this
request" — blaming the provider for FaultMaven's own fault.

Pinned here, on an app registered exactly as ``faultmaven/main.py`` registers
the handlers: a bare refusal (a route that lets it through), a refusal wrapped
in ``ServiceException`` and left to the global handler, and the same wrap
mapped inline the way ``/turns`` maps it all answer 500 ``SERVICE_ERROR``,
with the static detail and no ``Retry-After`` (the same request is refused
again), never the refusal's text.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    SERVICE_ERROR_DETAIL,
    get_exception_handlers,
    global_service_exception_http_exception,
    http_exception_handler,
    llm_service_error_http_exception,
    model_content_refused_handler,
)
from faultmaven.exceptions import LLMException, RawModelContentRefused, ServiceException

pytestmark = [pytest.mark.unit, pytest.mark.security]

#: A refusal's text: where and what kind, never content — and still never echoed.
_REFUSAL = "router: model-bound content not minted by the redaction module: prompt raw"


def _wrapped() -> ServiceException:
    """The turn's wrap (``prepare_turn``): ``ServiceException(...) from e``."""
    try:
        try:
            raise RawModelContentRefused(_REFUSAL)
        except RawModelContentRefused as e:
            raise ServiceException(f"Turn processing failed: {e}") from e
    except ServiceException as wrapped:
        return wrapped


def _wrapped_beside_a_provider_status() -> ServiceException:
    """A chain that also carries a provider 4xx: the refusal still decides."""
    try:
        try:
            try:
                raise LLMException("bad request", status_code=400)
            except LLMException as provider:
                raise RawModelContentRefused(_REFUSAL) from provider
        except RawModelContentRefused as e:
            raise ServiceException("Turn processing failed") from e
    except ServiceException as wrapped:
        return wrapped


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    @app.post("/bare")
    async def bare():
        raise RawModelContentRefused(_REFUSAL)

    @app.post("/wrapped")
    async def wrapped():
        raise _wrapped()

    @app.post("/inline")
    async def inline():
        try:
            raise _wrapped()
        except ServiceException as e:
            raise llm_service_error_http_exception(e, "corr-1")

    @app.post("/inline-beside-a-provider-status")
    async def inline_beside():
        try:
            raise _wrapped_beside_a_provider_status()
        except ServiceException as e:
            raise llm_service_error_http_exception(e)

    return TestClient(app, raise_server_exceptions=False)


def test_the_refusal_has_its_own_handler():
    assert get_exception_handlers()[RawModelContentRefused] is (
        model_content_refused_handler
    )


@pytest.mark.parametrize(
    "path", ["/bare", "/wrapped", "/inline", "/inline-beside-a-provider-status"]
)
def test_a_refusal_answers_500_service_error(client, path):
    resp = client.post(path)

    assert resp.status_code == 500, resp.text
    assert resp.headers["x-error-code"] == "SERVICE_ERROR"
    assert "retry-after" not in resp.headers
    assert resp.json() == {"detail": SERVICE_ERROR_DETAIL}
    assert "redaction" not in resp.text and "prompt raw" not in resp.text


def test_the_inline_mapping_keeps_its_correlation_id(client):
    assert client.post("/inline").headers["x-correlation-id"] == "corr-1"


def test_both_mappings_put_the_refusal_first():
    """The ``/turns`` mapping and the global one: a refusal on the chain wins
    over every provider signal, including ``retryable=False`` (which alone
    reads as ``LLM_PROVIDER_ERROR``)."""
    for exc in (_wrapped(), _wrapped_beside_a_provider_status()):
        for mapped in (
            llm_service_error_http_exception(exc),
            global_service_exception_http_exception(exc),
        ):
            assert (mapped.status_code, mapped.headers["x-error-code"]) == (
                500,
                "SERVICE_ERROR",
            )
