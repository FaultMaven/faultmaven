"""The endpoint label is an API route's template, or ``unmatched``.

Starlette 1.7 began setting ``scope["route"]`` for every match, FastAPI's own
documentation routes (plain Starlette routes) included, so a label read off
any route counted documentation traffic as the service surface in the SLA
tracker and Prometheus. The label is decided by the route's type.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from faultmaven.api.middleware.logging import _endpoint_label, _is_service_surface

pytestmark = pytest.mark.unit


def _request_matching(route) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    if route is not None:
        scope["route"] = route
    return Request(scope)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/things/{thing_id}")
    async def thing(thing_id: str):
        return {}

    return app


def _route(app: FastAPI, path: str):
    return next(route for route in app.routes if route.path == path)


def test_an_api_route_is_labelled_with_its_template(app):
    route = _route(app, "/api/v1/things/{thing_id}")
    assert isinstance(route, APIRoute)

    label = _endpoint_label(_request_matching(route))

    assert label == "/api/v1/things/{thing_id}"
    assert _is_service_surface(label)


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_documentation_route_is_not_the_service_surface(app, path):
    route = _route(app, path)
    assert not isinstance(route, APIRoute)

    label = _endpoint_label(_request_matching(route))

    assert label == "unmatched"
    assert not _is_service_surface(label)


def test_a_request_matching_no_route_is_unmatched():
    assert _endpoint_label(_request_matching(None)) == "unmatched"
