"""Refuse an over-size request body at the HTTP boundary (#1436).

``runbook_validator`` documents its own input contract, three times, as
caller-supplied content "up to ``MAX_UPLOAD_SIZE_MB``", and #1395's ReDoS work
was sized against that. On the JSON routes it was false: nothing bounded them at
all.

WHY A MIDDLEWARE, AND NOT A CHECK ON THOSE ROUTES. Two reasons, and the first is
decisive.

**FastAPI reads and parses the body before dependencies run.** In
``fastapi.routing.get_request_handler`` the ``await request.body()`` /
``request.json()`` happens ~50 lines ahead of ``solve_dependencies`` — so a
route-level check, or a ``Depends`` on one, executes only after the process has
already buffered and JSON-parsed the whole thing, for a caller who has not yet
been authenticated. A guard that runs after the cost it exists to avoid is not a
guard.

**And the enumeration would have been wrong again.** The issue that asked for
this named two routes. There are four — ``PUT /knowledge/conversions/{id}/drafts/{id}``,
``POST /knowledge/runbooks/create``, ``PUT /knowledge/suggestions/{id}`` and
``PUT /knowledge/documents/{id}`` — the last two taking an untyped ``dict`` body,
so they have no request model a field bound could even attach to. The gate is
not the worst consumer either: on the suggestion route Presidio scans the body
BEFORE the gate sees it, and on the document route a ``content`` key re-chunks
and re-embeds through BGE-M3, which costs minutes rather than seconds and runs
for any authenticated user.

NEITHER ARM RAISES. Both refuse by sending the 413 themselves, BEFORE the
downstream application is called, and that is not a style choice. The obvious
implementation of the streaming arm raises an ``HTTPException`` from inside a
``receive`` wrapper, and it does not survive this application's middleware
stack: ``BaseHTTPMiddleware`` runs the inner app inside an anyio TaskGroup, so
the exception re-emerges as an ``ExceptionGroup``, which is not an
``HTTPException``, so FastAPI's body read takes its ``except Exception -> 400
"There was an error parsing the body"`` branch. ``main.py`` mounts five
``BaseHTTPMiddleware`` beneath this one, so that was unconditional in
production — while a probe application holding only this middleware answered 413
and looked correct. Refusing before dispatch removes the exception path
entirely.

WHAT IT DOES NOT BOUND, stated because the first version of this docstring
overstated it:

* ``multipart/form-data`` is exempt. NOT because it is already bounded — it is
  not; ``main.py`` says so itself ("file parts are unbounded at the parser"),
  and the routes enforce ``MAX_UPLOAD_SIZE_MB`` per file only AFTER the parse,
  by which point the part is already spooled. It is exempt because a whole-body
  cap is the wrong instrument: ``POST /cases/{id}/turns`` legitimately carries a
  file AND a ``pasted_content`` AND a ``query``, each separately allowed up to
  the cap, so one whole-body cap at the cap would refuse a legal request, and a
  cap loose enough to admit it would let a 30 MB JSON body through. Bounding
  multipart properly means bounding the PARTS, which is the parser's job and a
  separate change.
* ``application/x-www-form-urlencoded`` is deliberately NOT exempt: Starlette
  buffers a urlencoded body whole, so it carries JSON's hazard and gets JSON's
  cap. Nothing legitimate loses — every client posts ``/turns`` as ``FormData``,
  and the only urlencoded consumers are the OAuth token and revoke routes, whose
  RFC 6749 bodies are a few hundred bytes.
* A JSON payload mislabelled ``multipart/form-data`` is read into memory, fails
  to parse as a form, and is refused 422 by pydantic without reaching Presidio,
  the gate or the embedder. Memory only, and bounded on cloud by the ingress.
* A refusal happens OUTSIDE the protection stack — rate limiting, request-id
  injection and the structured request log all sit beneath this middleware — so
  a flood of over-size requests is counted by none of them and appears only in
  this module's own warning. That follows from having to sit outside the two
  middlewares that buffer the body, and is the price of refusing before they do.
"""

from __future__ import annotations

import logging
from typing import Optional

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Body content types whose size is the PARSER's business, not this
#: middleware's. See the module docstring — this is about the right instrument,
#: not about multipart already being safe.
_EXEMPT_CONTENT_TYPES = frozenset({"multipart/form-data"})

