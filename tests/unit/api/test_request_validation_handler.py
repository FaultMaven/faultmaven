"""fm#1048: a bad request gets a 422, never a 500.

The handler is the last thing standing between a malformed request and the
error the caller is owed, so an exception *inside* it costs two things: the
client gets an opaque 500 with no validation detail, and `api.error_rate`
counts a server fault that never happened. During the fm#819 T3 rehearsal a
single form-encoded `POST /auth/oauth/token` — the encoding RFC 6749 §3.2
actually prescribes for token endpoints — tripped the SLA alert that way.

These drive real requests through a real app so the response goes through
Starlette's encoder, which is where four of the five shapes below actually
fail. Asserting on the handler's return value alone would miss them: it is
`JSONResponse.render`, not the handler body, that rejects NaN and lone
surrogates.
"""

from __future__ import annotations

import json
from typing import List, Optional

import pytest
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from faultmaven.api.exception_handlers import (
    MAX_VALIDATION_INPUT_BYTES,
    request_validation_exception_handler,
)


class LoginBody(BaseModel):
    """Shaped like the real auth bodies: a JSON object with a bounded field."""

    username: str = Field(min_length=3, max_length=50)
    password: str


@pytest.fixture
def client() -> TestClient:
    """A JSON endpoint and a multipart endpoint behind the real handler.

    `raise_server_exceptions=False` matters: without it TestClient re-raises the
    handler's own exception instead of returning the 500 a real client sees, and
    the test would fail with a confusing traceback rather than on the status.
    """
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )

    @app.post("/json")
    async def json_endpoint(body: LoginBody) -> dict:
        return {"ok": body.username}

    @app.post("/multipart")
    async def multipart_endpoint(
        query: Optional[str] = Form(None),
        files: List[UploadFile] = File(default=[]),
    ) -> dict:
        return {"ok": query}

    return TestClient(app, raise_server_exceptions=False)


def _errors(response) -> list:
    assert response.status_code == 422, (
        f"expected a validation error, got HTTP {response.status_code}: "
        f"{response.text[:300]}"
    )
    body = response.json()
    assert body["detail"] == "Validation error"
    return body["errors"]


@pytest.mark.unit
@pytest.mark.api
def test_form_encoded_body_on_a_json_endpoint(client):
    """The reported shape. `input` is the raw bytes body → TypeError."""
    response = client.post("/json", data={"username": "ab", "password": "x"})

    errors = _errors(response)
    # The body is echoed decoded, not as a repr, so it stays diagnosable.
    assert "username=ab" in json.dumps(errors)


@pytest.mark.unit
@pytest.mark.api
def test_binary_body_on_a_json_endpoint(client):
    """Undecodable bytes: `errors="replace"` matters, plain .decode() raises."""
    response = client.post(
        "/json",
        content=b"\xff\xfe\x00\x01binary",
        headers={"Content-Type": "application/octet-stream"},
    )

    _errors(response)


@pytest.mark.unit
@pytest.mark.api
def test_text_body_on_a_json_endpoint(client):
    response = client.post(
        "/json", content=b"hello", headers={"Content-Type": "text/plain"}
    )

    _errors(response)


@pytest.mark.unit
@pytest.mark.api
def test_file_part_bound_to_a_scalar_form_field(client):
    """`input` is an UploadFile — a second non-serializable type, same crash."""
    response = client.post("/multipart", files={"query": ("q.txt", b"hello")})

    assert "q.txt" in json.dumps(_errors(response))


@pytest.mark.unit
@pytest.mark.api
def test_non_finite_float_in_a_json_body(client):
    """json.loads accepts NaN; Starlette renders with allow_nan=False."""
    response = client.post(
        "/json",
        content=b'{"username": NaN, "password": "abcdefghij"}',
        headers={"Content-Type": "application/json"},
    )

    _errors(response)


@pytest.mark.unit
@pytest.mark.api
def test_lone_surrogate_in_a_json_body(client):
    """A plain str that UTF-8 cannot encode, from a *valid* JSON body."""
    response = client.post(
        "/json",
        content=rb'{"username": "\ud800", "password": "abcdefghij"}',
        headers={"Content-Type": "application/json"},
    )

    _errors(response)


@pytest.mark.unit
@pytest.mark.api
def test_deeply_nested_body_does_not_recurse_away(client):
    """`input` is attacker-supplied JSON; the walk runs inside the handler."""
    depth = 400
    payload = "[" * depth + "1" + "]" * depth

    response = client.post(
        "/json",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    _errors(response)


@pytest.mark.unit
@pytest.mark.api
def test_large_input_is_not_mirrored_back(client):
    """A 422 must not reflect the request body at its sender.

    Field-level errors were already 1:1 before fm#1048 (a 200 KB bad field
    produced a 200 KB 422); making body-level errors serializable would have
    extended that to the whole body, up to MAX_UPLOAD_SIZE_MB.
    """
    payload = json.dumps({"username": "x" * 200_000, "password": "abcdefghij"})

    response = client.post(
        "/json",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    _errors(response)
    assert len(response.content) < 4 * MAX_VALIDATION_INPUT_BYTES, (
        f"422 body was {len(response.content)} bytes for a "
        f"{len(payload)}-byte request"
    )


@pytest.mark.unit
@pytest.mark.api
def test_ordinary_validation_error_keeps_its_shape(client):
    """The fix must not cost the detail that makes a 422 useful."""
    response = client.post("/json", json={"username": "ab", "password": "x"})

    errors = _errors(response)
    assert len(errors) == 1
    error = errors[0]
    assert error["type"] == "string_too_short"
    assert error["loc"] == ["body", "username"]
    assert error["input"] == "ab"
    assert error["ctx"] == {"min_length": 3}
    assert "at least 3 characters" in error["msg"]
