"""An over-size request body is refused at the HTTP boundary (#1436).

``runbook_validator`` states its input contract three times as caller-supplied
content "up to ``MAX_UPLOAD_SIZE_MB``". On the multipart paths that was true. On
the JSON routes nothing bounded them at all, so the statement its ReDoS work was
sized against did not hold for four of its callers.

**What these tests assert, and why it is 413-versus-401 rather than a spy.**
The obvious design is to spy on the expensive consumers (Presidio, the gate,
BGE-M3) and assert they never ran. That spy is VACUOUS here and was written that
way first: every one of these routes answers 401 to an unauthenticated request,
so the consumers never run with or without the middleware and the assertion
cannot fail. Measured, not assumed — all four return 401 for an under-cap body.

The observable that does discriminate is the STATUS. An unauthenticated,
over-size request answered **413 rather than 401** can only mean the body was
refused before authentication ran — which is exactly the property at issue,
because FastAPI buffers and JSON-parses the body ~50 lines ahead of
``solve_dependencies``. A route-level check or a ``Depends`` could not produce
this result; only something wrapping ASGI ``receive`` can. So the status is not
weak corroboration for a spy, it is the strongest evidence available, and the
spy is gone rather than left in to look thorough.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.api]


def _cap_bytes() -> int:
    from faultmaven.config.settings import get_settings

    return get_settings().upload.max_upload_size_mb * 1024 * 1024


@pytest.fixture(scope="module")
def client():
    from faultmaven.main import app

    with TestClient(app) as c:
        yield c


#: Every JSON route that reaches a body-size-bound consumer. Four, not the two
#: the issue named — the last two take an untyped ``dict`` body, so they have no
#: request model a field-level bound could have attached to.
_GUARDED_ROUTES = [
    ("PUT", "/api/v1/knowledge/conversions/conv_x/drafts/draft_x", "content"),
    ("POST", "/api/v1/knowledge/runbooks/create", "causes"),
    ("PUT", "/api/v1/knowledge/suggestions/sug_x", "content"),
    ("PUT", "/api/v1/knowledge/documents/doc_x", "content"),
]


@pytest.mark.parametrize("method,path,field", _GUARDED_ROUTES)
def test_an_oversize_body_is_refused_before_any_consumer_reads_it(
    client, method, path, field
):
    """413 rather than the 401 this unauthenticated request would otherwise get.

    That difference is the whole assertion — see the module docstring for why a
    consumer spy cannot discriminate here."""
    payload = json.dumps({field: "x" * (_cap_bytes() + 1)})

    response = client.request(
        method, path, content=payload, headers={"content-type": "application/json"}
    )

    # 413, NOT the 401 this unauthenticated request would otherwise get. That
    # difference is the assertion: it can only happen if the body was refused
    # before authentication, i.e. before FastAPI buffered and parsed it.
    assert response.status_code == 413, response.text
    assert "Request body exceeds" in response.json()["detail"]


def test_a_body_at_exactly_the_cap_is_not_refused(client):
    """The POSITIVE CONTROL, without which "everything 413s" passes this file.

    The body is sized to exactly the cap, so it must pass the middleware. What
    the route then does with it is not this test's business — it will 404 or 422
    on a draft that does not exist — so the assertion is only that the answer is
    NOT the middleware's 413.
    """
    overhead = len(json.dumps({"content": ""}))
    payload = json.dumps({"content": "x" * (_cap_bytes() - overhead)})
    assert len(payload.encode("utf-8")) == _cap_bytes()

    response = client.put(
        "/api/v1/knowledge/conversions/conv_x/drafts/draft_x",
        content=payload,
        headers={"content-type": "application/json"},
    )

    assert response.status_code != 413, "a body exactly at the cap was refused"


def test_the_cap_is_measured_in_bytes_not_characters(client):
    """A four-byte code point counts as four.

    The bound this replaces was a pydantic ``max_length`` in CHARACTERS, where
    ``"漢" * 10_485_760`` is 30.0 MB of UTF-8 and passed.

    Scope, because an earlier docstring here overclaimed: this exercises the
    ``Content-Length`` arm, where the declared length is ALREADY a byte count,
    so it cannot detect a character-based count in the streaming arm — verified
    by mutation, which left it green. The streaming arm's units are pinned by
    the real-server test below, which sends multibyte chunks.
    """
    # `ensure_ascii=False`, or `json.dumps` escapes each CJK char to a 6-byte
    # ASCII `\uXXXX` and the body is no longer multibyte at all — the first
    # version of this test asserted against its own escaping.
    payload = json.dumps({"content": "漢" * (_cap_bytes() // 3)}, ensure_ascii=False)
    assert len(payload) < _cap_bytes() < len(payload.encode("utf-8"))

    response = client.put(
        "/api/v1/knowledge/documents/doc_x",
        content=payload,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413, response.text


@pytest.mark.slow
def test_a_chunked_body_with_no_content_length_is_refused_by_a_real_server():
    """The header check alone is bypassable with one curl flag, so the counting
    ``receive`` wrapper has to work — and it can only be tested against a REAL
    server.

    ``TestClient`` DEADLOCKS on this case. Its in-process transport keeps feeding
    the request generator while the application has stopped consuming, so the
    test hangs rather than failing. That is a property of the harness, not of the
    middleware: against uvicorn the same request is refused correctly.

    And the distinction is load-bearing rather than pedantic. A unit test driving
    the wrapper directly would have passed the FIRST version of this code, which
    raised a private sentinel — FastAPI's body read wraps anything that is not an
    ``HTTPException`` into a 400, so a real client got "There was an error parsing
    the body" for an over-size upload. Only an end-to-end request through a real
    server showed that. Hence the cost of standing one up here.
    """
    import threading
    import time

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    from faultmaven.api.middleware.body_size import RequestBodySizeLimitMiddleware

    class PassThroughMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    probe = FastAPI()

    @probe.put("/echo")
    async def echo(body: dict):  # pragma: no cover - reached only if unrefused
        return {"received": len(str(body))}

    # A BaseHTTPMiddleware BENEATH the limiter, because that is what the real
    # application has — five of them — and because without one this test cannot
    # see the defect it exists for. The first version of the streaming arm
    # raised an HTTPException from inside `receive`; BaseHTTPMiddleware runs the
    # inner app in an anyio TaskGroup, so it re-emerged as an ExceptionGroup and
    # FastAPI answered 400 "There was an error parsing the body". A probe app
    # holding only the limiter answered 413 and certified a broken middleware.
    probe.add_middleware(PassThroughMiddleware)
    probe.add_middleware(RequestBodySizeLimitMiddleware)

    from tests.integration.mock_servers import get_free_port

    port = get_free_port()
    config = uvicorn.Config(probe, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    try:
        for _ in range(80):
            try:
                httpx.get(f"http://127.0.0.1:{port}/docs", timeout=0.5)
                break
            except Exception:
                time.sleep(0.25)
        else:  # pragma: no cover
            raise AssertionError(
                "the probe server never came up — this used to `pytest.skip`, "
                "which turned the only test of the streaming arm into a silent "
                "pass whenever the port was busy or uvicorn failed to start"
            )

        cap = _cap_bytes()

        # MULTIBYTE chunks, deliberately. The counting wrapper must measure
        # BYTES; if someone re-implements it as
        # `len(message["body"].decode())` this body is under the cap in
        # characters and over it in bytes, so it stops being refused. The
        # Content-Length test cannot pin that — a declared length is already a
        # byte count — so this is the only place the counting arm's units are
        # observable. Verified by mutation: a decode-based count fails here.
        blob = "漢".encode("utf-8") * (64 * 1024)  # 192 KB, 64K characters

        def oversize_stream():
            yield b'{"content": "'
            sent = 0
            while sent <= cap:
                yield blob
                sent += len(blob)
            yield b'"}'

        response = httpx.put(
            f"http://127.0.0.1:{port}/echo",
            content=oversize_stream(),
            headers={"content-type": "application/json"},
            timeout=30,
        )

        assert response.status_code == 413, response.text
        assert "Request body exceeds" in response.json()["detail"], (
            "a chunked over-size body was refused with the wrong error — if this "
            "is 400 'There was an error parsing the body', the refusal is being "
            "raised as something FastAPI's body read swallows"
        )
    finally:
        server.should_exit = True


def test_multipart_is_not_double_bounded(client):
    """Multipart owns its own per-part bounds, and a whole-body cap would be
    wrong in both directions — see the middleware docstring.

    A ``/turns`` request legitimately carries a file AND a paste AND a query,
    each separately allowed up to the cap, so a total over the cap with every
    part under it must NOT be refused. This pins the exemption and fails if
    someone removes the content-type scoping to make the limiter "cover
    everything".

    ``files=`` is what makes this multipart. The first version passed only
    ``data=``, which httpx encodes as ``application/x-www-form-urlencoded`` — so
    it exercised a content type that is deliberately NOT exempt and reported the
    exemption as broken when it was the test that was.
    """
    part = "y" * (_cap_bytes() // 2)

    response = client.post(
        "/api/v1/cases/case_x/turns",
        data={"query": part, "pasted_content": part},
        files={"file": ("n.md", b"# note", "text/markdown")},
    )

    assert response.status_code != 413, (
        "a multipart request whose parts are each under the cap was refused by "
        "the whole-body limiter — the content-type exemption is gone"
    )


def test_urlencoded_is_deliberately_not_exempt(client):
    """Only MULTIPART is exempt, and the asymmetry is deliberate.

    Starlette bounds multipart per part (``max_part_size``), so the whole-body
    cap would double-bound it. It does NOT bound a urlencoded body — ``form()``
    buffers the lot — so that encoding has the same hazard as JSON and gets the
    same cap.

    Nothing legitimate loses: every client posts ``/turns`` as ``FormData``
    (multipart), and the only urlencoded consumers in the API are the OAuth
    token and revoke routes, whose RFC 6749 bodies are a few hundred bytes. This
    test exists so that asymmetry is a decision on the record rather than an
    accident of which prefix someone listed.
    """
    response = client.post(
        "/api/v1/cases/case_x/turns",
        data={"query": "z" * (_cap_bytes() + 1)},
    )

    assert response.status_code == 413, response.text
    assert "Request body exceeds" in response.json()["detail"]


def test_the_limiter_is_mounted_regardless_of_environment():
    """Several neighbours are registered only outside a test environment. This
    one must not be: a guard the test application does not mount is a guard with
    no test, and every assertion above would pass vacuously against an app that
    never installed it."""
    from faultmaven.api.middleware.body_size import RequestBodySizeLimitMiddleware
    from faultmaven.main import app

    mounted = [m.cls for m in app.user_middleware]
    assert RequestBodySizeLimitMiddleware in mounted

    # Only what this app actually mounts: under SKIP_SERVICE_CHECKS — which both
    # CI jobs set — IdempotencyMiddleware is absent. The test below rebuilds the
    # app with the flag off so that one is asserted somewhere rather than
    # silently skipped, which is what an `if name in names` guard did here.
    _assert_ordering(
        [getattr(m.cls, "__name__", "") for m in app.user_middleware],
        body_readers=("DeduplicationMiddleware",),
    )


def test_the_ordering_holds_with_the_full_protection_stack_mounted():
    """The ordering assertions must not be conditional on what happens to be
    mounted.

    ``IdempotencyMiddleware`` is gated on ``not skip_service_checks``, and BOTH
    CI jobs set ``SKIP_SERVICE_CHECKS=true`` — so an ``if name in names:`` guard
    around that assertion silently checks nothing exactly where it is checked.
    The app is rebuilt with the flag off instead, the way
    ``test_rate_limit_wire_refusal`` already does it, so the assertion has
    something to assert against.
    """
    import os

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("SKIP_SERVICE_CHECKS")
    os.environ["SKIP_SERVICE_CHECKS"] = "false"
    reset_settings()
    try:
        rebuilt = rebuild_app()
        names = [getattr(m.cls, "__name__", "") for m in rebuilt.user_middleware]
        assert "IdempotencyMiddleware" in names, (
            "the rebuild did not mount the body-buffering middleware this test "
            "exists to order against"
        )
        _assert_ordering(
            names,
            body_readers=("DeduplicationMiddleware", "IdempotencyMiddleware"),
        )
    finally:
        if previous is None:
            os.environ.pop("SKIP_SERVICE_CHECKS", None)
        else:
            os.environ["SKIP_SERVICE_CHECKS"] = previous
        reset_settings()


def _assert_ordering(names: list[str], body_readers: tuple[str, ...]) -> None:
    """Starlette wraps in reverse registration order, so a LOWER index is
    further out. The limiter must sit inside CORS (a refusal keeps its CORS
    headers, so the Dashboard sees a real 413) and outside every middleware that
    reads the body itself."""
    limiter = names.index("RequestBodySizeLimitMiddleware")
    assert names.index("CORSMiddleware") < limiter, "the limiter escaped CORS"
    for reads_the_body in body_readers:
        assert reads_the_body in names, f"{reads_the_body} is not mounted"
        assert limiter < names.index(reads_the_body), (
            f"{reads_the_body} buffers the body and now sits outside the "
            "limiter, so it reads an over-size body before it is refused"
        )
