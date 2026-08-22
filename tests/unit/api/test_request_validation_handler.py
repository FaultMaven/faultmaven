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
    MAX_VALIDATION_ERRORS,
    MAX_VALIDATION_INPUT_BYTES,
    request_validation_exception_handler,
)


class LoginBody(BaseModel):
    """Shaped like the real auth bodies: a JSON object with a bounded field."""

    username: str = Field(min_length=3, max_length=50)
    password: str


class ListBody(BaseModel):
    """A list field, so one bad request yields one error per item."""

    username: str
    password: str
    tags: List[str]


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

    @app.post("/json-list")
    async def json_list_endpoint(body: ListBody) -> dict:
        return {"ok": len(body.tags)}

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
    response = client.post("/json", data={"username": "ab", "password": "hunter2"})

    errors = _errors(response)
    assert errors[0]["type"] == "model_attributes_type"
    assert "valid dictionary or object" in errors[0]["msg"]


@pytest.mark.unit
@pytest.mark.api
def test_whole_body_error_does_not_echo_the_body(client):
    """A body-level `input` IS the body — on an auth route, the credentials.

    Restoring the 422 is what makes this reachable: the crash used to swallow
    the echo. The message still names the problem, which is the diagnosis; the
    payload adds nothing the sender does not already have.
    """
    response = client.post(
        "/json", data={"username": "ab", "password": "hunter2-SECRET"}
    )

    body = response.text
    assert "hunter2-SECRET" not in body
    assert "username=ab" not in body
    assert "<request body not echoed" in body


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
def test_error_count_is_capped(client):
    """Pydantic emits one error per offending item, so the count needs a cap.

    Without it a 128,900-byte body of wrong-typed list items measured a
    2,057,820-byte response (x16) — the per-error budget cannot bound that,
    because every individual error is already small.
    """
    payload = json.dumps(
        {"username": "abc", "password": "x", "tags": list(range(5000))}
    )

    response = client.post(
        "/json-list",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    errors = _errors(response)
    assert len(errors) == MAX_VALIDATION_ERRORS + 1
    assert errors[-1]["type"] == "too_many_errors"
    assert "4950 further" in errors[-1]["msg"]
    assert len(response.content) < len(payload)


@pytest.mark.unit
@pytest.mark.api
def test_budget_is_measured_in_utf8_bytes_not_characters(client):
    """The response is UTF-8; counting characters lets CJK through at ~3x.

    The value has to be a *structure* of short strings, not one long string:
    to_json_safe cuts every string to DEFAULT_SAFE_STRING_CHARS first, so a
    single long value is under both budgets by the time they are applied and
    would not exercise the boundary at all. 50 keys of 30 CJK characters
    render as 2040 characters and 5040 bytes — inside a character-counted
    budget of 2048, well outside a byte-counted one.
    """
    tags = {f"k{i}": "\u98df" * 30 for i in range(50)}
    payload = json.dumps(
        {"username": "abc", "password": "abcdefghij", "tags": tags},
        ensure_ascii=False,
    )

    response = client.post(
        "/json-list",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    errors = _errors(response)
    echoed = json.dumps(errors[0]["input"], ensure_ascii=False).encode("utf-8")
    assert len(echoed) <= MAX_VALIDATION_INPUT_BYTES, (
        f"echoed {len(echoed)} UTF-8 bytes under a "
        f"{MAX_VALIDATION_INPUT_BYTES}-byte budget"
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