#: Methods with no request body to bound. ``DELETE`` is NOT here: a DELETE body
#: is unusual but legal, FastAPI will bind a request model to one, and a choke
#: point whose whole argument is that it cannot go stale as routes are added
#: must not carry a method-shaped hole. An earlier version listed it while the
#: comment beside it named only these three.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _declared_length(scope: Scope) -> Optional[int]:
    """``Content-Length`` as an int, or ``None`` when absent or unparseable.

    The parse is isolated so its ``except`` cannot swallow anything else. An
    earlier version wrapped the refusal call in the same ``try``, where a
    ``ValueError`` raised while SENDING the response would have fallen through
    to dispatching the application after a response had already started — which
    the ASGI server answers with ``RuntimeError: Unexpected ASGI message``.
    """
    raw = Headers(scope=scope).get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _unused_receive() -> Message:  # pragma: no cover - never awaited
    """A ``receive`` for a response with a fixed body, which never reads one."""
    return {"type": "http.disconnect"}


class RequestBodySizeLimitMiddleware:
    """Refuse a request body larger than ``MAX_UPLOAD_SIZE_MB``, before it is read.

    Registered unconditionally — NOT behind a test-environment check the way
    several neighbours are. A guard the test application cannot see is a guard
    with no test.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _cap_bytes() -> int:
        # Read per request rather than captured in ``__init__`` so a test can
        # change the setting and see it take effect without rebuilding the app.
        #
        # NOT for hot reload: ``get_settings()`` returns a module-level
        # singleton that changes only on an explicit ``reset_settings()``, and
        # ``UploadSettings`` has no override path. An earlier version of this
        # comment claimed hot reload and would have had the next reader size a
        # change against behaviour the code does not have.
        from faultmaven.config.settings import get_settings

        return get_settings().upload.max_upload_size_mb * 1024 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        # Media types are case-insensitive (RFC 9110 §8.3.1) and carry
        # parameters and surrounding space. Compared the way ``idempotency.py``
        # already compares one: an earlier ``startswith`` on the raw header
        # refused ``Multipart/Form-Data; boundary=x`` with a spurious 413.
        content_type = Headers(scope=scope).get("content-type", "")
        if content_type.split(";")[0].strip().lower() in _EXEMPT_CONTENT_TYPES:
            await self.app(scope, receive, send)
            return

        cap = self._cap_bytes()

        # (a) The declared length, when there is one. This is the path every
        # real client takes — a browser ``fetch`` with a string body sets it,
        # and nginx re-emits a buffered request with one — and it refuses before
        # a single byte of body is read. h11 delivers exactly the declared
        # count, so the header is a trustworthy upper bound, not a hint.
        declared = _declared_length(scope)
        if declared is not None and declared > cap:
            await self._refuse(scope, send, declared, cap)
            return

        # (b) No usable ``Content-Length`` (``Transfer-Encoding: chunked``).
        # Read the body HERE, bounded, and only then dispatch — replaying what
        # was read. See the module docstring for why this cannot raise from a
        # ``receive`` wrapper instead. Buffering up to the cap is what the
        # application does anyway, and it is bounded by the cap, which is the
        # point.
        buffered: list[Message] = []
        counted = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            counted += len(message.get("body", b""))
            if counted > cap:
                await self._refuse(scope, send, counted, cap)
                return
            if not message.get("more_body", False):
                break

        replayed = iter(buffered)

        async def replay_receive() -> Message:
            try:
                return next(replayed)
            except StopIteration:
                return await receive()

        await self.app(scope, replay_receive, send)

    async def _refuse(self, scope: Scope, send: Send, size: int, cap: int) -> None:
        """Answer 413 directly, without calling the application.

        Directly, because Starlette's ``ExceptionMiddleware`` — which renders an
        ``HTTPException`` into the house envelope — is mounted INSIDE the user
        middleware stack, so nothing out here would catch one.
        """
        logger.warning(
            "Request body refused: %d bytes exceeds the %d byte cap (%s %r)",
            size,
            cap,
            scope.get("method"),
            # Repr'd: a percent-decoded path can contain a newline, and an
            # unescaped one forges a log line.
            scope.get("path"),
        )
        response = JSONResponse(
            status_code=413,
            content={
                "detail": f"Request body exceeds the {cap // (1024 * 1024)}MB limit."
            },
        )
        await response(scope, _unused_receive, send)
