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

fm#1156 extends the file to the other thing this handler must not do: carry the
caller's credentials out of the process, by either of its two routes — the 422
body and the ERROR log. Those tests are grouped at the end.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import pytest
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from faultmaven.api.exception_handlers import (
    _NO_VALUE_AT_LOC_TYPES,
    _NO_VALUE_ECHO,
    MAX_VALIDATION_ERRORS,
    MAX_VALIDATION_INPUT_BYTES,
    describe_request_body,
    request_validation_exception_handler,
    sanitize_validation_error,
)
from faultmaven.api.models import LLMConfigUpdateRequest
from faultmaven.modules.auth.domain.models.api_auth import (
    DevLoginRequest,
    TokenRefreshRequest,
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


# =============================================================================
# fm#1156: a validation failure must not carry credentials out of the process,
# by either route — the 422 body or the ERROR log.
#
# These drive the *published* request models, not look-alikes. The shape that
# leaks is a mis-keyed credential field, so what matters is that the real bodies
# have the fields they have: a rename or a new credential-carrying field should
# stay covered without anyone remembering to update a stand-in.
# =============================================================================

SECRET_REFRESH = "eyJhbGciOiJSUzI1NiJ9.LIVE-REFRESH-TOKEN.sig"
SECRET_API_KEY = "sk-live-REALKEY-abcdef123456"


class NestedCredentials(BaseModel):
    """A credential field one level down — where identity with `exc.body` fails."""

    refresh_token: str


class NestedBody(BaseModel):
    credentials: NestedCredentials


class _Capture(logging.Handler):
    """Collect records off the handler's own logger.

    Attached directly rather than via `caplog` so the assertions do not depend
    on propagation or on whatever global logging configuration a prior test
    left behind: the point of these tests is what this handler writes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def everything(self) -> str:
        """Every channel of every record, as one searchable string."""
        return json.dumps(
            [
                {
                    "message": record.getMessage(),
                    "body": getattr(record, "body", None),
                    "validation_errors": getattr(record, "validation_errors", None),
                }
                for record in self.records
            ],
            default=repr,
        )


@pytest.fixture
def logs():
    capture = _Capture()
    handler_logger = logging.getLogger("faultmaven.api.exception_handlers")
    handler_logger.addHandler(capture)
    try:
        yield capture
    finally:
        handler_logger.removeHandler(capture)


@pytest.fixture
def auth_client() -> TestClient:
    """The real published bodies of the routes that carry credentials."""
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )

    @app.post("/auth/refresh")
    async def refresh_endpoint(request_body: TokenRefreshRequest) -> dict:
        return {"ok": True}

    @app.post("/auth/login")
    async def login_endpoint(request_body: DevLoginRequest) -> dict:
        return {"ok": True}

    @app.put("/admin/llm/config")
    async def llm_config_endpoint(request: LLMConfigUpdateRequest) -> dict:
        return {"ok": True}

    @app.post("/nested")
    async def nested_endpoint(request_body: NestedBody) -> dict:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_missing_field_does_not_echo_the_enclosing_object(auth_client, logs):
    """The reported shape: camelCase on /auth/refresh returned the live token.

    `loc` is `["body", "refresh_token"]` — field-level, so `_WHOLE_BODY_LOC` does
    not fire — while `input` is the whole body, because a value that was never
    supplied has nothing else pydantic can report. Getting the field name wrong
    is the commonest client mistake and the one most likely to be carrying a
    real credential.
    """
    response = auth_client.post("/auth/refresh", json={"refreshToken": SECRET_REFRESH})

    errors = _errors(response)
    assert SECRET_REFRESH not in response.text
    assert SECRET_REFRESH not in logs.everything()

    # What makes the 422 actionable survives: the client still learns which
    # field it failed to send, and why.
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ["body", "refresh_token"]
    assert errors[0]["msg"] == "Field required"
    assert errors[0]["input"] == _NO_VALUE_ECHO


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_missing_field_in_a_nested_object_does_not_echo_its_siblings(auth_client, logs):
    """One level down, `input` is the sub-object — not `exc.body`.

    This is why the guard keys on the error *type* rather than on identity with
    `exc.body`: identity holds only for a flat body model. Here the echoed
    object is the credentials sub-object, which is no better than the body if
    that is where the credential lives.
    """
    response = auth_client.post(
        "/nested", json={"credentials": {"refreshToken": SECRET_REFRESH}}
    )

    errors = _errors(response)
    assert SECRET_REFRESH not in response.text
    assert SECRET_REFRESH not in logs.everything()
    assert errors[0]["loc"] == ["body", "credentials", "refresh_token"]
    assert errors[0]["input"] == _NO_VALUE_ECHO


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_missing_field_on_login_does_not_echo_the_rest_of_the_body(auth_client, logs):
    """/auth/login, same mechanism: one wrong key exposes every other field."""
    response = auth_client.post(
        "/auth/login", json={"user": "ab", "email": "someone@example.com"}
    )

    errors = _errors(response)
    assert "someone@example.com" not in response.text
    assert "someone@example.com" not in logs.everything()
    assert errors[0]["loc"] == ["body", "username"]
    assert errors[0]["input"] == _NO_VALUE_ECHO


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_error_log_does_not_carry_the_request_body(auth_client, logs):
    """The half the response already got right, and the log did not.

    `PUT /admin/llm/config` exists to receive provider API keys. One mistyped
    *other* field produced a 422 that correctly withheld the key and an ERROR
    record that carried it in full — the two channels disagreeing about whether
    a value is sensitive is what marks this an oversight rather than a decision.
    Logs are the sharper half: they are retained, aggregated, and read by people
    who were not party to the request.
    """
    response = auth_client.put(
        "/admin/llm/config",
        json={"api_key": SECRET_API_KEY, "fallback_chain": "openai,groq"},
    )

    errors = _errors(response)
    assert logs.records, "the handler must still log the validation failure"
    assert SECRET_API_KEY not in logs.everything()
    assert SECRET_API_KEY not in response.text

    # #1048's deliberate behaviour is intact: the *offending* field's own value
    # is still echoed, in both channels. Blanket-stripping would pass the
    # security assertion above while making every 422 useless.
    assert errors[0]["loc"] == ["body", "fallback_chain"]
    assert errors[0]["input"] == "openai,groq"
    assert "openai,groq" in logs.everything()

    # The body's shape survives, which is the part that was diagnostic.
    assert getattr(logs.records[0], "body") == "<dict: 2 keys>"


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_error_log_does_not_carry_a_form_encoded_body(auth_client, logs):
    """The #1048 shape: the response withheld the token, the log did not.

    A form-encoded POST to a JSON endpoint puts the raw bytes in `exc.body`, and
    on /auth/refresh those bytes are `refresh_token=<live token>`.
    """
    response = auth_client.post("/auth/refresh", data={"refresh_token": SECRET_REFRESH})

    _errors(response)
    assert SECRET_REFRESH not in response.text
    assert SECRET_REFRESH not in logs.everything()

    # Still distinguishable from a JSON body at a glance, which is what the
    # logged body was actually good for when #1048 was diagnosed.
    assert getattr(logs.records[0], "body") == "<bytes: 57 bytes>"


@pytest.mark.unit
@pytest.mark.security
def test_input_identical_to_the_body_is_withheld_at_any_loc():
    """Defence in depth for an error type that has not turned up yet.

    No shape FastAPI builds today reports the whole body at a field-level `loc`
    with a type outside the missing family — the type-keyed rule covers every
    measured case. This one is belt to that braces: if pydantic grows such a
    type, the identity check catches it without anyone noticing first.

    The second half is the mutation: drop `body` and the same error echoes the
    credential, which is what shows the check is load-bearing rather than
    decorative.
    """
    body = {"refreshToken": SECRET_REFRESH}
    error = {
        "type": "value_error",
        "loc": ("body", "refresh_token"),
        "msg": "Value error, hypothetical",
        "input": body,
    }

    withheld = sanitize_validation_error(error, body)
    assert SECRET_REFRESH not in json.dumps(withheld)
    assert withheld["input"] == "<request body not echoed>"

    assert sanitize_validation_error(error)["input"] == body


@pytest.mark.unit
@pytest.mark.security
def test_missing_family_names_still_exist_in_pydantic():
    """A rename upstream would empty the guard silently.

    `_NO_VALUE_AT_LOC_TYPES` is a set of pydantic's own error-type strings. If
    one is renamed the guard stops firing for that type and nothing fails —
    the 422 just starts echoing again. Pin the names against the catalogue.

    `missing_sentinel_error` is deliberately absent from the set: it is the one
    "missing*" type that reports the supplied value rather than the enclosing
    object, so prefix-matching would over-strip.
    """
    from pydantic_core import _pydantic_core

    catalogue = {entry["type"] for entry in _pydantic_core.list_all_errors()}

    missing_family = {name for name in catalogue if name.startswith("missing")}
    assert _NO_VALUE_AT_LOC_TYPES <= catalogue, (
        f"pydantic no longer defines {_NO_VALUE_AT_LOC_TYPES - catalogue}; "
        f"the missing* types it does define are {sorted(missing_family)}"
    )
    assert "missing_sentinel_error" not in _NO_VALUE_AT_LOC_TYPES


@pytest.mark.unit
@pytest.mark.security
def test_describe_request_body_names_shapes_without_content():
    """It runs inside an exception handler, so it must be total and quiet."""

    class Hostile:
        def __len__(self):  # pragma: no cover - exercised via describe only
            raise RuntimeError("no")

        def __repr__(self):  # pragma: no cover - must never be called
            return f"secret={SECRET_API_KEY}"

    assert describe_request_body(None) is None
    assert describe_request_body(b"refresh_token=x") == "<bytes: 15 bytes>"
    assert describe_request_body(bytearray(b"ab")) == "<bytearray: 2 bytes>"
    assert describe_request_body(memoryview(b"abc")) == "<memoryview: 3 bytes>"
    assert describe_request_body("a" * 9) == "<str: 9 characters>"
    assert describe_request_body({"api_key": SECRET_API_KEY}) == "<dict: 1 key>"
    assert describe_request_body([1, 2]) == "<list: 2 items>"

    # An unknown type is named, never repr'd: a repr is content.
    described = describe_request_body(Hostile())
    assert described == "<Hostile>"
    assert SECRET_API_KEY not in described
