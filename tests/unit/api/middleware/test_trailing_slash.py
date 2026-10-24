"""``TrailingSlashMiddleware`` edits the ASGI scope's own path values.

``scope["path"]`` is decoded and ``scope["raw_path"]`` is the percent-encoded
bytes; ``request.url.path`` is neither, because Starlette re-encodes it, so a
path rebuilt from it reached the route double-encoded.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.trailing_slash import TrailingSlashMiddleware

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"root": True}

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item_id": item_id}

    app.add_middleware(TrailingSlashMiddleware)
    with TestClient(app) as test_client:
        yield test_client


def test_a_trailing_slash_reaches_the_route_without_one(client):
    response = client.get("/items/plain/", follow_redirects=False)

    assert response.status_code == 200
    assert response.json() == {"item_id": "plain"}


def test_an_encoded_character_reaches_the_route_decoded_once(client):
    response = client.get("/items/a%5Cb/", follow_redirects=False)

    assert response.status_code == 200
    assert response.json() == {"item_id": "a\\b"}


def test_the_root_path_is_left_alone(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert response.json() == {"root": True}
