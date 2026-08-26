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
from pydantic import BaseModel, Field, model_validator
from starlette.datastructures import FormData

from faultmaven.api.exception_handlers import (
    _AGGREGATE_ECHO,
    _AGGREGATE_INPUT_TYPES,
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
from faultmaven.utils.serialization import to_json_safe


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


class CheckedCredentials(BaseModel):
    """A sub-object with a cross-field check, which fails as a whole.

    `@model_validator` reports the whole object as `input`, so one check that
    names no field discloses every field in it.
    """

    refresh_token: str
    password: str

    @model_validator(mode="after")
    def check(self):
        raise ValueError("credentials are inconsistent")


class CheckedBody(BaseModel):
    credentials: CheckedCredentials


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
    """Capture the handler's ERROR records, and refuse to capture none.

    Every test using this fixture provokes a 422, and a 422 always logs. So an
    empty capture means the record never reached the handler — a logger silenced
    somewhere, an exception swallowed — and the "the secret is not in the log"
    assertions would all pass on nothing. Failing here keeps a vacuous pass from
    reading as a security guarantee.
    """
    capture = _Capture()
    handler_logger = logging.getLogger("faultmaven.api.exception_handlers")
    handler_logger.addHandler(capture)
    try:
        yield capture
    finally:
        handler_logger.removeHandler(capture)
    assert capture.records, (
        "no ERROR record was captured, so every assertion about what the log "
        "does not contain was vacuous"
    )


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

    @app.post("/checked")
    async def checked_endpoint(request_body: CheckedBody) -> dict:
        return {"ok": True}

    @app.get("/search")
    async def search_endpoint(user_id: str, api_key: str) -> dict:
        return {"ok": user_id}

    @app.post("/upload")
    async def upload_endpoint(token: str = Form(...)) -> dict:
        return {"ok": token}

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
    assert getattr(logs.records[0], "body") == "<dict: 2 items>"


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
@pytest.mark.api
def test_model_validator_on_a_sub_object_does_not_echo_its_fields(auth_client, logs):
    """A cross-field check on a sub-object discloses every field inside it.

    `@model_validator` raising reports the whole sub-object as `input`, at the
    sub-object's own `loc`. So `loc` is field-level, `input` is not that
    field's value but an object of named fields, and the #1156 shape reopens on
    a type outside the missing family. Both credentials in one echo, from one
    validator that mentions neither.
    """
    response = auth_client.post(
        "/checked",
        json={
            "credentials": {"refresh_token": SECRET_REFRESH, "password": "PW-SECRET"}
        },
    )

    errors = _errors(response)
    assert SECRET_REFRESH not in response.text
    assert "PW-SECRET" not in response.text
    assert SECRET_REFRESH not in logs.everything()
    assert "PW-SECRET" not in logs.everything()

    # The diagnosis survives: which object failed, and the validator's reason.
    assert errors[0]["type"] == "value_error"
    assert errors[0]["loc"] == ["body", "credentials"]
    assert "inconsistent" in errors[0]["msg"]
    assert errors[0]["input"] == _AGGREGATE_ECHO


@pytest.mark.unit
@pytest.mark.api
def test_a_field_validator_error_still_echoes_its_value(auth_client):
    """The aggregate rule is scoped to Mapping inputs so this survives.

    `value_error` is raised by *field* validators too, and there the input is
    that field's own scalar — exactly the echo #1048 kept deliberately.
    `DevLoginRequest.validate_username` is a real published one, so widening
    the rule from "Mapping" to "any value_error" would make a live 422 stop
    telling the caller which username it rejected.
    """
    response = auth_client.post("/auth/login", json={"username": "bad user!!"})

    errors = _errors(response)
    assert errors[0]["type"] == "value_error"
    assert errors[0]["loc"] == ["body", "username"]
    assert errors[0]["input"] == "bad user!!"


@pytest.mark.unit
@pytest.mark.api
def test_missing_non_body_params_keep_their_null_input(auth_client):
    """The guard must not rewrite `input` where nothing was ever enclosed.

    FastAPI hard-codes `input=None` for a missing query, header, cookie, path
    or form field, so there is no enclosing object and nothing to withhold.
    Rewriting that `null` into a sentence would change the published shape of
    the single commonest client error across the whole API, and pad every such
    response by up to `MAX_VALIDATION_ERRORS` x the sentence.
    """
    query = _errors(auth_client.get("/search"))
    assert [e["loc"] for e in query] == [["query", "user_id"], ["query", "api_key"]]
    assert all(e["type"] == "missing" for e in query)
    assert all(e["input"] is None for e in query), query

    form = _errors(auth_client.post("/upload", data={"wrong": "1"}))
    assert form[0]["loc"] == ["body", "token"]
    assert form[0]["input"] is None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.api
def test_the_log_line_does_not_carry_the_query_string(auth_client, logs):
    """`request.url` would put a query-string credential on an ERROR record.

    Every sibling handler in the module logs `request.url.path`. This one
    logged the full URL, so the one channel #1156 was about — the ERROR
    record — still carried request content the response never echoed.
    """
    # `user_id` is omitted so the request actually fails validation; the
    # credential rides along in the query string of the very same request.
    response = auth_client.get("/search?api_key=" + SECRET_API_KEY)

    assert _errors(response)[0]["loc"] == ["query", "user_id"]
    assert SECRET_API_KEY not in logs.everything()
    assert "/search" in logs.records[0].getMessage()


@pytest.mark.unit
@pytest.mark.security
def test_input_identical_to_the_body_is_withheld_at_any_loc():
    """Defence in depth for a type the type-keyed rules do not name.

    The rules above cover the shapes measured to aggregate; they are not a
    complete classification (see `_withheld_input`). If some other error type
    reports the whole body at a field-level `loc`, identity catches it. The
    type here is deliberately outside `_AGGREGATE_INPUT_TYPES`, so identity is
    what does the work rather than rule 3 masking it.

    The second half is the mutation: drop `body` and the same error echoes the
    credential, which is what shows the check is load-bearing.
    """
    body = {"refreshToken": SECRET_REFRESH}
    error = {
        "type": "list_type",
        "loc": ("body", "refresh_token"),
        "msg": "Input should be a valid list",
        "input": body,
    }

    withheld = sanitize_validation_error(error, body)
    assert SECRET_REFRESH not in json.dumps(withheld)
    assert withheld["input"] == "<request body not echoed>"

    assert sanitize_validation_error(error)["input"] == body


@pytest.mark.unit
@pytest.mark.security
def test_a_null_body_does_not_swallow_a_legitimate_null_input():
    """`exc.body` is None for a GET and for a JSON `null` body.

    Guarding identity with a sentinel that production never passes left the
    check as `raw is None`, which rewrote every honest `input: null` into a
    message about a request body it was not.
    """
    error = {
        "type": "string_type",
        "loc": ("query", "user_id"),
        "msg": "Input should be a valid string",
        "input": None,
    }

    assert sanitize_validation_error(error, None)["input"] is None


@pytest.mark.unit
@pytest.mark.security
def test_a_withheld_input_is_never_walked():
    """The value is discarded, so converting it first is pure cost.

    `to_json_safe` used to run over the whole error — including an `input` that
    can be the entire request body — and the result was then overwritten. A
    20,000-key body producing 60 errors paid for that walk 50 times.
    """
    walked = []

    class CountingDict(dict):
        def items(self):
            walked.append(1)
            return super().items()

    payload = CountingDict({"refreshToken": SECRET_REFRESH})
    withheld = {
        "type": "missing",
        "loc": ("body", "refresh_token"),
        "msg": "Field required",
        "input": payload,
    }
    sanitize_validation_error(withheld)
    assert walked == [], "the withheld input was converted before being discarded"

    # The counter is real: an echoed input IS walked.
    echoed = {
        "type": "list_type",
        "loc": ("body", "tags"),
        "msg": "Input should be a valid list",
        "input": payload,
    }
    sanitize_validation_error(echoed)
    assert walked, "CountingDict never recorded a walk, so the check proves nothing"


@pytest.mark.unit
@pytest.mark.security
def test_missing_family_is_pinned_in_both_directions():
    """A rename empties the guard; a new upstream type leaves it incomplete.

    `_NO_VALUE_AT_LOC_TYPES` is a set of pydantic's own error-type strings, and
    both directions fail silently: a renamed type stops firing and a newly
    added `missing_*` type is simply not covered, while every other assertion
    in this file still passes. That is the same fail-by-omission shape as the
    bug this file is about, so pin the set against the catalogue both ways.

    `missing_sentinel_error` is the deliberate exclusion: it is the one
    "missing*" type that reports the supplied value rather than the enclosing
    object, which is why the guard enumerates instead of prefix-matching.
    """
    from pydantic_core import _pydantic_core

    catalogue = {entry["type"] for entry in _pydantic_core.list_all_errors()}
    missing_family = {name for name in catalogue if name.startswith("missing")}

    assert _NO_VALUE_AT_LOC_TYPES <= catalogue, (
        f"pydantic no longer defines {_NO_VALUE_AT_LOC_TYPES - catalogue}; "
        f"the missing* types it does define are {sorted(missing_family)}"
    )
    assert missing_family - {"missing_sentinel_error"} <= _NO_VALUE_AT_LOC_TYPES, (
        "pydantic defines missing* types the guard does not cover: "
        f"{sorted(missing_family - {'missing_sentinel_error'} - _NO_VALUE_AT_LOC_TYPES)}"
    )
    assert "missing_sentinel_error" not in _NO_VALUE_AT_LOC_TYPES


@pytest.mark.unit
@pytest.mark.security
def test_model_validator_types_are_pinned_against_pydantic():
    """Same fail-by-omission risk on the other set."""
    from pydantic_core import _pydantic_core

    catalogue = {entry["type"] for entry in _pydantic_core.list_all_errors()}
    assert (
        _AGGREGATE_INPUT_TYPES <= catalogue
    ), f"pydantic no longer defines {_AGGREGATE_INPUT_TYPES - catalogue}"


@pytest.mark.unit
@pytest.mark.security
def test_describe_request_body_names_shapes_without_content():
    """It runs inside an exception handler, so it must be total and quiet."""

    class HostileLen(dict):
        """Sized, so it reaches `len()` — and raises there.

        A plain object would return from the catch-all without ever calling
        `len()`, leaving the `except` branch uncovered. A raise there is a 500
        in place of a 422, which is #1048 exactly.
        """

        def __len__(self):
            raise RuntimeError("no")

        def __repr__(self):  # pragma: no cover - must never be called
            return f"secret={SECRET_API_KEY}"

    class Opaque:
        def __repr__(self):  # pragma: no cover - must never be called
            return f"secret={SECRET_API_KEY}"

    assert describe_request_body(None) is None
    assert describe_request_body(b"refresh_token=x") == "<bytes: 15 bytes>"
    assert describe_request_body(bytearray(b"ab")) == "<bytearray: 2 bytes>"
    assert describe_request_body(memoryview(b"abc")) == "<memoryview: 3 bytes>"
    assert describe_request_body("a" * 9) == "<str: 9 characters>"
    assert describe_request_body([1, 2]) == "<list: 2 items>"

    # Containers share `to_json_safe`'s spelling, because both can land on one
    # ERROR record and two spellings of one fact read as two facts.
    assert describe_request_body({"api_key": SECRET_API_KEY}) == "<dict: 1 items>"
    assert to_json_safe({"outer": {"a": 1}}, max_depth=1)["outer"] == "<dict: 1 items>"

    # Form-encoded shape is the reason this function exists, so the type that
    # carries it must not fall through to the unnamed catch-all.
    assert describe_request_body(FormData([("a", "1"), ("b", "2")])) == (
        "<FormData: 2 items>"
    )

    # A raise inside is caught, and no repr leaks through it.
    described = describe_request_body(HostileLen())
    assert described == "<unrepresentable request body>"
    assert SECRET_API_KEY not in described

    # An unknown type is named, never repr'd: a repr is content.
    assert describe_request_body(Opaque()) == "<Opaque>"
