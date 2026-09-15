"""Refuse an over-size request body at the HTTP boundary (#1436).

``runbook_validator`` documents its own input contract, three times, as
caller-supplied content "up to ``MAX_UPLOAD_SIZE_MB``". On the multipart paths
that was true — each part is bounded by ``max_part_size`` and an explicit
``UploadFile.size`` check. On the JSON routes it was false: nothing bounded them
at all, so the statement the validator's ReDoS work was sized against did not
hold for four of its callers.

WHY A MIDDLEWARE, AND NOT A CHECK ON THOSE ROUTES. Two reasons, and the first is
decisive.

**FastAPI reads and parses the body before dependencies run.** In
``fastapi.routing.get_request_handler`` the ``await request.body()`` /
``request.json()`` happens ~50 lines ahead of ``solve_dependencies`` — so a
route-level check, or a ``Depends`` on one, executes only after the process has
already buffered and JSON-parsed the whole thing, for a caller who has not yet
been authenticated. A guard that runs after the cost it exists to avoid is not a
guard. Only something wrapping the ASGI ``receive`` can refuse first.

**And the enumeration would have been wrong again.** The issue that asked for
this named two routes. There are four — ``PUT /knowledge/conversions/{id}/drafts/{id}``,
``POST /knowledge/runbooks/create``, ``PUT /knowledge/suggestions/{id}`` and
``PUT /knowledge/documents/{id}`` — the last two taking an untyped ``dict`` body,
so they have no request model a field bound could even attach to. The gate is
not the worst consumer either: on the suggestion route Presidio scans the body
BEFORE the gate sees it, and on the document route a ``content`` key re-chunks
and re-embeds through BGE-M3, which costs minutes rather than seconds and runs
for any authenticated user. A per-route list would have had to find all four and
stay correct as routes are added; a choke point does not.

PURE ASGI, NOT ``BaseHTTPMiddleware``: the class has to wrap ``receive`` to
count a chunked body, which ``BaseHTTPMiddleware``'s own stream re-wrapping
makes unreliable.

THE TWO ARMS REFUSE DIFFERENTLY, and each has to. The ``Content-Length`` arm
refuses BEFORE the application is called, so there is no handler beneath it —
Starlette's ``ExceptionMiddleware`` is mounted inside the user stack, so an
exception raised there would reach nothing and surface as a 500. It therefore
builds the 413 itself. The counting arm raises from inside ``receive`` while the
application IS running, so ``ExceptionMiddleware`` is beneath it and renders the
house envelope; see ``_too_large`` for why that exception must be an
``HTTPException`` and not a private sentinel. Both were verified against a real
uvicorn server, not only ``TestClient`` — the first version of the counting arm
answered 400 there.

``multipart/form-data`` IS DELIBERATELY EXEMPT. Those paths are already bounded
per part, and bounding the whole body would be wrong in both directions: a
``POST /cases/{id}/turns`` legitimately carries a file, a ``pasted_content`` and
a ``query``, each separately allowed up to the cap, so one whole-body cap at the
cap would refuse a legal request — and a cap loose enough to admit it would let a
30 MB JSON body reach the gate, leaving the contract false at three times the
limit. The residual is a JSON payload mislabelled ``multipart/form-data``: it is
read into memory, fails to parse as a form, and is refused 422 by pydantic
without reaching Presidio, the gate or the embedder. Memory only, and bounded on
cloud by the ingress.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Body content types that own their own per-part bounds. See the module
#: docstring for why a whole-body cap is the wrong instrument for these.
_EXEMPT_CONTENT_TYPE_PREFIXES = ("multipart/form-data",)

#: Methods that carry no body worth bounding. GET/HEAD/OPTIONS with a body is
#: legal but meaningless, and every health and metrics probe is one of these.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def _too_large(size: int, cap: int) -> HTTPException:
    """The refusal raised from inside the counting ``receive`` wrapper.

    An ``HTTPException`` SPECIFICALLY, and this is not a style choice. FastAPI's
    body read is wrapped in ``except HTTPException: raise`` / ``except Exception:
    -> 400 "There was an error parsing the body"``. A private sentinel therefore
    never reaches this middleware at all — it is swallowed and an over-size body
    is reported to the caller as a malformed one. Measured against a real uvicorn
    server before this was corrected: the chunked arm answered **400**, not 413.

    Raising ``HTTPException`` instead lets FastAPI re-raise it, and Starlette's
    ``ExceptionMiddleware`` — mounted INSIDE the user middleware stack — renders
    it into the house ``{"detail": ...}`` envelope. So this path needs no
    hand-built response, unlike the Content-Length path, which refuses before the
    application is ever called and so has no handler beneath it.
    """
    return HTTPException(
        status_code=413,
        detail=f"Request body exceeds the {cap // (1024 * 1024)}MB limit.",
    )


class RequestBodySizeLimitMiddleware:
    """Refuse a request body larger than ``MAX_UPLOAD_SIZE_MB``, before it is read.

    Registered unconditionally — NOT behind a test-environment check the way
    several neighbours are. A guard the test application cannot see is a guard
    with no test.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _cap_bytes(self) -> int:
        # Read per request from settings rather than captured at construction:
        # the cloud deployment hot-reloads configuration, and a cap frozen at
        # import time would silently keep the old value. Read from settings and
        # not from `os.environ` — `main.py` does the latter for `max_part_size`
        # and only agrees with settings by way of `load_dotenv` having run first.
        from faultmaven.config.settings import get_settings

        return get_settings().upload.max_upload_size_mb * 1024 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "")
        if content_type.startswith(_EXEMPT_CONTENT_TYPE_PREFIXES):
            await self.app(scope, receive, send)
            return

        cap = self._cap_bytes()

        # (a) The declared length, when there is one. This is the path every
        # real client takes — a browser `fetch` with a string body sets it, and
        # nginx re-emits a buffered request with one — and it refuses before a
        # single byte of body is read. h11 delivers exactly the declared count,
        # so the header is a trustworthy upper bound rather than a hint.
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    await self._refuse(scope, send, int(declared), cap)
                    return
            except ValueError:
                # Unparseable: fall through to counting. A malformed header is
                # not a reason to skip the check.
                pass

        # (b) No usable Content-Length (`Transfer-Encoding: chunked`). Count as
        # the body arrives and stop at the cap. Without this the check is
        # bypassable with a single curl flag; with only this, a declared
        # over-size body would be streamed in full before being refused.
        counted = 0

        async def counting_receive() -> Message:
            nonlocal counted
            message = await receive()
            if message["type"] == "http.request":
                counted += len(message.get("body", b""))
                if counted > cap:
                    logger.warning(
                        "Request body refused: streamed past the %s byte cap (%s %s)",
                        cap,
                        scope.get("method"),
                        scope.get("path"),
                    )
                    raise _too_large(counted, cap)
            return message

        await self.app(scope, counting_receive, send)

    async def _refuse(self, scope: Scope, send: Send, size: int, cap: int) -> None:
        """Emit the house 413 envelope directly.

        Directly, because Starlette's ``ExceptionMiddleware`` — which turns an
        ``HTTPException`` into that envelope — is mounted INSIDE the user
        middleware stack, so nothing out here would catch one.
        """
        import json

        logger.warning(
            "Request body refused: %s bytes exceeds the %s byte cap (%s %s)",
            size,
            cap,
            scope.get("method"),
            scope.get("path"),
        )
        body = json.dumps(
            {"detail": (f"Request body exceeds the {cap // (1024 * 1024)}MB limit.")}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
